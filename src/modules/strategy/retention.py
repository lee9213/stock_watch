"""策略链路数据留存清理。

机会相关表此前只增不减：``cleanup_context_data`` 只覆盖 context/news 三张表，
而候选/信号/评估/热榜快照按"每天全市场重刷"的节奏增长。按 2000 条信号 + 8000 条
评估估算，一年后策略表约 50 万行、评估表约 200 万行——SQLite 上单次刷新要把当日行
全量载入，加上评估回扫，会明显变慢且库文件持续膨胀。

这里给出一份集中式清理，按 snapshot_date / created_at 双口径裁撤，顺序遵循外键：
先删评估与因子快照（引用 signal_run / candidate），再删主体，最后删热榜快照。
``strategy_factor_snapshots`` 与信号行一对一同量级，漏删会留下永久孤儿行。
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta

from src.modules.strategy.scoring_config import (
    RETENTION_CANDIDATE_DAYS,
    RETENTION_FACTOR_SNAPSHOT_DAYS,
    RETENTION_MARKET_SCAN_DAYS,
    RETENTION_OUTCOME_DAYS,
    RETENTION_SIGNAL_DAYS,
    RETENTION_WEIGHT_HISTORY_DAYS,
)
from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import (
    EntryCandidate,
    EntryCandidateOutcome,
    FactorWeightHistory,
    MarketScanSnapshot,
    StrategyFactorSnapshot,
    StrategyOutcome,
    StrategySignalRun,
    StrategyWeightHistory,
)
from src.platform.scheduling.timezone import app_now

logger = logging.getLogger(__name__)

_CHUNK = 500


def cleanup_strategy_data(
    *,
    candidate_days: int | None = None,
    signal_days: int | None = None,
    outcome_days: int | None = None,
    market_scan_days: int | None = None,
    factor_snapshot_days: int | None = None,
    weight_history_days: int | None = None,
) -> dict[str, int]:
    """按保留天数裁撤策略链路的历史数据；返回各表删除行数。

    传 0 表示该表不清理（便于只开一部分、或在测试里关掉）。
    """
    candidate_days = RETENTION_CANDIDATE_DAYS if candidate_days is None else int(candidate_days)
    signal_days = RETENTION_SIGNAL_DAYS if signal_days is None else int(signal_days)
    outcome_days = RETENTION_OUTCOME_DAYS if outcome_days is None else int(outcome_days)
    market_scan_days = (
        RETENTION_MARKET_SCAN_DAYS if market_scan_days is None else int(market_scan_days)
    )
    factor_snapshot_days = (
        # 因子快照与信号行一对一同源，默认跟随信号保留期：单独关掉信号清理却
        # 继续删因子快照会留下指向已删信号的悬空引用。
        signal_days
        if factor_snapshot_days is None
        else int(factor_snapshot_days)
    )
    weight_history_days = (
        RETENTION_WEIGHT_HISTORY_DAYS
        if weight_history_days is None
        else int(weight_history_days)
    )

    deleted: dict[str, int] = {}
    today = app_now().date()

    def _cutoff(days: int) -> str | None:
        if days <= 0:
            return None
        return (today - timedelta(days=days)).strftime("%Y-%m-%d")

    db = SessionLocal()
    try:
        outcome_cutoff = _cutoff(outcome_days)
        if outcome_cutoff:
            deleted["strategy_outcomes"] = (
                db.query(StrategyOutcome)
                .filter(StrategyOutcome.snapshot_date < outcome_cutoff)
                .delete(synchronize_session=False)
            )
            deleted["entry_candidate_outcomes"] = (
                db.query(EntryCandidateOutcome)
                .filter(EntryCandidateOutcome.snapshot_date < outcome_cutoff)
                .delete(synchronize_session=False)
            )

        # 因子快照引用 signal_run，必须先于信号行删除，否则会留下孤儿行。
        factor_cutoff = _cutoff(factor_snapshot_days)
        if factor_cutoff:
            deleted["strategy_factor_snapshots"] = (
                db.query(StrategyFactorSnapshot)
                .filter(StrategyFactorSnapshot.snapshot_date < factor_cutoff)
                .delete(synchronize_session=False)
            )

        candidate_cutoff = _cutoff(candidate_days)
        if candidate_cutoff:
            # 先删评估再删主体：SQLite 默认不启用外键级联，顺序反了会留下孤儿行。
            stale_ids = [
                int(x[0])
                for x in db.query(EntryCandidate.id)
                .filter(EntryCandidate.snapshot_date < candidate_cutoff)
                .all()
            ]
            if stale_ids:
                chunk_deleted = 0
                for start in range(0, len(stale_ids), _CHUNK):
                    chunk = stale_ids[start : start + _CHUNK]
                    chunk_deleted += (
                        db.query(EntryCandidateOutcome)
                        .filter(EntryCandidateOutcome.candidate_id.in_(chunk))
                        .delete(synchronize_session=False)
                    )
                deleted["entry_candidate_outcomes"] = deleted.get(
                    "entry_candidate_outcomes", 0
                ) + chunk_deleted
            deleted["entry_candidates"] = (
                db.query(EntryCandidate)
                .filter(EntryCandidate.snapshot_date < candidate_cutoff)
                .delete(synchronize_session=False)
            )

        signal_cutoff = _cutoff(signal_days)
        if signal_cutoff:
            stale_signal_ids = [
                int(x[0])
                for x in db.query(StrategySignalRun.id)
                .filter(StrategySignalRun.snapshot_date < signal_cutoff)
                .all()
            ]
            if stale_signal_ids:
                for start in range(0, len(stale_signal_ids), _CHUNK):
                    chunk = stale_signal_ids[start : start + _CHUNK]
                    # 兜底：即使因子快照/评估的保留期被单独调长，也不留指向已删信号的孤儿行。
                    db.query(StrategyFactorSnapshot).filter(
                        StrategyFactorSnapshot.signal_run_id.in_(chunk)
                    ).delete(synchronize_session=False)
                    db.query(StrategyOutcome).filter(
                        StrategyOutcome.signal_run_id.in_(chunk)
                    ).delete(synchronize_session=False)
            deleted["strategy_signal_runs"] = (
                db.query(StrategySignalRun)
                .filter(StrategySignalRun.snapshot_date < signal_cutoff)
                .delete(synchronize_session=False)
            )

        # 权重变更审计按 created_at 裁撤（事件记录，没有 snapshot_date）。
        if weight_history_days > 0:
            history_since = datetime.combine(
                today - timedelta(days=weight_history_days), time.min
            )
            deleted["strategy_weight_history"] = (
                db.query(StrategyWeightHistory)
                .filter(StrategyWeightHistory.created_at < history_since)
                .delete(synchronize_session=False)
            )
            deleted["factor_weight_history"] = (
                db.query(FactorWeightHistory)
                .filter(FactorWeightHistory.created_at < history_since)
                .delete(synchronize_session=False)
            )

        scan_cutoff = _cutoff(market_scan_days)
        if scan_cutoff:
            deleted["market_scan_snapshots"] = (
                db.query(MarketScanSnapshot)
                .filter(MarketScanSnapshot.snapshot_date < scan_cutoff)
                .delete(synchronize_session=False)
            )

        db.commit()
        if any(v for v in deleted.values()):
            logger.info("[策略留存] 清理完成: %s", deleted)
        return deleted
    except Exception as e:  # noqa: BLE001 - 清理失败不应影响主流程
        db.rollback()
        logger.warning("[策略留存] 清理失败: %s", e)
        return deleted
    finally:
        db.close()
