"""百度日K vendor: CSV 解析、市场映射、风控/异常输入降级。"""

from marketdata.vendors import baidu as bv
from marketdata.symbol import Symbol
from marketdata.types import Bar


def _payload(rows: str) -> dict:
    return {"Result": {"newMarketData": {"marketData": rows}}}


def test_parses_ohlcv_csv(monkeypatch):
    rows = (
        "1780502400,2026-06-04,1250.97,1239.98,1260.97,1238.67,-13.91,-1.11,3350617,4273269325.00,--,1253.89,--,--,--;"
        "1793289600,2026-10-30,1363.00,1392.05,1393.37,1352.98,+38.05,+2.81,5992729,8257381778.00,--,1354.00,1356.54,--,--"
    )
    calls = {}

    def fake(url, **kwargs):
        calls["url"] = url
        calls["params"] = kwargs.get("params") or {}
        return _payload(rows)

    monkeypatch.setattr(bv, "market_get", fake)
    out = bv.BaiduKlineVendor().fetch([Symbol.parse("600519", market="CN")], {"days": 60})

    assert len(out) == 2 and isinstance(out[0], Bar)
    assert out[0].date == "2026-06-04"
    assert out[0].open == 1250.97 and out[0].close == 1239.98
    assert out[0].high == 1260.97 and out[0].low == 1238.67
    assert out[1].volume == 5992729.0
    assert calls["params"]["market_type"] == "ab"
    assert calls["params"]["code"] == "600519"


def test_hk_and_us_market_type_and_code(monkeypatch):
    seen = []

    def fake(url, **kwargs):
        p = kwargs.get("params") or {}
        seen.append((p.get("market_type"), p.get("code")))
        return _payload("1,2026-06-04,1,1,1,1,0,0,1,1,--,1,--,--,--")

    monkeypatch.setattr(bv, "market_get", fake)
    bv.BaiduKlineVendor().fetch([Symbol.parse("700", market="HK")], {})
    bv.BaiduKlineVendor().fetch([Symbol.parse("AAPL", market="US")], {})
    assert seen == [("hk", "00700"), ("us", "AAPL")]


def test_risk_403_returns_empty_not_raises(monkeypatch):
    monkeypatch.setattr(
        bv, "market_get", lambda *a, **k: {"Result": {"code": 403, "msg": "hit risk"}}
    )
    assert bv.BaiduKlineVendor().fetch([Symbol.parse("600519", market="CN")], {}) == []


def test_empty_and_malformed_payload(monkeypatch):
    monkeypatch.setattr(bv, "market_get", lambda *a, **k: None)
    assert bv.BaiduKlineVendor().fetch([Symbol.parse("600519", market="CN")], {}) == []

    # 列位不足的行被跳过,而不是整批失败
    monkeypatch.setattr(bv, "market_get", lambda *a, **k: _payload("1,2026-06-04,1,1"))
    assert bv.BaiduKlineVendor().fetch([Symbol.parse("600519", market="CN")], {}) == []


def test_days_truncates_tail(monkeypatch):
    rows = ";".join(
        f"{i},2026-06-{i:02d},1,2,3,0.5,0,0,10,100,--,1,--,--,--" for i in range(1, 11)
    )
    monkeypatch.setattr(bv, "market_get", lambda *a, **k: _payload(rows))
    assert len(bv.BaiduKlineVendor().fetch([Symbol.parse("600519", market="CN")], {"days": 3})) == 3
