"""入场计划的路径感知后验评估。

覆盖两类改造前会算错的场景：
1. 价格从未回到入场区间（跳空高开）——旧算法按区间中点当成本，假装成交；
2. 中途先跌破止损、之后又涨回目标价——旧算法只看末日收盘价，会记成命中目标。
"""

from __future__ import annotations

from datetime import date, timedelta

from src.modules.strategy.plan_outcome import evaluate_plan_path

BASE = date(2026, 10, 1)


def _bars(seq):
    return [
        {
            "date": BASE + timedelta(days=i),
            "open": o,
            "high": h,
            "low": low,
            "close": c,
        }
        for i, (o, h, low, c) in enumerate(seq)
    ]


def test_gap_up_never_fills_is_not_counted():
    result = evaluate_plan_path(
        _bars([(10.5, 10.6, 10.3, 10.5), (10.7, 10.8, 10.4, 10.6)]),
        entry_low=9.9,
        entry_high=10.1,
        stop_loss=9.7,
        target_price=10.6,
    )
    assert result["filled"] is False
    assert result["status"] == "not_filled"
    assert result["return_pct"] is None


def test_stop_hit_before_target_is_loss_even_if_price_recovers():
    result = evaluate_plan_path(
        _bars([(10.0, 10.1, 9.6, 9.7), (10.0, 10.9, 9.9, 10.8)]),
        entry_low=9.9,
        entry_high=10.1,
        stop_loss=9.7,
        target_price=10.6,
    )
    assert result["filled"] is True
    assert result["status"] == "hit_stop"
    assert result["first_hit"] == "stop"
    assert result["return_pct"] < 0


def test_target_hit_after_fill():
    result = evaluate_plan_path(
        _bars([(10.0, 10.2, 9.95, 10.1), (10.3, 10.7, 10.2, 10.6)]),
        entry_low=9.9,
        entry_high=10.1,
        stop_loss=9.7,
        target_price=10.6,
    )
    assert result["status"] == "hit_target"
    assert result["first_hit"] == "target"
    assert result["return_pct"] > 0


def test_gap_up_then_pullback_fills_at_band_edge():
    result = evaluate_plan_path(
        _bars([(10.3, 10.35, 9.95, 10.0)]),
        entry_low=9.9,
        entry_high=10.1,
        stop_loss=9.7,
        target_price=10.6,
    )
    assert result["filled"] is True
    assert result["fill_price"] == 10.1


def test_low_open_rebound_fills_at_lower_edge():
    result = evaluate_plan_path(
        _bars([(9.7, 9.95, 9.65, 9.8)]),
        entry_low=9.9,
        entry_high=10.1,
        stop_loss=9.5,
        target_price=10.6,
    )
    assert result["filled"] is True
    assert result["fill_price"] == 9.9


def test_same_bar_both_sides_hit_prefers_stop():
    """同一根 bar 同时跌破止损又摸到目标：无法判断顺序时按先止损保守处理。"""
    result = evaluate_plan_path(
        _bars([(10.0, 10.9, 9.6, 10.5)]),
        entry_low=9.9,
        entry_high=10.1,
        stop_loss=9.7,
        target_price=10.6,
    )
    assert result["status"] == "hit_stop"


def test_empty_bars_returns_not_due():
    result = evaluate_plan_path(
        [],
        entry_low=9.9,
        entry_high=10.1,
        stop_loss=9.7,
        target_price=10.6,
    )
    assert result["status"] == "not_due"


def test_high_open_pullback_ignores_pre_fill_high():
    """高开回落到区间上沿成交：当日最高价发生在成交之前，不能算命中目标。

    10.5 开盘 → 冲 10.8（此时还没成交）→ 回落到 10.1 成交 → 收 9.9。
    改造前这一根会被记成 hit_target +4.95%；真实结果是持有到收盘 -1.98%。
    """
    result = evaluate_plan_path(
        _bars([(10.5, 10.8, 9.8, 9.9)]),
        entry_low=9.9,
        entry_high=10.1,
        stop_loss=9.7,
        target_price=10.6,
    )
    assert result["fill_mode"] == "fall_back"
    assert result["status"] == "evaluated"
    assert result["first_hit"] is None
    assert result["return_pct"] < 0


def test_low_open_rebound_ignores_pre_fill_low():
    """低开反弹到区间下沿成交：当日最低价发生在成交之前，不能算打止损。

    9.5 低开（此时还没成交）→ 反弹到 9.9 成交 → 收 10.2。
    改造前这一根会被记成 hit_stop −2.02%；真实结果是 +3.03%。
    """
    result = evaluate_plan_path(
        _bars([(9.5, 10.3, 9.5, 10.2)]),
        entry_low=9.9,
        entry_high=10.1,
        stop_loss=9.7,
        target_price=10.6,
    )
    assert result["fill_mode"] == "bounce_up"
    assert result["status"] == "evaluated"
    assert result["first_hit"] is None
    assert result["return_pct"] > 0


def test_stop_after_fill_day_still_counted():
    """成交次日恢复双侧判定：高开回落成交后第二天真破止损要记 hit_stop。"""
    result = evaluate_plan_path(
        _bars([(10.5, 10.8, 9.8, 9.9), (9.9, 10.0, 9.5, 9.6)]),
        entry_low=9.9,
        entry_high=10.1,
        stop_loss=9.7,
        target_price=10.6,
    )
    assert result["status"] == "hit_stop"
    assert result["holding_days"] == 2


def test_not_filled_has_no_fill_day():
    result = evaluate_plan_path(
        _bars([(10.5, 10.6, 10.3, 10.5), (10.7, 10.8, 10.4, 10.6)]),
        entry_low=9.9,
        entry_high=10.1,
        stop_loss=9.7,
        target_price=10.6,
    )
    assert result["status"] == "not_filled"
    assert result["fill_day"] is None
    assert result["holding_days"] == 0
