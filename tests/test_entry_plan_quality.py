"""入场计划质量与过热否决单测。"""

from __future__ import annotations

import src.modules.strategy.entry_candidates as ec


def test_plan_quality_full_valid_plan():
    plan = {
        "entry_low": 9.9,
        "entry_high": 10.1,
        "stop_loss": 9.4,
        "target_price": 10.8,
        "invalidation": "若跌破 9.40 则失效",
    }
    assert ec._plan_quality(plan, quote={"current_price": 10.0}) == 100


def test_plan_quality_caps_when_stop_above_entry():
    plan = {
        "entry_low": 9.9,
        "entry_high": 10.1,
        "stop_loss": 10.5,
        "target_price": 10.8,
        "invalidation": "x",
    }
    assert ec._plan_quality(plan, quote={"current_price": 10.0}) <= 89


def test_plan_quality_caps_when_mid_far_from_quote():
    plan = {
        "entry_low": 38.0,
        "entry_high": 39.0,
        "stop_loss": 30.0,
        "target_price": 42.0,
        "invalidation": "x",
    }
    assert ec._plan_quality(plan, quote={"current_price": 5.3}) <= 89


def test_derive_overheat_forces_watch():
    decision = ec._derive_market_scan_decision(
        quote={"current_price": 10.0, "change_pct": 10.2},
        kline={"trend": "多头排列", "macd_cross": "金叉", "volume_ratio": 2.0},
    )
    assert decision["action"] == "watch"
    assert "过热" in (decision["action_label"] or "") or any(
        "过热" in r for r in (decision.get("reason") or "").split("，")
    )


def test_derive_crash_forces_watch():
    """林州重机案例：昨日多头排列 + MACD 金叉，今天放量跌 8.57%。"""
    decision = ec._derive_market_scan_decision(
        quote={"current_price": 2.5, "change_pct": -8.57},
        kline={"trend": "多头排列", "macd_cross": "金叉", "volume_ratio": 2.2},
    )
    assert decision["action"] == "watch"
    assert decision["action_label"] == "大跌观望"
    assert "volume_breakout" not in decision["strategy_tags"]


def test_volume_breakout_only_on_up_day():
    up = ec._derive_market_scan_decision(
        quote={"current_price": 10.0, "change_pct": 2.0},
        kline={"volume_ratio": 2.0},
    )
    down = ec._derive_market_scan_decision(
        quote={"current_price": 10.0, "change_pct": -2.0},
        kline={"volume_ratio": 2.0},
    )
    assert "volume_breakout" in up["strategy_tags"]
    assert "volume_breakout" not in down["strategy_tags"]


def test_cn_list_symbol_rejects_junk_hotlist_codes():
    assert ec.is_cn_list_symbol("600519")
    assert ec.is_cn_list_symbol("300308")
    assert ec.is_cn_list_symbol("688256")
    assert not ec.is_cn_list_symbol("0.920306")
    assert not ec.is_cn_list_symbol("123456")
    assert not ec.is_cn_list_symbol("123")
    assert not ec.is_cn_list_symbol("")


def test_bearish_daily_structure_is_watch_not_avoid():
    decision = ec._derive_market_scan_decision(
        quote={"current_price": 10.0, "change_pct": -1.2},
        kline={"trend": "空头排列", "macd_cross": "死叉"},
        symbol="300750",
    )
    assert decision["action"] == "watch"
    assert decision["action_label"] == "形态偏弱"


def test_missing_kline_never_avoids():
    decision = ec._derive_market_scan_decision(
        quote={"current_price": 10.0, "change_pct": -2.0},
        kline={"error": "无K线数据"},
        symbol="600030",
    )
    assert decision["action"] != "avoid"
    assert decision["action_label"] in {"观望", "K线不足"}


def test_crash_change_dropped_from_hotlist():
    assert ec._is_crash_change(-7.0)
    assert ec._is_crash_change(-8.57)
    assert not ec._is_crash_change(-6.9)
    assert not ec._is_crash_change(2.1)
    assert not ec._is_crash_change(None)


def test_is_st_name():
    assert ec._is_st_name("*ST海越")
    assert ec._is_st_name("ST华通")
    assert ec._is_st_name("S*ST前锋")
    assert not ec._is_st_name("世纪恒通")
    assert not ec._is_st_name(None)


def test_normalize_stock_name_strips_spaces():
    assert ec.normalize_stock_name("五 粮 液") == "五粮液"
    assert ec.normalize_stock_name("粤\u3000传\u3000媒") == "粤传媒"
    assert ec.normalize_stock_name(None) == ""


def test_overheat_threshold_uses_20cm_board():
    assert ec.overheat_change_pct("600519") == 9.5
    assert ec.overheat_change_pct("300308") == 19.5
    assert ec.overheat_change_pct("688256") == 19.5


def test_derive_20cm_board_not_overheat_at_10pct():
    decision = ec._derive_market_scan_decision(
        quote={"current_price": 10.0, "change_pct": 10.2},
        kline={"trend": "多头排列", "macd_cross": "金叉", "volume_ratio": 2.0},
        symbol="300308",
    )
    assert decision["action"] != "watch" or "过热" not in (decision["action_label"] or "")


def test_derive_daily_setup_without_intraday_is_watch():
    decision = ec._derive_market_scan_decision(
        quote={"current_price": 10.0, "change_pct": 0.3},
        kline={"trend": "多头排列", "macd_cross": "金叉", "volume_ratio": 1.0},
        symbol="600519",
    )
    assert decision["action"] == "watch"
    assert decision["action_label"] == "盘中未确认"


def test_has_intraday_confirm_momentum_or_volume():
    assert ec.has_intraday_confirm(["momentum"], {"change_pct": 2.0}, {})
    assert not ec.has_intraday_confirm(
        ["trend_follow", "macd_golden"],
        {"change_pct": 0.4},
        {"volume_ratio": 1.0},
    )
    assert ec.has_intraday_confirm(
        ["trend_follow", "macd_golden"],
        {"change_pct": 2.0},
        {"volume_ratio": 1.4},
    )


def test_derive_thin_tape_forces_watch():
    decision = ec._derive_market_scan_decision(
        quote={"current_price": 10.0, "change_pct": 3.0, "turnover": 12_000_000},
        kline={"trend": "多头排列", "macd_cross": "金叉", "volume_ratio": 2.0},
        symbol="600519",
    )
    assert decision["action"] == "watch"
    assert decision["action_label"] == "流动性观望"


def test_derive_normal_momentum_can_buy():
    decision = ec._derive_market_scan_decision(
        quote={"current_price": 10.0, "change_pct": 3.0},
        kline={
            "trend": "多头排列",
            "macd_cross": "金叉",
            "volume_ratio": 2.0,
            "support": 9.5,
        },
    )
    assert decision["action"] in {"buy", "add", "watch"}
