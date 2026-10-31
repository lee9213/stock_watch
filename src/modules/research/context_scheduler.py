"""上下文维护调度器：后验评估 + 过期数据清理 + 机会自动刷新。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.platform.marketdata.collectors.kline_collector import kline_source
from src.modules.research.context_store import cleanup_context_data
from src.modules.strategy.entry_candidates import evaluate_entry_candidate_outcomes
from src.modules.research.prediction_outcome import evaluate_pending_prediction_outcomes
from src.modules.strategy.strategy_engine import (
    evaluate_strategy_outcomes,
    rebalance_strategy_weights,
    refresh_strategy_signals,
)
from src.platform.scheduling.job_runs import cleanup_job_runs, track_job_run
from src.platform.scheduling.job_runs import cleanup_job_runs, track_job_run

logger = logging.getLogger(__name__)

# 机会自动刷新时点（调度器时区）：盘前 / 盘中（接新闻催化）/ 午盘 / 午后 / 晚间
REFRESH_SLOTS = ((9, 15), (10, 30), (13, 30), (14, 30), (22, 0))
# 启动后延迟多久检查是否需要补跑，避开启动期的新闻采集写库
STARTUP_CATCHUP_DELAY_SECONDS = 90


def _latest_candidate_snapshot() -> str:
    from src.platform.persistence.database import SessionLocal
    from src.platform.persistence.models import EntryCandidate

    db = SessionLocal()
    try:
        row = db.query(EntryCandidate.snapshot_date).order_by(EntryCandidate.snapshot_date.desc()).first()
        return row[0] if row else ""
    finally:
        db.close()


def needs_startup_catchup(now: datetime, latest_snapshot: str) -> bool:
    """首个刷新时点之后启动、且今天还没有快照时需要补跑。"""
    if (now.hour, now.minute) < REFRESH_SLOTS[0]:
        return False
    return (latest_snapshot or "") < now.date().isoformat()


class ContextMaintenanceScheduler:
    def __init__(
        self,
        timezone: str = "UTC",
        evaluation_hour: int = 4,
        evaluation_minute: int = 30,
        snapshot_retention_days: int = 180,
        outcome_retention_days: int = 365,
    ):
        self.scheduler = AsyncIOScheduler(timezone=timezone)
        self.evaluation_hour = max(0, min(23, int(evaluation_hour)))
        self.evaluation_minute = max(0, min(59, int(evaluation_minute)))
        self.snapshot_retention_days = max(30, int(snapshot_retention_days))
        self.outcome_retention_days = max(60, int(outcome_retention_days))
        self._evaluating = False
        self._cleaning = False
        self._refreshing = False

    async def _evaluate_job(self):
        if self._evaluating:
            logger.debug("[上下文维护] 上一轮后验评估仍在执行，跳过本轮")
            return
        self._evaluating = True
        try:
            with track_job_run("context", "context_maintenance_evaluate") as job_summary:
                await self._run_evaluate_body(job_summary)
        except Exception as e:
            logger.exception(f"[上下文维护] 后验评估异常: {e}")
        finally:
            self._evaluating = False

    async def _run_evaluate_body(self, job_summary: dict) -> None:
        stats = await asyncio.to_thread(evaluate_pending_prediction_outcomes)
        job_summary["agent_predictions"] = {
            "evaluated": stats.get("evaluated", 0),
            "eligible": stats.get("eligible", 0),
        }
        level = logging.INFO if stats.get("evaluated", 0) else logging.DEBUG
        logger.log(
            level,
            "[上下文维护] 后验评估完成: pending=%s eligible=%s evaluated=%s skipped_not_due=%s skipped_no_price=%s",
            stats.get("total_pending", 0),
            stats.get("eligible", 0),
            stats.get("evaluated", 0),
            stats.get("skipped_not_due", 0),
            stats.get("skipped_no_price", 0),
        )
        with kline_source("outcome_eval"):
            cand_stats = await asyncio.to_thread(
                evaluate_entry_candidate_outcomes,
                horizons=(1, 3, 5, 10),
                snapshot_days=45,
                limit=500,
            )
        job_summary["entry_candidates"] = {
            "evaluated": cand_stats.get("evaluated", 0),
            "eligible": cand_stats.get("eligible", 0),
        }
        level = logging.INFO if cand_stats.get("evaluated", 0) else logging.DEBUG
        logger.log(
            level,
            "[上下文维护] 候选后验评估完成: total=%s eligible=%s evaluated=%s skipped_not_due=%s skipped_no_price=%s",
            cand_stats.get("total_candidates", 0),
            cand_stats.get("eligible", 0),
            cand_stats.get("evaluated", 0),
            cand_stats.get("skipped_not_due", 0),
            cand_stats.get("skipped_no_price", 0),
        )
        with kline_source("outcome_eval"):
            strategy_stats = await asyncio.to_thread(
                evaluate_strategy_outcomes,
                horizons=(1, 3, 5, 10),
                snapshot_days=60,
                limit=1200,
            )
        job_summary["strategy_outcomes"] = {
            "evaluated": strategy_stats.get("evaluated", 0),
            "eligible": strategy_stats.get("eligible", 0),
        }
        level = logging.INFO if strategy_stats.get("evaluated", 0) else logging.DEBUG
        logger.log(
            level,
            "[上下文维护] 策略后验评估完成: total=%s eligible=%s evaluated=%s skipped_not_due=%s skipped_no_price=%s",
            strategy_stats.get("total_signals", 0),
            strategy_stats.get("eligible", 0),
            strategy_stats.get("evaluated", 0),
            strategy_stats.get("skipped_not_due", 0),
            strategy_stats.get("skipped_no_price", 0),
        )
        rebalance = await asyncio.to_thread(
            rebalance_strategy_weights,
            window_days=45,
            min_samples=8,
            alpha=0.35,
            regime="default",
        )
        job_summary["strategy_rebalance"] = {
            "changed": rebalance.get("changed", 0),
            "checked": rebalance.get("checked", 0),
        }
        level = logging.INFO if rebalance.get("changed", 0) else logging.DEBUG
        logger.log(
            level,
            "[上下文维护] 策略调权完成: changed=%s checked=%s skipped_low_sample=%s",
            rebalance.get("changed", 0),
            rebalance.get("checked", 0),
            rebalance.get("skipped_low_sample", 0),
        )

        # Phase 4 → 因子自校准闭环:把 IC/IR 接进每因子权重的轻量标定
        try:
            from src.modules.strategy.factor_calibration import calibrate_all_markets

            fcal = await asyncio.to_thread(calibrate_all_markets)
            changed = sum(r.get("changed", 0) for r in fcal.values())
            job_summary["factor_calibration_changed"] = changed
            logger.log(
                logging.INFO if changed else logging.DEBUG,
                "[上下文维护] 因子自校准完成: changed=%s detail=%s",
                changed,
                {m: r.get("changed", 0) for m, r in fcal.items()},
            )
        except Exception as fc_err:
            logger.debug("[上下文维护] 因子自校准跳过: %s", fc_err)

    async def _cleanup_job(self):
        if self._cleaning:
            logger.debug("[上下文维护] 上一轮清理仍在执行，跳过本轮")
            return
        self._cleaning = True
        try:
            with track_job_run("context", "context_maintenance_cleanup") as summary:
                deleted = await asyncio.to_thread(
                    cleanup_context_data,
                    snapshot_days=self.snapshot_retention_days,
                    topic_days=self.snapshot_retention_days,
                    context_run_days=self.snapshot_retention_days,
                    outcome_days=self.outcome_retention_days,
                )
                job_runs_deleted = await asyncio.to_thread(
                    cleanup_job_runs, retention_days=30
                )
                summary["context_deleted"] = deleted if isinstance(deleted, dict) else {"deleted": deleted}
                summary["scheduler_job_runs_deleted"] = int(job_runs_deleted or 0)
                has_work = bool(
                    deleted and any(deleted.values()) if isinstance(deleted, dict) else deleted
                ) or bool(job_runs_deleted)
                if not has_work:
                    summary["_idle"] = True
                level = logging.INFO if has_work else logging.DEBUG
                logger.log(level, "[上下文维护] 清理完成: %s job_runs=%s", deleted, job_runs_deleted)
        except Exception as e:
            logger.exception(f"[上下文维护] 清理异常: {e}")
        finally:
            self._cleaning = False

    async def evaluate_once(self) -> dict:
        agent_task = asyncio.to_thread(evaluate_pending_prediction_outcomes)
        candidate_task = asyncio.to_thread(
            evaluate_entry_candidate_outcomes,
            horizons=(1, 3, 5, 10),
            snapshot_days=45,
            limit=500,
        )
        strategy_eval_task = asyncio.to_thread(
            evaluate_strategy_outcomes,
            horizons=(1, 3, 5, 10),
            snapshot_days=60,
            limit=1200,
        )
        strategy_rebalance_task = asyncio.to_thread(
            rebalance_strategy_weights,
            window_days=45,
            min_samples=8,
            alpha=0.35,
            regime="default",
        )
        agent_stats, candidate_stats, strategy_eval_stats, strategy_rebalance_stats = await asyncio.gather(
            agent_task,
            candidate_task,
            strategy_eval_task,
            strategy_rebalance_task,
        )
        # 因子自校准:须在 outcome 评估之后(IC 才新鲜),不能并进上面的 gather。
        from src.modules.strategy.factor_calibration import calibrate_all_markets

        factor_calibration_stats = await asyncio.to_thread(calibrate_all_markets)
        return {
            "agent_predictions": agent_stats,
            "entry_candidates": candidate_stats,
            "strategy_outcomes": strategy_eval_stats,
            "strategy_rebalance": strategy_rebalance_stats,
            "factor_calibration": factor_calibration_stats,
        }

    async def _refresh_opportunities_job(self):
        """定时刷新机会池（候选 + 策略信号）。全市场休市日跳过。"""
        from src.platform.scheduling.trading_calendar import eligible_markets
        from src.modules.strategy.opportunity_markets import resolve_opportunity_markets

        markets = resolve_opportunity_markets(eligible_markets())
        if not markets:
            logger.debug("[上下文维护] 非交易日，跳过机会刷新")
            return
        if self._refreshing:
            logger.debug("[上下文维护] 上一轮机会刷新仍在执行，跳过本轮")
            return
        self._refreshing = True
        try:
            with track_job_run("context", "context_maintenance_refresh_opportunities") as summary:
                with kline_source("refresh_opportunities"):
                    result = await asyncio.to_thread(
                        refresh_strategy_signals,
                        rebuild_candidates=True,
                        markets=markets,
                        max_inputs=500,
                        market_scan_limit=80,
                        max_kline_symbols=60,
                        limit_candidates=2000,
                    )
                summary["snapshot_date"] = result.get("snapshot_date", "")
                summary["count"] = int(result.get("count", 0) or 0)
                summary["markets"] = list(markets)
                if not summary["count"]:
                    summary["_idle"] = True
                level = logging.INFO if result.get("count", 0) else logging.DEBUG
                logger.log(
                    level,
                    "[上下文维护] 机会自动刷新完成: snapshot_date=%s count=%s",
                    result.get("snapshot_date", ""),
                    result.get("count", 0),
                )
        except Exception as e:
            logger.exception(f"[上下文维护] 机会自动刷新异常: {e}")
        finally:
            self._refreshing = False

    async def _startup_catchup_refresh_job(self):
        """服务停机错过定时刷新时，启动后补跑一次（休市日由刷新任务自身跳过）。"""
        slot_jobs = [
            self.scheduler.get_job(f"context_maintenance_refresh_opportunities_{h:02d}{m:02d}")
            for h, m in REFRESH_SLOTS
        ]
        if not any(job is not None and job.next_run_time is not None for job in slot_jobs):
            return  # 定时刷新已全部暂停，尊重配置不补跑
        now = datetime.now(self.scheduler.timezone)
        latest = await asyncio.to_thread(_latest_candidate_snapshot)
        if not needs_startup_catchup(now, latest):
            return
        logger.info("[上下文维护] 启动补跑机会刷新：最新快照=%s，今天=%s", latest or "-", now.date())
        await self._refresh_opportunities_job()

    async def refresh_opportunities_once(self) -> dict:
        """手动触发一次机会刷新。"""
        with kline_source("refresh_opportunities"):
            return await asyncio.to_thread(
                refresh_strategy_signals,
                rebuild_candidates=True,
                max_inputs=500,
                market_scan_limit=80,
                max_kline_symbols=60,
                limit_candidates=2000,
            )

    async def cleanup_once(self) -> dict:
        return await asyncio.to_thread(
            cleanup_context_data,
            snapshot_days=self.snapshot_retention_days,
            topic_days=self.snapshot_retention_days,
            context_run_days=self.snapshot_retention_days,
            outcome_days=self.outcome_retention_days,
        )

    async def _refresh_trading_calendar_job(self):
        """刷新本地近期窗口,不联网拉取历史日历。"""
        from src.platform.scheduling.trading_calendar import refresh

        try:
            await refresh()
        except Exception as e:  # refresh 内部已兜异常,这里只防意外
            logger.exception(f"[上下文维护] 交易日历刷新异常: {e}")

    def start(self):
        # 夜间全量后验（含多日到期样本）
        self.scheduler.add_job(
            self._evaluate_job,
            "cron",
            hour=self.evaluation_hour,
            minute=self.evaluation_minute,
            jitter=120,  # 错峰,避免与 price_alert/paper_trading(60s)同刻写 SQLite
            id="context_maintenance_evaluate",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        # A 股收盘后快速复盘（未到期样本会 skip，不抢夜间全量）
        self.scheduler.add_job(
            self._evaluate_job,
            "cron",
            hour=15,
            minute=45,
            jitter=90,
            id="context_maintenance_evaluate_cn_close",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        self.scheduler.add_job(
            self._cleanup_job,
            "cron",
            hour=4,
            minute=15,
            jitter=120,
            id="context_maintenance_cleanup",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        # 交易日历每日刷新 —— 03:00,早于所有盘前通知
        self.scheduler.add_job(
            self._refresh_trading_calendar_job,
            "cron",
            hour=3,
            minute=0,
            jitter=120,
            id="context_maintenance_trading_calendar",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        # 时间点按调度器时区(app_timezone,默认 Asia/Shanghai)解释,与 Agent cron 语义一致。
        for job_hour, job_minute in REFRESH_SLOTS:
            self.scheduler.add_job(
                self._refresh_opportunities_job,
                "cron",
                hour=job_hour,
                minute=job_minute,
                jitter=120,  # 错峰,避免与其它调度同刻写 SQLite
                id=f"context_maintenance_refresh_opportunities_{job_hour:02d}{job_minute:02d}",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
            )
        self.scheduler.add_job(
            self._startup_catchup_refresh_job,
            "date",
            run_date=datetime.now(self.scheduler.timezone) + timedelta(seconds=STARTUP_CATCHUP_DELAY_SECONDS),
            id="context_maintenance_refresh_opportunities_catchup",
            replace_existing=True,
        )
        self.scheduler.start()
        from src.platform.scheduling.scheduler_registry import register
        from src.platform.scheduling.job_manager import apply_configs_to_scheduler
        register("context", self.scheduler)
        apply_configs_to_scheduler("context", self.scheduler, timezone=str(self.scheduler.timezone))
        logger.info(
            "上下文维护调度器已启动（后验评估 %02d:%02d + 15:45，快照保留 %s 天，后验保留 %s 天，机会自动刷新 09:15/10:30/13:30/14:30/22:00，交易日历刷新 03:00）",
            self.evaluation_hour,
            self.evaluation_minute,
            self.snapshot_retention_days,
            self.outcome_retention_days,
        )

    def shutdown(self):
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:
            pass
        logger.info("上下文维护调度器已关闭")
