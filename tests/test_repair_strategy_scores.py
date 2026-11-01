"""历史评分修正脚本的单测：口径统一、幂等、降级行回落。

对应的问题：库里的行是旧代码打的，``rank_score`` 上并存两种口径（可执行行是
当日百分位，n=1 时必然 100；非可执行行是封顶后的合成分），``absolute_score``
一行两个值（payload 写自己的 composite，breakdown 写股票最高值），非可执行行
干脆没有 payload.absolute_score。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.modules.strategy import strategy_engine
from src.platform.persistence.database import Base
from src.platform.persistence.models import StrategySignalRun


def _load_script():
    path = Path(__file__).resolve().parent.parent / "scripts" / "repair_strategy_scores.py"
    spec = importlib.util.spec_from_file_location("repair_strategy_scores", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


repair = _load_script()


@pytest.fixture
def mem_session(monkeypatch):
    import src.platform.persistence.models  # noqa: F401  注册 ORM 元数据

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(strategy_engine, "SessionLocal", factory)
    return factory


def _row(
    db,
    *,
    symbol: str,
    composite: float,
    rank: float,
    absolute_payload: float | None,
    absolute_breakdown: float,
    status: str = "active",
    action: str = "buy",
    entry: float | None = 10.0,
    plan_quality: int = 100,
):
    row = StrategySignalRun(
        snapshot_date="2026-11-01",
        stock_symbol=symbol,
        stock_market="CN",
        strategy_code="trend_follow",
        strategy_name="趋势跟随",
        status=status,
        action=action,
        rank_score=rank,
        plan_quality=plan_quality,
        entry_low=entry,
        entry_high=entry + 0.2 if entry is not None else None,
    )
    row.payload = {
        "score_breakdown": {
            "composite_score": composite,
            "absolute_score": absolute_breakdown,
            "weighted_score": rank,
        },
    }
    if absolute_payload is not None:
        row.payload["absolute_score"] = absolute_payload
    db.add(row)
    db.flush()
    return row


def test_single_actionable_stock_drops_from_100_and_syncs_absolute(mem_session):
    db = mem_session()
    # 旧代码：n=1 时百分位除零退回 1.0 → 100 分；两处 absolute 分别是 136.05 / 145.78
    row = _row(
        db,
        symbol="600519",
        composite=145.781,
        rank=100.0,
        absolute_payload=136.054,
        absolute_breakdown=145.781,
    )
    db.commit()

    stats = repair.repair_snapshot(db, "2026-11-01", apply=True)

    db.refresh(row)
    assert row.rank_score == 87.5  # 中点口径，不再是"没有对照的第一名"
    breakdown = row.payload["score_breakdown"]
    assert breakdown["absolute_score"] == 145.781
    assert row.payload["absolute_score"] == 145.781  # 顶层与 breakdown 同一个数
    assert breakdown["percentile_score"] == 87.5
    assert breakdown["rank_pct"] == 50.0
    assert stats["rank_changed"] == 1


def test_non_actionable_row_gets_payload_absolute(mem_session):
    db = mem_session()
    row = _row(
        db,
        symbol="600118",
        composite=98.5307,
        rank=65.0,
        absolute_payload=None,  # 旧行没有这个键
        absolute_breakdown=98.5307,
        status="inactive",
        action="watch",
        entry=None,
        plan_quality=70,
    )
    db.commit()

    repair.repair_snapshot(db, "2026-11-01", apply=True)

    db.refresh(row)
    assert row.rank_score == 65.0  # 封顶值本来就对，不该被百分位改掉
    assert row.payload["absolute_score"] == 98.5307
    assert row.payload["score_breakdown"]["absolute_score"] == 98.5307
    assert "percentile_score" not in row.payload  # 没参与排名就不该有排名分
    assert row.actionable in (False, 0)


def test_rows_that_lost_the_entry_window_fall_back_to_the_cap(mem_session):
    db = mem_session()
    row = _row(
        db,
        symbol="600519",
        composite=145.781,
        rank=100.0,
        absolute_payload=145.781,
        absolute_breakdown=145.781,
        entry=None,  # 买点没了 → 不可执行
        plan_quality=70,
    )
    db.commit()

    repair.repair_snapshot(db, "2026-11-01", apply=True)

    db.refresh(row)
    assert row.actionable in (False, 0)
    # buy 但无买点 → CAP_NO_ENTRY_PLAN(66)，不再挂着 100 分说"可买"
    assert row.rank_score == 66.0


def test_repair_is_idempotent(mem_session):
    db = mem_session()
    _row(
        db,
        symbol="600519",
        composite=145.781,
        rank=100.0,
        absolute_payload=136.054,
        absolute_breakdown=145.781,
    )
    db.commit()

    repair.repair_snapshot(db, "2026-11-01", apply=True)
    first = [(r.stock_symbol, r.rank_score, r.payload["absolute_score"]) for r in db.query(StrategySignalRun)]

    repair.repair_snapshot(db, "2026-11-01", apply=True)
    second = [(r.stock_symbol, r.rank_score, r.payload["absolute_score"]) for r in db.query(StrategySignalRun)]

    assert first == second
