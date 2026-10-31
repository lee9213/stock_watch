"""模拟盘建仓约束：尾盘禁买、平仓冷却、持仓只数与总仓位上限。"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.modules.paper_trading import paper_trading_engine as module
from src.platform.persistence.database import Base
from src.platform.persistence.models import (
    PaperTradingAccount, PaperTradingPosition, PaperTradingTrade, StrategySignalRun,
)
from src.platform.scheduling import trading_calendar as calendar

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 9, 10, tzinfo=SHANGHAI)  # 周五
NEWS = {"news_metric": {"news_count": 2, "direct_news_count": 1, "event_tier": "medium", "event_bias": 0.3}}


def freeze(monkeypatch, now=NOW):
    monkeypatch.setattr(module, "_utc_now", lambda: now.astimezone(timezone.utc))
    monkeypatch.setattr(calendar, "_now_in_market_tz", lambda code: now.astimezone(calendar._market_tz(code)))


def naive_utc(dt):
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def storage(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(module, "SessionLocal", factory)
    with factory() as db:
        db.add(PaperTradingAccount(initial_capital=1_000_000, current_capital=1_000_000, enabled=True,
                                   market_allocations={"CN": 1.0, "HK": 0.0, "US": 0.0}))
        db.commit()
    yield factory
    engine.dispose()


def add_signals(storage, symbols, *, snapshot="2026-10-09"):
    with storage() as db:
        for i, sym in enumerate(symbols):
            db.add(StrategySignalRun(stock_symbol=sym, stock_market="CN", stock_name=sym,
                                     strategy_code="trend_follow", snapshot_date=snapshot, status="active",
                                     action="buy", entry_low=9.9, entry_high=10.1, stop_loss=9.4,
                                     target_price=11.0, rank_score=100 - i, payload=NEWS))
        db.commit()


def scan(monkeypatch, symbols):
    engine = module.PaperTradingEngine()
    monkeypatch.setattr(engine, "_fetch_quotes_map",
                        lambda pairs: {("CN", s): {"current_price": 10.0} for s in symbols})
    return engine._scan_sync()


@pytest.mark.parametrize("hhmm,expected", [((9, 40), False), ((10, 0), True), ((14, 40), True), ((14, 45), False), ((14, 59), False)])
def test_entry_window_closes_15_minutes_before_close(monkeypatch, hhmm, expected):
    freeze(monkeypatch, NOW.replace(hour=hhmm[0], minute=hhmm[1]))
    assert module.entry_window_open("CN") is expected


def test_no_entry_after_cutoff(monkeypatch, storage):
    freeze(monkeypatch, NOW.replace(hour=14, minute=50))
    add_signals(storage, ["600001"])
    assert scan(monkeypatch, ["600001"])["opened"] == 0


@pytest.mark.parametrize("closed_day,expected_opened", [(8, 0), (7, 1)])
def test_reentry_cooldown_two_sessions(monkeypatch, storage, closed_day, expected_opened):
    """周四平仓→周五只过 1 个交易日，冷却中；周三平仓→已过 2 个交易日，可再买。"""
    freeze(monkeypatch)
    with storage() as db:
        closed = datetime(2026, 10, closed_day, 14, tzinfo=SHANGHAI)
        db.add(PaperTradingTrade(stock_symbol="600001", stock_market="CN", quantity=100, entry_price=10,
                                 exit_price=9.4, exit_reason="stop_loss", opened_at=naive_utc(closed),
                                 closed_at=naive_utc(closed)))
        db.commit()
    add_signals(storage, ["600001"])
    assert scan(monkeypatch, ["600001"])["opened"] == expected_opened


def test_max_open_positions(monkeypatch, storage):
    freeze(monkeypatch)
    monkeypatch.setattr(module, "MAX_OPEN_POSITIONS", 3)
    monkeypatch.setattr(module, "MAX_ENTRIES_PER_SCAN", 5)
    monkeypatch.setattr(module, "MAX_POSITIONS_PER_SECTOR", 10)
    symbols = [f"60000{i}" for i in range(5)]
    add_signals(storage, symbols)
    assert scan(monkeypatch, symbols)["opened"] == 3
    with storage() as db:
        assert db.query(PaperTradingPosition).filter_by(status="open").count() == 3


def test_total_exposure_cap(monkeypatch, storage):
    """每笔 18% → 50% 上限下第 3 笔吃剩余额度，第 4 笔买不了。"""
    freeze(monkeypatch)
    monkeypatch.setattr(module, "MAX_OPEN_POSITIONS", 10)
    monkeypatch.setattr(module, "MAX_ENTRIES_PER_SCAN", 10)
    monkeypatch.setattr(module, "MAX_TOTAL_EXPOSURE", 0.50)
    monkeypatch.setattr(module, "MAX_POSITIONS_PER_SECTOR", 10)
    symbols = [f"60000{i}" for i in range(6)]
    add_signals(storage, symbols)
    scan(monkeypatch, symbols)
    with storage() as db:
        positions = db.query(PaperTradingPosition).filter_by(status="open").all()
        invested = sum(module.position_buy_cost(p) for p in positions)
    assert len(positions) == 3
    assert invested <= 500_000


def test_max_two_per_sector(monkeypatch, storage):
    freeze(monkeypatch)
    monkeypatch.setattr(module, "MAX_OPEN_POSITIONS", 6)
    monkeypatch.setattr(module, "MAX_ENTRIES_PER_SCAN", 6)
    monkeypatch.setattr(module, "MAX_POSITIONS_PER_SECTOR", 2)
    names = {
        "000858": "五粮液",
        "600519": "贵州茅台",
        "000568": "泸州老窖",
    }
    with storage() as db:
        for i, (sym, name) in enumerate(names.items()):
            db.add(StrategySignalRun(
                stock_symbol=sym, stock_market="CN", stock_name=name,
                strategy_code="trend_follow", snapshot_date="2026-10-09", status="active",
                action="buy", entry_low=9.9, entry_high=10.1, stop_loss=9.4,
                target_price=11.5, rank_score=100 - i, payload=NEWS,
            ))
        db.commit()
    assert scan(monkeypatch, list(names))["opened"] == 2
