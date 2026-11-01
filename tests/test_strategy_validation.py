"""机会功能的验证口径单测：超额收益、rank 分档区分度、结论判定。

这一层的核心不是"算得准"，而是"样本不足时不许装出结论"。
历史上的问题是 ``strategy_outcomes`` 0 行、``factor_weights`` 全是默认 1.0，
却因为 ``last_calibrated_at`` 每轮都被刷新，看起来像"已标定、有胜率"。
"""

from __future__ import annotations

import itertools
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.modules.strategy import strategy_engine
from src.platform.persistence.database import Base
from src.platform.persistence.models import StrategyOutcome
from src.platform.scheduling.timezone import app_now, utc_now


@pytest.fixture
def mem_session(monkeypatch):
    """内存库，并把 strategy_engine.SessionLocal 指过去。"""
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


def _day(offset: int) -> str:
    return (app_now().date() - timedelta(days=offset)).strftime("%Y-%m-%d")


# (signal_run_id, horizon_days) 有唯一约束，每行必须给不同 id。
_run_ids = itertools.count(1)


def _add(
    db,
    *,
    rank: float | None,
    ret: float,
    bench: float | None,
    status: str = "evaluated",
    hit_target=None,
    hit_stop=None,
    day_offset: int = 10,
    symbol: str = "600519",
    horizon: int = 5,
):
    excess = round(ret - bench, 4) if bench is not None else None
    row = StrategyOutcome(
        signal_run_id=next(_run_ids),
        strategy_code="trend_follow",
        snapshot_date=_day(day_offset),
        stock_symbol=symbol,
        stock_market="CN",
        source_pool="market_scan",
        horizon_days=horizon,
        target_date=_day(1),
        base_price=10.0,
        outcome_price=round(10.0 * (1 + ret / 100.0), 4),
        outcome_return_pct=ret,
        benchmark_return_pct=bench,
        excess_return_pct=excess,
        hit_target=hit_target,
        hit_stop=hit_stop,
        outcome_status=status,
        meta={"rank_score": rank},
        evaluated_at=utc_now(),
    )
    db.add(row)
    return row


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


def test_t_stat_detects_consistent_positive_mean():
    """均值稳定为正的序列应给出显著 t 值。"""
    values = [1.0, 1.2, 0.9, 1.1, 1.0, 0.95, 1.05]
    assert strategy_engine._t_stat(values) > 2.0


def test_t_stat_needs_two_samples():
    assert strategy_engine._t_stat([1.0]) is None
    assert strategy_engine._t_stat([]) is None


def test_t_stat_none_when_zero_variance():
    """全等值方差为 0，无法判断显著性，返回 None 而不是除零或无穷。"""
    assert strategy_engine._t_stat([2.0, 2.0, 2.0]) is None


# ---------------------------------------------------------------------------
# 结论判定
# ---------------------------------------------------------------------------


def test_insufficient_samples_never_claims_edge(mem_session):
    """今天真实状态：0 条后验 → 必须报 insufficient_samples，不许给胜率。"""
    result = strategy_engine.get_strategy_validation(horizon=5, min_samples=30)
    assert result["verdict"] == "insufficient_samples"
    assert result["sample"]["evaluated"] == 0
    assert result["alpha"]["avg_return_pct"] is None
    assert "噪音" in result["note"]


def test_below_threshold_returns_insufficient_samples(mem_session):
    db = mem_session()
    for i in range(10):
        _add(db, rank=90.0, ret=3.0, bench=1.0, day_offset=10 + i)
    db.commit()
    db.close()

    result = strategy_engine.get_strategy_validation(horizon=5, min_samples=30)
    assert result["verdict"] == "insufficient_samples"
    assert result["sample"]["evaluated"] == 10
    assert result["sample"]["sufficient"] is False


def test_consistent_excess_yields_has_edge(mem_session):
    """40 条超额稳定 +2%（波动小）→ 显著正向，应判 has_edge。"""
    db = mem_session()
    for i in range(40):
        _add(db, rank=90.0, ret=3.0 + (i % 3) * 0.1, bench=1.0, day_offset=10 + i)
    db.commit()
    db.close()

    result = strategy_engine.get_strategy_validation(horizon=5, min_samples=30)
    assert result["verdict"] == "has_edge"
    assert result["alpha"]["avg_excess_pct"] == pytest.approx(2.0, abs=0.15)
    assert result["alpha"]["t_stat"] > 2.0
    assert result["alpha"]["beat_benchmark_rate"] == 100.0


def test_returns_without_benchmark_cannot_claim_edge(mem_session):
    """有收益但没基准：只能说 no_benchmark，不能把绝对收益当 alpha。"""
    db = mem_session()
    for i in range(40):
        _add(db, rank=90.0, ret=3.0, bench=None, day_offset=10 + i)
    db.commit()
    db.close()

    result = strategy_engine.get_strategy_validation(horizon=5, min_samples=30)
    assert result["verdict"] == "no_benchmark"
    assert result["alpha"]["avg_excess_pct"] is None


def test_high_rank_bucket_beats_low_rank_bucket(mem_session):
    """分档表要能看出区分度：高分档超额应高于低分档。"""
    db = mem_session()
    for i in range(20):
        _add(db, rank=95.0, ret=5.0, bench=1.0, day_offset=10 + i)
    for i in range(20):
        _add(db, rank=65.0, ret=1.2, bench=1.0, day_offset=40 + i)
    db.commit()
    db.close()

    result = strategy_engine.get_strategy_validation(horizon=5, min_samples=30)
    buckets = {b["label"]: b for b in result["discrimination"]["buckets"]}
    assert buckets["90+"]["count"] == 20
    assert buckets["60-70"]["count"] == 20
    assert buckets["90+"]["avg_excess_pct"] > buckets["60-70"]["avg_excess_pct"]
    assert result["discrimination"]["spread_pct"] > 0


def test_not_filled_excluded_from_returns(mem_session):
    """未成交的计划不是"买入结果"，不能混进收益统计。"""
    db = mem_session()
    for i in range(30):
        _add(db, rank=90.0, ret=3.0, bench=1.0, day_offset=10 + i)
    for i in range(5):
        _add(db, rank=90.0, ret=99.0, bench=1.0, status="not_filled", day_offset=50 + i)
    db.commit()
    db.close()

    result = strategy_engine.get_strategy_validation(horizon=5, min_samples=30)
    assert result["sample"]["not_filled"] == 5
    assert result["sample"]["evaluated"] == 30
    # 99% 的假收益若被计入，均值会被拉高到 15% 以上
    assert result["alpha"]["avg_return_pct"] == pytest.approx(3.0, abs=0.01)


def test_calibration_block_reports_never_calibrated(mem_session):
    """零样本时校准块必须说 never_calibrated，而不是给一个"已标定"的假象。"""
    result = strategy_engine.get_strategy_validation(horizon=5, min_samples=30)
    block = result["calibration"]
    assert block["status"] == "never_calibrated"
    assert block["calibrated_count"] == 0
    assert block["total"] >= 5
