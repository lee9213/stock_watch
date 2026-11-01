"""批量入库、判重索引与「按市场阶段节流采集」的回归测试。"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.modules.market import news_flow_scheduler, news_store
from src.modules.market.news_flow_scheduler import (
    INGEST_INTERVAL_BREAK_SEC,
    INGEST_INTERVAL_NIGHT_SEC,
    INGEST_INTERVAL_NON_TRADING_SEC,
    NewsFlowScheduler,
    ingest_min_interval_sec,
    market_phase,
)
from src.modules.market.news_ingest import (
    IngestDedupIndex,
    ingest_news_sources,
    invalidate_dedup_index,
)
from src.modules.market.news_sources import NewsSourceSpec
from src.modules.market.news_store import insert_news, insert_news_batch
from src.platform.persistence.models import MarketNews, MarketNewsDiscard, NewsDigest
from src.platform.persistence.news_keys import TitleIndex

SH = ZoneInfo("Asia/Shanghai")


def _factory():
    engine = create_engine("sqlite://")
    MarketNews.__table__.create(engine)
    MarketNewsDiscard.__table__.create(engine)
    NewsDigest.__table__.create(engine)
    return sessionmaker(bind=engine)


def _fake_plan(groups: dict[str, list[dict]]) -> list[tuple[NewsSourceSpec, dict]]:
    """构造只含指定源与条目的采集计划：入库逻辑不联网，也不依赖 data_sources 表。"""
    return [
        (
            NewsSourceSpec(
                key=key,
                label=key,
                description="",
                config={},
                # 默认参数把当轮的条目绑死，避免闭包共享循环变量。
                fetch=lambda dedup, cfg, _items=items: list(_items),
            ),
            {},
        )
        for key, items in groups.items()
    ]


def _item(
    digest: str, *, minutes_ago: int = 5, md5: str = "", url: str = "") -> dict:
    return {
        "digest": digest,
        "news_time": datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        "news_md5": md5 or f"{abs(hash(digest)):032d}"[-32:],
        "url": url,
    }


# ---------------------------------------------------------------------------
# 批量入库：一次预取 + 一次提交，判重口径不变
# ---------------------------------------------------------------------------


def _count_commits(db) -> dict:
    counter = {"n": 0}
    original = db.commit

    def counting():
        counter["n"] += 1
        original()

    db.commit = counting
    return counter


def test_batch_insert_commits_once_and_dedupes_in_batch():
    factory = _factory()
    db = factory()
    commits = _count_commits(db)
    items = [
        _item("宁德时代业绩预增30%", md5="a" * 32),
        _item("宁德时代业绩预增30%", md5="a" * 32),  # 同批完全重复
        _item("比亚迪公告回购股份", md5="b" * 32, url="https://example.com/1"),
        _item("比亚迪公告回购股份(转载)", md5="c" * 32, url="https://example.com/1"),
        _item("央行宣布降准0.5个百分点", md5="d" * 32),
    ]
    created, skipped = insert_news_batch(db, items)

    assert len(created) == 3
    assert commits["n"] == 1
    assert len(skipped) == 2
    assert len({news_id for news_id, _ in created}) == 3
    assert db.query(MarketNews).count() == 3
    db.close()


def test_batch_insert_skips_rows_already_in_db():
    factory = _factory()
    db = factory()
    existing = insert_news(db, _item("宁德时代业绩预增", md5="e" * 32))
    assert existing is not None

    created, skipped = insert_news_batch(
        db,
        [
            _item("宁德时代业绩预增", md5="e" * 32),
            _item("央行宣布降准0.5个百分点", md5="f" * 32),
        ],
    )
    assert len(created) == 1
    assert len(skipped) == 1
    assert db.query(MarketNews).count() == 2
    db.close()


def test_batch_insert_title_window_matches_single_insert():
    """相似标题只在 ±12h 内算重复，与 insert_news 的口径一致。"""
    factory = _factory()
    db = factory()
    first = insert_news(db, _item("同花顺快讯：宁德时代业绩预增超预期", md5="1" * 32, minutes_ago=0))
    # 那条是当天早些时候收进来的：把入库时间也拨回去，否则 13h/24h 前的候选
    # 会被「转载」规则挡住（见 test_batch_insert_blocks_stale_reprint）。
    db.query(MarketNews).filter(MarketNews.id == first.id).update(
        {MarketNews.created_at: datetime.now(timezone.utc) - timedelta(days=1)}
    )
    db.commit()

    near, _ = insert_news_batch(db, [_item("宁德时代业绩预增超预期", md5="2" * 32, minutes_ago=600)])
    far, _ = insert_news_batch(db, [_item("宁德时代业绩预增超预期", md5="3" * 32, minutes_ago=13 * 60)])

    assert near == []  # 10 小时前：同一事件
    assert len(far) == 1  # 13 小时前：视为新事件
    db.close()


def test_batch_insert_blocks_stale_reprint():
    """发布时间比现在早一个窗口以上、却是刚被收进来的 → 转载，判重。"""
    factory = _factory()
    db = factory()
    insert_news(db, _item("利好来了！北京，重磅发布！事关千亿产业", md5="a" * 32, minutes_ago=30))
    created, skipped = insert_news_batch(
        db, [_item("利好来了！北京，重磅发布！事关氢能产业", md5="b" * 32, minutes_ago=19 * 60)]
    )
    assert created == []
    assert len(skipped) == 1
    assert db.query(MarketNews).count() == 1
    db.close()


# ---------------------------------------------------------------------------
# 判重索引：小时分桶后口径不变
# ---------------------------------------------------------------------------


def test_title_index_respects_half_open_window():
    index = TitleIndex(window_hours=12)
    now = datetime.now(timezone.utc)
    index.add("宁德时代业绩预增超预期", now - timedelta(hours=11))

    assert index.has_similar("宁德时代业绩预增超预期", now) is True
    assert index.has_similar("宁德时代业绩预增超预期", now + timedelta(hours=13)) is False
    assert len(index) == 1


def test_ingest_dedup_index_sees_rows_after_invalidation():
    factory = _factory()
    db = factory()
    insert_news(db, _item("央行宣布降准0.5个百分点", md5="9" * 32, url="https://example.com/9"))
    invalidate_dedup_index(db)

    index = IngestDedupIndex(db)
    assert index.has_url("https://example.com/9") is True
    # 判重口径与 insert_news 一致：同一标题同一天视为重复
    assert index.precheck("央行宣布降准0.5个百分点", datetime.now(timezone.utc)) is True
    assert index.precheck("证监会就再融资新规征求意见", datetime.now(timezone.utc)) is False
    db.close()


# ---------------------------------------------------------------------------
# 采集后入队顺序：按发布时间倒序
# ---------------------------------------------------------------------------


def test_ingest_sources_returns_ids_newest_first(monkeypatch):
    factory = _factory()
    db = factory()
    invalidate_dedup_index(db)

    plan = _fake_plan(
        {
            "ths": [_item("同花顺：某公司业绩预增", minutes_ago=30, md5="1" * 32)],
            "wallstreet": [_item("华尔街见闻：央行宣布降准", minutes_ago=5, md5="2" * 32)],
            "stock_star": [_item("证券之星：比亚迪公告回购", minutes_ago=20, md5="3" * 32)],
        }
    )

    result = ingest_news_sources(db, parallel=False, plan=plan)
    assert len(result["created_ids"]) == 3

    rows = db.query(MarketNews).all()
    order = {int(row.id): row.news_time for row in rows}
    stamps = [order[news_id] for news_id in result["created_ids"]]
    assert stamps == sorted(stamps, reverse=True), "最新的新闻必须先进分析队列"
    db.close()


def test_ingest_sources_builds_one_dedup_snapshot_for_all_sources(monkeypatch):
    """一轮采集只建一次判重快照，不再按来源各扫一遍标题窗口。"""
    factory = _factory()
    db = factory()
    invalidate_dedup_index(db)

    plan = _fake_plan(
        {
            "ths": [_item("同花顺：A公司中标大单", minutes_ago=10, md5="1" * 32)],
            "wallstreet": [_item("华尔街见闻：央行宣布降准", minutes_ago=5, md5="2" * 32)],
            "stock_star": [_item("证券之星：B公司公告回购", minutes_ago=20, md5="3" * 32)],
        }
    )

    calls = {"n": 0}
    original = news_store.load_news_dedup_snapshot

    def counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(news_store, "load_news_dedup_snapshot", counting)
    result = ingest_news_sources(db, parallel=False, plan=plan)

    assert calls["n"] == 1
    assert len(result["created_ids"]) == 3
    assert result["sources"] == {"ths": 1, "wallstreet": 1, "stock_star": 1}
    db.close()


def test_ingest_sources_dedups_across_sources_in_one_round(monkeypatch):
    """同一轮里两个来源发同一件事，后到的那个不再入库（共用快照要累积）。"""
    factory = _factory()
    db = factory()
    invalidate_dedup_index(db)

    # 入参顺序即入库顺序：ths 在前，同一事件的副本由 ths 入库，wallstreet 被跳过。
    plan = _fake_plan(
        {
            "ths": [_item("宁德时代业绩预增超预期", minutes_ago=10, md5="1" * 32)],
            "wallstreet": [_item("同花顺快讯：宁德时代业绩预增超预期", minutes_ago=12, md5="2" * 32)],
            "stockstar": [],
        }
    )

    result = ingest_news_sources(db, parallel=False, plan=plan)
    assert len(result["created_ids"]) == 1
    db.close()


# ---------------------------------------------------------------------------
# 采集节流：休市 / 午休 / 夜间降频
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phase,minutes",
    [
        ("trading", 0),
        ("break", INGEST_INTERVAL_BREAK_SEC // 60),
        ("non_trading", INGEST_INTERVAL_NON_TRADING_SEC // 60),
        ("unknown", 0),
    ],
)
def test_ingest_min_interval_by_phase(phase, minutes):
    daytime = datetime(2026, 11, 2, 14, 0, tzinfo=SH)
    assert ingest_min_interval_sec(phase, daytime) == minutes * 60


def test_ingest_min_interval_longer_at_night():
    night = datetime(2026, 11, 2, 3, 0, tzinfo=SH)
    evening = datetime(2026, 11, 2, 20, 0, tzinfo=SH)
    assert ingest_min_interval_sec("after_hours", night) == INGEST_INTERVAL_NIGHT_SEC
    assert ingest_min_interval_sec("after_hours", evening) == 0


def test_market_phase_uses_trading_calendar():
    assert market_phase(datetime(2026, 11, 1, 14, 0, tzinfo=SH)) == "non_trading"  # 周日
    assert market_phase(datetime(2026, 11, 2, 10, 0, tzinfo=SH)) == "trading"
    assert market_phase(datetime(2026, 11, 2, 12, 0, tzinfo=SH)) == "break"


def test_ingest_job_skips_until_interval_elapsed(monkeypatch):
    scheduler = NewsFlowScheduler()
    calls: list[str] = []

    async def fake_run(*, trigger_source: str = "schedule"):
        calls.append(trigger_source)
        return {}

    monkeypatch.setattr(scheduler, "_run_ingest", fake_run)
    monkeypatch.setattr(news_flow_scheduler, "market_phase", lambda now=None: "non_trading")

    scheduler._last_ingest_at = time.monotonic()  # 刚跑过 → 休市期应跳过
    asyncio.run(scheduler._ingest_job())
    assert calls == []

    scheduler._last_ingest_at = time.monotonic() - INGEST_INTERVAL_NON_TRADING_SEC - 1
    asyncio.run(scheduler._ingest_job())
    assert calls == ["schedule"]


def test_ingest_job_runs_every_tick_while_trading(monkeypatch):
    scheduler = NewsFlowScheduler()
    calls: list[str] = []

    async def fake_run(*, trigger_source: str = "schedule"):
        calls.append(trigger_source)
        return {}

    monkeypatch.setattr(scheduler, "_run_ingest", fake_run)
    monkeypatch.setattr(news_flow_scheduler, "market_phase", lambda now=None: "trading")
    scheduler._last_ingest_at = time.monotonic()
    asyncio.run(scheduler._ingest_job())
    assert calls == ["schedule"]


# ---------------------------------------------------------------------------
# 待分析积压索引
# ---------------------------------------------------------------------------


def test_pending_index_migration_creates_index():
    from src.platform.persistence.migrations import _m155_market_news_pending_index

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE market_news (id INTEGER PRIMARY KEY, analyzed INTEGER, news_time DATETIME)"
            )
        )
        _m155_market_news_pending_index(conn)
        _m155_market_news_pending_index(conn)  # 幂等
        names = {row[1] for row in conn.execute(text("PRAGMA index_list(market_news)")).fetchall()}
    assert "ix_market_news_pending" in names


def test_after_hours_ingest_default_schedule_matches_registration():
    """页面「恢复默认」不能把盘后采集从 15 分钟退化成 1 小时。

    代码在 ``NewsFlowScheduler.start()`` 里注册的是 ``minute="*/15",
    hour="15-23,0-8"``；调度目录里的默认表达式必须与之保持一致，否则用户点一次
    「恢复默认」，盘后/夜间采集频率就掉到四分之一，公告高峰当天的新闻要等一小时。
    """
    from src.platform.scheduling.job_manager import SYSTEM_JOB_CATALOG

    spec = next(s for s in SYSTEM_JOB_CATALOG if s.job_id == "news_flow_ingest_after_hours")
    assert spec.default_schedule == "*/15 15-23,0-8 * * *"
