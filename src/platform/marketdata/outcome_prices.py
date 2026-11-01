"""Forward outcomes count actual, completed daily bars rather than calendar days."""
from datetime import date
from math import isfinite

from src.platform.scheduling import trading_calendar as calendar


def _forward_rows(klines, base_day: date, market) -> list[dict]:
    """base_day 之后所有"已完成"的日线，按日期升序，带 OHLC。

    只用已收盘的 bar：当天未收盘的 bar 会被丢弃，避免用盘中价当收盘价。
    缺 high/low 的数据源（部分只回 close）用 close 兜底，保证下游不会因为
    字段缺失而误判"未触及"。
    """
    code = calendar._to_market_code(market)
    if code is None:
        return []
    now = calendar._now_in_market_tz(code)
    sessions = calendar.trading_sessions(code, now)
    today_complete = bool(sessions and now.time() > sessions[-1].end)
    rows: dict[date, dict] = {}
    for bar in klines or []:
        try:
            day = date.fromisoformat(str(bar.date)[:10].replace("/", "-"))
            close = float(bar.close)
        except (AttributeError, TypeError, ValueError):
            continue
        if day <= base_day or day > now.date() or (day == now.date() and not today_complete):
            continue
        if not isfinite(close) or close <= 0:
            continue
        if calendar.calendar_known(code, day) and not calendar.is_trading_day(code, day):
            continue

        def _px(name: str) -> float:
            try:
                value = float(getattr(bar, name))
            except (AttributeError, TypeError, ValueError):
                return close
            return value if isfinite(value) and value > 0 else close

        high = max(_px("high"), close)
        low = min(_px("low"), close)
        rows[day] = {
            "date": day,
            "open": _px("open"),
            "high": high,
            "low": low,
            "close": close,
        }
    return [rows[d] for d in sorted(rows)]


def forward_bars(klines, base_day: date, horizon: int, market) -> list[dict] | None:
    """返回 base_day 之后前 horizon 根已完成日线；不足 horizon 根时返回 None（未到期）。"""
    rows = _forward_rows(klines, base_day, market)
    index = max(1, int(horizon))
    if len(rows) < index:
        return None
    return rows[:index]


def completed_outcome_bar(klines, base_day: date, horizon: int, market) -> tuple[date, float] | None:
    rows = _forward_rows(klines, base_day, market)
    index = max(1, int(horizon)) - 1
    if index >= len(rows):
        return None
    bar = rows[index]
    return bar["date"], bar["close"]
