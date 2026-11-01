"""盘前轮次与行情新鲜度判定。

09:15 那轮拿不到当日盘口，"盘中确认"否决必须跳过，否则每次刷新都产出 0 条机会。
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import src.modules.strategy.entry_candidates as ec
from src.platform.scheduling.timezone import to_iso_with_tz, utc_now


def test_phase_has_intraday_data_only_after_open():
    assert not ec.phase_has_intraday_data("pre_market")
    assert not ec.phase_has_intraday_data("closed")
    assert not ec.phase_has_intraday_data("unknown")
    assert ec.phase_has_intraday_data("trading")
    assert ec.phase_has_intraday_data("break")
    assert ec.phase_has_intraday_data("after_hours")


def test_only_intraday_phases_freeze_point_in_time_state():
    assert ec.phase_allows_freeze("trading")
    assert ec.phase_allows_freeze("break")
    # 收盘后已含当日结果、盘前是昨天的数据，都不能作为评估基准
    assert not ec.phase_allows_freeze("after_hours")
    assert not ec.phase_allows_freeze("pre_market")


def test_market_phase_reads_calendar(monkeypatch):
    monkeypatch.setattr(ec.trading_calendar, "market_status", lambda m: "pre_market")
    assert ec.market_phase("CN") == "pre_market"

    def _boom(_m):
        raise RuntimeError("calendar down")

    monkeypatch.setattr(ec.trading_calendar, "market_status", _boom)
    assert ec.market_phase("CN") == "unknown"


def test_quote_is_stale_by_timestamp():
    now = utc_now()
    fresh = {"current_price": 10.0, "quote_at": to_iso_with_tz(now - timedelta(minutes=5))}
    old = {"current_price": 10.0, "quote_at": to_iso_with_tz(now - timedelta(hours=5))}
    assert not ec.quote_is_stale(fresh)
    assert ec.quote_is_stale(old)
    # 没有时间戳不做拦截，避免把历史数据一律判死
    assert not ec.quote_is_stale({"current_price": 10.0})
    # 只有快照日期（补位数据）也能判断
    assert ec.quote_is_stale({}, {"fallback_snapshot_date": "2020-01-02"})


def test_resolve_evaluation_state_prefers_frozen():
    row = SimpleNamespace(
        status="inactive",
        action="watch",
        action_label="观望",
        score=40.0,
        entry_low=None,
        entry_high=None,
        stop_loss=None,
        target_price=None,
        frozen={
            "status": "active",
            "action": "buy",
            "action_label": "建仓",
            "score": 88.0,
            "entry_low": 9.9,
            "entry_high": 10.1,
            "stop_loss": 9.4,
            "target_price": 10.8,
            "phase": "trading",
            "frozen_at": "2026-10-30T02:30:00+08:00",
        },
    )
    state = ec.resolve_evaluation_state(row)
    assert state is not None
    assert state["point_in_time"] is True
    assert state["entry_high"] == 10.1
    assert state["score"] == 88.0


def test_resolve_evaluation_state_skips_non_active_frozen():
    row = SimpleNamespace(
        status="active",
        action="buy",
        action_label="建仓",
        score=90.0,
        entry_low=1.0,
        entry_high=1.1,
        stop_loss=0.9,
        target_price=1.2,
        frozen={"status": "inactive", "phase": "trading"},
    )
    assert ec.resolve_evaluation_state(row) is None


def test_resolve_evaluation_state_falls_back_for_legacy_rows():
    row = SimpleNamespace(
        status="active",
        action="buy",
        action_label="建仓",
        score=80.0,
        entry_low=1.0,
        entry_high=1.1,
        stop_loss=0.9,
        target_price=1.2,
        frozen=None,
    )
    state = ec.resolve_evaluation_state(row)
    assert state is not None
    # 迁移前没有 frozen：仍可评估，但显式标记不是 point-in-time
    assert state["point_in_time"] is False
