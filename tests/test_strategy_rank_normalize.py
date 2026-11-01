"""可执行信号排序分百分位归一化单测。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.modules.strategy.strategy_engine import (
    _apply_direct_news_constraint,
    _normalize_actionable_rank,
)

_seq = iter(range(100000))


def _row(composite, *, rank=100.0, status="active", action="buy", market="CN", entry=10.0, symbol=None,
         plan_quality=100, gateway=None):
    return SimpleNamespace(
        stock_symbol=symbol or f"S{next(_seq)}",
        status=status,
        action=action,
        stock_market=market,
        entry_low=entry,
        entry_high=entry,
        plan_quality=plan_quality,
        rank_score=rank,
        is_holding_snapshot=False,
        reason="",
        confidence=0.9,
        payload={
            "score_breakdown": {"composite_score": composite, "weighted_score": rank},
            **({"news_gate": gateway} if gateway else {}),
        },
    )


def test_saturated_scores_spread_inside_75_100():
    """叠加分数集体封顶 100 后仍可排序：按中点百分位铺在 (75, 100) 开区间内。

    中点口径 (idx+0.5)/n 让上下限都取不到，n=3 时落在 79.17 / 87.5 / 95.83。
    """
    rows = [_row(120.0), _row(130.0), _row(140.0)]
    changed = _normalize_actionable_rank(rows=rows)
    assert changed == 3
    assert [r.rank_score for r in rows] == [pytest.approx(79.1667), 87.5, pytest.approx(95.8333)]
    assert rows[2].payload["score_breakdown"]["weighted_score"] == pytest.approx(95.8333)
    assert all(75.0 < r.rank_score < 100.0 for r in rows)


def test_ties_share_same_rank():
    rows = [_row(130.0), _row(130.0), _row(110.0)]
    _normalize_actionable_rank(rows=rows)
    assert rows[0].rank_score == rows[1].rank_score == 93.75
    assert rows[2].rank_score == 81.25


def test_single_actionable_stock_is_not_topped_out():
    """当日只有 1 只可执行票时旧口径除零 ⇒ 恒等 100，"最高分"只是没有对照。

    实测：2026-11-01 可执行只有 600519（4 条策略行 → 1 只票），4 行全读 100。
    中点口径下退回中位 87.5；absolute_score 是绝对强度，可以超过 100，不做封顶。
    """
    rows = [_row(145.781, symbol="600519") for _ in range(4)]
    _normalize_actionable_rank(rows=rows)
    assert [r.rank_score for r in rows] == [87.5] * 4
    breakdown = rows[0].payload["score_breakdown"]
    assert breakdown["rank_pct"] == 50.0
    assert breakdown["absolute_score"] == 145.781  # 不封顶
    # payload 与 breakdown 必须给出同一个绝对分（旧版分别写 136.05 / 145.78）
    assert rows[0].payload["absolute_score"] == breakdown["absolute_score"]


def test_absolute_score_stays_per_row_while_rank_is_shared():
    """同一只票的多条策略：rank 共享（排序比的是股票），absolute 各报各的。

    旧版把股票级最高值写进每一行，600519 的四条策略全读 145.78，而实际合成分是
    145.78 / 139.44 / 125.98 / 136.05 —— 三条行报的是别人的分。
    """
    scores = [145.781, 139.4427, 125.9759, 136.054]
    rows = [_row(s, symbol="600519") for s in scores]
    _normalize_actionable_rank(rows=rows)
    assert {r.rank_score for r in rows} == {87.5}
    assert [r.payload["absolute_score"] for r in rows] == scores
    for r in rows:
        assert r.payload["absolute_score"] == r.payload["score_breakdown"]["absolute_score"]


def test_format_signal_keeps_unbounded_absolute_score():
    """absolute_score 可以 >100，读出口不许"顺手"夹到 100；只要顶层与 breakdown 一致。"""
    from src.modules.strategy.strategy_engine import _format_signal
    from src.platform.persistence.models import StrategySignalRun

    row = StrategySignalRun(
        snapshot_date="2026-11-01",
        stock_symbol="600519",
        stock_market="CN",
        strategy_code="trend_follow",
        strategy_name="趋势跟随",
        status="active",
        action="buy",
        rank_score=87.5,
        plan_quality=100,
        entry_low=1500.0,
        entry_high=1520.0,
    )
    # 旧快照：payload 写的是每行 composite，breakdown 写的是股票级最高值，两个数不一样
    row.payload = {
        "absolute_score": 136.054,
        "score_breakdown": {"absolute_score": 145.781, "composite_score": 145.781},
    }
    item = _format_signal(row)
    assert item["absolute_score"] == pytest.approx(145.781, abs=0.01)  # >100 照原样出，不夹
    assert item["score_breakdown"]["absolute_score"] == pytest.approx(145.781)  # 与顶层收敛
    assert item["score_breakdown"]["composite_score"] == 145.781


def test_non_actionable_rows_untouched():
    """不可执行 = 非 active / 非 buy-add / 无区间 / 计划质量不达标 / 缺直接新闻。

    改造前用 "rank_score >= 75" 兼作判据，现在这个职责交给 _is_actionable，
    rank_score 只是排序分——所以这里用"计划质量不达标"表达不可执行，而不是"分低"。
    """
    weak_plan = _row(80.0, rank=70.0, plan_quality=80)
    inactive = _row(150.0, status="inactive", rank=65.0)
    watch = _row(150.0, action="watch", rank=65.0)
    no_plan = _row(150.0, entry=None)
    no_news = _row(150.0, gateway="missing_direct_news")
    rows = [weak_plan, inactive, watch, no_plan, no_news]
    assert _normalize_actionable_rank(rows=rows) == 0
    assert [r.rank_score for r in rows] == [70.0, 65.0, 65.0, 100.0, 100.0]


def test_news_penalized_row_is_not_actionable():
    """缺直接新闻的行被扣分封顶后仍在 active，但不应再算进"可执行"。"""
    from src.modules.strategy.strategy_engine import _is_actionable

    assert _is_actionable(_row(120.0)) is True
    assert _is_actionable(_row(120.0, gateway="missing_direct_news")) is False
    assert _is_actionable(_row(120.0, plan_quality=89)) is False
    assert _is_actionable(_row(120.0, entry=None)) is False


def test_same_stock_strategies_share_stock_rank():
    """奥比中光 4 条策略信号：按股票最高 composite 排，不再被策略权重拆开。"""
    a1 = _row(162.8, symbol="688322")
    a2 = _row(140.7, symbol="688322")
    b = _row(150.0, symbol="002249")
    c = _row(130.0, symbol="000858")
    _normalize_actionable_rank(rows=[a1, a2, b, c])
    assert a1.rank_score == a2.rank_score == pytest.approx(95.8333)
    assert b.rank_score == 87.5
    assert c.rank_score == pytest.approx(79.1667)


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


def test_direct_news_constraint_penalizes_zero_dnc_instead_of_demoting():
    """没有个股直接新闻 = 新闻维度"未知"，改为降权+封顶，不再整片清空机会列表。"""
    row = _row(120.0, symbol="002929")
    row.payload["news_metric"] = {"direct_news_count": 0, "news_count": 1, "event_bias": 0.3}
    stats = _apply_direct_news_constraint(rows=[row])
    assert stats["by_reason"].get("missing_direct_news_penalized") == 1
    # 单条新闻、无同业对照时仍保持 active，但分数被压到可执行门槛（75）以下
    assert row.status == "active"
    assert row.action == "buy"
    assert row.rank_score <= 74.0
    assert row.payload["news_gate"] == "missing_direct_news"
    assert "缺少个股直接新闻" in row.reason


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
    # 每个市场各自只有 1 个样本 ⇒ 各自落在中位，而不是互相比较后都读 100
    assert cn.rank_score == hk.rank_score == 87.5


def test_news_data_missing_is_fail_open():
    """该股一条新闻都没有 = 数据覆盖缺失，不是"没有利好"，不该封顶。

    混为一谈的实测后果：新闻服务没采到的票一律被压到 74，全市场可执行数恒为 0。
    """
    row = SimpleNamespace(
        status="active",
        action="buy",
        rank_score=88.0,
        confidence=0.9,
        reason="",
        updated_at=None,
        payload={"news_metric": {"news_count": 0, "direct_news_count": 0}},
    )
    stats = _apply_direct_news_constraint(rows=[row])
    assert row.rank_score == 88.0
    assert row.payload["news_gate"] == "news_data_missing"
    assert stats["news_data_missing"] == 1
    assert stats["demoted"] == 0


def test_news_present_but_not_direct_is_still_capped():
    """有相关新闻但都不直接命中，才是消息面没支撑，仍按原逻辑封顶。"""
    row = SimpleNamespace(
        status="active",
        action="buy",
        rank_score=88.0,
        confidence=0.9,
        reason="",
        updated_at=None,
        payload={"news_metric": {"news_count": 5, "direct_news_count": 0}},
    )
    stats = _apply_direct_news_constraint(rows=[row])
    assert row.rank_score <= 74.0
    assert row.payload["news_gate"] == "missing_direct_news"
    assert stats["demoted"] == 1
