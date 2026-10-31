"""模拟盘调度器：60 秒间隔扫描建仓/平仓。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.modules.paper_trading.paper_trading_engine import ENGINE
from src.platform.scheduling import trading_calendar as calendar
from src.platform.scheduling.exchange_calendar_data import EARLY_CLOSES
from src.platform.scheduling.job_runs import track_job_run
from src.platform.marketdata.models import MARKETS, MarketCode

logger = logging.getLogger(__name__)

# 模拟盘定时任务仅覆盖 A 股（港股/美股盘前与日终任务不再注册）。
PAPER_SCHEDULE_MARKETS: tuple[MarketCode, ...] = (MarketCode.CN,)


def _any_market_trading() -> bool:
    """关注市场任一在交易时段即为 True。全休市时行情不动,扫描可跳过(行为中性)。"""
    for m in PAPER_SCHEDULE_MARKETS:
        md = MARKETS.get(m)
        if md and md.is_trading_time():
            return True
    return False


class PaperTradingScheduler:
    def __init__(self, timezone: str = "UTC", interval_seconds: int = 60):
        self.scheduler = AsyncIOScheduler(timezone=timezone)
        self.interval_seconds = max(15, int(interval_seconds))
        self._running = False

    async def _scan_job(self):
        if self._running:
            logger.debug("[模拟盘] 上轮扫描仍在执行，跳过本轮")
            return
        if not _any_market_trading():
            logger.debug("[模拟盘] 全市场休市,跳过本轮扫描")
            return
        self._running = True
        try:
            with track_job_run(
                "paper_trading", "paper_trading_scan", persist_idle=False
            ) as summary:
                result = await ENGINE.scan_once()
                opened = int(result.get("opened", 0) or 0)
                closed = int(result.get("closed", 0) or 0)
                status = result.get("status", "?")
                summary["opened"] = opened
                summary["closed"] = closed
                summary["status"] = status
                if result.get("error"):
                    summary["error"] = str(result.get("error"))[:500]
                if status == "error":
                    raise RuntimeError(str(result.get("error") or "paper_trading_scan_error"))
                if not opened and not closed:
                    summary["_idle"] = True
                # 有实际开/平仓才是业务事件,否则只是心跳。
                level = logging.INFO if (opened or closed) else logging.DEBUG
                logger.log(
                    level,
                    "[模拟盘] 扫描完成: opened=%s closed=%s status=%s",
                    opened,
                    closed,
                    status,
                )
        except Exception as e:
            logger.exception(f"[模拟盘] 扫描异常: {e}")
        finally:
            self._running = False

    async def _premarket_job(self, market: str | None = None):
        """盘前计划通知。非交易日(周末/节假日)跳过。"""
        markets = calendar.eligible_markets([market] if market else None)
        if not markets:
            logger.debug("[模拟盘] 非交易日,跳过盘前计划通知")
            return
        try:
            from src.modules.paper_trading.paper_trading_notifier import send_premarket_plan
            await send_premarket_plan(markets=markets)
        except Exception as e:
            logger.exception(f"[模拟盘] 盘前计划通知异常: {e}")

    async def _summary_job(self, market: str | None = None):
        """日终摘要通知。非交易日(周末/节假日)跳过。"""
        markets = calendar.eligible_markets([market] if market else None)
        if not markets:
            logger.debug("[模拟盘] 非交易日,跳过日终摘要通知")
            return
        if market:
            now = calendar._now_in_market_tz(calendar._to_market_code(market))
            sessions = calendar.trading_sessions(market, now)
            due = datetime.combine(now.date(), sessions[-1].end, now.tzinfo) + timedelta(minutes=30)
            if (now.hour, now.minute) != (due.hour, due.minute):
                return
        try:
            from src.modules.paper_trading.paper_trading_notifier import send_daily_summary
            await send_daily_summary(markets=markets)
        except Exception as e:
            logger.exception(f"[模拟盘] 日终摘要通知异常: {e}")

    def start(self):
        self.scheduler.add_job(
            self._scan_job,
            "interval",
            seconds=self.interval_seconds,
            jitter=20,  # 抖动错峰,避免与价格提醒扫描每 60s 同刻并发写 SQLite
            id="paper_trading_scan",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        # 盘前/日终通知按交易所本地时钟；当前仅注册 A 股。
        for code in PAPER_SCHEDULE_MARKETS:
            definition = MARKETS.get(code)
            if not definition:
                continue
            self.scheduler.add_job(
                self._premarket_job, "cron", hour=9, minute=0,
                timezone=definition.timezone, args=[code.value],
                id=f"paper_trading_premarket_{code.value}", replace_existing=True,
                coalesce=True, max_instances=1,
            )
            closes = {definition.sessions[-1].end}
            closes.update(close for (market, _day), close in EARLY_CLOSES.items() if market == code.value)
            for close in closes:
                due = datetime.combine(datetime.today(), close) + timedelta(minutes=30)
                self.scheduler.add_job(
                    self._summary_job, "cron", hour=due.hour, minute=due.minute,
                    timezone=definition.timezone, args=[code.value],
                    id=f"paper_trading_summary_{code.value}_{due:%H%M}", replace_existing=True,
                    coalesce=True, max_instances=1,
                )
        self.scheduler.start()
        from src.platform.scheduling.scheduler_registry import register
        from src.platform.scheduling.job_manager import apply_configs_to_scheduler
        register("paper_trading", self.scheduler)
        apply_configs_to_scheduler("paper_trading", self.scheduler, timezone=str(self.scheduler.timezone))
        # 清理历史残留的港股/美股盘前与日终任务（若仍挂在调度器上）
        for job in list(self.scheduler.get_jobs()):
            jid = str(getattr(job, "id", "") or "")
            if jid.startswith("paper_trading_premarket_") or jid.startswith("paper_trading_summary_"):
                if not any(jid.endswith(f"_{m.value}") or f"_{m.value}_" in jid for m in PAPER_SCHEDULE_MARKETS):
                    # premarket_HK / summary_HK_HHMM / premarket_US / summary_US_*
                    if "_HK" in jid or jid.endswith("_HK") or "_US" in jid or jid.endswith("_US"):
                        try:
                            self.scheduler.remove_job(jid)
                            logger.info("[模拟盘] 已移除港股/美股任务: %s", jid)
                        except Exception:
                            pass
        logger.info(
            "模拟盘调度器已启动，扫描间隔 %ss，通知市场=%s",
            self.interval_seconds,
            ",".join(m.value for m in PAPER_SCHEDULE_MARKETS),
        )

    def shutdown(self):
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:
            pass
        logger.info("模拟盘调度器已关闭")
