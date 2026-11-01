"""将结构化日志缓冲写入 SQLite 的可观测性适配器。

它维护写入节流与保留策略，但不理解日志关联的产品业务含义，因此归属
``platform.observability`` 而非 HTTP 路由层。
"""

import logging
import os
import threading
import time
from datetime import datetime, timezone

from sqlalchemy import or_

from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import LogEntry

MAX_LOG_ENTRIES_TOTAL = 120_000
MAX_INFRA_LOG_ENTRIES = 30_000
MAX_BUFFERED_ENTRIES = 2_000
BUFFER_SIZE = 80
FLUSH_INTERVAL = 1.0  # seconds
CLEANUP_EVERY_FLUSHES = 10

# 三方库的逐请求日志(每条 HTTP 请求几十行)与调度器心跳对排查无价值,
# 却占了实测量 95% 以上,并会把 SQLite 写入挤到 locked/readonly。
# 这些 logger 在 WARNING 以下直接丢弃(不写库、不进控制台)。
NOISY_LOGGER_PREFIXES = (
    "httpx",
    "httpcore",
    "h11",
    "h2",
    "urllib3",
    "requests",
    "charset_normalizer",
    "apscheduler",
    "openai",
    "anthropic",
    "sqlalchemy.engine",
)

# access log 不整体丢弃:4xx/5xx 仍有排查价值,由 AccessLogFilter 只留错误请求。
# 保留策略里仍把它们算作基础设施日志,优先清理。
INFRA_LOGGER_PREFIXES = NOISY_LOGGER_PREFIXES + ("uvicorn.access",)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# 需要做“静默”处理的 logger 会被提到这个级别,避免逐请求日志进入任何 handler。
THIRD_PARTY_LOG_LEVEL = os.environ.get("LOG_THIRD_PARTY_LEVEL", "WARNING").upper()

# 重复 WARNING 的抑制窗口(秒);0 表示不抑制。
REPEAT_WINDOW_SEC = _env_float("LOG_REPEAT_WINDOW_SEC", 60.0)


def _matches_prefix(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


def is_noisy_logger(name: str) -> bool:
    """判断是否属于三方库噪音 logger。

    部分依赖会重复打包同类库, logger 名带版本后缀(如 ``httpcore2.http11``),
    这里把首段尾部的数字去掉后再匹配, 避免换个包名就漏过去。
    """
    if not name:
        return False
    if any(_matches_prefix(name, p) for p in NOISY_LOGGER_PREFIXES):
        return True
    head, _, rest = name.partition(".")
    base = head.rstrip("0123456789")
    if base and base != head:
        return is_noisy_logger(base + ("." + rest if rest else ""))
    return False


class NoiseFilter(logging.Filter):
    """丢弃三方库 WARNING 以下的日志(逐请求 HTTP、调度器心跳等)。

    异常仍会经由业务代码的 ``logger.exception`` 记录,因此不影响排障。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        return not is_noisy_logger(record.name or "")


class AccessLogFilter(logging.Filter):
    """uvicorn access log 只保留 4xx/5xx, 正常请求不再写库。

    前端会周期性轮询若干接口, 2xx access log 纯属噪音; 失败的请求保留。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "uvicorn.access" or record.levelno >= logging.WARNING:
            return True
        args = record.args
        if isinstance(args, tuple) and args:
            try:
                return int(args[-1]) >= 400
            except (TypeError, ValueError):
                return True
        return True


def silence_noisy_loggers() -> None:
    """把三方库 logger 提到 WARNING, 连 DEBUG 采集也不产生记录。

    需要临时还原完整请求日志时设置 ``LOG_THIRD_PARTY_LEVEL=DEBUG``。
    """
    level = getattr(logging, THIRD_PARTY_LOG_LEVEL, logging.WARNING)
    for name in NOISY_LOGGER_PREFIXES:
        logging.getLogger(name).setLevel(level)


class RepeatFilter(logging.Filter):
    """同一 logger 在窗口内重复出现的 WARNING 只保留首条。

    行情批量失败时每个标的都会打一条几乎相同的告警, 轮询几轮就刷屏。
    ERROR 及以上不节流: 真故障必须每次可见。
    """

    def __init__(self, window: float = REPEAT_WINDOW_SEC, max_keys: int = 512):
        super().__init__()
        self._window = window
        self._max_keys = max_keys
        self._seen: dict[tuple, float] = {}
        self._lock = threading.Lock()
        self._suppressed = 0

    def filter(self, record: logging.LogRecord) -> bool:
        if self._window <= 0 or record.levelno < logging.WARNING:
            return True
        if record.levelno >= logging.ERROR:
            return True
        try:
            key = (record.name, record.levelno, record.getMessage())
        except Exception:
            return True
        now = time.monotonic()
        with self._lock:
            last = self._seen.get(key)
            if last is not None and now - last < self._window:
                self._suppressed += 1
                return False
            self._seen[key] = now
            if len(self._seen) > self._max_keys:
                cutoff = now - self._window
                self._seen = {
                    k: v for k, v in self._seen.items() if v >= cutoff
                }
        return True


_ACTIVE_HANDLER = None


def get_log_handler_stats() -> dict:
    """Get runtime health stats of DB log handler."""
    h = _ACTIVE_HANDLER
    if not h:
        return {
            "enabled": False,
            "pending_entries": 0,
            "dropped_entries": 0,
            "suppressed_repeat": 0,
            "flush_errors": 0,
            "last_flush_error": "",
            "last_flush_at": "",
        }
    with h._lock:
        return {
            "enabled": True,
            "pending_entries": len(h._buffer),
            "dropped_entries": h._dropped_entries,
            "suppressed_repeat": getattr(
                getattr(h, "_repeat_filter", None), "_suppressed", 0
            ),
            "flush_errors": h._flush_errors,
            "last_flush_error": h._last_flush_error,
            "last_flush_at": h._last_flush_at.isoformat() if h._last_flush_at else "",
        }


class DBLogHandler(logging.Handler):
    """Buffered logging handler that writes to the log_entries table."""

    def __init__(self, level=logging.DEBUG):
        super().__init__(level)
        # 三道闸: 三方库噪音先被丢弃, 再按 4xx/5xx 过滤 access log,
        # 最后压掉窗口内重复的 WARNING(批量行情失败会按标的各打一条)。
        self.addFilter(NoiseFilter())
        self.addFilter(AccessLogFilter())
        self._repeat_filter = RepeatFilter()
        self.addFilter(self._repeat_filter)
        self._buffer: list[dict] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._closed = False
        self._dropped_entries = 0
        self._flush_errors = 0
        self._last_flush_error = ""
        self._last_flush_at = None
        self._flush_count = 0
        global _ACTIVE_HANDLER
        _ACTIVE_HANDLER = self
        self._worker = threading.Thread(target=self._write_loop, name="db-log-writer", daemon=True)
        self._worker.start()

    def emit(self, record: logging.LogRecord):
        try:
            tags = getattr(record, "tags", {})
            if not isinstance(tags, dict):
                tags = {}
            entry = {
                "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc),
                "level": record.levelname,
                # Persist original module logger name; UI maps to Chinese for display
                "logger_name": getattr(record, "name", ""),
                "message": self.format(record),
                "trace_id": str(getattr(record, "trace_id", "") or "")[:64],
                "run_id": str(getattr(record, "run_id", "") or "")[:64],
                "agent_name": str(getattr(record, "agent_name", "") or "")[:64],
                "event": str(getattr(record, "event", "") or "")[:64],
                "tags": tags,
                "notify_status": str(getattr(record, "notify_status", "") or "")[:32],
                "notify_reason": str(getattr(record, "notify_reason", "") or "")[:255],
            }
            with self._lock:
                if self._closed:
                    return
                if len(self._buffer) >= MAX_BUFFERED_ENTRIES:
                    overflow = len(self._buffer) - MAX_BUFFERED_ENTRIES + 1
                    if overflow > 0:
                        del self._buffer[:overflow]
                        self._dropped_entries += overflow
                self._buffer.append(entry)
                if record.levelno >= logging.ERROR or len(self._buffer) >= BUFFER_SIZE:
                    self._wake.set()
        except Exception:
            # Avoid recursion if logging path fails
            pass

    def _write_loop(self):
        while True:
            self._wake.wait(FLUSH_INTERVAL)
            self._wake.clear()
            # Never hold the producer lock during SQLite I/O or retention work.
            with self._lock:
                entries, self._buffer = self._buffer, []
                closed = self._closed
            if entries:
                self._write_entries(entries)
            if closed:
                return

    def _write_entries(self, entries):
        persisted = False
        try:
            db = SessionLocal()
            try:
                db.bulk_insert_mappings(LogEntry, entries)
                db.commit()
                persisted = True
                with self._lock:
                    self._last_flush_at = datetime.now(timezone.utc)
                    self._flush_count += 1
                if self._flush_count % CLEANUP_EVERY_FLUSHES == 0:
                    self._cleanup(db)
            finally:
                db.close()
        except Exception as e:
            with self._lock:
                self._flush_errors += 1
                self._last_flush_error = str(e)[:500]
                # Retain a bounded retry buffer; failed writes must not silently
                # discard a complete batch during temporary database contention.
                pending = ([] if persisted else entries) + self._buffer
                self._dropped_entries += max(0, len(pending) - MAX_BUFFERED_ENTRIES)
                self._buffer = pending[-MAX_BUFFERED_ENTRIES:]

    def _cleanup(self, db):
        """Retention policy: prioritize preserving business logs."""
        # 1) cap infrastructure noise first
        infra_filters = [LogEntry.logger_name.startswith(p) for p in INFRA_LOGGER_PREFIXES]
        infra_count = db.query(LogEntry).filter(or_(*infra_filters)).count()
        if infra_count > MAX_INFRA_LOG_ENTRIES:
            overflow = infra_count - MAX_INFRA_LOG_ENTRIES
            # delete oldest infra logs in one batch
            victim_ids = (
                db.query(LogEntry.id)
                .filter(or_(*infra_filters))
                .order_by(LogEntry.id.asc())
                .limit(overflow)
                .all()
            )
            if victim_ids:
                ids = [x[0] for x in victim_ids]
                db.query(LogEntry).filter(LogEntry.id.in_(ids)).delete(
                    synchronize_session=False
                )
                db.commit()

        # 2) global hard cap
        total = db.query(LogEntry).count()
        if total > MAX_LOG_ENTRIES_TOTAL:
            cutoff = (
                db.query(LogEntry.id)
                .order_by(LogEntry.id.desc())
                .offset(MAX_LOG_ENTRIES_TOTAL)
                .first()
            )
            if cutoff:
                db.query(LogEntry).filter(LogEntry.id <= cutoff[0]).delete(
                    synchronize_session=False
                )
                db.commit()

    def close(self):
        with self._lock:
            self._closed = True
        self._wake.set()
        if threading.current_thread() is not self._worker:
            self._worker.join(timeout=6)
        super().close()
