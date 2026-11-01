"""事件驱动新闻采集：判重索引 + 按 data_sources 配置调度各源入库。

各源怎么抓、字段怎么归一化在 ``news_sources``；这里只负责：
建判重索引（抓详情前预判重）→ 按开关与顺序并行抓取 → 按顺序串行入库。
顺序有意义：同一事件被多源同时报到时，先入库的那条留下，所以 priority 靠前的
源等于「默认代表来源」。
"""

from __future__ import annotations

import logging
import threading
import time
import weakref
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session

from src.modules.market.news_prefilter import should_ingest_news
from src.modules.market.news_sources import (
    NewsSourceSpec,
    load_news_sources,
)
from src.modules.market.news_store import (
    TITLE_DUPLICATE_WINDOW_HOURS,
    NewsDedupSnapshot,
    build_dedup_snapshot,
    enrich_existing_news,
    insert_news_batch,
    is_within_ingest_window,
    prepare_news_items,
)
from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import MarketNews, MarketNewsDiscard
from src.platform.persistence.news_keys import TitleIndex, news_title_key

logger = logging.getLogger(__name__)

# 判重索引的时间跨度：比入库判重的 ±12h 再宽一档，留出时钟/时区误差。
TITLE_INDEX_WINDOW_HOURS = TITLE_DUPLICATE_WINDOW_HOURS + 12
# 各源抓取并行度（纯网络 IO，不含写库）。默认源数是 6，按 3 会让后半批排队等。
SOURCE_FETCH_WORKERS = 6
# 判重索引的复用时长：同一进程内多轮采集（如手动「立即采集」与定时任务撞上）
# 不必重复读两张 4000 行的表。过期只是多读一次库，不会漏判重（写入侧仍会判重）。
DEDUP_INDEX_TTL_SEC = 60

# engine -> (构建时刻, 已入库链接, 标题索引)；Engine 回收后自动失效，测试各用各的库。
_dedup_cache: weakref.WeakKeyDictionary[object, tuple[float, frozenset[str], TitleIndex]] = (
    weakref.WeakKeyDictionary()
)
_dedup_lock = threading.Lock()


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


def _as_aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _recent_title_index(
    db: Session, window_hours: int = TITLE_INDEX_WINDOW_HOURS
) -> TitleIndex:
    """最近窗口内已入库/已丢弃的标题键（按小时分桶），供采集前判重。

    发布时间与入库时间各入一次桶：转载文章两者的差可以到十几小时，只按发布
    时间开窗会让同一事件在换来源后逃过预判重，白抓一次详情页。
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, int(window_hours)))
    index = TitleIndex(TITLE_DUPLICATE_WINDOW_HOURS)
    for model, ingested_col in (
        (MarketNews, MarketNews.created_at),
        (MarketNewsDiscard, MarketNewsDiscard.discarded_at),
    ):
        query = (
            db.query(model.title_key, model.news_time, ingested_col)
            .filter(
                model.title_key != "",
                model.news_time.isnot(None),
                or_(
                    model.news_time >= cutoff,
                    ingested_col >= cutoff,
                ),
            )
            .limit(4000)
        )
        for key, moment, ingested_at in query.all():
            aware = _as_aware(moment)
            ingested = _as_aware(ingested_at)
            if key and aware is not None:
                index.add_anchors(str(key), aware, ingested or aware)
    return index


def invalidate_dedup_index(db: Session | None = None) -> None:
    """写库后让判重索引失效（只影响下一次预判，不影响正确性）。"""
    with _dedup_lock:
        if db is None:
            _dedup_cache.clear()
            return
        engine = _dedup_cache_key(db)
        if engine is not None:
            _dedup_cache.pop(engine, None)


def _dedup_cache_key(db: Session):
    """用绑定的 Engine 当缓存键：不同库（含测试临时库）互不干扰。"""
    return db.get_bind()


def load_dedup_index(db: Session) -> tuple[frozenset[str], TitleIndex]:
    """读判重索引（带 TTL 缓存）：(已入库链接, 标题索引)。"""
    engine = _dedup_cache_key(db)
    now = time.monotonic()
    if engine is not None:
        with _dedup_lock:
            entry = _dedup_cache.get(engine)
        if entry is not None and now - entry[0] < DEDUP_INDEX_TTL_SEC:
            return entry[1], entry[2]
    urls = frozenset(_existing_urls(db))
    titles = _recent_title_index(db)
    if engine is not None:
        with _dedup_lock:
            _dedup_cache[engine] = (now, urls, titles)
    return urls, titles


class IngestDedupIndex:
    """采集前的本地判重索引：已入库链接 + 最近标题键。

    抓正文前就能判断「这条注定会被入库去重丢掉」，避免白抓详情页
    （实测一轮三源 12 次详情请求里，有 11 次抓回来直接被丢掉）。
    判定口径与 ``insert_news`` 完全一致：news_title_key + ±12h 窗口 + 相似度阈值。
    """

    def __init__(self, db: Session):
        self.urls, self._titles = load_dedup_index(db)

    def has_url(self, url: str) -> bool:
        return bool(url) and url in self.urls

    def is_duplicate_title(self, title: str, news_time: datetime | None) -> bool:
        key = news_title_key(title)
        moment = _as_aware(news_time)
        if not key or moment is None:
            return False
        if self._titles.has_similar(key, moment):
            return True
        # 本身就是旧文（发布时间比现在早一个窗口以上）时，再按「入库时刻」比一次：
        # 转载常常隔十几小时才被另一个来源收进来，两条发布时间差远超窗口，但
        # 其实是同一件事。正常时效的新闻不走这条，每日模板标题不会被互相压制。
        now = datetime.now(timezone.utc)
        window = timedelta(seconds=self._titles.window_seconds)
        return moment < now - window and self._titles.has_similar(key, now)

    def precheck(self, title: str, news_time: datetime | None) -> bool:
        """抓取层回调：返回 True 表示已有同一事件，可跳过详情请求。"""
        return self.is_duplicate_title(title, news_time)


def _insert_batch(
    db: Session,
    items: list[dict],
    *,
    snapshot: NewsDedupSnapshot | None = None,
) -> tuple[list[tuple[int, datetime]], int]:
    """批量入库；返回 (新入库的 (id, news_time) 列表, 回填条数)。"""
    accepted = [
        item
        for item in items
        if is_within_ingest_window(item.get("news_time")) and should_ingest_news(item)
    ]
    if not accepted:
        return [], 0
    created, skipped = insert_news_batch(db, accepted, snapshot=snapshot)
    enriched = 0
    for item in skipped:
        # 被判重跳过的条目：来源自带关联股票时回填到未分析的旧记录上。
        if enrich_existing_news(db, item):
            enriched += 1
    if created:
        invalidate_dedup_index(db)
    return created, enriched


# ---------------------------------------------------------------------------
# 抓取调度（各源实现见 news_sources；判重索引只读，可跨线程共享）
# ---------------------------------------------------------------------------


def _run_collectors(
    tasks: dict[str, Callable[[], list[dict]]],
    *,
    parallel: bool,
) -> dict[str, list[dict]]:
    """并行跑各源抓取；单源异常只影响自己，返回空列表。"""
    if not parallel or len(tasks) <= 1:
        return {name: _safe_collect(name, task) for name, task in tasks.items()}

    with ThreadPoolExecutor(
        max_workers=min(SOURCE_FETCH_WORKERS, len(tasks)),
        thread_name_prefix="news-fetch",
    ) as pool:
        futures = {name: pool.submit(_safe_collect, name, task) for name, task in tasks.items()}
        return {name: future.result() for name, future in futures.items()}


def _safe_collect(name: str, task: Callable[[], list[dict]]) -> list[dict]:
    try:
        return task() or []
    except Exception:
        logger.exception("新闻源抓取失败 source=%s", name)
        return []


# ---------------------------------------------------------------------------
# 入库（单 Session 串行写，避免 SQLite 多写者抢锁）
# ---------------------------------------------------------------------------


def ingest_news_sources(
    db: Session | None = None,
    *,
    parallel: bool = True,
    plan: list[tuple[NewsSourceSpec, dict]] | None = None,
) -> dict:
    """按 data_sources(type='event_news') 的开关与顺序采集一轮。

    参数：
    - ``plan``：显式指定源（测试/演练用）；为 None 时读数据库配置。

    返回：
    - ``sources``：``{provider: 新入库条数}``，键序即本轮入库顺序
    - ``enabled``：本轮启用的源（按 priority 升序）
    - ``created_ids``：新入库 id，**按 news_time 倒序**（最新先进分析队列）

    入库顺序 = priority 顺序：同一事件被多源同时报到时，先入库的那条留下，
    后到的因共享判重快照被跳过，所以排在最前的源等于「默认代表来源」。
    """
    own = db is None
    session = db or SessionLocal()
    try:
        plan = plan if plan is not None else load_news_sources(session)
        if not plan:
            logger.info("事件驱动采集：没有启用的新闻源，本轮跳过")
            return {"sources": {}, "enabled": [], "created_ids": []}

        order = [spec.key for spec, _ in plan]
        dedup = IngestDedupIndex(session)
        groups = _run_collectors(
            {
                spec.key: (lambda spec=spec, cfg=cfg: spec.collect(dedup, cfg))
                for spec, cfg in plan
            },
            parallel=parallel,
        )

        prepared = [
            prepare_news_items(
                [
                    item
                    for item in (groups.get(key) or [])
                    if is_within_ingest_window(item.get("news_time"))
                    and should_ingest_news(item)
                ]
            )
            for key in order
        ]
        pooled = [rows for rows in prepared if rows]
        snapshot = build_dedup_snapshot(session, pooled) if pooled else None

        counts: dict[str, int] = {}
        created: list[tuple[int, datetime]] = []
        for key in order:
            rows, _ = _insert_batch(session, groups.get(key) or [], snapshot=snapshot)
            counts[key] = len(rows)
            created.extend(rows)
        created.sort(key=lambda pair: pair[1], reverse=True)

        total = len(created)
        if total:
            logger.info(
                "事件驱动采集完成 %s 条：%s",
                total,
                " / ".join(f"{key}={counts[key]}" for key in order),
            )
        return {
            "sources": counts,
            "enabled": order,
            "created_ids": [news_id for news_id, _ in created],
        }
    finally:
        if own:
            session.close()
