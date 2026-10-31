"""发现(热门榜)vendor:市场级、非 symbol 模型。

东财 clist 仍是主源,但 push2 / push2delay 会偶发断连。本 vendor 在包内做
多源故障转移:东财多主机 → 新浪行情中心 → 同花顺热榜 → 东财人气榜。
不进 Engine(非 symbol 请求模型),vendor 内不做缓存。
"""
from __future__ import annotations

import logging
from typing import Any, Callable

import httpx

from marketdata.http import market_get, record_error
from marketdata.types import HotBoard, HotStock

logger = logging.getLogger(__name__)

# 同一 clist 契约的东财主机。push2delay 在部分代理环境更稳,push2 / 82.push2 作备源。
_EASTMONEY_CLIST_ENDPOINTS: tuple[tuple[str, str, str], ...] = (
    ("eastmoney:push2delay", "https://push2delay.eastmoney.com/api/qt/clist/get", "push2delay.eastmoney.com"),
    ("eastmoney:push2", "https://push2.eastmoney.com/api/qt/clist/get", "push2.eastmoney.com"),
    ("eastmoney:82.push2", "https://82.push2.eastmoney.com/api/qt/clist/get", "82.push2.eastmoney.com"),
)

_SINA_RANK_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)
_SINA_HOST = "vip.stock.finance.sina.com.cn"
_SINA_NODES = {
    "CN": "hs_a",
    "HK": "hk_main",
    "US": "us",
}

_THS_HOT_URL = "https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock"
_THS_HOST = "dq.10jqka.com.cn"

_RANK_URL = "https://emappdata.eastmoney.com/stockrank/getAllCurrentList"
_RANK_HOST = "emappdata.eastmoney.com"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}
_SINA_HEADERS = {
    **_HEADERS,
    "Referer": "https://finance.sina.com.cn/",
}
_THS_HEADERS = {
    **_HEADERS,
    "Referer": "https://eq.10jqka.com.cn/",
}
_RANK_HEADERS = {
    **_HEADERS,
    "Referer": "https://guba.eastmoney.com/",
    "Content-Type": "application/json",
}

# 换源优先于同机重试:单源失败尽快切下一源。
_SOURCE_TIMEOUT = 6.0
_SOURCE_RETRIES = 0


def _to_float(value: Any) -> float | None:
    if value is None or value == "" or value == "-":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_diff(data: dict | None) -> list[dict]:
    """东财 clist 的 diff 字段可能是 list,也可能是 dict(按 index 为 key)。统一成 list。"""
    diff = ((data or {}).get("data") or {}).get("diff") or []
    if isinstance(diff, dict):
        return list(diff.values())
    return diff


def _first_nonempty(sources: list[tuple[str, Callable[[], list]]]) -> list:
    """按顺序试源;空结果或异常都切下一源,首个非空即返回。"""
    last_err = ""
    tried: list[str] = []
    for name, fetch in sources:
        tried.append(name)
        try:
            items = fetch()
        except Exception as exc:
            last_err = str(exc)
            logger.warning("发现榜单 %s 失败,切换下一源: %s", name, exc)
            record_error(f"发现榜单 {name}: {type(exc).__name__}: {exc}")
            continue
        if items:
            logger.info("发现榜单命中源=%s count=%s", name, len(items))
            return items
        last_err = "empty"
        logger.warning("发现榜单 %s 返回空,切换下一源", name)
    if last_err:
        logger.warning(
            "发现榜单全部源失败: tried=%s last=%s",
            ",".join(tried),
            last_err,
        )
    return []


def _strip_symbol(raw: str, market: str) -> str:
    symbol = (raw or "").strip()
    if not symbol:
        return ""
    lowered = symbol.lower()
    if market == "CN" and lowered[:2] in {"sh", "sz", "bj"} and lowered[2:].isdigit():
        return symbol[2:]
    if market == "HK" and lowered.startswith("hk"):
        return symbol[2:]
    if market == "US" and lowered.startswith("gb_"):
        return symbol[3:].upper()
    if market == "US":
        return symbol.upper()
    return symbol


def _map_eastmoney_stocks(rows: list[dict], market: str) -> list[HotStock]:
    result: list[HotStock] = []
    for it in rows:
        try:
            symbol = str(it.get("f12") or "").strip()
            if not symbol:
                continue
            result.append(
                HotStock(
                    symbol=symbol,
                    market=market,
                    name=str(it.get("f14") or "").strip(),
                    price=_to_float(it.get("f2")),
                    change_pct=_to_float(it.get("f3")),
                    turnover=_to_float(it.get("f6")),
                    volume=_to_float(it.get("f5")),
                )
            )
        except Exception:
            continue
    return result


def _map_eastmoney_boards(rows: list[dict]) -> list[HotBoard]:
    result: list[HotBoard] = []
    for it in rows:
        try:
            code = str(it.get("f12") or "").strip()
            if not code:
                continue
            result.append(
                HotBoard(
                    code=code,
                    name=str(it.get("f14") or "").strip(),
                    change_pct=_to_float(it.get("f3")),
                    change_amount=_to_float(it.get("f4")),
                    turnover=_to_float(it.get("f6")),
                )
            )
        except Exception:
            continue
    return result


def _map_sina_stocks(rows: list, market: str) -> list[HotStock]:
    if not isinstance(rows, list):
        return []
    result: list[HotStock] = []
    for it in rows:
        if not isinstance(it, dict):
            continue
        symbol = _strip_symbol(str(it.get("code") or it.get("symbol") or ""), market)
        if not symbol:
            continue
        result.append(
            HotStock(
                symbol=symbol,
                market=market,
                name=str(it.get("name") or "").strip(),
                price=_to_float(it.get("trade")),
                change_pct=_to_float(it.get("changepercent")),
                turnover=_to_float(it.get("amount")),
                volume=_to_float(it.get("volume")),
            )
        )
    return result


def _map_ths_stocks(payload: dict | None, market: str, limit: int) -> list[HotStock]:
    data = payload or {}
    rows = (
        (data.get("data") or {}).get("stock_list")
        or (data.get("data") or {}).get("list")
        or data.get("stock_list")
        or []
    )
    if not isinstance(rows, list):
        return []
    result: list[HotStock] = []
    for it in rows[: max(1, limit)]:
        if not isinstance(it, dict):
            continue
        symbol = _strip_symbol(str(it.get("code") or it.get("stock_code") or ""), market)
        if not symbol:
            continue
        result.append(
            HotStock(
                symbol=symbol,
                market=market,
                name=str(it.get("name") or it.get("stock_name") or "").strip(),
                price=_to_float(it.get("latest") or it.get("price")),
                change_pct=_to_float(it.get("rate") or it.get("rise_and_fall")),
                turnover=_to_float(it.get("turnover") or it.get("amount")),
                volume=_to_float(it.get("volume")),
            )
        )
    return result


def _map_rank_stocks(items: list, market: str) -> list[HotStock]:
    if not isinstance(items, list):
        return []
    result: list[HotStock] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        sc = str(it.get("sc") or "").strip().upper()
        if len(sc) < 8:
            continue
        symbol = sc[2:]
        if not symbol:
            continue
        result.append(
            HotStock(
                symbol=symbol,
                market=market,
                name="",
                price=None,
                change_pct=None,
                turnover=None,
                volume=None,
            )
        )
    return result


def _post_json(
    url: str,
    *,
    host_key: str,
    payload: dict,
    headers: dict,
    proxy: str | None,
    timeout: float = _SOURCE_TIMEOUT,
) -> dict | None:
    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=timeout,
            headers=headers,
            trust_env=True,
            verify=False,
            **({"proxy": proxy} if proxy else {}),
        ) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.warning("发现榜单 POST %s 失败: %s", host_key, exc)
        record_error(f"发现榜单 {host_key}: {type(exc).__name__}: {exc}")
        return None
    return data if isinstance(data, dict) else None


class DiscoveryVendor:
    """热门榜(股票/板块/板块成分)。多源故障转移,不继承 Vendor(市场级、非 symbol)。"""

    def hot_stocks(
        self,
        *,
        market: str = "CN",
        mode: str = "turnover",
        limit: int = 20,
        proxy: str | None = None,
    ) -> list[HotStock]:
        market = (market or "CN").upper()
        safe_limit = max(1, min(int(limit), 100))
        params = self._eastmoney_stock_params(market, mode, safe_limit)
        if params is None:
            return []

        sources: list[tuple[str, Callable[[], list]]] = [
            (
                name,
                lambda url=url, host=host, name=name: self._hot_stocks_eastmoney(
                    url, host, params, market, proxy, name
                ),
            )
            for name, url, host in _EASTMONEY_CLIST_ENDPOINTS
        ]
        sources.append(
            ("sina:hqnode", lambda: self._hot_stocks_sina(market, mode, safe_limit, proxy))
        )
        if market == "CN":
            sources.append(("ths:hot_list", lambda: self._hot_stocks_ths(safe_limit, proxy)))
            sources.append(("eastmoney:stockrank", lambda: self._hot_stocks_rank(safe_limit, proxy)))
        return _first_nonempty(sources)

    def hot_boards(
        self,
        *,
        market: str = "CN",
        mode: str = "gainers",
        limit: int = 12,
        proxy: str | None = None,
    ) -> list[HotBoard]:
        if (market or "CN").upper() != "CN":
            return []

        fid = "f3" if mode in ("gainers", "hot") else "f6"
        params = {
            "pn": 1,
            "pz": max(1, min(int(limit), 100)),
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": fid,
            "fs": "m:90+t:2",
            "fields": "f12,f14,f2,f3,f4,f6",
        }
        sources = [
            (
                name,
                lambda url=url, host=host, name=name: self._hot_boards_eastmoney(
                    url, host, params, proxy, name
                ),
            )
            for name, url, host in _EASTMONEY_CLIST_ENDPOINTS
        ]
        return _first_nonempty(sources)

    def board_stocks(
        self,
        *,
        board_code: str,
        mode: str = "gainers",
        limit: int = 20,
        proxy: str | None = None,
    ) -> list[HotStock]:
        code = (board_code or "").strip()
        if not code:
            return []

        fid = "f3" if mode in ("gainers", "hot") else "f6"
        params = {
            "pn": 1,
            "pz": max(1, min(int(limit), 100)),
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": fid,
            "fs": f"b:{code}",
            "fields": "f12,f14,f2,f3,f6,f5",
        }
        sources = [
            (
                name,
                lambda url=url, host=host, name=name: self._hot_stocks_eastmoney(
                    url, host, params, "CN", proxy, name
                ),
            )
            for name, url, host in _EASTMONEY_CLIST_ENDPOINTS
        ]
        return _first_nonempty(sources)

    def _eastmoney_stock_params(self, market: str, mode: str, limit: int) -> dict[str, Any] | None:
        fid = "f6" if mode == "turnover" else "f3"
        if market == "CN":
            fs = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
        elif market == "HK":
            fs = "m:128+t:3,m:128+t:4,m:128+t:1,m:128+t:2"
        elif market == "US":
            fs = "m:105,m:106,m:107"
        else:
            return None
        return {
            "pn": 1,
            "pz": limit,
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": fid,
            "fs": fs,
            "fields": "f12,f14,f2,f3,f6,f5",
        }

    def _hot_stocks_eastmoney(
        self,
        url: str,
        host_key: str,
        params: dict,
        market: str,
        proxy: str | None,
        source_name: str,
    ) -> list[HotStock]:
        rows = self._fetch_eastmoney_diff(url, host_key, params, proxy, source_name)
        return _map_eastmoney_stocks(rows, market)

    def _hot_boards_eastmoney(
        self,
        url: str,
        host_key: str,
        params: dict,
        proxy: str | None,
        source_name: str,
    ) -> list[HotBoard]:
        rows = self._fetch_eastmoney_diff(url, host_key, params, proxy, source_name)
        return _map_eastmoney_boards(rows)

    def _fetch_eastmoney_diff(
        self,
        url: str,
        host_key: str,
        params: dict,
        proxy: str | None,
        source_name: str,
    ) -> list[dict]:
        data = self._get_json(url, params=params, proxy=proxy, host_key=host_key, label=source_name)
        if not data:
            return []
        return _normalize_diff(data)

    def _hot_stocks_sina(
        self,
        market: str,
        mode: str,
        limit: int,
        proxy: str | None,
    ) -> list[HotStock]:
        node = _SINA_NODES.get(market)
        if not node:
            return []
        params = {
            "page": 1,
            "num": limit,
            "sort": "amount" if mode == "turnover" else "changepercent",
            "asc": 0,
            "node": node,
            "_s_r_a": "page",
        }
        data = market_get(
            _SINA_RANK_URL,
            host_key=_SINA_HOST,
            params=params,
            headers=_SINA_HEADERS,
            proxy=proxy,
            verify=False,
            parse="json",
            retries=_SOURCE_RETRIES,
            timeout=_SOURCE_TIMEOUT,
            min_interval_s=0.0,
            log_label="发现榜单 sina:hqnode",
        )
        return _map_sina_stocks(data or [], market)

    def _hot_stocks_ths(self, limit: int, proxy: str | None) -> list[HotStock]:
        data = market_get(
            _THS_HOT_URL,
            host_key=_THS_HOST,
            params={"stock_type": "a", "type": "hour", "list_type": "normal"},
            headers=_THS_HEADERS,
            proxy=proxy,
            verify=False,
            parse="json",
            retries=_SOURCE_RETRIES,
            timeout=_SOURCE_TIMEOUT,
            min_interval_s=0.0,
            log_label="发现榜单 ths:hot_list",
        )
        return _map_ths_stocks(data if isinstance(data, dict) else None, "CN", limit)

    def _hot_stocks_rank(self, limit: int, proxy: str | None) -> list[HotStock]:
        payload = _post_json(
            _RANK_URL,
            host_key=_RANK_HOST,
            payload={
                "appId": "appId01",
                "globalId": "",
                "marketType": "",
                "pageNo": 1,
                "pageSize": limit,
            },
            headers=_RANK_HEADERS,
            proxy=proxy,
        )
        if not payload:
            return []
        code = payload.get("code")
        try:
            ok = code is None or int(code) == 0
        except (TypeError, ValueError):
            ok = False
        if not ok:
            return []
        return _map_rank_stocks(payload.get("data") or [], "CN")

    def _get_json(
        self,
        url: str,
        *,
        params: dict,
        proxy: str | None,
        host_key: str,
        label: str,
    ) -> dict[str, Any] | None:
        data = market_get(
            url,
            host_key=host_key,
            params=params,
            headers=_HEADERS,
            proxy=proxy,
            verify=False,
            parse="json",
            retries=_SOURCE_RETRIES,
            timeout=_SOURCE_TIMEOUT,
            min_interval_s=0.0,
            log_label=f"发现榜单 {label}",
        )
        return data if isinstance(data, dict) else None
