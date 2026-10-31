"""机会页只覆盖 A 股。港股 / 美股仍可用于持仓、自选与深度分析，但不进入机会扫描与列表。"""

from __future__ import annotations

OPPORTUNITY_MARKETS = ("CN",)


def resolve_opportunity_markets(markets: list[str] | None = None) -> list[str]:
    """把调用方传入的市场列表收窄到机会页允许的市场。

    ``None`` 表示未指定，默认只扫 A 股；显式传入港股/美股会被丢掉。
    """
    allowed = {item.upper() for item in OPPORTUNITY_MARKETS}
    if markets is None:
        return list(OPPORTUNITY_MARKETS)
    resolved: list[str] = []
    for item in markets:
        key = str(item or "").strip().upper()
        if key in allowed and key not in resolved:
            resolved.append(key)
    return resolved


def resolve_opportunity_market_query(market: str = "") -> list[str] | None:
    """列表接口的市场过滤。空 / ALL 视为全部机会市场（仅 CN）；HK/US 返回 None 表示无结果。"""
    raw = (market or "").strip().upper()
    if not raw or raw == "ALL":
        return list(OPPORTUNITY_MARKETS)
    resolved = resolve_opportunity_markets([raw])
    return resolved or None
