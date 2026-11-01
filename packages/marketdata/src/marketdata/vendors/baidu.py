"""百度股市通 K 线 vendor(HTTP,免 key)。

数据来自 ``finance.pae.baidu.com/sapi/v1/get_analysis_quotation``:该接口本是
个股详情页画"压力位/支撑位"用的,但会一并返回日线 OHLCV(前复权口径与页面一致),
字段为 CSV:时间戳,日期,开,收,高,低,涨跌额,涨跌幅,成交量,成交额,持仓量,昨收,ma5,ma10,ma20。

已知边界(选型时注意):
- 固定只返回**近 100 个交易日**,不接受天数参数,长历史/回测场景会被上层判定为"不足"。
- 同域的实时接口(``/vapi/v1/getquotation``、分时 ``minitrend``、资金流 ``fundflow``)
  走百度 ACS 签名风控,脚本直连返回 ``403 hit risk``,故本模块**只接日K**。
- 风控命中是概率性的:同一出口 IP 偶发可用、多数被拦,因此命中时返回空而非抛异常,
  由 Engine 按 priority 继续回落到下一个源。
"""

from __future__ import annotations

import logging

from marketdata.http import market_get, record_error
from marketdata.symbol import Market, Symbol
from marketdata.types import Bar
from marketdata.vendors.base import KlineVendor

logger = logging.getLogger(__name__)

_URL = "https://finance.pae.baidu.com/sapi/v1/get_analysis_quotation"
_HOST = "finance.pae.baidu.com"

# 缺了 Accept/Referer 会被直接判风控,三者一起才能稳定命中 sapi 层。
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/vnd.finance-web.v1+json",
    "Referer": "https://finance.baidu.com/",
}

_MARKET_TYPE = {Market.CN.value: "ab", Market.HK.value: "hk", Market.US.value: "us"}

# CSV 列位(见模块 docstring)。缺失列位时不猜,直接跳过该行。
_COL_DATE, _COL_OPEN, _COL_CLOSE, _COL_HIGH, _COL_LOW, _COL_VOLUME = 1, 2, 3, 4, 5, 8


def _to_float(value: str | None) -> float | None:
    if value is None:
        return None
    v = str(value).strip().replace("+", "")
    if not v or v in ("--", "-", "null"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_bars(payload: object) -> list[Bar]:
    """把 Result.newMarketData.marketData 的 CSV 串解析为 Bar 列表(按日期升序)。"""
    if not isinstance(payload, dict):
        return []
    result = payload.get("Result")
    if not isinstance(result, dict):
        return []
    market_data = (result.get("newMarketData") or {}).get("marketData")
    if not isinstance(market_data, str) or not market_data.strip():
        return []

    bars: list[Bar] = []
    for row in market_data.split(";"):
        cols = row.split(",")
        if len(cols) <= _COL_VOLUME:
            continue
        date_s = cols[_COL_DATE].strip()
        open_p = _to_float(cols[_COL_OPEN])
        close_p = _to_float(cols[_COL_CLOSE])
        high_p = _to_float(cols[_COL_HIGH])
        low_p = _to_float(cols[_COL_LOW])
        if not date_s or None in (open_p, close_p, high_p, low_p):
            continue
        bars.append(
            Bar(
                date=date_s,
                open=float(open_p),
                close=float(close_p),
                high=float(high_p),
                low=float(low_p),
                volume=_to_float(cols[_COL_VOLUME]) or 0.0,
            )
        )
    bars.sort(key=lambda b: b.date)
    return bars


def fetch_baidu_bars(symbol: Symbol) -> list[Bar]:
    """按 Symbol 取百度日线(最近 100 交易日)。风控/无数据时返回 []。"""
    market_type = _MARKET_TYPE.get(symbol.market.value)
    if not market_type:
        return []
    code = symbol.code
    if symbol.market == Market.HK and code.isdigit():
        code = code.zfill(5)   # 百度港股用 5 位(00700)

    payload = market_get(
        _URL,
        host_key=_HOST,
        headers=_HEADERS,
        params={"code": code, "market_type": market_type, "finClientType": "pc"},
        timeout=10,
        retries=1,
        parse="json",
        log_label="百度K线",
        symbol=code,
    )
    if not payload:
        return []
    if isinstance(payload, dict):
        result = payload.get("Result")
        if isinstance(result, dict) and str(result.get("code")) == "403":
            # 风控命中:按"无数据"处理,让 Engine 继续回落,不打警告噪音。
            record_error(f"baidu: 风控拦截 {code}")
            return []
    return _parse_bars(payload)


class BaiduKlineVendor(KlineVendor):
    name = "baidu"
    supports_markets = {Market.CN.value, Market.HK.value, Market.US.value}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[Bar]:
        if not symbols:
            return []
        days = config.get("days")
        try:
            days = int(days) if days else 0
        except (TypeError, ValueError):
            days = 0
        bars = fetch_baidu_bars(symbols[0])
        if days > 0:
            return bars[-days:]
        return bars
