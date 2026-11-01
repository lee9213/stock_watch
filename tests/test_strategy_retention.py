"""策略链路数据留存清理。

四张机会相关表此前只增不减（cleanup_context_data 只覆盖 context/news），
按每天全市场重刷的节奏一年能涨到几十万行。这里验证：
按 snapshot_date 裁撤、外键顺序正确（先评估后主体）、0 表示不清理。
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.platform.persistence.models  # noqa: F401  注册所有 ORM 模型
import src.modules.strategy.retention as retention
from src.platform.persistence.database import Base
from src.platform.persistence.models import (
    EntryCandidate,
    EntryCandidateOutcome,
    MarketScanSnapshot,
    StrategyOutcome,
    StrategySignalRun,
)
from src.platform.scheduling.timezone import app_now


def _session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _days_ago(days: int) -> str:
    return (app_now().date() - timedelta(days=days)).strftime("%Y-%m-%d")


def test_cleanup_drops_only_expired_rows(monkeypatch):
    Session = _session_factory()
    monkeypatch.setattr(retention, "SessionLocal", Session)

    old, fresh = _days_ago(400), _days_ago(1)
    db = Session()
    try:
        for snap in (old, fresh):
            cand = EntryCandidate(
                stock_symbol="600519",
                stock_market="CN",
                snapshot_date=snap,
                status="active",
                score=80,
                action="buy",
            )
            db.add(cand)
            db.flush()
            db.add(
                EntryCandidateOutcome(
                    candidate_id=cand.id,
                    snapshot_date=snap,
                    stock_symbol="600519",
                    stock_market="CN",
                    horizon_days=3,
                )
            )
            signal = StrategySignalRun(
                snapshot_date=snap,
                stock_symbol="600519",
                stock_market="CN",
                strategy_code="trend_follow",
                rank_score=80,
                actionable=True,
            )
            db.add(signal)
            db.flush()
            db.add(
                StrategyOutcome(
                    signal_run_id=signal.id,
                    strategy_code="trend_follow",
                    snapshot_date=snap,
                    stock_symbol="600519",
                    stock_market="CN",
                    horizon_days=3,
                )
            )
            db.add(
                MarketScanSnapshot(
                    snapshot_date=snap,
                    stock_symbol="600519",
                    stock_market="CN",
                )
            )
        db.commit()
    finally:
        db.close()

    deleted = retention.cleanup_strategy_data(
        candidate_days=180,
        signal_days=365,
        outcome_days=365,
        market_scan_days=60,
    )

    assert deleted["entry_candidates"] == 1
    assert deleted["strategy_signal_runs"] == 1
    assert deleted["market_scan_snapshots"] == 1

    db = Session()
    try:
        assert db.query(EntryCandidate).count() == 1
        assert db.query(EntryCandidate).one().snapshot_date == fresh
        assert db.query(StrategySignalRun).count() == 1
        assert db.query(MarketScanSnapshot).count() == 1
        # 过期候选的评估行必须一起删掉，不能留孤儿
        assert db.query(EntryCandidateOutcome).count() == 1
    finally:
        db.close()


def test_cleanup_zero_days_means_keep_all(monkeypatch):
    Session = _session_factory()
    monkeypatch.setattr(retention, "SessionLocal", Session)

    db = Session()
    try:
        db.add(
            EntryCandidate(
                stock_symbol="000001",
                stock_market="CN",
                snapshot_date=_days_ago(500),
                status="inactive",
                score=50,
            )
        )
        db.commit()
    finally:
        db.close()

    deleted = retention.cleanup_strategy_data(
        candidate_days=0,
        signal_days=0,
        outcome_days=0,
        market_scan_days=0,
        factor_snapshot_days=0,
        weight_history_days=0,
    )
    assert deleted == {}

    db = Session()
    try:
        assert db.query(EntryCandidate).count() == 1
    finally:
        db.close()


def test_factor_snapshots_removed_with_signals(monkeypatch):
    """因子快照与信号行一对一，漏删会留下永久孤儿行。

    改造前 retention 只删 strategy_signal_runs，strategy_factor_snapshots 无人清理，
    而它和信号行同量级（每个信号一行）。
    """
    from src.platform.persistence.models import StrategyFactorSnapshot

    Session = _session_factory()
    monkeypatch.setattr(retention, "SessionLocal", Session)

    old, fresh = _days_ago(400), _days_ago(1)
    db = Session()
    try:
        for snap in (old, fresh):
            signal = StrategySignalRun(
                snapshot_date=snap,
                stock_symbol="600519",
                stock_market="CN",
                strategy_code="trend_follow",
                rank_score=80,
                actionable=True,
            )
            db.add(signal)
            db.flush()
            db.add(
                StrategyFactorSnapshot(
                    signal_run_id=signal.id,
                    snapshot_date=snap,
                    stock_symbol="600519",
                    stock_market="CN",
                    strategy_code="trend_follow",
                    alpha_score=3.0,
                    final_score=80.0,
                )
            )
        db.commit()
    finally:
        db.close()

    retention.cleanup_strategy_data(
        candidate_days=0,
        signal_days=365,
        outcome_days=0,
        market_scan_days=0,
        weight_history_days=0,
    )

    db = Session()
    try:
        assert db.query(StrategySignalRun).count() == 1
        assert db.query(StrategyFactorSnapshot).count() == 1
        remaining = db.query(StrategyFactorSnapshot).one()
        assert remaining.snapshot_date == fresh
    finally:
        db.close()


def test_factor_snapshot_default_follows_signal_retention():
    """单独关掉信号清理时，因子快照也不该被删（否则留下悬空引用）。"""
    import inspect

    src = inspect.getsource(retention.cleanup_strategy_data)
    assert "signal_days\n        if factor_snapshot_days is None" in src or (
        "factor_snapshot_days is None" in src and "signal_days" in src
    )
