"""多源新闻入库：同花顺 + 华尔街见闻 + 证券之星（新闻/研报）。"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from src.modules.market.news_prefilter import should_ingest_news
from src.modules.market.news_store import (
    enrich_existing_news,
    insert_news,
    is_within_ingest_window,
)
from src.modules.market.stockstar_news import (
    fetch_stockstar_news,
    fetch_stockstar_reports,
    fetch_stockstar_roll,
)
from src.modules.market.ths_news import fetch_ths_flash
from src.modules.market.wallstreet_news import fetch_wallstreet_hot
from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import MarketNews, MarketNewsDiscard

logger = logging.getLogger(__name__)


def _existing_urls(db: Session, limit: int = 4000) -> set[str]:
    """已入库或已丢弃的链接，抓详情前跳过，省掉无效请求。"""
    urls: set[str] = set()
    for model in (MarketNews, MarketNewsDiscard):
        rows = (
            db.query(model.url)
            .filter(model.url.isnot(None), model.url != "")
            .order_by(model.id.desc())
            .limit(limit)
            .all()
        )
        urls.update(str(row[0]) for row in rows if row and row[0])
    return urls


def _insert_batch(db: Session, items: list[dict]) -> tuple[list[int], int]:
    """批量入库；返回 (新入库 id 列表, 回填条数)。"""
    created_ids: list[int] = []
    enriched = 0
    for item in items:
        if not is_within_ingest_window(item.get("news_time")):
            continue
        if not should_ingest_news(item):
            continue
        row = insert_news(db, item)
        if row is not None and row.id is not None:
            created_ids.append(int(row.id))
        elif enrich_existing_news(db, item):
            enriched += 1
    return created_ids, enriched


def ingest_ths_news(db: Session | None = None, pagesize: int = 50, pages: int = 2) -> list[int]:
    own = db is None
    session = db or SessionLocal()
    created_ids: list[int] = []
    try:
        enriched = 0
        for page in range(1, max(int(pages or 1), 1) + 1):
            batch_ids, batch_enriched = _insert_batch(
                session, fetch_ths_flash(page=page, pagesize=pagesize)
            )
            created_ids.extend(batch_ids)
            enriched += batch_enriched
        if created_ids or enriched:
            logger.info(
                "同花顺快讯入库 %s 条，回填关联股票 %s 条",
                len(created_ids),
                enriched,
            )
        return created_ids
    finally:
        if own:
            session.close()


def ingest_wallstreet_news(db: Session | None = None, limit: int = 8) -> list[int]:
    own = db is None
    session = db or SessionLocal()
    try:
        skip = _existing_urls(session)
        items = fetch_wallstreet_hot(limit=limit, skip_urls=skip)
        created_ids, enriched = _insert_batch(session, items)
        if created_ids or enriched:
            logger.info(
                "华尔街见闻入库 %s 条，回填 %s 条",
                len(created_ids),
                enriched,
            )
        return created_ids
    finally:
        if own:
            session.close()


def ingest_stockstar_news(
    db: Session | None = None,
    *,
    news_limit: int = 6,
    report_limit: int = 4,
    include_roll: bool = False,
) -> list[int]:
    own = db is None
    session = db or SessionLocal()
    try:
        skip = _existing_urls(session)
        items: list[dict] = []
        items.extend(fetch_stockstar_news(limit=news_limit, skip_urls=skip))
        skip |= {item.get("url") or "" for item in items}
        items.extend(fetch_stockstar_reports(limit=report_limit, skip_urls=skip))
        if include_roll:
            skip |= {item.get("url") or "" for item in items}
            items.extend(fetch_stockstar_roll(limit=max(2, news_limit // 2), skip_urls=skip))
        created_ids, enriched = _insert_batch(session, items)
        if created_ids or enriched:
            logger.info(
                "证券之星入库 %s 条，回填 %s 条",
                len(created_ids),
                enriched,
            )
        return created_ids
    finally:
        if own:
            session.close()


def ingest_news_sources(
    db: Session | None = None,
    *,
    ths_pages: int = 2,
    ths_pagesize: int = 50,
    wallstreet_limit: int = 8,
    stockstar_news_limit: int = 6,
    stockstar_report_limit: int = 4,
    include_stockstar_roll: bool = False,
) -> dict:
    """统一采集三源。

    返回：
    - ths / wallstreet / stock_star：各源新入库条数
    - created_ids：本次新入库的全部新闻 id（供分析队列直接入队）
    """
    own = db is None
    session = db or SessionLocal()
    try:
        ths_ids = ingest_ths_news(session, pagesize=ths_pagesize, pages=ths_pages)
        wallstreet_ids = ingest_wallstreet_news(session, limit=wallstreet_limit)
        stock_star_ids = ingest_stockstar_news(
            session,
            news_limit=stockstar_news_limit,
            report_limit=stockstar_report_limit,
            include_roll=include_stockstar_roll,
        )
        created_ids = [*ths_ids, *wallstreet_ids, *stock_star_ids]
        result = {
            "ths": len(ths_ids),
            "wallstreet": len(wallstreet_ids),
            "stock_star": len(stock_star_ids),
            "created_ids": created_ids,
        }
        total = len(created_ids)
        if total:
            logger.info(
                "多源新闻采集完成：同花顺 %s / 华尔街见闻 %s / 证券之星 %s",
                result["ths"],
                result["wallstreet"],
                result["stock_star"],
            )
        return result
    finally:
        if own:
            session.close()
