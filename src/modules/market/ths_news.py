"""同花顺财经快讯抓取（独立实现，不引用兄弟项目）。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from src.modules.market.news_store import is_within_ingest_window, news_md5

logger = logging.getLogger(__name__)

THS_URL = "https://news.10jqka.com.cn/tapp/news/push/stock/"
SHANGHAI = ZoneInfo("Asia/Shanghai")
THS_HEADERS = {
    "Host": "news.10jqka.com.cn",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) Gecko/20100101 Firefox/136.0",
    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.8,en-US;q=0.3,en;q=0.2",
}


def _as_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        return [part.strip() for part in value.replace("，", ",").split(",") if part.strip()]
    return []


def _parse_time(raw: dict) -> datetime:
    for key in ("ctime", "rtime", "mtime"):
        stamp = raw.get(key)
        if stamp in (None, "", 0, "0"):
            continue
        try:
            return datetime.fromtimestamp(int(stamp), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            continue
    return datetime.now(timezone.utc)


def _stock_code(item: dict) -> str:
    for key in ("stockCode", "stock_code", "code", "stockcode", "symbol", "stockMarketCode"):
        value = item.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _stock_name(item: dict, code: str) -> str:
    for key in ("name", "stockName", "stock_name", "stockname"):
        value = item.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return code


def _stocks(raw: dict) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in _as_list(raw.get("stock") or raw.get("stocks") or raw.get("code_list")):
        if not isinstance(item, dict):
            continue
        code = _stock_code(item)
        name = _stock_name(item, code)
        if not code or code in seen:
            continue
        seen.add(code)
        rows.append({"code": code, "name": name})
    return rows


def _compose_digest(title: str, body: str) -> str:
    """标题常带公司名，正文不一定有；正文已包含标题时不重复。"""
    if not title:
        return body
    if not body or title in body:
        return body or title
    return f"{title}。{body}"


def normalize_ths_item(raw: dict) -> dict | None:
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or raw.get("short") or "").strip()
    body = str(raw.get("digest") or raw.get("content") or "").strip()
    digest = _compose_digest(title, body)
    if not digest:
        return None
    tags = _as_list(raw.get("tag") or raw.get("tags"))
    text = f"{title}{digest}{' '.join(tags)}"
    news_type = "report" if "研报" in text else "news"
    url = str(raw.get("url") or raw.get("shareUrl") or raw.get("shareurl") or "").strip()
    return {
        "external_id": str(raw.get("id") or raw.get("seq") or ""),
        "digest": digest,
        # 去重键沿用旧口径（仅正文），避免拼标题后把已入库快讯当成新条目。
        "news_md5": news_md5(body or title),
        "title": title,
        "tags": tags,
        "relations_stocks": _stocks(raw),
        "news_time": _parse_time(raw),
        "news_type": news_type,
        "url": url,
        "sources": "ths",
    }


def fetch_ths_flash(page: int = 1, pagesize: int = 50, timeout: float = 8.0) -> list[dict]:
    """拉取同花顺实时快讯并规范化。失败返回空列表。"""
    try:
        response = httpx.get(
            THS_URL,
            params={"page": page, "tag": "", "track": "website", "pagesize": pagesize},
            headers=THS_HEADERS,
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        logger.exception("同花顺快讯拉取失败")
        return []
    rows = (((payload or {}).get("data") or {}).get("list")) or []
    items = []
    for raw in rows:
        item = normalize_ths_item(raw)
        if item and is_within_ingest_window(item.get("news_time")):
            items.append(item)
    return items
