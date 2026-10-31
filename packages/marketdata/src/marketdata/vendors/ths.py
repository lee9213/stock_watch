"""同花顺 A 股报价 vendor(quote 第三源)。

公开 JSONP:`d.10jqka.com.cn/v2/realhead/hs_{code}/last.js`,免 key。
仅 CN;港美同路径未通,不在此 vendor 覆盖。

字段来自 realhead `items` 数字键(社区与实抓交叉核对):
10 现价 / 6 昨收 / 7 开 / 8 高 / 9 低 / 13 量 / 19 额 /
199112 涨跌幅(%) / 264648 涨跌额 / name 名称 / 5 代码。
市值类字段量级未校准,宁缺毋错不映射。
"""

from __future__ import annotations

import json
import logging
import re

from marketdata.http import market_get
from marketdata.symbol import Market, Symbol
from marketdata.types import Quote
from marketdata.vendors.base import QuoteVendor

logger = logging.getLogger(__name__)

_HOST = "d.10jqka.com.cn"
_URL_TMPL = "https://d.10jqka.com.cn/v2/realhead/hs_{code}/last.js"
_MIN_INTERVAL_S = 0.25
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://q.10jqka.com.cn/",
    "Accept": "*/*",
}
_JSONP_RE = re.compile(r"\((\{.*\})\)\s*$", re.S)


def _to_float(value) -> float | None:
    if value is None or value == "" or value == "-":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_jsonp(text: str) -> dict | None:
    """剥 quotebridge_v2_realhead_..._last({...}) 外壳。"""
    stripped = (text or "").strip()
    match = _JSONP_RE.search(stripped)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _parse_one(items: dict | None, market: str, fallback_code: str) -> Quote | None:
    if not isinstance(items, dict):
        return None
    price = _to_float(items.get("10"))
    if price is None or price <= 0:
        return None
    code = str(items.get("5") or fallback_code).strip() or fallback_code
    name = str(items.get("name") or "").strip()
    return Quote(
        symbol=code,
        market=market,
        name=name,
        current_price=price,
        prev_close=_to_float(items.get("6")),
        open_price=_to_float(items.get("7")),
        high_price=_to_float(items.get("8")),
        low_price=_to_float(items.get("9")),
        change_amount=_to_float(items.get("264648")),
        change_pct=_to_float(items.get("199112")),
        volume=_to_float(items.get("13")),
        turnover=_to_float(items.get("19")),
        source="ths",
    )


class ThsQuoteVendor(QuoteVendor):
    name = "ths"
    supports_markets = {"CN"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[Quote]:
        if not symbols:
            return []
        out: list[Quote] = []
        for sym in symbols:
            if sym.market != Market.CN:
                continue
            text = market_get(
                _URL_TMPL.format(code=sym.code),
                host_key=_HOST,
                min_interval_s=_MIN_INTERVAL_S,
                headers=_HEADERS,
                timeout=8,
                retries=2,
                parse="text",
                log_label="同花顺报价",
                symbol=sym.code,
            )
            if not text:
                continue
            payload = _parse_jsonp(text if isinstance(text, str) else str(text))
            items = (payload or {}).get("items") if isinstance(payload, dict) else None
            quote = _parse_one(items if isinstance(items, dict) else None, sym.market.value, sym.code)
            if quote:
                out.append(quote)
        return out
