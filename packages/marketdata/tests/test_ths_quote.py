"""同花顺 A 股报价 vendor 离线单测。"""

from __future__ import annotations

import marketdata.vendors.ths as ths
from marketdata.symbol import Symbol
from marketdata.types import Quote


def _jsonp(items: dict, code: str = "600519") -> str:
    payload = {"items": items}
    import json

    body = json.dumps(payload, ensure_ascii=False)
    return f"quotebridge_v2_realhead_hs_{code}_last({body})"


def _items(
    *,
    code: str = "600519",
    name: str = "贵州茅台",
    price: str = "1700.00",
    prev: str = "1680.00",
    open_: str = "1685.00",
    high: str = "1710.00",
    low: str = "1670.00",
    volume: str = "12345.00",
    turnover: str = "6789000000.00",
    change_pct: str = "1.19",
    change_amount: str = "20.00",
) -> dict:
    return {
        "5": code,
        "name": name,
        "10": price,
        "6": prev,
        "7": open_,
        "8": high,
        "9": low,
        "13": volume,
        "19": turnover,
        "199112": change_pct,
        "264648": change_amount,
    }


def test_ths_parses_quote(monkeypatch):
    monkeypatch.setattr(ths, "market_get", lambda *a, **k: _jsonp(_items()))
    out = ths.ThsQuoteVendor().fetch([Symbol.parse("600519", market="CN")], {})
    assert len(out) == 1
    q = out[0]
    assert isinstance(q, Quote)
    assert q.symbol == "600519" and q.name == "贵州茅台" and q.market == "CN"
    assert q.current_price == 1700.0
    assert q.prev_close == 1680.0
    assert q.open_price == 1685.0
    assert q.high_price == 1710.0
    assert q.low_price == 1670.0
    assert q.change_amount == 20.0
    assert q.change_pct == 1.19
    assert q.volume == 12345.0
    assert q.turnover == 6789000000.0
    assert q.source == "ths"


def test_ths_batch_loops_per_symbol(monkeypatch):
    calls: list[str] = []

    def fake_market_get(url, **kwargs):
        calls.append(url)
        if "hs_600519" in url:
            return _jsonp(_items(code="600519", name="贵州茅台"), "600519")
        if "hs_000001" in url:
            return _jsonp(_items(code="000001", name="平安银行", price="11.20"), "000001")
        return None

    monkeypatch.setattr(ths, "market_get", fake_market_get)
    symbols = [Symbol.parse("600519", market="CN"), Symbol.parse("000001", market="CN")]
    out = ths.ThsQuoteVendor().fetch(symbols, {})
    assert len(calls) == 2
    assert {q.symbol for q in out} == {"600519", "000001"}


def test_ths_empty_response_returns_empty(monkeypatch):
    monkeypatch.setattr(ths, "market_get", lambda *a, **k: None)
    assert ths.ThsQuoteVendor().fetch([Symbol.parse("600519", market="CN")], {}) == []


def test_ths_non_jsonp_returns_empty(monkeypatch):
    monkeypatch.setattr(ths, "market_get", lambda *a, **k: "<html>bad</html>")
    assert ths.ThsQuoteVendor().fetch([Symbol.parse("600519", market="CN")], {}) == []


def test_ths_no_symbols_returns_empty():
    assert ths.ThsQuoteVendor().fetch([], {}) == []


def test_ths_unsupported_market_skipped(monkeypatch):
    calls = {"n": 0}

    def fake_market_get(*a, **k):
        calls["n"] += 1
        return _jsonp(_items())

    monkeypatch.setattr(ths, "market_get", fake_market_get)
    out = ths.ThsQuoteVendor().fetch([Symbol.parse("00700", market="HK")], {})
    assert out == []
    assert calls["n"] == 0


def test_ths_missing_price_returns_empty(monkeypatch):
    monkeypatch.setattr(
        ths,
        "market_get",
        lambda *a, **k: _jsonp({"5": "600519", "name": "贵州茅台", "10": "0"}),
    )
    assert ths.ThsQuoteVendor().fetch([Symbol.parse("600519", market="CN")], {}) == []
