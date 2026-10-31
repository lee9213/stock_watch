"""可执行信号排序分百分位归一化单测。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.modules.strategy.strategy_engine import (
    _apply_direct_news_constraint,
    _normalize_actionable_rank,
)


_seq = iter(range(100000))


def _row(composite, *, rank=100.0, status="active", action="buy", market="CN", entry=10.0, symbol=None):
    return SimpleNamespace(
        stock_symbol=symbol or f"S{next(_seq)}",
        status=status,
        action=action,
        stock_market=market,
        entry_low=entry,
        entry_high=entry,
        rank_score=rank,
        is_holding_snapshot=False,
        reason="",
        confidence=0.9,
        payload={"score_breakdown": {"composite_score": composite, "weighted_score": rank}},
    )


def test_saturated_scores_spread_to_75_100():
    rows = [_row(120.0), _row(130.0), _row(140.0)]
    changed = _normalize_actionable_rank(rows=rows)
    assert changed == 3
    assert [r.rank_score for r in rows] == [75.0, 87.5, 100.0]
    assert rows[2].payload["score_breakdown"]["weighted_score"] == 100.0


def test_ties_share_same_rank():
    rows = [_row(130.0), _row(130.0), _row(110.0)]
    _normalize_actionable_rank(rows=rows)
    assert rows[0].rank_score == rows[1].rank_score == 100.0
    assert rows[2].rank_score == 75.0


def test_non_actionable_rows_untouched():
    low = _row(80.0, rank=70.0)
    inactive = _row(150.0, status="inactive", rank=65.0)
    watch = _row(150.0, action="watch", rank=65.0)
    no_plan = _row(150.0, entry=None)
    rows = [low, inactive, watch, no_plan]
    assert _normalize_actionable_rank(rows=rows) == 0
    assert [r.rank_score for r in rows] == [70.0, 65.0, 65.0, 100.0]


def test_same_stock_strategies_share_stock_rank():
    """奥比中光 4 条策略信号：按股票最高 composite 排，不再被策略权重拆开。"""
    a1 = _row(162.8, symbol="688322")
    a2 = _row(140.7, symbol="688322")
    b = _row(150.0, symbol="002249")
    c = _row(130.0, symbol="000858")
    _normalize_actionable_rank(rows=[a1, a2, b, c])
    assert a1.rank_score == a2.rank_score == 100.0
    assert b.rank_score == 87.5
    assert c.rank_score == 75.0


def test_unheld_cap_counts_stocks_not_rows():
    from src.modules.strategy import strategy_engine as se

    def sig(sym, rank):
        return SimpleNamespace(stock_symbol=sym, stock_market="CN", status="active", action="buy",
                               is_holding_snapshot=False, rank_score=rank, risk_level="medium",
                               strategy_code=f"s{rank}", confidence=None, reason="", payload={})

    rows = [sig("A", 100), sig("A", 99), sig("A", 98), sig("B", 90), sig("C", 80)]
    original = se.MAX_UNHELD_ACTIVE_BY_MARKET
    se.MAX_UNHELD_ACTIVE_BY_MARKET = {"CN": 2}
    original_share = se.MAX_SINGLE_STRATEGY_SHARE
    se.MAX_SINGLE_STRATEGY_SHARE = 1.0
    try:
        se._apply_portfolio_constraints(rows=rows)
    finally:
        se.MAX_UNHELD_ACTIVE_BY_MARKET = original
        se.MAX_SINGLE_STRATEGY_SHARE = original_share
    assert [r.status for r in rows] == ["active", "active", "active", "active", "inactive"]


def _catalyst_for(*news):
    from src.modules.strategy.strategy_engine import _news_catalyst, _news_impact

    return _news_catalyst(sum(_news_impact(**item) for item in news))


def _fresh(bullish, relation_level=8, age_hours=0.0, name_only=False):
    return {"bullish": bullish, "relation_level": relation_level, "age_hours": age_hours, "name_only": name_only}


def test_news_catalyst_scales_with_bullish_strength():
    weak, medium, strong = _catalyst_for(_fresh(2)), _catalyst_for(_fresh(5)), _catalyst_for(_fresh(8))
    assert weak == pytest.approx(1.10, abs=0.05)
    assert medium == pytest.approx(2.50, abs=0.05)
    assert strong == pytest.approx(3.43, abs=0.05)
    assert weak < medium < strong


def test_news_catalyst_more_news_adds_with_diminishing_returns():
    one = _catalyst_for(_fresh(5))
    two = _catalyst_for(_fresh(5), _fresh(5))
    many = _catalyst_for(*[_fresh(8)] * 5)
    assert one < two < many <= 4.5
    assert two - one < one


def test_news_catalyst_stale_bullish_news_never_deducts():
    assert _catalyst_for(_fresh(6, age_hours=70.0)) > 0
    assert _catalyst_for(_fresh(6, age_hours=48.0)) == pytest.approx(
        _catalyst_for(_fresh(1.5)), abs=0.01
    )


def test_news_catalyst_bearish_weighs_more_than_bullish():
    bull = _catalyst_for(_fresh(5))
    bear = _catalyst_for(_fresh(-5))
    assert bear < 0
    assert abs(bear) > bull
    assert _catalyst_for(_fresh(5), _fresh(-5)) < 0


def test_news_catalyst_weaker_relevance_and_name_only_count_less():
    direct = _catalyst_for(_fresh(5, relation_level=8))
    mid = _catalyst_for(_fresh(5, relation_level=5))
    low = _catalyst_for(_fresh(5, relation_level=2))
    named = _catalyst_for(_fresh(5, relation_level=8, name_only=True))
    assert direct > mid > low > 0
    assert named < direct


def test_direct_news_constraint_demotes_zero_dnc():
    row = _row(120.0, symbol="002929")
    row.payload["news_metric"] = {"direct_news_count": 0, "news_count": 1, "event_bias": 0.3}
    stats = _apply_direct_news_constraint(rows=[row])
    assert stats["demoted"] == 1
    assert row.status == "inactive"
    assert row.action == "watch"


def test_direct_news_constraint_keeps_direct_hit():
    row = _row(120.0, symbol="301428")
    row.payload["news_metric"] = {"direct_news_count": 2, "news_count": 2, "event_bias": 0.3}
    stats = _apply_direct_news_constraint(rows=[row])
    assert stats["demoted"] == 0
    assert row.status == "active"
    assert row.action == "buy"


def test_direct_news_constraint_demotes_bearish():
    """大西洋/百洋：有直接新闻但整体偏空，信号层也要降为观望。"""
    row = _row(120.0, symbol="600558")
    row.payload["news_metric"] = {"direct_news_count": 2, "news_count": 2, "event_bias": -0.32}
    stats = _apply_direct_news_constraint(rows=[row])
    assert stats["demoted"] == 1
    assert stats["by_reason"].get("bearish_news") == 1
    assert row.status == "inactive"
    assert row.action == "watch"


def test_dedupe_signal_rows_keeps_best_and_sibling_codes():
    from src.modules.strategy.strategy_engine import _dedupe_signal_rows

    rows = [
        _row(100.0, symbol="600519"),
        _row(99.0, symbol="600519"),
        _row(90.0, symbol="601398"),
    ]
    rows[0].strategy_code = "trend_follow"
    rows[1].strategy_code = "macd_golden"
    rows[2].strategy_code = "momentum"
    kept, codes = _dedupe_signal_rows(rows)
    assert [r.stock_symbol for r in kept] == ["600519", "601398"]
    assert codes[("CN", "600519")] == ["trend_follow", "macd_golden"]


def test_markets_normalized_separately():
    cn = _row(120.0)
    hk = _row(90.0, market="HK")
    _normalize_actionable_rank(rows=[cn, hk])
    assert cn.rank_score == 100.0
    assert hk.rank_score == 100.0
