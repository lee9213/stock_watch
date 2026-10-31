"""事件驱动门面：话题、检索、速览。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from src.modules.market.news_digest_job import digest_payload
from src.modules.market.news_store import (
    ALL_NEWS_TOPIC,
    FIXED_TOPICS,
    search_news,
    topic_items,
)

__all__ = [
    "ALL_NEWS_TOPIC",
    "FIXED_TOPICS",
    "digest_payload",
    "search_news_flow",
    "topic_items",
]


def search_news_flow(
    *,
    keyword: str = "",
    page: int = 1,
    page_size: int = 20,
    db: Session | None = None,
    name_by_symbol: dict[str, str] | None = None,
) -> dict:
    if db is None:
        raise ValueError("search_news_flow 需要数据库会话")
    _ = name_by_symbol
    return search_news(
        db,
        keyword=keyword,
        page=page,
        page_size=page_size,
        relation_level_only=False,
    )
