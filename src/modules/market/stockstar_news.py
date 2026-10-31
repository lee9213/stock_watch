"""证券之星新闻 / 研报抓取（独立实现，不引用兄弟项目）。"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from src.modules.market.news_store import is_within_ingest_window

logger = logging.getLogger(__name__)

SHANGHAI = ZoneInfo("Asia/Shanghai")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
}
# 新闻列表 + 资讯栏目；研报专栏单独配置
NEWS_LIST_URLS = (
    "https://stock.stockstar.com/list/2_1.shtml",
    "https://stock.stockstar.com/list/5921.shtml",
)
REPORT_LIST_URLS = (
    "https://stock.stockstar.com/list/6003_1.shtml",
)
ROLL_LIST_URLS = (
    "https://roll.stockstar.com/finance.shtml",
)
DIGEST_MAX = 1200


def _decode_stockstar(content: bytes) -> str:
    """证券之星页面多为 GBK；httpx 在缺 charset 时常误判为 utf-8。"""
    raw = content or b""
    for encoding in ("gb18030", "gbk", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("gb18030", errors="ignore")


def _soup(html: str):
    from bs4 import BeautifulSoup

    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def _absolute_url(href: str) -> str:
    text = (href or "").strip()
    if not text:
        return ""
    if text.startswith("http://") or text.startswith("https://"):
        return text
    if text.startswith("//"):
        return "https:" + text
    if text.startswith("/"):
        return "https://stock.stockstar.com" + text
    return "https://stock.stockstar.com/" + text.lstrip("/")


def _parse_list_time(raw: str) -> datetime:
    text = (raw or "").strip()
    now = datetime.now(SHANGHAI)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=SHANGHAI).astimezone(timezone.utc)
        except ValueError:
            continue
    matched = re.match(r"^(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?$", text)
    if matched:
        month, day, hour, minute = (int(matched.group(i)) for i in range(1, 5))
        second = int(matched.group(5) or 0)
        try:
            local = datetime(now.year, month, day, hour, minute, second, tzinfo=SHANGHAI)
            if local > now + timedelta(days=1):
                local = local.replace(year=now.year - 1)
            return local.astimezone(timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


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


def _is_article_href(href: str) -> bool:
    path = (href or "").strip()
    if not path or "/list/" in path.lower():
        return False
    # 证券之星正文页常见 IG / SS / JR 前缀
    upper = path.upper()
    return any(token in upper for token in ("/IG", "/SS", "/JR"))


def fetch_list_page(list_url: str, timeout: float = 10.0) -> list[dict]:
    """只解析列表页：title / url / publish_date，不抓正文。"""
    try:
        response = httpx.get(list_url, headers=HEADERS, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
        html = _decode_stockstar(response.content)
    except Exception:
        logger.exception("证券之星列表拉取失败 url=%s", list_url)
        return []

    soup = _soup(html)
    results: list[dict] = []
    seen: set[str] = set()
    for item in soup.find_all("li"):
        time_span = item.find("span")
        if time_span is None:
            continue
        pub_time = time_span.get_text(strip=True)
        if not pub_time:
            continue
        for link in item.find_all("a", href=True):
            href = link.get("href") or ""
            if not _is_article_href(href):
                continue
            title = link.get_text(strip=True)
            url = _absolute_url(href)
            if not title or not url or url in seen:
                continue
            seen.add(url)
            results.append(
                {
                    "title": title,
                    "url": url,
                    "publish_date": pub_time,
                }
            )
    return results


def fetch_article_content(url: str, timeout: float = 10.0) -> dict | None:
    try:
        response = httpx.get(url, headers=HEADERS, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
        html = _decode_stockstar(response.content)
    except Exception:
        logger.exception("证券之星详情拉取失败 url=%s", url)
        return None

    soup = _soup(html)
    title_elem = soup.select_one(".tst h1") or soup.select_one("h1")
    title = title_elem.get_text(strip=True) if title_elem else ""
    time_elem = soup.select_one("#pubtime_baidu") or soup.select_one(".time")
    publish_time = time_elem.get_text(strip=True) if time_elem else ""
    content_div = soup.select_one(".article_content") or soup.select_one("#article_content") or soup.select_one(".article")
    paragraphs: list[str] = []
    if content_div:
        for node in content_div.find_all(["p", "div"]):
            text = node.get_text(strip=True)
            if text and text not in paragraphs:
                paragraphs.append(text)
    content = "\n".join(paragraphs).strip()
    if not content:
        content = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:DIGEST_MAX]
    return {
        "title": title,
        "publish_time": publish_time,
        "content": content,
        "url": url,
    }


def normalize_stockstar_item(
    list_item: dict,
    detail: dict | None = None,
    *,
    news_type: str = "news",
) -> dict | None:
    title = str((detail or {}).get("title") or list_item.get("title") or "").strip()
    content = str((detail or {}).get("content") or "").strip()
    digest = _digest(title, content)
    if not digest:
        return None
    url = str((detail or {}).get("url") or list_item.get("url") or "").strip()
    time_raw = (detail or {}).get("publish_time") or list_item.get("publish_date") or ""
    md5_seed = url or digest
    tags = ["证券之星"]
    if news_type == "report":
        tags.append("研报")
    return {
        "external_id": "",
        "digest": digest,
        "title": title,
        "tags": tags,
        "relations_stocks": [],
        "news_time": _parse_list_time(str(time_raw)),
        "news_type": news_type if news_type in ("news", "report") else "news",
        "url": url,
        "sources": "stock_star",
        "news_md5": hashlib.md5(md5_seed.encode("utf-8")).hexdigest(),
        "analyzed": 0,
    }


def fetch_stockstar_feed(
    *,
    list_urls: tuple[str, ...] | list[str],
    news_type: str = "news",
    limit: int = 8,
    delay: float = 0.35,
    skip_urls: set[str] | None = None,
    timeout: float = 10.0,
) -> list[dict]:
    """抓列表 → 跳过已入库 URL → 补正文 → 规范化。"""
    skip = skip_urls or set()
    candidates: list[dict] = []
    seen: set[str] = set()
    for list_url in list_urls:
        for row in fetch_list_page(list_url, timeout=timeout):
            url = row.get("url") or ""
            if not url or url in seen or url in skip:
                continue
            if not is_within_ingest_window(_parse_list_time(str(row.get("publish_date") or ""))):
                continue
            seen.add(url)
            candidates.append(row)
            if len(candidates) >= max(int(limit or 1), 1) * 3:
                break
        if len(candidates) >= max(int(limit or 1), 1) * 3:
            break

    items: list[dict] = []
    for index, row in enumerate(candidates):
        if len(items) >= max(int(limit or 1), 1):
            break
        detail = fetch_article_content(row["url"], timeout=timeout)
        item = normalize_stockstar_item(row, detail, news_type=news_type)
        if item and is_within_ingest_window(item.get("news_time")):
            items.append(item)
        if index + 1 < len(candidates):
            time.sleep(max(float(delay or 0), 0))
    return items


def fetch_stockstar_news(**kwargs) -> list[dict]:
    return fetch_stockstar_feed(list_urls=NEWS_LIST_URLS, news_type="news", **kwargs)


def fetch_stockstar_reports(**kwargs) -> list[dict]:
    return fetch_stockstar_feed(list_urls=REPORT_LIST_URLS, news_type="report", **kwargs)


def fetch_stockstar_roll(**kwargs) -> list[dict]:
    return fetch_stockstar_feed(list_urls=ROLL_LIST_URLS, news_type="news", **kwargs)
