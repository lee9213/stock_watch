"""事件驱动新闻 / 速览的落库与检索。"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import String, and_, cast, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.platform.persistence.models import MarketNews, MarketNewsDiscard, NewsDigest
from src.platform.persistence.news_keys import has_similar_title, news_title_key

logger = logging.getLogger(__name__)

ALL_NEWS_TOPIC = "全部新闻"
FIXED_TOPICS = [
    ALL_NEWS_TOPIC,
    "特朗普",
    "马斯克",
    "美联储",
    "央行",
    "证监会",
    "货币政策",
    "业绩披露",
    "商业航天",
    "半导体",
    "机器人",
    "CPO",
    "PCB",
    "芯片",
    "创新药",
    "黄金",
]
TOPIC_ALIASES = {
    "特朗普": ["特朗普", "trump"],
    "马斯克": ["马斯克", "musk"],
    "美联储": ["美联储", "federal reserve", "fomc"],
    "央行": ["央行", "降准"],
    "证监会": ["证监会"],
    "货币政策": ["货币政策", "降息", "加息", "降准"],
    "业绩披露": ["业绩披露", "三季报", "半年报", "年报", "季报", "业绩预告", "业绩快报"],
    "商业航天": ["商业航天", "航天", "卫星发射"],
    "半导体": ["半导体", "芯片", "晶圆", "集成电路", "光刻", "封测", "存储芯片"],
    "机器人": ["机器人", "人形机器人", "具身智能"],
    "CPO": ["CPO", "光电共封装", "光模块"],
    "PCB": ["PCB", "覆铜板", "印刷电路"],
    "芯片": ["芯片", "半导体", "gpu", "存储芯片"],
    "创新药": ["创新药", "新药", "临床试验", "glp-1"],
    "黄金": ["黄金", "金价", "黄金etf"],
}
MAX_PAGE_SIZE = 200
REFRESH_MINUTE = 30
REFRESH_HOUR_START = 7
REFRESH_HOUR_END = 23
KEEP_DAYS = 30
INGEST_MAX_AGE_HOURS = 24
# 丢弃记录要比采集窗口长，否则窗口内的同一条会被再次入库。
DISCARD_KEEP_HOURS = INGEST_MAX_AGE_HOURS * 2
# 相似标题只在发布时间相近时视为同一条；每天重复出现的模板标题（如「A股开盘」）相隔 24 小时，不受影响。
TITLE_DUPLICATE_WINDOW_HOURS = 12
MAX_ANALYZE_FAILS = 3
# |bullish_level| 低于此值视为中性：模型常用 ±1 表达「中性偏正面 / 影响有限」。
MIN_SIGNAL_STRENGTH = 2
DYNAMIC_TOPIC_HOURS = 24
DYNAMIC_TOPIC_LIMIT = 8
DYNAMIC_TOPIC_MIN_COUNT = 2
_SOURCE_TOPIC_SKIP = {
    "同花顺",
    "华尔街见闻",
    "证券之星",
    "财联社",
    "ths",
    "wallstreet",
    "stock_star",
    "stockstar",
}
# 市场名、情绪词等泛化标签，不能当话题。
_GENERIC_TOPIC_SKIP = {
    "a股",
    "港股",
    "美股",
    "股市",
    "大盘",
    "个股",
    "上市公司",
    "公告",
    "研报",
    "快讯",
    "利好",
    "利空",
    "股价上涨",
    "股价下跌",
    "市场传闻",
    "市场情绪",
}
# 含这些字的标签都属于「业绩披露」，不再单列。
_EARNINGS_TOPIC_MARKERS = ("业绩", "净利", "亏损", "营收", "盈利", "扣非", "财报", "季报", "年报", "增收")
SHANGHAI = ZoneInfo("Asia/Shanghai")
_LIKE_ESCAPE = str.maketrans({"\\": "\\\\", "%": "\\%", "_": "\\_"})


def topic_items(db: Session | None = None) -> list[dict[str, str]]:
    items = [{"key": topic, "label": topic} for topic in FIXED_TOPICS]
    if db is None:
        return items
    seen = {item["key"] for item in items}
    for topic in _recent_dynamic_topics(db):
        if topic in seen:
            continue
        seen.add(topic)
        items.append({"key": topic, "label": topic})
    return items


def _fixed_topic_terms() -> set[str]:
    terms = {topic.lower() for topic in FIXED_TOPICS}
    for aliases in TOPIC_ALIASES.values():
        terms.update(alias.lower() for alias in aliases)
    return terms


def _is_dynamic_topic_candidate(tag: str, fixed_terms: set[str]) -> bool:
    """排除固定话题及其别名、来源名、泛化词和业绩类变体。"""
    lowered = tag.lower()
    if len(tag) < 2 or len(tag) > 12:
        return False
    if lowered in fixed_terms or tag in _SOURCE_TOPIC_SKIP or lowered in _GENERIC_TOPIC_SKIP:
        return False
    return not any(marker in tag for marker in _EARNINGS_TOPIC_MARKERS)


def is_weak_signal(bullish_level: int | None) -> bool:
    return abs(int(bullish_level or 0)) < MIN_SIGNAL_STRENGTH


def visible_news_clause():
    """兜底：已分析但利多利空不足 MIN_SIGNAL_STRENGTH 的新闻不进列表、话题和速览；正常流程中这类新闻分析后即删除。"""
    return or_(
        MarketNews.analyzed == 0,
        func.abs(func.coalesce(MarketNews.bullish_level, 0)) >= MIN_SIGNAL_STRENGTH,
    )


def _recent_dynamic_topics(db: Session) -> list[str]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=DYNAMIC_TOPIC_HOURS)
    rows = (
        db.query(MarketNews.tags)
        .filter(MarketNews.news_time >= cutoff, visible_news_clause())
        .all()
    )
    fixed_terms = _fixed_topic_terms()
    counts: Counter[str] = Counter()
    for (tags,) in rows:
        for raw in tags or []:
            text = str(raw).strip()
            if _is_dynamic_topic_candidate(text, fixed_terms):
                counts[text] += 1
    return [
        tag
        for tag, count in counts.most_common()
        if count >= DYNAMIC_TOPIC_MIN_COUNT
    ][:DYNAMIC_TOPIC_LIMIT]


def topic_terms(keyword: str) -> list[str]:
    text = (keyword or "").strip()
    if not text or text == ALL_NEWS_TOPIC:
        return []
    return list(dict.fromkeys(TOPIC_ALIASES.get(text, [text])))


def topic_blob(digest: str = "", tags: list | None = None, stocks: list | None = None) -> str:
    parts = [digest or ""]
    for tag in tags or []:
        parts.append(str(tag))
    for stock in merge_stocks(stocks):
        parts.append(stock.get("code") or "")
        parts.append(stock.get("name") or "")
    return " ".join(parts)


def match_topics(text: str) -> list[str]:
    blob = (text or "").lower()
    matched: list[str] = []
    for topic in FIXED_TOPICS:
        if topic == ALL_NEWS_TOPIC:
            continue
        if any(alias.lower() in blob for alias in topic_terms(topic)):
            matched.append(topic)
    return matched


def attach_topic_tags(digest: str = "", tags: list | None = None, stocks: list | None = None) -> list[str]:
    current: list[str] = []
    for tag in tags or []:
        text = str(tag).strip()
        if text and text not in current:
            current.append(text)
    for topic in match_topics(topic_blob(digest, current, stocks)):
        if topic not in current:
            current.append(topic)
    return current[:12]


def retag_news_row(row: MarketNews) -> bool:
    """只给当前行补话题标签，不扫全表。"""
    next_tags = attach_topic_tags(row.digest or "", row.tags, row.relations_stocks)
    if next_tags == (row.tags or []):
        return False
    row.tags = next_tags
    return True


def apply_topic_tags(db: Session, rows: list[MarketNews] | None = None) -> int:
    """只给指定行补话题标签；未传 rows 时不再扫全表。"""
    if not rows:
        return 0
    changed = 0
    for row in rows:
        if retag_news_row(row):
            changed += 1
    if changed:
        db.commit()
    return changed


def news_md5(digest: str) -> str:
    return hashlib.md5((digest or "").encode("utf-8")).hexdigest()


def _escape_like(value: str) -> str:
    return str(value or "").translate(_LIKE_ESCAPE)


def _like_fragments(term: str) -> list[str]:
    text = str(term or "").strip()
    if not text:
        return []
    escaped = json.dumps(text, ensure_ascii=True)[1:-1]
    return list(dict.fromkeys([text, escaped]))


def _wall_clock(moment: datetime | None) -> str:
    if moment is None:
        return ""
    local = moment.astimezone(SHANGHAI) if moment.tzinfo else moment.replace(tzinfo=timezone.utc).astimezone(SHANGHAI)
    return local.strftime("%Y-%m-%d %H:%M:%S")


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def is_within_ingest_window(
    news_time: datetime | None,
    *,
    now: datetime | None = None,
    max_age_hours: int = INGEST_MAX_AGE_HOURS,
) -> bool:
    """采集只收近 N 小时（默认 24h）的新闻；无时间戳视为过期。"""
    moment = _aware(news_time)
    if moment is None:
        return False
    current = _aware(now) or datetime.now(timezone.utc)
    cutoff = current - timedelta(hours=max(1, int(max_age_hours)))
    return moment >= cutoff


def analysis_status(row: MarketNews) -> str:
    """done=已分析；failed=失败次数用尽不再重试；pending=等待分析。"""
    if int(row.analyzed or 0):
        return "done"
    if int(getattr(row, "analyze_fail_count", 0) or 0) >= MAX_ANALYZE_FAILS:
        return "failed"
    return "pending"


def to_view(row: MarketNews) -> dict:
    stocks = merge_stocks(row.relations_stocks)
    analyzed = int(row.analyzed or 0)
    return {
        "id": str(row.id),
        "digest": row.digest or "",
        "raw_text": getattr(row, "raw_text", None) or "",
        "news_time": _aware(row.news_time).isoformat() if row.news_time else "",
        "url": row.url or "",
        "news_type": row.news_type or "news",
        "tags": row.tags or [],
        "relations_stocks": stocks,
        "bullish_level": int(row.bullish_level or 0),
        "bullish_level_reason": getattr(row, "bullish_level_reason", None) or "",
        "relation_level": int(row.relation_level or 0),
        "relation_reason": getattr(row, "relation_reason", None) or "",
        "analyzed": analyzed,
        "analysis_status": analysis_status(row),
        "analyze_fail_count": int(getattr(row, "analyze_fail_count", 0) or 0),
        "source": row.sources or "",
        "sources": row.sources or "",
    }


def merge_stocks(*groups: list | None) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for group in groups:
        for item in group or []:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip()
            name = str(item.get("name") or code).strip()
            if not code or code in seen:
                continue
            seen.add(code)
            rows.append({"code": code, "name": name})
            if len(rows) >= 10:
                return rows
    return rows


def enrich_existing_news(db: Session, item: dict) -> bool:
    """用来源自带的关联股票回填未分析的记录（按 url / 原文对齐）。

    已分析的行不再改：分析时已合并来源股票并截到上限，事后回填会把板块综述重新撑到 10 只。
    """
    stocks = merge_stocks(item.get("relations_stocks"))
    if not stocks:
        return False
    digest = (item.get("digest") or "").strip()
    url = (item.get("url") or "").strip()
    row = None
    if url:
        row = db.query(MarketNews).filter(MarketNews.url == url).first()
    if row is None and digest:
        row = (
            db.query(MarketNews)
            .filter(or_(MarketNews.raw_text == digest, MarketNews.digest == digest))
            .first()
        )
    if row is None or int(row.analyzed or 0):
        return False
    changed = False
    merged = merge_stocks(row.relations_stocks, stocks)
    if merged != (row.relations_stocks or []):
        row.relations_stocks = merged
        changed = True
    if merged and int(row.relation_level or 0) <= 0:
        row.relation_level = 3
        changed = True
    if retag_news_row(row):
        changed = True
    if changed:
        db.commit()
    return changed


def is_discarded(db: Session, digest_hash: str, url: str = "") -> bool:
    clauses = [MarketNewsDiscard.news_md5 == digest_hash]
    if url:
        clauses.append(MarketNewsDiscard.url == url)
    return db.query(MarketNewsDiscard.id).filter(or_(*clauses)).first() is not None


def is_title_duplicate(db: Session, key: str, news_time: datetime | None) -> bool:
    """发布时间前后 12 小时内已有相似标题的新闻或丢弃记录。"""
    moment = _aware(news_time)
    if not key or moment is None:
        return False
    window = timedelta(hours=TITLE_DUPLICATE_WINDOW_HOURS)
    start, end = moment - window, moment + window
    kept = (
        db.query(MarketNews.title_key)
        .filter(
            MarketNews.title_key != "",
            MarketNews.news_time >= start,
            MarketNews.news_time <= end,
        )
        .all()
    )
    dropped = (
        db.query(MarketNewsDiscard.title_key)
        .filter(
            MarketNewsDiscard.title_key != "",
            MarketNewsDiscard.news_time.isnot(None),
            MarketNewsDiscard.news_time >= start,
            MarketNewsDiscard.news_time <= end,
        )
        .all()
    )
    return has_similar_title(key, (row[0] for row in [*kept, *dropped]))


def discard_news_row(db: Session, row: MarketNews) -> None:
    """删除中性新闻并留去重键（md5 / 链接 / 标题）；由调用方 commit。"""
    exists = (
        db.query(MarketNewsDiscard.id)
        .filter(MarketNewsDiscard.news_md5 == row.news_md5)
        .first()
    )
    if exists is None:
        db.add(MarketNewsDiscard(
            news_md5=row.news_md5,
            url=row.url or "",
            title_key=row.title_key or news_title_key(row.raw_text or row.digest),
            news_time=row.news_time,
        ))
    db.delete(row)


def prune_discards(db: Session, keep_hours: int = DISCARD_KEEP_HOURS) -> int:
    deadline = datetime.now(timezone.utc) - timedelta(hours=max(1, int(keep_hours)))
    deleted = (
        db.query(MarketNewsDiscard)
        .filter(MarketNewsDiscard.discarded_at < deadline.replace(tzinfo=None))
        .delete(synchronize_session=False)
    )
    db.commit()
    return int(deleted or 0)


def insert_news(db: Session, item: dict) -> MarketNews | None:
    digest = (item.get("digest") or "").strip()
    if not digest:
        return None
    digest_hash = item.get("news_md5") or news_md5(digest)
    if db.query(MarketNews).filter(MarketNews.news_md5 == digest_hash).first():
        return None
    url = (item.get("url") or "").strip()
    if url and db.query(MarketNews).filter(MarketNews.url == url).first():
        return None
    if is_discarded(db, digest_hash, url):
        return None
    news_time = _aware(item.get("news_time")) or datetime.now(timezone.utc)
    title_key = news_title_key(digest)
    if is_title_duplicate(db, title_key, news_time):
        return None
    stocks = merge_stocks(item.get("relations_stocks"))
    row = MarketNews(
        digest=digest,
        raw_text=digest,
        title_key=title_key,
        tags=attach_topic_tags(digest, item.get("tags") or [], stocks),
        relation_level=int(item.get("relation_level") if item.get("relation_level") is not None else (3 if stocks else 0)),
        bullish_level=int(item.get("bullish_level") or 0),
        relations_stocks=stocks,
        news_time=news_time,
        news_type=item.get("news_type") or "news",
        news_md5=digest_hash,
        sources=item.get("sources") or "ths",
        url=url,
        analyzed=int(item.get("analyzed") or 0),
    )
    db.add(row)
    try:
        db.commit()
        db.refresh(row)
        return row
    except IntegrityError:
        db.rollback()
        return None


def search_news(
    db: Session,
    *,
    keyword: str = "",
    page: int = 1,
    page_size: int = 20,
    relation_level_only: bool = False,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
) -> dict:
    safe_page = max(int(page or 1), 1)
    safe_size = min(max(int(page_size or 20), 1), MAX_PAGE_SIZE)
    query = db.query(MarketNews).filter(visible_news_clause())
    if start_time:
        query = query.filter(MarketNews.news_time >= _aware(start_time))
    if end_time:
        query = query.filter(MarketNews.news_time <= _aware(end_time))
    text = (keyword or "").strip()
    terms = topic_terms(text)
    if terms:
        clauses = []
        for term in terms:
            for fragment in _like_fragments(term):
                pattern = f"%{_escape_like(fragment)}%"
                clauses.append(MarketNews.digest.like(pattern, escape="\\"))
                clauses.append(MarketNews.raw_text.like(pattern, escape="\\"))
                clauses.append(cast(MarketNews.tags, String).like(pattern, escape="\\"))
        query = query.filter(or_(*clauses))
    if relation_level_only:
        query = query.filter(MarketNews.relation_level > 0)
    total = query.count()
    rows = (
        query.order_by(MarketNews.news_time.desc(), MarketNews.id.desc())
        .offset((safe_page - 1) * safe_size)
        .limit(safe_size)
        .all()
    )
    return {
        "items": [to_view(row) for row in rows],
        "total": total,
        "page": safe_page,
        "page_size": safe_size,
        "has_more": safe_page * safe_size < total,
    }


def list_news_in_window(
    db: Session,
    start: datetime,
    end: datetime,
    limit: int = 200,
    *,
    analyzed: int | None = None,
) -> list[dict]:
    query = db.query(MarketNews).filter(
        and_(MarketNews.news_time >= _aware(start), MarketNews.news_time <= _aware(end))
    )
    if analyzed is not None:
        query = query.filter(MarketNews.analyzed == int(analyzed))
    rows = (
        query.order_by(MarketNews.news_time.desc(), MarketNews.id.desc())
        .limit(limit)
        .all()
    )
    return [to_view(row) for row in rows]


def list_unanalyzed(
    db: Session,
    limit: int = 8,
    *,
    older_than: datetime | None = None,
) -> list[MarketNews]:
    """列出未分析新闻。

    older_than：若传入，只返回 created_at 不晚于该时刻的记录
    （补扫用，避免与「本轮刚入库直入队」抢同一批新新闻）。
    """
    query = db.query(MarketNews).filter(
        MarketNews.analyzed == 0,
        or_(
            MarketNews.analyze_fail_count.is_(None),
            MarketNews.analyze_fail_count < MAX_ANALYZE_FAILS,
        ),
    )
    if older_than is not None:
        cutoff = older_than
        if cutoff.tzinfo is not None:
            cutoff = cutoff.astimezone(timezone.utc).replace(tzinfo=None)
        query = query.filter(MarketNews.created_at.isnot(None), MarketNews.created_at <= cutoff)
    return (
        query.order_by(MarketNews.news_time.desc())
        .limit(limit)
        .all()
    )


def save_digest(
    db: Session,
    *,
    headlines: list[dict],
    highlights: list[str],
    title: str,
    window_start: datetime,
    window_end: datetime,
    news_count: int,
    model: str | None,
    trigger_type: str,
    generated_at: datetime | None = None,
    input_hash: str = "",
) -> dict | None:
    if not headlines:
        return None
    now = generated_at or datetime.now(timezone.utc)
    row = NewsDigest(
        generated_at=_aware(now),
        window_start=_aware(window_start),
        window_end=_aware(window_end),
        news_count=int(news_count or 0),
        title=title or "新闻流速览",
        headlines=headlines[:3],
        highlights=highlights or [],
        model=model or "",
        trigger_type=trigger_type,
        input_hash=input_hash or "",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return digest_view(row)


def digest_view(row: NewsDigest | None) -> dict | None:
    if row is None:
        return None
    return {
        "id": row.id,
        "title": row.title or "新闻流速览",
        "headlines": row.headlines or [],
        "highlights": row.highlights or [],
        "news_count": row.news_count or 0,
        "generated_at": _wall_clock(_aware(row.generated_at)),
        "window_start": _wall_clock(_aware(row.window_start)),
        "window_end": _wall_clock(_aware(row.window_end)),
        "model": row.model or None,
        "trigger_type": row.trigger_type or "auto",
        "input_hash": getattr(row, "input_hash", None) or "",
    }


def latest_digest(db: Session) -> dict | None:
    row = db.query(NewsDigest).order_by(NewsDigest.generated_at.desc(), NewsDigest.id.desc()).first()
    return digest_view(row)


def latest_digest_input_hash(db: Session) -> str:
    row = db.query(NewsDigest).order_by(NewsDigest.generated_at.desc(), NewsDigest.id.desc()).first()
    if row is None:
        return ""
    return str(getattr(row, "input_hash", None) or "")


def recent_digests(db: Session, limit: int = 24) -> list[dict]:
    rows = (
        db.query(NewsDigest)
        .order_by(NewsDigest.generated_at.desc(), NewsDigest.id.desc())
        .limit(max(1, min(int(limit or 24), 200)))
        .all()
    )
    return [digest_view(row) for row in rows if row]


def prune_digests(db: Session, keep_days: int = KEEP_DAYS) -> int:
    deadline = datetime.now(timezone.utc) - timedelta(days=max(1, int(keep_days)))
    deleted = db.query(NewsDigest).filter(NewsDigest.generated_at < deadline).delete(synchronize_session=False)
    db.commit()
    return int(deleted or 0)


def prune_market_news(db: Session, keep_days: int = KEEP_DAYS) -> int:
    """事件驱动新闻只保留近 N 天（默认 30），与速览同一窗口。"""
    deadline = datetime.now(timezone.utc) - timedelta(days=max(1, int(keep_days)))
    deleted = db.query(MarketNews).filter(MarketNews.news_time < deadline).delete(synchronize_session=False)
    db.commit()
    return int(deleted or 0)


def next_refresh_at(now: datetime | None = None) -> datetime:
    current = now.astimezone(SHANGHAI) if now and now.tzinfo else (now or datetime.now(SHANGHAI))
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI)
    candidate = current.replace(minute=REFRESH_MINUTE, second=0, microsecond=0)
    if candidate <= current:
        candidate += timedelta(hours=1)
    if candidate.hour > REFRESH_HOUR_END:
        candidate = (candidate + timedelta(days=1)).replace(hour=REFRESH_HOUR_START)
    elif candidate.hour < REFRESH_HOUR_START:
        candidate = candidate.replace(hour=REFRESH_HOUR_START)
    return candidate
