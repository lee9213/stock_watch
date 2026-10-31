"""模拟盘入场门禁单元测试。"""

from __future__ import annotations

from types import SimpleNamespace

import src.modules.paper_trading.paper_trading_engine as engine


def _sig(**kwargs):
    base = {
        "status": "active",
        "action": "buy",
        "entry_low": 10.0,
        "entry_high": 10.5,
        "rank_score": 80.0,
        "stock_market": "CN",
        "snapshot_date": "2099-01-02",  # 测试里用 monkeypatch 覆盖时效
        "payload": {
            "news_metric": {
                "news_count": 2,
                "direct_news_count": 1,
                "event_score": 5.0,
                "event_tier": "medium",
            },
            "score_breakdown": {"catalyst_score": 5.0},
        },
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def _patch_fresh(monkeypatch, ok: bool = True, reason: str = "ok"):
    monkeypatch.setattr(
        engine,
        "signal_snapshot_is_fresh",
        lambda *args, **kwargs: (ok, reason),
    )


def test_entry_gate_pass_with_news(monkeypatch):
    monkeypatch.setattr(engine, "MIN_RANK_SCORE_FOR_ENTRY", 75.0)
    monkeypatch.setattr(engine, "REQUIRE_NEWS_CATALYST", True)
    _patch_fresh(monkeypatch, True)
    ok, reason = engine.signal_passes_entry_gate(_sig())
    assert ok and reason == "ok"


def test_entry_gate_reject_low_rank(monkeypatch):
    monkeypatch.setattr(engine, "MIN_RANK_SCORE_FOR_ENTRY", 75.0)
    monkeypatch.setattr(engine, "REQUIRE_NEWS_CATALYST", True)
    _patch_fresh(monkeypatch, True)
    ok, reason = engine.signal_passes_entry_gate(_sig(rank_score=70.0))
    assert not ok and reason == "rank_below_threshold"


def test_entry_gate_reject_missing_plan(monkeypatch):
    monkeypatch.setattr(engine, "MIN_RANK_SCORE_FOR_ENTRY", 75.0)
    monkeypatch.setattr(engine, "REQUIRE_NEWS_CATALYST", False)
    _patch_fresh(monkeypatch, True)
    ok, reason = engine.signal_passes_entry_gate(_sig(entry_low=None, entry_high=None))
    assert not ok and reason == "missing_entry_plan"


def test_entry_gate_reject_missing_news(monkeypatch):
    monkeypatch.setattr(engine, "MIN_RANK_SCORE_FOR_ENTRY", 75.0)
    monkeypatch.setattr(engine, "REQUIRE_NEWS_CATALYST", True)
    _patch_fresh(monkeypatch, True)
    ok, reason = engine.signal_passes_entry_gate(
        _sig(
            payload={
                "news_metric": {"news_count": 0, "event_score": 0.0, "event_tier": "low"},
                "score_breakdown": {"catalyst_score": 1.0},
            }
        )
    )
    assert not ok and reason == "missing_news_catalyst"


def test_entry_gate_reject_tech_catalyst_without_news(monkeypatch):
    """技术面 catalyst_score 不能冒充新闻催化。"""
    monkeypatch.setattr(engine, "MIN_RANK_SCORE_FOR_ENTRY", 75.0)
    monkeypatch.setattr(engine, "REQUIRE_NEWS_CATALYST", True)
    _patch_fresh(monkeypatch, True)
    ok, reason = engine.signal_passes_entry_gate(
        _sig(
            payload={
                "news_metric": {"news_count": 0, "event_score": 0.0, "event_tier": "low"},
                "score_breakdown": {"catalyst_score": 9.0},
            }
        )
    )
    assert not ok and reason == "missing_news_catalyst"


def test_entry_gate_reject_only_generic_market_news(monkeypatch):
    """东方财富案例：关联新闻多是大盘综述（关联度低），不算个股催化。"""
    monkeypatch.setattr(engine, "MIN_RANK_SCORE_FOR_ENTRY", 75.0)
    monkeypatch.setattr(engine, "REQUIRE_NEWS_CATALYST", True)
    _patch_fresh(monkeypatch, True)
    ok, reason = engine.signal_passes_entry_gate(
        _sig(payload={"news_metric": {"news_count": 4, "direct_news_count": 0,
                                      "high_importance_count": 2, "event_tier": "high", "event_bias": 0.4}})
    )
    assert not ok and reason == "missing_news_catalyst"


def test_entry_gate_old_payload_without_direct_news_rejects(monkeypatch):
    monkeypatch.setattr(engine, "MIN_RANK_SCORE_FOR_ENTRY", 75.0)
    monkeypatch.setattr(engine, "REQUIRE_NEWS_CATALYST", True)
    _patch_fresh(monkeypatch, True)
    ok, reason = engine.signal_passes_entry_gate(
        _sig(payload={"news_metric": {"news_count": 1, "high_importance_count": 1, "event_bias": 0.2}})
    )
    assert not ok and reason == "missing_news_catalyst"


def test_entry_gate_reject_bearish_news(monkeypatch):
    """五粮液案例：4 条高重要性新闻但整体偏空，不算催化。"""
    monkeypatch.setattr(engine, "MIN_RANK_SCORE_FOR_ENTRY", 75.0)
    monkeypatch.setattr(engine, "REQUIRE_NEWS_CATALYST", True)
    monkeypatch.setattr(engine, "MIN_EVENT_BIAS_FOR_ENTRY", 0.0)
    _patch_fresh(monkeypatch, True)
    ok, reason = engine.signal_passes_entry_gate(
        _sig(payload={"news_metric": {"news_count": 4, "direct_news_count": 4, "event_score": 12.0,
                                      "event_tier": "high", "event_bias": -0.3713}})
    )
    assert not ok and reason == "bearish_news"


def test_entry_gate_news_optional_when_disabled(monkeypatch):
    monkeypatch.setattr(engine, "MIN_RANK_SCORE_FOR_ENTRY", 75.0)
    monkeypatch.setattr(engine, "REQUIRE_NEWS_CATALYST", False)
    _patch_fresh(monkeypatch, True)
    ok, reason = engine.signal_passes_entry_gate(
        _sig(payload={"news_metric": {}, "score_breakdown": {}})
    )
    assert ok and reason == "ok"


def test_entry_gate_reject_stale_snapshot(monkeypatch):
    monkeypatch.setattr(engine, "MIN_RANK_SCORE_FOR_ENTRY", 75.0)
    monkeypatch.setattr(engine, "REQUIRE_NEWS_CATALYST", False)
    _patch_fresh(monkeypatch, False, "signal_stale")
    ok, reason = engine.signal_passes_entry_gate(_sig(snapshot_date="2026-10-26"))
    assert not ok and reason == "signal_stale"


def test_allowed_snapshot_dates_includes_prior_session(monkeypatch):
    from datetime import date

    monkeypatch.setattr(
        "src.platform.scheduling.trading_calendar.is_trading_day",
        lambda market, d: d.weekday() < 5,
    )
    # 周三 → 应含周三+周二
    allowed = engine.allowed_signal_snapshot_dates(
        "CN", max_age_sessions=1, as_of=date(2026, 10, 28)
    )
    assert "2026-10-28" in allowed
    assert "2026-10-27" in allowed
    assert "2026-10-26" not in allowed


def test_price_in_entry_band_pass(monkeypatch):
    monkeypatch.setattr(engine, "ENTRY_BAND_TOLERANCE", 0.01)
    monkeypatch.setattr(engine, "MAX_ENTRY_PLAN_PRICE_RATIO", 3.0)
    ok, reason = engine.price_passes_entry_band(5.31, 5.25, 5.40)
    assert ok and reason == "ok"


def test_price_outside_entry_band_rejects_atlantic_case(monkeypatch):
    """大西洋案例：现价 5.31，计划区间 ~38，应拒绝。"""
    monkeypatch.setattr(engine, "ENTRY_BAND_TOLERANCE", 0.01)
    monkeypatch.setattr(engine, "MAX_ENTRY_PLAN_PRICE_RATIO", 3.0)
    ok, reason = engine.price_passes_entry_band(5.31, 38.0457, 38.8143)
    assert not ok
    assert reason in {"price_outside_entry_band", "entry_plan_price_mismatch"}


def test_clamp_exit_levels_caps_far_stop():
    """大业股份案例：止损 -48% 收到 -10%，止盈按 1.5 倍提到 +15%。"""
    stop, target = engine.clamp_exit_levels(17.61, 9.1354, 18.6539)
    assert stop == round(17.61 * 0.90, 4)
    assert target == round(17.61 * 1.15, 4)


def test_clamp_exit_levels_raises_tiny_target():
    """大西洋案例：止盈仅 +0.9%，按 1.5 倍止损抬到 +15%。"""
    stop, target = engine.clamp_exit_levels(5.31, 3.2909, 5.3581)
    assert stop == round(5.31 * 0.90, 4)
    assert target == round(5.31 * 1.15, 4)


def test_clamp_exit_levels_widens_tight_stop_and_defaults_invalid():
    stop, _ = engine.clamp_exit_levels(10.0, 9.95, 11.0)
    assert stop == 9.7
    stop, target = engine.clamp_exit_levels(10.0, None, 9.0)
    assert stop == 9.2
    assert target == 11.5


def test_lot_size_star_board_is_200():
    assert engine.lot_size("688256", "CN") == 200
    assert engine.lot_size("300308", "CN") == 100
    assert engine.lot_size("600519", "CN") == 100


def test_industry_bucket_groups_liquor_and_boards():
    assert engine.industry_bucket("000858", "五 粮 液") == "白酒"
    assert engine.industry_bucket("600519", "贵州茅台") == "白酒"
    assert engine.industry_bucket("601398", "工商银行") == "银行"
    assert engine.industry_bucket("688648", "中邮科技") == "科创板"


def test_tape_gate_rejects_near_limit_up():
    ok, reason = engine.quote_passes_tape_gate(
        {"change_pct": 9.6, "turnover": 5e8}, "600519", "CN"
    )
    assert not ok and reason == "near_limit_up"


def test_tape_gate_rejects_low_turnover():
    ok, reason = engine.quote_passes_tape_gate(
        {"change_pct": 2.1, "turnover": 12_000_000}, "300006", "CN"
    )
    assert not ok and reason == "low_liquidity"


def test_tape_gate_rejects_flat_or_down_day():
    ok, reason = engine.quote_passes_tape_gate(
        {"change_pct": -0.4, "turnover": 4e8}, "002354", "CN"
    )
    assert not ok and reason == "intraday_unconfirmed"


def test_tape_gate_passes_liquid_up_day():
    ok, reason = engine.quote_passes_tape_gate(
        {"change_pct": 3.2, "turnover": 3e8}, "000858", "CN"
    )
    assert ok and reason == "ok"


def test_tape_gate_20cm_board_not_blocked_at_10pct():
    ok, reason = engine.quote_passes_tape_gate(
        {"change_pct": 10.2, "turnover": 4e8}, "300308", "CN"
    )
    assert ok and reason == "ok"


def test_price_band_allows_small_tolerance(monkeypatch):
    monkeypatch.setattr(engine, "ENTRY_BAND_TOLERANCE", 0.01)
    monkeypatch.setattr(engine, "MAX_ENTRY_PLAN_PRICE_RATIO", 3.0)
    ok, reason = engine.price_passes_entry_band(10.09, 10.0, 10.0)
    assert ok and reason == "ok"
