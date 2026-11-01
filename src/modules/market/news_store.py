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
from src.platform.persistence.news_keys import (
    CONTENT_SIGNATURE_LENGTH,
    ContentSignature,
    TitleIndex,
    content_signature,
    contents_duplicate,
    has_similar_title,
    news_title_key,
)

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
    """已有相似标题的新闻或丢弃记录（发布时间或入库时间落在 ±12h 窗口内）。

    只看发布时间会漏判转载：同一事件被别家隔十几小时重发时，两条的 news_time
    相差远超窗口，但它们其实是同一时刻被我们收进来的。所以窗口同时按
    「入库时间」开一次，判定时也按「本次入库时刻」再比一次。
    """
    moment = _aware(news_time)
    if not key or moment is None:
        return False
    now = datetime.now(timezone.utc)
    window = timedelta(hours=TITLE_DUPLICATE_WINDOW_HOURS)
    stale = moment < now - window
    start, end = moment - window, moment + window
    ingested_window = (now - window, now + window)
    time_clause = (
        or_(
            and_(MarketNews.news_time >= start, MarketNews.news_time <= end),
            and_(
                MarketNews.created_at >= ingested_window[0],
                MarketNews.created_at <= ingested_window[1],
            ),
        )
        if stale
        else and_(MarketNews.news_time >= start, MarketNews.news_time <= end)
    )
    kept = (
        db.query(MarketNews.title_key)
        .filter(MarketNews.title_key != "", time_clause)
        .all()
    )
    dropped_clause = (
        or_(
            and_(
                MarketNewsDiscard.news_time >= start,
                MarketNewsDiscard.news_time <= end,
            ),
            and_(
                MarketNewsDiscard.discarded_at >= ingested_window[0],
                MarketNewsDiscard.discarded_at <= ingested_window[1],
            ),
        )
        if stale
        else and_(
            MarketNewsDiscard.news_time >= start,
            MarketNewsDiscard.news_time <= end,
        )
    )
    dropped = (
        db.query(MarketNewsDiscard.title_key)
        .filter(
            MarketNewsDiscard.title_key != "",
            MarketNewsDiscard.news_time.isnot(None),
            dropped_clause,
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


def _news_row(*, item: dict, digest: str, digest_hash: str, url: str, news_time: datetime) -> MarketNews:
    """构造待写入的新闻行；单条与批量入库共用，避免两边默认值漂移。"""
    stocks = merge_stocks(item.get("relations_stocks"))
    relation_level = item.get("relation_level")
    return MarketNews(
        digest=digest,
        # 快讯源没有独立详情页，raw_text 直接等于正文；抓详情页的源可不传。
        raw_text=item.get("raw_text") or digest,
        title_key=news_title_key(digest),
        tags=attach_topic_tags(digest, item.get("tags") or [], stocks),
        relation_level=int(relation_level if relation_level is not None else (3 if stocks else 0)),
        bullish_level=int(item.get("bullish_level") or 0),
        relations_stocks=stocks,
        news_time=news_time,
        news_type=item.get("news_type") or "news",
        news_md5=digest_hash,
        sources=item.get("sources") or "ths",
        url=url,
        analyzed=int(item.get("analyzed") or 0),
    )


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
    row = _news_row(
        item=item, digest=digest, digest_hash=digest_hash, url=url, news_time=news_time
    )
    db.add(row)
    try:
        db.commit()
        db.refresh(row)
        return row
    except IntegrityError:
        db.rollback()
        return None


# 单条 SQL 的 IN 参数上限（SQLite 变量上限 999，留余量）。
_SQL_CHUNK_SIZE = 400


def _chunked(values: list[str], size: int = _SQL_CHUNK_SIZE) -> list[list[str]]:
    return [values[start:start + size] for start in range(0, len(values), size)]


def _unique(values) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


class NewsDedupSnapshot:
    """一次批量入库需要的去重状态，替代逐条 5 次查库。

    标题键按「发布时间」和「入库时间」两个时间锚分别入桶：转载 / 旧文重发会让
    两者相差十几小时，只按发布时间开窗时，同一事件换个来源再进来会落在窗口外。
    """

    __slots__ = ("_now", "_window", "md5s", "titles", "urls", "_contents")

    def __init__(
        self,
        *,
        window_hours: int = TITLE_DUPLICATE_WINDOW_HOURS,
        now: datetime | None = None,
    ):
        self.md5s: set[str] = set()
        self.urls: set[str] = set()
        self.titles = TitleIndex(window_hours)
        self._now = _aware(now) or datetime.now(timezone.utc)
        self._window = timedelta(hours=max(1, int(window_hours)))
        # 正文指纹：标题判重只能拦住「首句几乎一样」的转载，拦不住同一事件被
        # 各家改写成完全不同的标题（见 news_keys.contents_duplicate 的说明）。
        self._contents: list[ContentSignature] = []

    def add(self, md5: str, url: str, title_key: str, moment: datetime | None, digest: str = "") -> None:
        if md5:
            self.md5s.add(md5)
        if url:
            self.urls.add(url)
        if title_key:
            self.titles.add_anchors(title_key, moment, self._now)
        self.add_content(digest, moment)

    def add_content(self, digest: str, moment: datetime | None) -> None:
        """记录一条正文指纹，供后续候选做「同事件改写」判重。"""
        if not digest:
            return
        signature = content_signature(digest)
        if signature.grams:
            self._contents.append(signature)

    def blocked(
        self,
        md5: str,
        url: str,
        title_key: str,
        moment: datetime | None,
        digest: str = "",
    ) -> bool:
        """与 insert_news 的判重口径一致：md5 / 链接 / ±12h 相似标题 / 正文相似。

        候选本身就是旧文（发布时间比现在早一个窗口以上）时，再按「入库时刻」
        比一次：转载常常隔十几小时才被另一个来源收进来，两条的发布时间差远超
        窗口，但它们其实是同一件事。
        """
        if md5 and md5 in self.md5s:
            return True
        if url and url in self.urls:
            return True
        if self.titles.has_similar(title_key, moment):
            return True
        if (
            title_key
            and moment is not None
            and moment < self._now - self._window
            and self.titles.has_similar(title_key, self._now)
        ):
            return True
        return self.blocked_by_content(digest)

    def blocked_by_content(self, digest: str) -> bool:
        """正文 2-gram 相似度判重（标题被改写的同一事件）。"""
        if not digest or not self._contents:
            return False
        signature = content_signature(digest)
        if not signature.grams:
            return False
        for other in self._contents:
            if contents_duplicate(signature, other):
                return True
        return False


def load_news_dedup_snapshot(
    db: Session,
    *,
    md5s: list[str],
    urls: list[str],
    start: datetime,
    end: datetime,
) -> NewsDedupSnapshot:
    """一次性预取 md5 / 链接 / 时段内标题键（含已丢弃记录）。"""
    snapshot = NewsDedupSnapshot()
    for chunk in _chunked(_unique(md5s)):
        for model in (MarketNews, MarketNewsDiscard):
            rows = db.query(model.news_md5).filter(model.news_md5.in_(chunk)).all()
            snapshot.md5s.update(str(row[0]) for row in rows if row[0])
    for chunk in _chunked(_unique(urls)):
        for model in (MarketNews, MarketNewsDiscard):
            rows = db.query(model.url).filter(model.url.in_(chunk)).all()
            snapshot.urls.update(str(row[0]) for row in rows if row[0])
    now = datetime.now(timezone.utc)
    window = timedelta(hours=TITLE_DUPLICATE_WINDOW_HOURS)
    in_window = or_(
        and_(MarketNews.news_time >= start, MarketNews.news_time <= end),
        and_(MarketNews.created_at >= now - window, MarketNews.created_at <= now + window),
    )
    # 正文只取前 CONTENT_SIGNATURE_LENGTH 字：事件要素都在开头，全取会白读
    # 几十 KB 文本、也会稀释相似度。
    rows = (
        db.query(
            MarketNews.title_key,
            MarketNews.news_time,
            MarketNews.created_at,
            func.substr(MarketNews.digest, 1, CONTENT_SIGNATURE_LENGTH),
        )
        .filter(MarketNews.title_key != "", MarketNews.news_time.isnot(None), in_window)
        .all()
    )
    for key, moment, ingested_at, digest in rows:
        aware = _aware(moment)
        if aware is None:
            continue
        if key:
            snapshot.titles.add_anchors(str(key), aware, _aware(ingested_at) or now)
        if digest:
            snapshot.add_content(str(digest), aware)

    # 已丢弃的记录没有 digest 列，只能按标题键参与判重（同名标题不会再进来）。
    discarded = (
        db.query(MarketNewsDiscard.title_key, MarketNewsDiscard.news_time, MarketNewsDiscard.discarded_at)
        .filter(
            MarketNewsDiscard.title_key != "",
            MarketNewsDiscard.news_time.isnot(None),
            or_(
                and_(MarketNewsDiscard.news_time >= start, MarketNewsDiscard.news_time <= end),
                and_(
                    MarketNewsDiscard.discarded_at >= now - window,
                    MarketNewsDiscard.discarded_at <= now + window,
                ),
            ),
        )
        .all()
    )
    for key, moment, ingested_at in discarded:
        aware = _aware(moment)
        if aware is None or not key:
            continue
        snapshot.titles.add_anchors(str(key), aware, _aware(ingested_at) or now)
    return snapshot


def prepare_news_items(
    items: list[dict],
) -> list[tuple[dict, str, str, str, datetime]]:
    """把原始条目整理成 (item, digest, md5, url, news_time)。

    与 ``insert_news_batch`` 分开，是为了让一轮采集可以先把多个来源的条目
    一起整理、只建一次判重快照（否则每个来源各建一次，SQL 白白多一倍）。
    """
    prepared: list[tuple[dict, str, str, str, datetime]] = []
    for item in items:
        digest = (item.get("digest") or "").strip()
        if not digest:
            continue
        prepared.append(
            (
                item,
                digest,
                item.get("news_md5") or news_md5(digest),
                (item.get("url") or "").strip(),
                _aware(item.get("news_time")) or datetime.now(timezone.utc),
            )
        )
    return prepared


def build_dedup_snapshot(
    db: Session, prepared_groups: list[list[tuple[dict, str, str, str, datetime]]]
) -> NewsDedupSnapshot:
    """按多组已整理条目建一份共用的判重快照。"""
    prepared = [row for group in prepared_groups for row in group]
    window = timedelta(hours=TITLE_DUPLICATE_WINDOW_HOURS)
    moments = [row[4] for row in prepared]
    return load_news_dedup_snapshot(
        db,
        md5s=[row[2] for row in prepared],
        urls=[row[3] for row in prepared],
        start=min(moments) - window,
        end=max(moments) + window,
    )


def insert_prepared_news(
    db: Session,
    prepared: list[tuple[dict, str, str, str, datetime]],
    snapshot: NewsDedupSnapshot,
) -> tuple[list[tuple[int, datetime]], list[dict]]:
    """用给定快照落库一组已整理条目；返回 (新入库, 被判重跳过)。"""
    if not prepared:
        return [], []
    rows: list[MarketNews] = []
    stamps: list[datetime] = []
    skipped: list[dict] = []
    for item, digest, digest_hash, url, news_time in prepared:
        title_key = news_title_key(digest)
        if snapshot.blocked(digest_hash, url, title_key, news_time, digest):
            skipped.append(item)
            continue
        rows.append(
            _news_row(
                item=item, digest=digest, digest_hash=digest_hash, url=url, news_time=news_time
            )
        )
        stamps.append(news_time)
        # 同批内重复：后到的直接判重，不必等 flush 撞唯一索引。
        snapshot.add(digest_hash, url, title_key, news_time, digest)
    if not rows:
        return [], skipped

    db.add_all(rows)
    try:
        db.flush()
        created = [(int(row.id), moment) for row, moment in zip(rows, stamps)]
        db.commit()
        return created, skipped
    except IntegrityError:
        # 并发写入撞唯一索引（另一进程刚插入同一条）：整批回滚后逐条重试。
        db.rollback()
        created: list[tuple[int, datetime]] = []
        for item in [row[0] for row in prepared]:
            row = insert_news(db, item)
            if row is not None and row.id is not None:
                created.append((int(row.id), _aware(row.news_time) or datetime.now(timezone.utc)))
        return created, []


def insert_news_batch(
    db: Session,
    items: list[dict],
    *,
    snapshot: NewsDedupSnapshot | None = None,
) -> tuple[list[tuple[int, datetime]], list[dict]]:
    """批量入库：一次预取去重状态、一次提交。

    返回 ``(新入库的 (id, news_time) 列表, 被判重跳过的条目)``。
    调用方按 news_time 排序，就能让最新的一条先进分析队列。

    ``snapshot``：一轮采集抓了多个来源时，可由 ``build_dedup_snapshot``
    统一建一份传进来复用，避免每个来源各扫一遍标题窗口。
    """
    prepared = prepare_news_items(items)
    if not prepared:
        return [], []
    if snapshot is None:
        snapshot = build_dedup_snapshot(db, [prepared])
    return insert_prepared_news(db, prepared, snapshot)


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


def count_pending_analysis(db: Session) -> int:
    """待分析条数（不含失败次数用尽的），用于监控面板显示积压。"""
    return int(
        db.query(func.count(MarketNews.id))
        .filter(
            MarketNews.analyzed == 0,
            or_(
                MarketNews.analyze_fail_count.is_(None),
                MarketNews.analyze_fail_count < MAX_ANALYZE_FAILS,
            ),
        )
        .scalar()
        or 0
    )


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
