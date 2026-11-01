"""价格提醒调度器：独立于 Agent 调度。"""

from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.modules.market.price_alert_engine import ENGINE
from src.modules.market.alert_delivery import AlertDeliveryWorker

logger = logging.getLogger(__name__)


class PriceAlertScheduler:
    def __init__(self, timezone: str = "UTC", interval_seconds: int = 60):
        self.scheduler = AsyncIOScheduler(timezone=timezone)
        self.interval_seconds = max(15, int(interval_seconds))
        self._running = False
        self._delivering = False
        self.delivery_worker = AlertDeliveryWorker()

    async def _delivery_job(self):
        if self._delivering:
            return
        self._delivering = True
        try:
            await self.delivery_worker.drain()
        except Exception:
            logger.exception("[价格提醒] 投递队列处理失败")
        finally:
            self._delivering = False

    async def _scan_job(self):
        if self._running:
            logger.debug("[价格提醒] 上轮扫描仍在执行，跳过本轮")
            return
        self._running = True
        try:
            result = await ENGINE.scan_once()
            triggered = result.get("triggered", 0)
            total_rules = result.get("total_rules", 0)
            # 只在有业务意义时落日志: 触发告警=INFO, 有规则但未触发=DEBUG,
            # 一条规则都没有的纯心跳不再打印,否则每分钟一条把日志板刷满。
            if triggered:
                logger.info(
                    "[价格提醒] 扫描完成: rules=%s triggered=%s skipped=%s",
                    total_rules,
                    triggered,
                    result.get("skipped", 0),
                )
            elif total_rules:
                logger.debug(
                    "[价格提醒] 扫描完成: rules=%s triggered=0 skipped=%s",
                    total_rules,
                    result.get("skipped", 0),
                )
        except Exception as e:
            logger.exception(f"[价格提醒] 扫描异常: {e}")
        finally:
            self._running = False

    async def trigger_once(self, *, dry_run: bool = False, rule_id: int | None = None) -> dict:
        return await ENGINE.scan_once(
            dry_run=dry_run, only_rule_id=rule_id, bypass_market_hours=True
        )

    def start(self):
        self.scheduler.add_job(
            self._delivery_job, "interval", seconds=10,
            id="price_alert_delivery", replace_existing=True,
            coalesce=True, max_instances=1,
        )
        self.scheduler.add_job(
            self._scan_job,
            "interval",
            seconds=self.interval_seconds,
            jitter=20,  # 抖动错峰,避免与模拟盘扫描每 60s 同刻并发写 SQLite
            id="price_alert_scan",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        self.scheduler.start()
        from src.platform.scheduling.scheduler_registry import register
        from src.platform.scheduling.job_manager import apply_configs_to_scheduler
        register("price_alert", self.scheduler)
        apply_configs_to_scheduler("price_alert", self.scheduler, timezone=str(self.scheduler.timezone))
        logger.info(f"价格提醒调度器已启动，扫描间隔 {self.interval_seconds}s")

    def shutdown(self):
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:
            pass
        logger.info("价格提醒调度器已关闭")
