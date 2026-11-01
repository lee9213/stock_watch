"""同期基准收益单测。

这一层的意义：``outcome_return_pct`` 单独看无法回答"选股准不准"。
大盘涨 3% 时 +2% 的信号是跑输的。基准口径必须固定且可复算：
T+1 开盘买入 → 第 horizon 根日线收盘卖出。
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from src.modules.strategy import benchmark
from src.platform.scheduling import trading_calendar
from src.platform.scheduling.timezone import app_now


def _trading_days(count: int, *, back_from: int = 40) -> list:
    """取距今 back_from 天之前的 count 个连续交易日（升序）。"""
    end = app_now().date() - timedelta(days=back_from)
    days: list = []
    cursor = end
    while len(days) < count:
        if trading_calendar.is_trading_day("CN", cursor):
            days.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(days))


def _bars(days: list, *, closes: list | None = None) -> list:
    """构造 KlineData 形状的假日线。open 故意与 close 不同以验证用的是开盘价。"""
    out = []
    for idx, day in enumerate(days):
        close = closes[idx] if closes else 10.0 + idx * 0.5
        out.append(
            SimpleNamespace(
                date=day.isoformat(),
                open=close - 0.2,  # 开盘刻意低于收盘：用错价会立刻算错
                high=close + 0.1,
                low=close - 0.3,
                close=close,
                volume=1000.0,
            )
        )
    return out


def test_benchmark_uses_t1_open_and_horizon_close():
    """收益 = (第 horizon 根收盘 / 第 1 根开盘 - 1)，不是首根收盘。"""
    days = _trading_days(8)
    bars = _bars(days, closes=[10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0])

    result = benchmark.compute_benchmark_return(bars, days[0], 1, "CN")
    # horizon=1：起点是 days[1] 的 open（11.0-0.2），终点是 days[1] 的 close（11.0）
    assert result["base_price"] == pytest.approx(10.8)
    assert result["end_price"] == pytest.approx(11.0)
    assert result["return_pct"] == pytest.approx(round((11.0 / 10.8 - 1) * 100, 4))
    assert result["target_date"] == days[1].isoformat()
    assert result["bars"] == 1

    longer = benchmark.compute_benchmark_return(bars, days[0], 3, "CN")
    assert longer["base_price"] == pytest.approx(10.8)
    assert longer["end_price"] == pytest.approx(13.0)  # days[3] 收盘
    assert longer["target_date"] == days[3].isoformat()


def test_benchmark_returns_none_when_not_due():
    """bar 不够 horizon 根 → None，调用方据此不写库（绝不能记 0）。"""
    days = _trading_days(3)
    bars = _bars(days)
    assert benchmark.compute_benchmark_return(bars, days[0], 5, "CN") is None
    assert benchmark.compute_benchmark_return(bars, days[0], 1, "CN") is not None


def test_benchmark_returns_none_without_forward_bars():
    """snapshot 日就是最后一根 bar 时没有前向数据。"""
    days = _trading_days(3)
    bars = _bars(days)
    assert benchmark.compute_benchmark_return(bars, days[-1], 1, "CN") is None


def test_excess_is_stock_minus_benchmark():
    assert benchmark.compute_excess(3.0, 1.0) == pytest.approx(2.0)
    assert benchmark.compute_excess(-1.0, 2.0) == pytest.approx(-3.0)


def test_excess_is_none_when_either_side_missing():
    """缺基准时返回 None，不拿 0 兜底——0 兜底会让超额等于原始收益，凭空造 alpha。"""
    assert benchmark.compute_excess(3.0, None) is None
    assert benchmark.compute_excess(None, 1.0) is None


def test_benchmark_symbol_defaults_to_510300():
    assert benchmark.benchmark_symbol("CN") == "510300"
    # 未配置的市场回落到默认值，不抛异常
    assert benchmark.benchmark_symbol("XX") == "510300"
