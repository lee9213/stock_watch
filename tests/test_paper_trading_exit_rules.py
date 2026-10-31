"""模拟盘出场规则单测：移动止损保本、交易日持有期、avoid 反转。"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.modules.paper_trading import paper_trading_engine as module
from src.platform.persistence.database import Base
from src.platform.persistence.models import PaperTradingAccount, PaperTradingPosition, StrategySignalRun
from src.platform.scheduling import trading_calendar as calendar

SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_trailing_line_never_below_entry():
    # 最高 +5%：回撤 4% 线在 10.08，保本后仍不低于买入价
    assert module.trailing_stop_line(10.0, 10.5) == pytest.approx(10.08)


def test_trailing_line_follows_high_after_bigger_gain():
    assert module.trailing_stop_line(10.0, 12.0) == pytest.approx(11.52)


def test_trailing_inactive_below_min_profit():
    assert module.trailing_stop_line(10.0, 10.2) is None
    assert module.trailing_stop_line(10.0, 10.4) == pytest.approx(10.0)


def test_trading_days_held_skips_weekend():
    thu = datetime(2026, 10, 8, 10, tzinfo=SHANGHAI)  # 周四
    sun = datetime(2026, 10, 11, 10, tzinfo=SHANGHAI)
    tue = datetime(2026, 10, 13, 10, tzinfo=SHANGHAI)
    assert module.trading_days_held("CN", thu, sun) == 1  # 只有周五
    assert module.trading_days_held("CN", thu, tue) == 3  # 周五、周一、周二


def test_trading_days_held_naive_utc_opened_at():
    opened = datetime(2026, 10, 9, 2, 0)  # 库里存 naive UTC = 周五 10:00 北京
    now = datetime(2026, 10, 12, 10, tzinfo=SHANGHAI)
    assert module.trading_days_held("CN", opened, now) == 1


@pytest.fixture
def storage(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(module, "SessionLocal", factory)
    with factory() as db:
        db.add(PaperTradingAccount(initial_capital=100000, current_capital=99000, enabled=True))
        db.commit()
    yield factory
    engine.dispose()


def test_inactive_avoid_signal_triggers_reversal(monkeypatch, storage):
    now = datetime(2026, 10, 13, 10, tzinfo=SHANGHAI)
    monkeypatch.setattr(module, "_utc_now", lambda: now.astimezone(timezone.utc))
    monkeypatch.setattr(calendar, "_now_in_market_tz", lambda code: now.astimezone(calendar._market_tz(code)))
    with storage() as db:
        buy = StrategySignalRun(stock_symbol="600519", stock_market="CN", strategy_code="trend_follow",
                                snapshot_date="2026-10-12", action="buy", status="active", holding_days=5)
        avoid = StrategySignalRun(stock_symbol="600519", stock_market="CN", strategy_code="market_scan",
                                  snapshot_date="2026-10-13", action="avoid", status="inactive")
        db.add_all([buy, avoid])
        db.flush()
        db.add(PaperTradingPosition(
            stock_symbol="600519", stock_market="CN", quantity=100, entry_price=10,
            current_price=10, highest_price=10, stop_loss=9.2, target_price=11.5, status="open",
            signal_run_id=buy.id,
            opened_at=datetime(2026, 10, 12, 10, tzinfo=SHANGHAI).astimezone(timezone.utc).replace(tzinfo=None)))
        db.commit()
    engine = module.PaperTradingEngine()
    monkeypatch.setattr(engine, "_fetch_quotes_map", lambda pairs: {("CN", "600519"): {"current_price": 10}})
    assert engine._scan_sync()["closed"] == 1
    with storage() as db:
        pos = db.query(PaperTradingPosition).one()
        assert pos.status == "closed"
