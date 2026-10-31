"""华尔街见闻热门文章抓取（独立实现，不引用兄弟项目）。"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from src.modules.market.news_store import is_within_ingest_window

logger = logging.getLogger(__name__)

HOT_API_URL = "https://api-one-wscn.awtmt.com/apiv1/content/articles/hot?period=all"
ARTICLE_API_TEMPLATE = (
    "https://api-one-wscn.awtmt.com/apiv1/content/articles/{}"
    "?extract=0&accept_theme=theme%2Cpremium-theme"
)
ARTICLE_PAGE = "https://wallstreetcn.com/articles/{}"
SHANGHAI = ZoneInfo("Asia/Shanghai")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://wallstreetcn.com/",
}
DIGEST_MAX = 1200


def _html_to_text(html: str) -> str:
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
        text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", text).strip()
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "aside", "footer", "nav"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(separator=" ", strip=True)).strip()


def _parse_ts(stamp) -> datetime:
    try:
        value = int(stamp or 0)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _digest(title: str, content: str) -> str:
    title = (title or "").strip()
    body = (content or "").strip()
    if title and body:
        text = f"{title}。{body}"
    else:
        text = title or body
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > DIGEST_MAX:
        return text[: DIGEST_MAX - 1].rstrip() + "…"
    return text


def normalize_wallstreet_article(raw: dict) -> dict | None:
    if not isinstance(raw, dict) or raw.get("error"):
        return None
    article_id = raw.get("id")
    title = str(raw.get("title") or "").strip()
    content = str(raw.get("content_text") or "").strip()
    digest = _digest(title, content)
    if not digest:
        return None
    url = str(raw.get("url") or (ARTICLE_PAGE.format(article_id) if article_id else "")).strip()
    md5_seed = url or digest
    return {
        "external_id": str(article_id or ""),
        "digest": digest,
        "title": title,
        "tags": ["华尔街见闻"],
        "relations_stocks": [],
        "news_time": _parse_ts(raw.get("display_time")),
        "news_type": "news",
        "url": url,
        "sources": "wallstreet",
        "news_md5": hashlib.md5(md5_seed.encode("utf-8")).hexdigest(),
        "analyzed": 0,
    }


def fetch_hot_article_ids(timeout: float = 10.0) -> list[int]:
    try:
        response = httpx.get(HOT_API_URL, headers=HEADERS, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except Exception:
        logger.exception("华尔街见闻热门列表拉取失败")
        return []
    if (payload or {}).get("code") != 20000:
        logger.warning("华尔街见闻热门列表异常: %s", (payload or {}).get("message"))
        return []
    data = (payload or {}).get("data") or {}
    seen: set[int] = set()
    ids: list[int] = []
    for item in list(data.get("day_items") or []) + list(data.get("week_items") or []):
        if not isinstance(item, dict):
            continue
        try:
            aid = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if aid in seen:
            continue
        seen.add(aid)
        ids.append(aid)
    return ids


def fetch_article(article_id: int, timeout: float = 10.0) -> dict:
    url = ARTICLE_API_TEMPLATE.format(article_id)
    result = {
        "id": article_id,
        "title": "",
        "content_text": "",
        "display_time": 0,
        "url": ARTICLE_PAGE.format(article_id),
        "error": None,
    }
    try:
        response = httpx.get(url, headers=HEADERS, timeout=timeout)
        if response.status_code != 200:
            result["error"] = f"HTTP {response.status_code}"
            return result
        payload = response.json()
        if (payload or {}).get("code") != 20000:
            result["error"] = (payload or {}).get("message") or "api_error"
            return result
        art = (payload or {}).get("data") or {}
        result.update(
            {
                "title": art.get("title") or "",
                "content_text": _html_to_text(art.get("content") or ""),
                "display_time": art.get("display_time") or 0,
            }
        )
        return result
    except Exception as exc:
        result["error"] = str(exc)
        return result


def fetch_wallstreet_hot(
    *,
    limit: int = 10,
    delay: float = 0.5,
    skip_urls: set[str] | None = None,
    timeout: float = 10.0,
) -> list[dict]:
    """拉取热门文章并规范化；已存在 URL 跳过详情请求。"""
    skip = skip_urls or set()
    ids = fetch_hot_article_ids(timeout=timeout)[: max(int(limit or 1) * 2, 1)]
    items: list[dict] = []
    for index, aid in enumerate(ids):
        if len(items) >= max(int(limit or 1), 1):
            break
        page_url = ARTICLE_PAGE.format(aid)
        if page_url in skip:
            continue
        raw = fetch_article(aid, timeout=timeout)
        item = normalize_wallstreet_article(raw)
        if item and is_within_ingest_window(item.get("news_time")):
            items.append(item)
        if index + 1 < len(ids):
            time.sleep(max(float(delay or 0), 0))
    return items
