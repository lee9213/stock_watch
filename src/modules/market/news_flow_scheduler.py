"""事件驱动调度：定时拉同花顺/华尔街见闻/证券之星、分析未处理新闻、每小时生成速览。"""

from __future__ import annotations

import asyncio
import logging
import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from src.modules.market.news_analysis import (
    BATCH_ANALYZE_SIZE,
    CATCHUP_ANALYZE_LIMIT,
    CATCHUP_MIN_AGE_MINUTES,
    INGEST_ANALYZE_CONCURRENCY,
    analysis_breaker_remaining,
    analyze_many,
)
from src.modules.market.news_digest_job import job_news_digest_hourly
from src.modules.market.news_ingest import ingest_news_sources
from src.modules.market.news_signal_alert import push_if_strong
from src.modules.market.news_store import (
    REFRESH_HOUR_END,
    REFRESH_HOUR_START,
    REFRESH_MINUTE,
    list_unanalyzed,
)
from src.platform.persistence.database import SessionLocal
from src.platform.scheduling.job_runs import track_job_run

logger = logging.getLogger(__name__)

# 有界分析队列：元素为新闻 id；满则跳过，由后续 tick / catchup 再补。
ANALYZE_QUEUE_MAXSIZE = 200
# 并发 worker 数（每条新闻独立消费 id）。
ANALYZE_WORKER_CONCURRENCY = INGEST_ANALYZE_CONCURRENCY
# 页面「立即采集」冷却，避免连点打满三源接口。
MANUAL_INGEST_COOLDOWN_SEC = 60
# 手动采集后的免攒批窗口：这段时间内进来的新闻立刻分析，不参与跨轮凑批。
FORCE_IMMEDIATE_AFTER_MANUAL_SEC = 120.0
# worker 缓存模型客户端的时长；过期或未配模型时重取，换默认模型无需重启。
ANALYZE_CLIENT_REFRESH_SEC = 60
# 同一 worker 同类异常的日志间隔：未做节流时每个 id 都会失败并各写一条完整堆栈
# （实测 2000+ 条/分钟），日志表写入风暴会把 SQLite 写锁挤到 readonly。
ANALYZE_ERROR_LOG_INTERVAL_SEC = 300
# 补扫最多占用队列的一半，给「采集后新新闻」留出入口；否则积压一多就把新条目全挤掉。
CATCHUP_QUEUE_SHARE = 0.5
# 攒批等待（秒）：按 A 股阶段取值，而不是固定值。
# 实测同一轮采集是「同步 put 一批 id」，worker 拿到第一条后 get_nowait 一次
# 就能取空——对这种到达模式等多久都没用（1.5s 与 0s 的调用次数完全一致，
# 纯属给每条新闻多加 1.5 秒延迟）。
# 真正需要等待的场景只有一个：两轮采集之间只来了 1 条，要跨轮凑批。
# 那就只在非交易时段等（新闻次日才看），盘中必须立即分析。
BATCH_LINGER_TRADING_SEC = 0.0
BATCH_LINGER_AFTER_CLOSE_SEC = 900.0  # 盘后 15:00-23:00：公告高峰，最多等 15 分钟
BATCH_LINGER_NIGHT_SEC = 1800.0  # 23:00-07:00：与 30 分钟采集间隔对齐
BATCH_LINGER_NON_TRADING_SEC = 1800.0  # 周末/节假日
# 攒批等待期间重查市场阶段的间隔：进入盘中/盘前要立刻放行，
# 否则夜间那次 30 分钟的等待会把开盘后的第一条快讯一起拖住。
BATCH_PHASE_RECHECK_SEC = 5.0
# 攒批轮询间隔（用 qsize 轮询而不是 wait_for(queue.get())：后者超时取消
# get() 时可能刚好取走一条又丢掉，会静默丢新闻）。
BATCH_POLL_INTERVAL_SEC = 0.05

# 采集节奏：cron 只负责「唤醒」，真正跑不跑由 A 股当前阶段决定。
# 原实现凌晨、周末与盘中同频（实测 04:00-04:51 跑 8 轮采集），纯属空转打接口。
INGEST_INTERVAL_BREAK_SEC = 15 * 60  # 午休 11:30-13:00：新闻照发，但不必 3 分钟一轮
INGEST_INTERVAL_NIGHT_SEC = 30 * 60  # 23:00-07:00 夜间
INGEST_INTERVAL_NON_TRADING_SEC = 60 * 60  # 周末与节假日
SHANGHAI = ZoneInfo("Asia/Shanghai")

_active_scheduler: "NewsFlowScheduler | None" = None


def get_active_scheduler() -> "NewsFlowScheduler | None":
    """API 层取当前运行中的调度器；未启动时为 None。"""
    return _active_scheduler


def market_phase(now: datetime | None = None) -> str:
    """A 股当前阶段：trading / break / pre_market / after_hours / closed /
    non_trading（周末节假日）/ unknown（当年日历未发布）。"""
    moment = now or datetime.now(SHANGHAI)
    try:
        from src.platform.scheduling import trading_calendar

        if not trading_calendar.calendar_known("CN", moment):
            # 当年日历未随包发布：不猜测，回退 cron 默认节奏。
            return "unknown"
        if not trading_calendar.is_trading_day("CN", moment):
            return "non_trading"
        return trading_calendar.market_status("CN", moment)
    except Exception:
        logger.debug("交易日历不可用，采集节奏回退到 cron 默认值", exc_info=True)
        return "unknown"


def ingest_min_interval_sec(phase: str, now: datetime | None = None) -> int:
    """当前阶段的最小采集间隔（秒）；0 表示不额外限制，照 cron 跑。"""
    if phase == "non_trading":
        return INGEST_INTERVAL_NON_TRADING_SEC
    if phase == "break":
        return INGEST_INTERVAL_BREAK_SEC
    if phase == "trading":
        return 0
    moment = now or datetime.now(SHANGHAI)
    if moment.hour >= 23 or moment.hour < 7:
        return INGEST_INTERVAL_NIGHT_SEC
    return 0


def batch_linger_sec(phase: str | None = None, now: datetime | None = None) -> float:
    """当前阶段允许的攒批等待（秒）。0 = 取到就走，不等第二批。

    盘中与午休一律 0：快讯的时效性比省几次 LLM 调用重要得多。
    非交易时段新闻次日才用，等下一轮采集凑批能把 2-3 条并成一次调用。
    """
    current = phase or market_phase(now)
    moment = now or datetime.now(SHANGHAI)
    if current == "non_trading":
        return BATCH_LINGER_NON_TRADING_SEC
    if current in ("trading", "break"):
        return BATCH_LINGER_TRADING_SEC
    if current == "pre_market":
        # 凌晨到 7 点可以慢慢攒；7 点后要为开盘准备，不再等待。
        night = moment.hour >= 23 or moment.hour < 7
        return BATCH_LINGER_NIGHT_SEC if night else 0.0
    if current in ("after_hours", "closed"):
        return (
            BATCH_LINGER_NIGHT_SEC
            if moment.hour >= 23
            else BATCH_LINGER_AFTER_CLOSE_SEC
        )
    # unknown（当年日历未发布）：不猜，立刻分析。
    return 0.0


def _load_analysis_client():
    db = SessionLocal()
    try:
        from src.platform.ai.ai_failover import get_configured_failover_client

        return get_configured_failover_client(db)
    finally:
        db.close()


class NewsFlowScheduler:
    def __init__(self, timezone: str = "Asia/Shanghai"):
        self.scheduler = AsyncIOScheduler(timezone=timezone)
        self._ingesting = False
        self._digesting = False
        self._manual_ingest_until = 0.0
        # 手动「立即采集」后的免攒批窗口：用户主动点的时候不该让他等 30 分钟。
        self._force_immediate_until = 0.0
        # 上轮采集完成时刻（monotonic），用于按市场阶段节流（见 _ingest_job）。
        self._last_ingest_at = 0.0
        self._analyze_queue: asyncio.Queue[int] = asyncio.Queue(
            maxsize=ANALYZE_QUEUE_MAXSIZE
        )
        # 已在队列中或正在分析的 id，避免重复入队。
        self._queued_ids: set[int] = set()
        self._analyze_worker_tasks: list[asyncio.Task] = []
        # 取批串行化：4 个 worker 同时从队列各取 1 条，会把「一次入队的 5 条」
        # 拆成 5 次单条调用（固定指令成本 ×5）。只有取批阶段串行，
        # 分析调用仍然并发。
        self._collect_lock = asyncio.Lock()
        # 强信号推送的后台任务（不阻塞分析 worker 消费）。
        self._push_tasks: set[asyncio.Task] = set()

    def _enqueue_news_ids(
        self,
        news_ids: list[int],
        *,
        log_label: str,
    ) -> tuple[int, int]:
        """按 id 入队；返回 (入队数, 因队列满跳过数)。已在队列中的 id 静默跳过。"""
        enqueued = 0
        skipped_full = 0
        for index, raw_id in enumerate(news_ids):
            news_id = int(raw_id)
            if news_id in self._queued_ids:
                continue
            try:
                self._analyze_queue.put_nowait(news_id)
            except asyncio.QueueFull:
                # 队列已满，本条及后续均跳过。
                skipped_full = len(news_ids) - index
                break
            self._queued_ids.add(news_id)
            enqueued += 1
        if enqueued or skipped_full:
            logger.info(
                "分析 id 入队：%s enqueued=%s skipped_full=%s qsize=%s/%s",
                log_label,
                enqueued,
                skipped_full,
                self._analyze_queue.qsize(),
                ANALYZE_QUEUE_MAXSIZE,
            )
        elif skipped_full == 0 and not enqueued and news_ids:
            logger.debug(
                "分析 id 入队：%s 候选均已在队列中 candidates=%s",
                log_label,
                len(news_ids),
            )
        return enqueued, skipped_full

    def _enqueue_unanalyzed(
        self,
        *,
        limit: int,
        log_label: str,
        job_id: str,
        older_than: datetime | None = None,
    ) -> int:
        """从库中捞未分析新闻 id 入队。"""
        capacity = max(0, ANALYZE_QUEUE_MAXSIZE - self._analyze_queue.qsize())
        pull = min(max(1, int(limit or 1)), capacity) if capacity else 0
        if pull <= 0:
            logger.warning(
                "分析队列已满（max=%s），跳过：%s",
                ANALYZE_QUEUE_MAXSIZE,
                log_label,
            )
            with track_job_run("news_flow", job_id, persist_idle=False) as summary:
                summary["enqueued"] = 0
                summary["skipped_full"] = 1
                summary["_idle"] = True
            return 0

        db = SessionLocal()
        try:
            rows = list_unanalyzed(db, limit=pull, older_than=older_than)
            news_ids = [int(row.id) for row in rows if row.id is not None]
        finally:
            db.close()

        with track_job_run("news_flow", job_id, persist_idle=False) as summary:
            enqueued, skipped_full = self._enqueue_news_ids(
                news_ids, log_label=log_label
            )
            summary["candidates"] = len(news_ids)
            summary["enqueued"] = enqueued
            summary["skipped_full"] = skipped_full
            summary["qsize"] = self._analyze_queue.qsize()
            if older_than is not None:
                summary["older_than_minutes"] = int(CATCHUP_MIN_AGE_MINUTES)
            if enqueued <= 0:
                summary["_idle"] = True
        return enqueued

    def _fill_nowait(self, batch: list[int], size: int) -> None:
        """把队列里已排队的 id 立刻取走，不等待。"""
        while len(batch) < size:
            try:
                batch.append(int(self._analyze_queue.get_nowait()))
            except asyncio.QueueEmpty:
                return

    async def _wait_more(self, batch: list[int], size: int, linger: float) -> None:
        """跨轮凑批：队列已空，等下一轮采集的新闻一起分析。

        只在非交易时段调用（``batch_linger_sec() > 0``）。等待期间持续吸收新到
        的 id；一旦市场进入盘中/盘前（等待额度归零）立刻放行，避免把开盘后的
        第一条快讯一起拖住。
        """
        deadline = time.monotonic() + linger
        recheck_at = time.monotonic() + BATCH_PHASE_RECHECK_SEC
        while len(batch) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(BATCH_POLL_INTERVAL_SEC, remaining))
            self._fill_nowait(batch, size)
            if len(batch) >= size:
                return
            if time.monotonic() >= recheck_at:
                recheck_at = time.monotonic() + BATCH_PHASE_RECHECK_SEC
                if self._current_linger() <= 0:
                    logger.debug(
                        "市场阶段已切换（进入盘中/盘前）或手动采集，攒批提前放行 batch=%s",
                        len(batch),
                    )
                    return

    def _current_linger(self) -> float:
        """本次取批允许的等待秒数；手动采集后的窗口期内一律不等。"""
        if time.monotonic() < self._force_immediate_until:
            return 0.0
        return batch_linger_sec()

    async def _next_batch(self) -> list[int]:
        """取一批 id：先把已排队的取空，只在非交易时段等下一轮凑批。

        取批阶段用 ``_collect_lock`` 串行化——否则多个 worker 会同时各抢一条，
        把一次采集入队的 N 条拆成 N 次单条调用（固定指令要付 N 遍）。
        同一轮采集是同步 put 的，``get_nowait`` 一次就能取空，不需要等待。
        """
        batch: list[int] = []
        size = max(1, int(BATCH_ANALYZE_SIZE))
        linger = max(0.0, float(self._current_linger()))
        try:
            async with self._collect_lock:
                batch.append(int(await self._analyze_queue.get()))
                self._fill_nowait(batch, size)
                if linger > 0 and len(batch) < size:
                    await self._wait_more(batch, size, linger)
        except asyncio.CancelledError:
            # 取批途中被取消（关服务）：已取出的 id 要交还 task_done，否则 join() 挂住。
            for _ in batch:
                self._analyze_queue.task_done()
            raise
        return batch

    async def _analyze_worker(self, worker_index: int) -> None:
        """消费队列中的新闻 id，攒成小批一次分析。"""
        client = None
        loaded_at = 0.0
        # 同一类异常的最近打印时间，做日志节流（见 ANALYZE_ERROR_LOG_INTERVAL_SEC）。
        last_logged: dict[str, float] = {}

        def _due(kind: str) -> bool:
            now = time.monotonic()
            if now - last_logged.get(kind, 0.0) < ANALYZE_ERROR_LOG_INTERVAL_SEC:
                return False
            last_logged[kind] = now
            return True

        while True:
            # 调用级熔断：模型链整体不可用时先等，不要把队列里的 id 一条条
            # 取出来再失败（既浪费调用，也白白耗掉每条的重试额度）。
            remaining = analysis_breaker_remaining()
            if remaining > 0:
                logger.debug(
                    "分析 worker-%s 熔断中，等待 %.0fs", worker_index, remaining
                )
                await asyncio.sleep(remaining)

            batch = await self._next_batch()
            try:
                now = time.monotonic()
                stale = now - loaded_at >= ANALYZE_CLIENT_REFRESH_SEC
                if client is None or not getattr(client, "model", None) or stale:
                    try:
                        client = _load_analysis_client()
                        loaded_at = time.monotonic()
                    except Exception as exc:
                        # 未配置模型是常态：保持逐条重试（配好模型立刻生效），
                        # 但不再每条都打整段堆栈。
                        client = None
                        if _due("client"):
                            logger.warning(
                                "分析 worker-%s 模型客户端不可用（%ss 内同类告警只打一次，"
                                "新闻留在库里等补扫）：%s: %s",
                                worker_index,
                                ANALYZE_ERROR_LOG_INTERVAL_SEC,
                                type(exc).__name__,
                                str(exc)[:200],
                            )
                done_ids = await analyze_many(client, batch)
                if done_ids:
                    logger.debug(
                        "分析完成 worker-%s ids=%s（批量 %s 条）",
                        worker_index,
                        done_ids,
                        len(batch),
                    )
                    self._schedule_strong_signals(done_ids)
            except Exception:
                if _due("analyze"):
                    logger.exception(
                        "分析队列 worker-%s 异常 ids=%s（%ss 内同类异常只打一次）",
                        worker_index,
                        batch,
                        ANALYZE_ERROR_LOG_INTERVAL_SEC,
                    )
                else:
                    logger.debug(
                        "分析队列 worker-%s 异常 ids=%s",
                        worker_index,
                        batch,
                        exc_info=True,
                    )
            finally:
                for item_id in batch:
                    self._queued_ids.discard(item_id)
                    self._analyze_queue.task_done()

    async def _push_strong_signals(self, news_ids: list[int]) -> None:
        """强信号主动推送；只有真正跑起来的调度器才推，避免测试/离线实例乱发通知。"""
        if _active_scheduler is not self:
            return
        for news_id in news_ids:
            try:
                await push_if_strong(news_id)
            except Exception:
                logger.exception("强信号推送异常 id=%s", news_id)

    def _schedule_strong_signals(self, news_ids: list[int]) -> None:
        """把推送丢到后台：推送渠道（webhook/邮件）慢的时候不能卡住队列消费。"""
        if _active_scheduler is not self:
            return
        task = asyncio.create_task(
            self._push_strong_signals(list(news_ids)),
            name="news_flow_push_strong",
        )
        self._push_tasks.add(task)
        task.add_done_callback(self._push_tasks.discard)

    def _enqueue_created_ids(self, created_ids: list[int]) -> int:
        """采集后：直接把本轮新入库 id 入队（不查库扫全量 pending）。"""
        with track_job_run(
            "news_flow", "news_flow_analyze_after_ingest", persist_idle=False
        ) as summary:
            enqueued, skipped_full = self._enqueue_news_ids(
                created_ids, log_label="采集后分析"
            )
            summary["candidates"] = len(created_ids)
            summary["enqueued"] = enqueued
            summary["skipped_full"] = skipped_full
            summary["qsize"] = self._analyze_queue.qsize()
            if enqueued <= 0:
                summary["_idle"] = True
        return enqueued

    def _enqueue_catchup(self) -> None:
        """补扫：只捞入库超过 N 分钟仍未分析的 id（漏入队 / 队列满 / 历史积压）。"""
        older_than = datetime.now(timezone.utc) - timedelta(
            minutes=max(1, int(CATCHUP_MIN_AGE_MINUTES))
        )
        # 最多只占队列的一半：全占满会让「刚采集到的新新闻」直接 skipped_full。
        limit = max(
            CATCHUP_ANALYZE_LIMIT,
            int(ANALYZE_QUEUE_MAXSIZE * CATCHUP_QUEUE_SHARE),
        )
        self._enqueue_unanalyzed(
            limit=limit,
            log_label="事件驱动遗漏扫描",
            job_id="news_flow_analyze",
            older_than=older_than,
        )

    async def _ingest_job(self) -> None:
        """定时采集：按市场阶段节流，休市/午休/夜间自动降频。"""
        phase = market_phase()
        interval = ingest_min_interval_sec(phase)
        if interval > 0 and self._last_ingest_at:
            elapsed = time.monotonic() - self._last_ingest_at
            if elapsed < interval:
                logger.debug(
                    "采集节流：阶段=%s 最小间隔=%ss 距上轮=%.0fs，本轮跳过",
                    phase,
                    interval,
                    elapsed,
                )
                return
        await self._run_ingest()

    async def ingest_now(self) -> dict:
        """页面手动采集：与定时采集共用互斥，并带冷却。"""
        wait = int(self._manual_ingest_until - time.monotonic())
        if wait > 0:
            return {"status": "cooldown", "retry_after": wait}
        if self._ingesting:
            return {"status": "busy"}
        self._manual_ingest_until = time.monotonic() + MANUAL_INGEST_COOLDOWN_SEC
        # 手动采集是用户主动发起的，本条新闻要立刻分析：否则周末/夜间会
        # 被跨轮攒批拖住半小时，页面上看不到结果。
        self._force_immediate_until = (
            time.monotonic() + FORCE_IMMEDIATE_AFTER_MANUAL_SEC
        )
        result = await self._run_ingest(trigger_source="manual")
        if result is None:
            return {"status": "busy"}
        if result.pop("failed", False):
            self._manual_ingest_until = 0.0
            return {"status": "failed"}
        return {"status": "ok", **result}

    async def _run_ingest(self, *, trigger_source: str = "schedule") -> dict | None:
        """采集一轮并把新 id 入分析队列；已有采集在跑时返回 None。"""
        if self._ingesting:
            return None
        self._ingesting = True
        created_ids: list[int] = []
        counts = {"ths": 0, "wallstreet": 0, "stock_star": 0}
        failed = False
        try:
            with track_job_run(
                "news_flow",
                "news_flow_ingest",
                trigger_source=trigger_source,
                persist_idle=False,
            ) as summary:
                # 三源抓取是同步网络 IO，放线程里跑，避免卡住事件循环。
                # 同花顺第 2 页实测恒为空，抓 1 页即可。
                result = await asyncio.to_thread(
                    ingest_news_sources,
                    ths_pages=1,
                    ths_pagesize=50,
                    wallstreet_limit=8,
                    stockstar_news_limit=6,
                    stockstar_report_limit=4,
                    include_stockstar_roll=False,
                ) or {}
                created_ids = [
                    int(x) for x in (result.get("created_ids") or []) if x is not None
                ]
                counts = {
                    "ths": int(result.get("ths") or 0),
                    "wallstreet": int(result.get("wallstreet") or 0),
                    "stock_star": int(result.get("stock_star") or 0),
                }
                summary.update(counts)
                summary["created"] = len(created_ids)
                total = sum(counts.values())
                if total <= 0:
                    summary["_idle"] = True
                else:
                    logger.info(
                        "事件驱动采集：入库 %s（ths=%s wallstreet=%s stock_star=%s）",
                        total,
                        counts["ths"],
                        counts["wallstreet"],
                        counts["stock_star"],
                    )
        except Exception:
            failed = True
            logger.exception("事件驱动采集失败")
        finally:
            self._ingesting = False
        if not failed:
            # 只有成功的轮次才推进节流时钟：失败时下一 tick 应立刻重试。
            self._last_ingest_at = time.monotonic()
        # 不 await：本轮新 id 直接入队；队列满则多余跳过，由 catchup 补。
        enqueued = self._enqueue_created_ids(created_ids) if created_ids else 0
        return {
            **counts,
            "created": len(created_ids),
            "enqueued": enqueued,
            "failed": failed,
        }

    async def _analyze_catchup_job(self) -> None:
        """定时扫遗漏：把库里未分析 id 补进队列。"""
        self._enqueue_catchup()

    async def _digest_job(self) -> None:
        if self._digesting:
            return
        self._digesting = True
        try:
            with track_job_run("news_flow", "news_flow_digest") as summary:
                result = await job_news_digest_hourly()
                if isinstance(result, dict):
                    summary.update(result)
                elif result is not None:
                    summary["result"] = result
        except Exception:
            logger.exception("事件驱动速览失败")
        finally:
            self._digesting = False

    def _ensure_workers(self) -> None:
        alive = [t for t in self._analyze_worker_tasks if t is not None and not t.done()]
        self._analyze_worker_tasks = alive
        need = max(1, int(ANALYZE_WORKER_CONCURRENCY or 1)) - len(alive)
        for i in range(need):
            idx = len(self._analyze_worker_tasks)
            task = asyncio.create_task(
                self._analyze_worker(idx),
                name=f"news_flow_analyze_worker_{idx}",
            )
            self._analyze_worker_tasks.append(task)

    def stats(self) -> dict:
        """队列积压 / 待分析存量，供页面与监控读取。"""
        from src.modules.market.news_store import count_pending_analysis

        db = SessionLocal()
        try:
            pending = count_pending_analysis(db)
        except Exception:
            pending = -1
        finally:
            db.close()
        phase = market_phase()
        return {
            "queue_size": self._analyze_queue.qsize(),
            "queue_max": ANALYZE_QUEUE_MAXSIZE,
            "queue_in_flight": len(self._queued_ids),
            "workers": len([t for t in self._analyze_worker_tasks if not t.done()]),
            "worker_concurrency": ANALYZE_WORKER_CONCURRENCY,
            "batch_size": BATCH_ANALYZE_SIZE,
            "batch_linger_sec": batch_linger_sec(phase),
            "pending_analysis": pending,
            "ingesting": self._ingesting,
            "digesting": self._digesting,
            "market_phase": phase,
            "ingest_min_interval_sec": ingest_min_interval_sec(phase),
        }

    def start(self) -> None:
        self._ensure_workers()
        # 盘中：09:00-14:57 每 3 分钟；盘后：15:00-次日 08:00 每 15 分钟。
        self.scheduler.add_job(
            self._ingest_job,
            "cron",
            minute="*/3",
            hour="9-14",
            id="news_flow_ingest",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        self.scheduler.add_job(
            self._ingest_job,
            "cron",
            minute="*/15",
            hour="15-23,0-8",
            id="news_flow_ingest_after_hours",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        self.scheduler.add_job(
            self._analyze_catchup_job,
            "interval",
            minutes=5,
            id="news_flow_analyze",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        self.scheduler.add_job(
            self._digest_job,
            "cron",
            hour=f"{REFRESH_HOUR_START}-{REFRESH_HOUR_END}",
            minute=REFRESH_MINUTE,
            id="news_flow_digest",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        self.scheduler.start()
        from src.platform.scheduling.scheduler_registry import register
        from src.platform.scheduling.job_manager import apply_configs_to_scheduler
        register("news_flow", self.scheduler)
        apply_configs_to_scheduler("news_flow", self.scheduler, timezone=self.scheduler.timezone)
        global _active_scheduler
        _active_scheduler = self
        logger.info(
            "事件驱动调度器已启动（不立即采集；盘中*/3@9-14 / 盘后*/15@15-8；"
            "午休%smin / 夜间%smin / 休市%smin 自动降频；"
            "分析队列 maxsize=%s workers=%s 批量=%s；"
            "攒批等待：盘中即时 / 盘后%ds / 夜间与休市%ds）",
            INGEST_INTERVAL_BREAK_SEC // 60,
            INGEST_INTERVAL_NIGHT_SEC // 60,
            INGEST_INTERVAL_NON_TRADING_SEC // 60,
            ANALYZE_QUEUE_MAXSIZE,
            ANALYZE_WORKER_CONCURRENCY,
            BATCH_ANALYZE_SIZE,
            int(BATCH_LINGER_AFTER_CLOSE_SEC),
            int(BATCH_LINGER_NIGHT_SEC),
        )

    def shutdown(self) -> None:
        global _active_scheduler
        if _active_scheduler is self:
            _active_scheduler = None
        for task in self._push_tasks:
            if task is not None and not task.done():
                task.cancel()
        self._push_tasks.clear()
        for task in self._analyze_worker_tasks:
            if task is not None and not task.done():
                task.cancel()
        self._analyze_worker_tasks = []
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:
            pass
        logger.info("事件驱动调度器已关闭")
