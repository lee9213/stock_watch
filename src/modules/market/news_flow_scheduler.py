"""事件驱动调度：定时拉同花顺/华尔街见闻/证券之星、分析未处理新闻、每小时生成速览。"""

from __future__ import annotations

import asyncio
import logging
import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from datetime import datetime, timedelta, timezone

from src.modules.market.news_analysis import (
    CATCHUP_ANALYZE_LIMIT,
    CATCHUP_MIN_AGE_MINUTES,
    INGEST_ANALYZE_CONCURRENCY,
    analyze_by_id,
)
from src.modules.market.news_digest_job import job_news_digest_hourly
from src.modules.market.news_ingest import ingest_news_sources
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
# worker 缓存模型客户端的时长；过期或未配模型时重取，换默认模型无需重启。
ANALYZE_CLIENT_REFRESH_SEC = 60

_active_scheduler: "NewsFlowScheduler | None" = None


def get_active_scheduler() -> "NewsFlowScheduler | None":
    """API 层取当前运行中的调度器；未启动时为 None。"""
    return _active_scheduler


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
        self._analyze_queue: asyncio.Queue[int] = asyncio.Queue(
            maxsize=ANALYZE_QUEUE_MAXSIZE
        )
        # 已在队列中或正在分析的 id，避免重复入队。
        self._queued_ids: set[int] = set()
        self._analyze_worker_tasks: list[asyncio.Task] = []

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

    async def _analyze_worker(self, worker_index: int) -> None:
        """消费队列中的新闻 id，逐条分析。"""
        client = None
        loaded_at = 0.0

        while True:
            news_id = await self._analyze_queue.get()
            try:
                now = time.monotonic()
                stale = now - loaded_at >= ANALYZE_CLIENT_REFRESH_SEC
                if client is None or not getattr(client, "model", None) or stale:
                    try:
                        client = _load_analysis_client()
                        loaded_at = now
                    except Exception:
                        client = None
                        logger.exception("分析 worker-%s 获取模型客户端失败", worker_index)
                ok = await analyze_by_id(news_id, client=client)
                if ok:
                    logger.debug(
                        "分析完成 worker-%s id=%s", worker_index, news_id
                    )
            except Exception:
                logger.exception(
                    "分析队列 worker-%s 异常 id=%s", worker_index, news_id
                )
            finally:
                self._queued_ids.discard(news_id)
                self._analyze_queue.task_done()

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
        self._enqueue_unanalyzed(
            limit=max(CATCHUP_ANALYZE_LIMIT, ANALYZE_QUEUE_MAXSIZE),
            log_label="事件驱动遗漏扫描",
            job_id="news_flow_analyze",
            older_than=older_than,
        )

    async def _ingest_job(self) -> None:
        await self._run_ingest()

    async def ingest_now(self) -> dict:
        """页面手动采集：与定时采集共用互斥，并带冷却。"""
        wait = int(self._manual_ingest_until - time.monotonic())
        if wait > 0:
            return {"status": "cooldown", "retry_after": wait}
        if self._ingesting:
            return {"status": "busy"}
        self._manual_ingest_until = time.monotonic() + MANUAL_INGEST_COOLDOWN_SEC
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
                result = await asyncio.to_thread(
                    ingest_news_sources,
                    ths_pages=2,
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

    def start(self) -> None:
        self._ensure_workers()
        # 盘中：09:00-14:57 每 3 分钟；盘后：15:00-次日 08:00 每小时整点。
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
            minute=0,
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
            "事件驱动调度器已启动（不立即采集；盘中*/3@9-14 / 盘后整点@15-8；分析队列 maxsize=%s workers=%s）",
            ANALYZE_QUEUE_MAXSIZE,
            ANALYZE_WORKER_CONCURRENCY,
        )

    def shutdown(self) -> None:
        global _active_scheduler
        if _active_scheduler is self:
            _active_scheduler = None
        for task in self._analyze_worker_tasks:
            if task is not None and not task.done():
                task.cancel()
        self._analyze_worker_tasks = []
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:
            pass
        logger.info("事件驱动调度器已关闭")
