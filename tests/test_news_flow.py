from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.modules.market.news_analysis import (
    _apply_analysis_result,
    build_analysis_prompt,
    mark_analyze_failure,
    normalize_analysis,
)
from src.modules.market.news_digest_job import (
    _select_for_prompt,
    collect_digest_items,
    digest_input_hash,
    normalize_digest_result,
)
from src.modules.market.news_store import (
    FIXED_TOPICS,
    MAX_ANALYZE_FAILS,
    apply_topic_tags,
    discard_news_row,
    enrich_existing_news,
    insert_news,
    is_weak_signal,
    latest_digest,
    latest_digest_input_hash,
    list_unanalyzed,
    news_md5,
    next_refresh_at,
    prune_discards,
    prune_market_news,
    save_digest,
    search_news,
    topic_items,
)
from src.modules.market.ths_news import normalize_ths_item
from src.platform.persistence.models import MarketNews, MarketNewsDiscard, NewsDigest

SH = ZoneInfo("Asia/Shanghai")


@pytest.fixture(autouse=True)
def _no_cross_round_linger(monkeypatch):
    """非交易时段会跨轮攒批 15-30 分钟，测试里一律关闭，避免用例挂住。"""
    from src.modules.market import news_flow_scheduler as nfs

    monkeypatch.setattr(nfs, "batch_linger_sec", lambda *a, **k: 0.0)


def _db():
    engine = create_engine("sqlite://")
    MarketNews.__table__.create(engine)
    MarketNewsDiscard.__table__.create(engine)
    NewsDigest.__table__.create(engine)
    return sessionmaker(bind=engine)()


def test_search_news_filters_digest_and_keeps_unrelated_on_all_topic():
    db = _db()
    insert_news(db, {
        "digest": "半导体设备招标扩大",
        "news_time": datetime(2026, 10, 26, 12, 0, tzinfo=SH),
        "relations_stocks": [{"code": "688981", "name": "中芯国际"}],
        "news_md5": "a" * 32,
    })
    insert_news(db, {
        "digest": "黄金上涨",
        "news_time": datetime(2026, 10, 26, 11, 0, tzinfo=SH),
        "news_md5": "b" * 32,
        "relation_level": 0,
    })
    page = search_news(db, keyword="半导体", page=1, page_size=20)
    assert page["total"] == 1
    assert page["items"][0]["digest"] == "半导体设备招标扩大"
    assert page["items"][0]["relations_stocks"] == [{"code": "688981", "name": "中芯国际"}]
    all_page = search_news(db, keyword="全部新闻", relation_level_only=False)
    assert all_page["total"] == 2


def test_search_news_matches_topic_aliases_and_tags():
    db = _db()
    insert_news(db, {
        "digest": "存储芯片板块走强",
        "news_time": datetime(2026, 10, 26, 12, 0, tzinfo=SH),
        "news_md5": "f" * 32,
    })
    insert_news(db, {
        "digest": "某公司发布三季报",
        "news_time": datetime(2026, 10, 26, 11, 0, tzinfo=SH),
        "news_md5": "g" * 32,
    })
    insert_news(db, {
        "digest": "海外宏观评论",
        "tags": ["美联储"],
        "news_time": datetime(2026, 10, 26, 10, 0, tzinfo=SH),
        "news_md5": "h" * 32,
    })
    assert search_news(db, keyword="半导体")["total"] == 1
    assert search_news(db, keyword="芯片")["total"] == 1
    assert search_news(db, keyword="业绩披露")["items"][0]["digest"] == "某公司发布三季报"
    assert search_news(db, keyword="美联储")["total"] == 1
    assert "半导体" in search_news(db, keyword="半导体")["items"][0]["tags"]


def test_next_refresh_at_follows_seven_to_twenty_three_thirty():
    assert next_refresh_at(datetime(2026, 10, 26, 10, 10, tzinfo=SH)).strftime("%H:%M") == "10:30"
    assert next_refresh_at(datetime(2026, 10, 26, 10, 40, tzinfo=SH)).strftime("%H:%M") == "11:30"
    nxt = next_refresh_at(datetime(2026, 10, 26, 23, 40, tzinfo=SH))
    assert nxt.strftime("%Y-%m-%d %H:%M") == "2026-10-27 07:30"
    assert next_refresh_at(datetime(2026, 10, 26, 3, 10, tzinfo=SH)).strftime("%H:%M") == "07:30"


def test_normalize_digest_result_keeps_only_usable_headlines():
    result = normalize_digest_result({
        "title": "新闻流速览",
        "headlines": [
            {"topic": "产业动向", "keywords": "半导体、创新药", "summary": "设备招标扩大"},
            {"topic": "", "keywords": [], "summary": ""},
        ],
        "highlights": ["国常会研究稳楼市"],
    })
    assert result["headlines"][0]["keywords"] == ["半导体", "创新药"]
    assert result["highlights"] == ["国常会研究稳楼市"]
    assert normalize_digest_result({"headlines": []}) is None


def test_normalize_ths_item_reads_stock_and_ctime():
    item = normalize_ths_item({
        "id": "99",
        "title": "汇川技术中标",
        "digest": "工业自动化订单增加",
        "ctime": 1761468000,
        "tag": "机器人,自动化",
        "url": "https://example.com/n",
        "stock": [{"code": "300124", "name": "汇川技术"}],
    })
    assert item["digest"] == "汇川技术中标。工业自动化订单增加"
    assert item["news_md5"] == news_md5("工业自动化订单增加")
    assert item["news_type"] == "news"
    assert item["relations_stocks"] == [{"code": "300124", "name": "汇川技术"}]
    assert "机器人" in item["tags"]
    assert item["url"] == "https://example.com/n"


def test_normalize_ths_item_skips_title_already_in_body():
    item = normalize_ths_item({
        "title": "央行降准",
        "digest": "央行降准0.5个百分点，释放长期资金",
        "ctime": 1761468000,
    })
    assert item["digest"] == "央行降准0.5个百分点，释放长期资金"


def test_normalize_ths_item_only_research_report_is_report():
    announcement = normalize_ths_item({
        "title": "南方泵业公告",
        "digest": "拟挂牌出售闲置资产",
        "ctime": 1761468000,
    })
    report = normalize_ths_item({
        "title": "券商研报",
        "digest": "上调某公司评级至买入",
        "ctime": 1761468000,
    })
    assert announcement["news_type"] == "news"
    assert report["news_type"] == "report"


def test_insert_news_keeps_raw_text_after_analysis():
    db = _db()
    row = insert_news(db, {
        "digest": "汇川技术中标。工业自动化订单增加",
        "news_time": datetime(2026, 10, 30, 10, 0, tzinfo=SH),
        "news_md5": "k" * 32,
    })
    assert row is not None
    assert row.raw_text == "汇川技术中标。工业自动化订单增加"
    result = normalize_analysis({
        "digest": "汇川技术获自动化大单",
        "tags": ["机器人"],
        "relation_level": 7,
        "bullish_level": 4,
        "relations_stocks": [{"code": "300124", "name": "汇川技术"}],
    }, row.digest)
    assert _apply_analysis_result(row, result) is True
    db.commit()
    db.refresh(row)
    assert row.digest == "汇川技术获自动化大单"
    assert row.raw_text == "汇川技术中标。工业自动化订单增加"
    item = search_news(db, keyword="订单增加")["items"][0]
    assert item["raw_text"] == "汇川技术中标。工业自动化订单增加"


def test_enrich_existing_news_skips_analyzed_rows():
    db = _db()
    row = insert_news(db, {
        "digest": "游戏板块持续走弱",
        "news_time": datetime(2026, 10, 30, 10, 0, tzinfo=SH),
        "news_md5": "j" * 32,
        "relations_stocks": [{"code": "002555", "name": "三七互娱"}],
    })
    assert row is not None
    row.digest = "模型改写后的板块综述"
    row.analyzed = 1
    db.commit()
    assert enrich_existing_news(db, {
        "digest": "游戏板块持续走弱",
        "relations_stocks": [
            {"code": "300315", "name": "掌趣科技"},
            {"code": "300052", "name": "中青宝"},
            {"code": "002624", "name": "完美世界"},
        ],
    }) is False
    db.refresh(row)
    assert row.relations_stocks == [{"code": "002555", "name": "三七互娱"}]


def test_normalize_ths_item_reads_stock_code_camel_case():
    item = normalize_ths_item({
        "title": "南方泵业：拟挂牌出售闲置资产",
        "digest": "南方泵业拟出售宜兴土地",
        "ctime": 1761468000,
        "stock": [{"name": "南方泵业", "stockCode": "300145", "stockMarket": "33"}],
        "url": "https://example.com/nanfang",
    })
    assert item["relations_stocks"] == [{"code": "300145", "name": "南方泵业"}]


def test_enrich_existing_news_fills_empty_stocks():
    db = _db()
    insert_news(db, {
        "digest": "南方泵业拟出售宜兴土地",
        "url": "https://example.com/nanfang",
        "news_time": datetime(2026, 10, 26, 12, 0, tzinfo=SH),
        "news_md5": "c" * 32,
    })
    assert enrich_existing_news(db, {
        "digest": "南方泵业拟出售宜兴土地",
        "url": "https://example.com/nanfang",
        "relations_stocks": [{"code": "300145", "name": "南方泵业"}],
    })
    page = search_news(db, keyword="南方泵业")
    assert page["items"][0]["relations_stocks"] == [{"code": "300145", "name": "南方泵业"}]
    assert page["items"][0]["relation_level"] == 3


def test_search_news_exposes_analysis_status():
    db = _db()
    insert_news(db, {
        "digest": "待分析宏观快讯",
        "news_time": datetime(2026, 10, 26, 12, 0, tzinfo=SH),
        "news_md5": "f" * 32,
        "analyzed": 0,
    })
    insert_news(db, {
        "digest": "已分析半导体快讯",
        "news_time": datetime(2026, 10, 26, 13, 0, tzinfo=SH),
        "news_md5": "a" * 32,
        "analyzed": 1,
        "bullish_level": 3,
        "relations_stocks": [{"code": "688981", "name": "中芯国际"}],
    })
    pending = search_news(db, keyword="待分析")["items"][0]
    done = search_news(db, keyword="已分析")["items"][0]
    assert pending["analyzed"] == 0
    assert pending["analysis_status"] == "pending"
    assert done["analyzed"] == 1
    assert done["analysis_status"] == "done"


def test_normalize_analysis_keeps_stocks_when_relation_level_missing():
    result = normalize_analysis({
        "digest": "南方泵业拟出售闲置资产",
        "tags": ["公告"],
        "relations_stocks": [{"code": "300145", "name": "南方泵业"}],
        "bullish_level": 4,
        "bullish_level_reason": "资产处置有望增厚现金",
        "relation_reason": "公告主体即为该公司",
    }, "南方泵业拟出售闲置资产")
    assert result["relations_stocks"] == [{"code": "300145", "name": "南方泵业"}]
    assert result["relation_level"] == 3
    assert result["bullish_level"] == 4
    assert "现金" in result["bullish_level_reason"]


def test_normalize_analysis_caps_fanout_and_relation():
    stocks = [
        {"code": "601398", "name": "工商银行"},
        {"code": "601288", "name": "农业银行"},
        {"code": "601939", "name": "建设银行"},
        {"code": "601988", "name": "中国银行"},
        {"code": "600036", "name": "招商银行"},
    ]
    result = normalize_analysis({
        "digest": "银行板块普涨",
        "tags": ["银行"],
        "relation_level": 8,
        "bullish_level": 5,
        "relations_stocks": stocks,
    }, "银行板块普涨")
    assert len(result["relations_stocks"]) == 3
    assert result["relation_level"] == 4
    assert result["relations_stocks"][0]["code"] == "601398"


def test_normalize_analysis_zeros_bullish_without_relation():
    result = normalize_analysis({
        "digest": "纯国际政治快讯",
        "tags": ["政治"],
        "relation_level": 0,
        "bullish_level": 5,
        "relations_stocks": [],
    }, "纯国际政治快讯")
    assert result["relation_level"] == 0
    assert result["bullish_level"] == 0
    assert result["relations_stocks"] == []


def test_apply_analysis_result_keeps_pending_when_parse_fails():
    row = MarketNews(digest="待分析", analyzed=0, relation_level=0, bullish_level=0)
    assert _apply_analysis_result(row, None) is False
    assert row.analyzed == 0


def test_apply_analysis_result_marks_analyzed_on_success():
    row = MarketNews(digest="原文", analyzed=0, relation_level=0, bullish_level=0)
    result = normalize_analysis({
        "digest": "半导体设备招标扩大",
        "tags": ["半导体"],
        "relation_level": 6,
        "bullish_level": 3,
        "bullish_level_reason": "招标扩大利好设备商",
        "relation_reason": "点名中芯国际产业链",
        "relations_stocks": [{"code": "688981", "name": "中芯国际"}],
    }, "原文")
    assert _apply_analysis_result(row, result) is True
    assert row.analyzed == 1
    assert row.digest == "半导体设备招标扩大"
    assert row.bullish_level_reason == "招标扩大利好设备商"
    assert row.relation_reason == "点名中芯国际产业链"
    assert "半导体" in (row.tags or [])


def test_build_analysis_prompt_embeds_alphlom_policy_and_known_stocks():
    prompt = build_analysis_prompt(
        news_time="2026-10-27T10:00:00+08:00",
        news_content="digest: 工业母机招标扩大",
        known_stocks=[{"code": "300124", "name": "汇川技术"}],
    )
    assert "十五五" in prompt
    assert "工业母机" in prompt
    assert "bullish_level_reason" in prompt
    assert "relation_reason" in prompt
    assert "最多 3 只" in prompt
    assert "汇川技术" in prompt
    assert "2026-10-27" in prompt
    assert "$prompt_155th" not in prompt
    assert "$news_content" not in prompt


def test_save_and_read_latest_digest():
    db = _db()
    saved = save_digest(
        db,
        headlines=[{"topic": "产业动向", "keywords": ["半导体"], "summary": "招标扩大"}],
        highlights=[],
        title="新闻流速览",
        window_start=datetime(2026, 10, 26, 9, 0, tzinfo=SH),
        window_end=datetime(2026, 10, 26, 10, 0, tzinfo=SH),
        news_count=12,
        model="glm-4",
        trigger_type="auto",
        generated_at=datetime(2026, 10, 26, 10, 30, tzinfo=SH),
    )
    latest = latest_digest(db)
    assert saved["news_count"] == 12
    assert latest["model"] == "glm-4"
    assert latest["headlines"][0]["topic"] == "产业动向"


def test_analyze_queue_stores_news_ids_and_skips_when_full():
    import asyncio
    from unittest.mock import patch

    from src.modules.market.news_flow_scheduler import NewsFlowScheduler

    async def _run():
        sched = NewsFlowScheduler()
        # 用小队列测满载跳过，避免填满 200。
        sched._analyze_queue = asyncio.Queue(maxsize=3)
        calls: list[int] = []

        async def fake_analyze(_client, news_ids, **_kwargs):
            calls.extend(int(x) for x in news_ids)
            await asyncio.sleep(0.02)
            return [int(x) for x in news_ids]

        with patch(
            "src.modules.market.news_flow_scheduler.analyze_many",
            side_effect=fake_analyze,
        ):
            worker = asyncio.create_task(sched._analyze_worker(0))
            try:
                enqueued, skipped = sched._enqueue_news_ids(
                    [11, 12, 13, 14], log_label="test"
                )
                assert enqueued == 3
                assert skipped == 1
                # 重复 id 不占位
                enqueued2, skipped2 = sched._enqueue_news_ids(
                    [11, 15], log_label="dup"
                )
                assert enqueued2 == 0
                assert skipped2 >= 1
                await sched._analyze_queue.join()
                assert set(calls) == {11, 12, 13}
                assert 14 not in calls
            finally:
                worker.cancel()
                try:
                    await worker
                except asyncio.CancelledError:
                    pass

    asyncio.run(_run())


def test_apply_topic_tags_skips_full_table_without_rows():
    db = _db()
    row = insert_news(db, {
        "digest": "黄金上涨",
        "news_time": datetime(2026, 10, 26, 12, 0, tzinfo=SH),
        "news_md5": "t" * 32,
    })
    assert row is not None
    row.digest = "半导体设备招标扩大"
    db.commit()
    assert apply_topic_tags(db) == 0
    stale = db.query(MarketNews).first()
    assert "半导体" not in (stale.tags or [])
    assert apply_topic_tags(db, [stale]) == 1
    assert "半导体" in (db.query(MarketNews).first().tags or [])


def test_search_news_exposes_analysis_reasons():
    db = _db()
    row = insert_news(db, {
        "digest": "南方泵业拟出售闲置资产",
        "news_time": datetime(2026, 10, 26, 12, 0, tzinfo=SH),
        "news_md5": "r" * 32,
        "analyzed": 1,
        "bullish_level": 4,
        "relations_stocks": [{"code": "300145", "name": "南方泵业"}],
    })
    assert row is not None
    row.bullish_level_reason = "资产处置增厚现金"
    row.relation_reason = "公告主体即为该公司"
    db.commit()
    item = search_news(db, keyword="南方泵业")["items"][0]
    assert item["bullish_level_reason"] == "资产处置增厚现金"
    assert item["relation_reason"] == "公告主体即为该公司"


def test_prune_market_news_drops_old_rows():
    db = _db()
    insert_news(db, {
        "digest": "四十天前旧闻",
        "news_time": datetime.now(timezone.utc) - timedelta(days=40),
        "news_md5": "o" * 32,
    })
    insert_news(db, {
        "digest": "两小时近闻",
        "news_time": datetime.now(timezone.utc) - timedelta(hours=2),
        "news_md5": "n" * 32,
    })
    deleted = prune_market_news(db, keep_days=30)
    assert deleted == 1
    rows = db.query(MarketNews).all()
    assert len(rows) == 1
    assert rows[0].digest == "两小时近闻"


def test_news_flow_scheduler_has_no_startup_backfill():
    from src.modules.market.news_flow_scheduler import NewsFlowScheduler

    assert not hasattr(NewsFlowScheduler, "_backfill_job")


def test_select_for_prompt_prefers_analyzed_over_pending():
    items = [
        {"digest": "待分析快讯", "analyzed": 0, "relation_level": 9},
        {"digest": "已分析弱关联", "analyzed": 1, "relation_level": 1},
        {"digest": "已分析强关联", "analyzed": 1, "relation_level": 7},
    ]
    picked = _select_for_prompt(items)
    assert [item["digest"] for item in picked] == [
        "已分析强关联",
        "已分析弱关联",
        "待分析快讯",
    ]


def test_collect_digest_items_uses_analyzed_only_when_enough():
    db = _db()
    now = datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc)
    for index in range(5):
        insert_news(db, {
            "digest": f"已分析{index}",
            "news_time": now - timedelta(minutes=10 + index),
            "news_md5": str(index) * 32,
            "analyzed": 1,
            "relation_level": 3,
            "bullish_level": 2,
        })
    insert_news(db, {
        "digest": "未分析不应入池",
        "news_time": now - timedelta(minutes=5),
        "news_md5": "p" * 32,
        "analyzed": 0,
    })
    hours, items = collect_digest_items(db, now)
    assert hours == 1
    assert {item["digest"] for item in items} == {f"已分析{index}" for index in range(5)}


def test_collect_digest_items_fills_with_pending_when_analyzed_short():
    db = _db()
    now = datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc)
    insert_news(db, {
        "digest": "已分析一条",
        "news_time": now - timedelta(minutes=8),
        "news_md5": "a" * 32,
        "analyzed": 1,
        "relation_level": 3,
        "bullish_level": 2,
    })
    for index in range(4):
        insert_news(db, {
            "digest": f"待分析{index}",
            "news_time": now - timedelta(minutes=6 + index),
            "news_md5": chr(ord("b") + index) * 32,
            "analyzed": 0,
        })
    hours, items = collect_digest_items(db, now)
    assert hours == 1
    digests = {item["digest"] for item in items}
    assert "已分析一条" in digests
    assert len(items) >= 5


def test_list_unanalyzed_skips_exhausted_retries():
    db = _db()
    pending = insert_news(db, {
        "digest": "尚可重试",
        "news_time": datetime(2026, 10, 30, 10, 0, tzinfo=SH),
        "news_md5": "u" * 32,
    })
    exhausted = insert_news(db, {
        "digest": "多次失败",
        "news_time": datetime(2026, 10, 30, 9, 0, tzinfo=SH),
        "news_md5": "v" * 32,
    })
    assert pending is not None and exhausted is not None
    exhausted.analyze_fail_count = MAX_ANALYZE_FAILS
    db.commit()
    rows = list_unanalyzed(db, limit=10)
    assert [row.digest for row in rows] == ["尚可重试"]


def test_search_news_marks_exhausted_rows_as_failed():
    db = _db()
    row = insert_news(db, {
        "digest": "多次解析失败的快讯",
        "news_time": datetime(2026, 10, 30, 9, 0, tzinfo=SH),
        "news_md5": "w" * 32,
    })
    assert row is not None
    row.analyze_fail_count = MAX_ANALYZE_FAILS
    db.commit()
    item = search_news(db, keyword="多次解析失败")["items"][0]
    assert item["analysis_status"] == "failed"
    assert item["analyze_fail_count"] == MAX_ANALYZE_FAILS


def _stub_track_job_run():
    from contextlib import contextmanager

    @contextmanager
    def _track(*_args, **_kwargs):
        yield {}

    return _track


def test_ingest_now_enqueues_then_enforces_cooldown():
    import asyncio
    from unittest.mock import patch

    from src.modules.market.news_flow_scheduler import NewsFlowScheduler

    async def _run():
        sched = NewsFlowScheduler()
        fake_result = {"sources": {"cls": 2}, "enabled": ["cls"], "created_ids": [101, 102]}
        with patch(
            "src.modules.market.news_flow_scheduler.ingest_news_sources",
            return_value=fake_result,
        ), patch(
            "src.modules.market.news_flow_scheduler.track_job_run",
            _stub_track_job_run(),
        ):
            first = await sched.ingest_now()
            second = await sched.ingest_now()
        assert first["status"] == "ok"
        assert first["created"] == 2
        assert first["enqueued"] == 2
        assert sched._analyze_queue.qsize() == 2
        assert second["status"] == "cooldown"
        assert second["retry_after"] > 0

    asyncio.run(_run())


def test_ingest_now_reports_busy_and_failure():
    import asyncio
    from unittest.mock import patch

    from src.modules.market.news_flow_scheduler import NewsFlowScheduler

    async def _run():
        busy = NewsFlowScheduler()
        busy._ingesting = True
        assert (await busy.ingest_now())["status"] == "busy"

        broken = NewsFlowScheduler()
        with patch(
            "src.modules.market.news_flow_scheduler.ingest_news_sources",
            side_effect=RuntimeError("源站超时"),
        ), patch(
            "src.modules.market.news_flow_scheduler.track_job_run",
            _stub_track_job_run(),
        ):
            failed = await broken.ingest_now()
        assert failed["status"] == "failed"
        assert broken._manual_ingest_until == 0.0
        assert broken._ingesting is False

    asyncio.run(_run())


def test_analyze_worker_throttles_repeated_client_failures(caplog):
    """未配置模型时每个 id 都会失败，但同类告警只打一次，避免日志表写风暴。"""
    import asyncio
    from unittest.mock import patch

    from src.modules.market.news_flow_scheduler import NewsFlowScheduler

    async def _run():
        sched = NewsFlowScheduler()
        analyzed: list[int] = []

        async def fake_analyze(_client, news_ids, **_kwargs):
            analyzed.extend(int(x) for x in news_ids)
            raise RuntimeError("Missing credentials")

        with patch(
            "src.modules.market.news_flow_scheduler._load_analysis_client",
            side_effect=RuntimeError("Missing credentials"),
        ), patch(
            "src.modules.market.news_flow_scheduler.analyze_many",
            side_effect=fake_analyze,
        ), patch(
            "src.modules.market.news_flow_scheduler.track_job_run",
            _stub_track_job_run(),
        ):
            worker = asyncio.create_task(sched._analyze_worker(0))
            try:
                sched._enqueue_news_ids([1, 2, 3], log_label="test")
                await sched._analyze_queue.join()
            finally:
                worker.cancel()
                try:
                    await worker
                except asyncio.CancelledError:
                    pass
        # 队列照常消费，不被退避卡住
        assert analyzed == [1, 2, 3]

    with caplog.at_level("WARNING"):
        asyncio.run(_run())
    client_warnings = [
        record for record in caplog.records
        if "模型客户端不可用" in record.getMessage()
    ]
    assert len(client_warnings) == 1


def test_analyze_worker_reloads_client_until_model_configured():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import patch

    from src.modules.market.news_flow_scheduler import NewsFlowScheduler

    async def _run():
        sched = NewsFlowScheduler()
        clients = [SimpleNamespace(model=None), SimpleNamespace(model="glm-4")]
        loads: list[int] = []
        seen: list[str | None] = []

        def fake_load():
            loads.append(1)
            return clients[min(len(loads) - 1, 1)]

        async def fake_analyze(client, news_ids, **_kwargs):
            seen.append(getattr(client, "model", None))
            return [int(x) for x in news_ids]

        with patch(
            "src.modules.market.news_flow_scheduler._load_analysis_client",
            side_effect=fake_load,
        ), patch(
            "src.modules.market.news_flow_scheduler.analyze_many",
            side_effect=fake_analyze,
        ):
            worker = asyncio.create_task(sched._analyze_worker(0))
            try:
                # 逐条入队并等消费完：每次迭代一个 id，验证「模型为空时下一条重取」
                for news_id in (1, 2, 3):
                    sched._enqueue_news_ids([news_id], log_label="test")
                    await sched._analyze_queue.join()
            finally:
                worker.cancel()
                try:
                    await worker
                except asyncio.CancelledError:
                    pass
        assert seen == [None, "glm-4", "glm-4"]
        assert len(loads) == 2

    asyncio.run(_run())


def test_collect_digest_items_excludes_failed_from_pending_pool():
    db = _db()
    now = datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc)
    insert_news(db, {
        "digest": "已分析一条",
        "news_time": now - timedelta(minutes=8),
        "news_md5": "1" * 32,
        "analyzed": 1,
        "relation_level": 3,
        "bullish_level": 2,
    })
    for index in range(4):
        insert_news(db, {
            "digest": f"待分析{index}",
            "news_time": now - timedelta(minutes=6 + index),
            "news_md5": str(index + 2) * 32,
        })
    failed = insert_news(db, {
        "digest": "多次失败不应进速览",
        "news_time": now - timedelta(minutes=3),
        "news_md5": "9" * 32,
    })
    assert failed is not None
    failed.analyze_fail_count = MAX_ANALYZE_FAILS
    db.commit()
    hours, items = collect_digest_items(db, now)
    digests = {item["digest"] for item in items}
    assert hours == 1
    assert "多次失败不应进速览" not in digests
    assert len(items) == 5


def test_mark_analyze_failure_increments_until_cap():
    row = MarketNews(digest="失败样本", analyzed=0, analyze_fail_count=0)
    assert mark_analyze_failure(row) == 1
    assert mark_analyze_failure(row) == 2
    assert mark_analyze_failure(row) == 3
    assert row.analyzed == 0


def test_digest_input_hash_stable_and_changes_with_items():
    first = digest_input_hash([
        {"id": "1", "digest": "半导体招标", "analyzed": 1, "relation_level": 6, "bullish_level": 3},
    ])
    same = digest_input_hash([
        {"id": "1", "digest": "半导体招标", "analyzed": 1, "relation_level": 6, "bullish_level": 3},
    ])
    changed = digest_input_hash([
        {"id": "1", "digest": "半导体招标扩大", "analyzed": 1, "relation_level": 6, "bullish_level": 3},
    ])
    assert first == same
    assert first != changed


def test_save_digest_records_input_hash():
    db = _db()
    saved = save_digest(
        db,
        headlines=[{"topic": "产业动向", "keywords": ["半导体"], "summary": "招标扩大"}],
        highlights=[],
        title="新闻流速览",
        window_start=datetime(2026, 10, 26, 9, 30, tzinfo=SH),
        window_end=datetime(2026, 10, 26, 10, 30, tzinfo=SH),
        news_count=5,
        model="glm-4",
        trigger_type="auto",
        generated_at=datetime(2026, 10, 26, 10, 30, tzinfo=SH),
        input_hash="abc123",
    )
    assert saved["input_hash"] == "abc123"
    assert latest_digest_input_hash(db) == "abc123"


def test_topic_items_appends_frequent_recent_tags():
    db = _db()
    now = datetime.now(timezone.utc)
    insert_news(db, {
        "digest": "某公司固态电池量产",
        "tags": ["固态电池"],
        "news_time": now - timedelta(hours=2),
        "news_md5": "x" * 32,
    })
    insert_news(db, {
        "digest": "另一家固态电池产能扩张",
        "tags": ["固态电池"],
        "news_time": now - timedelta(hours=1),
        "news_md5": "y" * 32,
    })
    keys = [item["key"] for item in topic_items(db)]
    assert keys[0] == "全部新闻"
    assert "固态电池" in keys


def test_topic_items_skips_aliases_generic_and_earnings_tags():
    db = _db()
    now = datetime.now(timezone.utc)
    noisy = ["三季报", "光模块", "业绩下滑", "净利润下滑", "A股", "美股", "股价下跌", "地缘政治"]
    for index in range(2):
        insert_news(db, {
            "digest": f"综合快讯{index}",
            "tags": noisy,
            "news_time": now - timedelta(hours=index + 1),
            "news_md5": str(index) * 32,
        })
    dynamic = [item["key"] for item in topic_items(db) if item["key"] not in FIXED_TOPICS]
    assert dynamic == ["地缘政治"]


def test_search_news_hides_analyzed_without_sentiment_but_keeps_pending():
    db = _db()
    now = datetime(2026, 10, 30, 10, 0, tzinfo=SH)
    insert_news(db, {
        "digest": "中国向古巴捐赠生活物资",
        "news_time": now,
        "news_md5": "q" * 32,
        "analyzed": 1,
        "relation_level": 0,
    })
    insert_news(db, {
        "digest": "日本央行维持利率不变",
        "news_time": now - timedelta(minutes=1),
        "news_md5": "r" * 32,
        "analyzed": 1,
        "relation_level": 5,
        "bullish_level": 0,
        "relations_stocks": [{"code": "EWJ", "name": "iShares MSCI日本ETF"}],
    })
    insert_news(db, {
        "digest": "汇川技术中标30亿元电驱定点",
        "news_time": now - timedelta(minutes=2),
        "news_md5": "t" * 32,
        "analyzed": 1,
        "relation_level": 7,
        "bullish_level": 7,
        "relations_stocks": [{"code": "300124", "name": "汇川技术"}],
    })
    insert_news(db, {
        "digest": "尚未分析的外交快讯",
        "news_time": now - timedelta(minutes=3),
        "news_md5": "s" * 32,
        "analyzed": 0,
        "relation_level": 0,
    })
    page = search_news(db, keyword="全部新闻")
    digests = [item["digest"] for item in page["items"]]
    assert page["total"] == 2
    assert "中国向古巴捐赠生活物资" not in digests
    assert "日本央行维持利率不变" not in digests
    assert "汇川技术中标30亿元电驱定点" in digests
    assert "尚未分析的外交快讯" in digests
    assert db.query(MarketNews).count() == 4


def test_collect_digest_items_skips_analyzed_irrelevant():
    db = _db()
    now = datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc)
    for index in range(5):
        insert_news(db, {
            "digest": f"有关联{index}",
            "news_time": now - timedelta(minutes=10 + index),
            "news_md5": str(index) * 32,
            "analyzed": 1,
            "relation_level": 3,
            "bullish_level": 2,
        })
    insert_news(db, {
        "digest": "党建宣讲无关联",
        "news_time": now - timedelta(minutes=2),
        "news_md5": "z" * 32,
        "analyzed": 1,
        "relation_level": 0,
    })
    insert_news(db, {
        "digest": "大盘午评涨跌互现",
        "news_time": now - timedelta(minutes=3),
        "news_md5": "y" * 32,
        "analyzed": 1,
        "relation_level": 4,
        "bullish_level": 0,
    })
    _, items = collect_digest_items(db, now)
    digests = {item["digest"] for item in items}
    assert "党建宣讲无关联" not in digests
    assert "大盘午评涨跌互现" not in digests
    assert len(items) == 5


def test_analyze_row_deletes_neutral_news_and_blocks_reingest():
    import asyncio
    from unittest.mock import patch

    from src.modules.market import news_analysis

    engine = create_engine("sqlite://")
    MarketNews.__table__.create(engine)
    MarketNewsDiscard.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    item = {
        "digest": "ETF两市成交额缩量",
        "url": "https://example.com/etf",
        "news_time": datetime(2026, 10, 30, 10, 0, tzinfo=SH),
        "news_md5": "e" * 32,
    }
    row = insert_news(db, item)
    assert row is not None
    news_id = row.id
    neutral = normalize_analysis({
        "digest": "ETF成交额统计，无方向",
        "tags": ["ETF"],
        "relation_level": 3,
        "bullish_level": 0,
        "relations_stocks": [{"code": "510300", "name": "沪深300ETF"}],
    }, item["digest"])

    async def fake_analyze_one(_client, _row):
        return neutral

    with patch.object(news_analysis, "SessionLocal", factory), patch.object(
        news_analysis, "analyze_one", side_effect=fake_analyze_one
    ):
        assert asyncio.run(news_analysis._analyze_row_by_id(object(), news_id)) is True

    db.expire_all()
    assert db.query(MarketNews).count() == 0
    discard = db.query(MarketNewsDiscard).one()
    assert discard.news_md5 == "e" * 32
    assert discard.url == "https://example.com/etf"
    assert insert_news(db, item) is None
    assert insert_news(db, {**item, "news_md5": "f" * 32}) is None


def test_news_title_key_matches_ths_and_stockstar_formats():
    from src.platform.persistence.news_keys import news_title_key

    ths = news_title_key("佑驾创新与腾讯云达成战略合作。双方将在智能驾驶领域合作")
    star = news_title_key("佑驾创新与腾讯云达成战略合作。（原标题：佑驾创新与腾讯云达成战略合作） 人民财讯10月30日电")
    assert ths == star == "佑驾创新与腾讯云达成战略合作"
    # 首句过短时累积后续句子：只返回空串会让这类标题党彻底退出跨来源判重
    # （实测「利好来了！北京，重磅发布！事关氢能产业」同一事件被分析了两次）。
    assert news_title_key("暴雷！新易盛三季度净利润增长") == "暴雷新易盛三季度净利润增长"
    assert news_title_key("利好来了！北京，重磅发布！事关氢能产业。") == "利好来了北京重磅发布事关氢能产业"
    # 全文都凑不出 8 字才真的没有键
    assert news_title_key("暴雷") == ""
    assert news_title_key("A股开盘：三大指数集体低开。上证指数低开1.25%") == "A股开盘三大指数集体低开"


def test_news_title_key_extends_short_lead_but_keeps_them_distinguishable():
    """累积出的键要带上区分信息，避免把同城不同政策并成一条。"""
    from src.platform.persistence.news_keys import news_title_key, titles_similar

    hydrogen = news_title_key("利好来了！北京，重磅发布！事关氢能产业。（原标题：利好来了！北京，重磅发布！事关千亿产业）")
    hydrogen2 = news_title_key("利好来了！北京，重磅发布！事关千亿产业。（原标题：利好来了！北京，重磅发布！事关千亿产业）")
    space = news_title_key("利好来了！北京，重磅发布！事关商业航天。")
    assert titles_similar(hydrogen, hydrogen2)
    assert not titles_similar(hydrogen, space)


def test_titles_similar_matches_real_duplicates_but_not_different_events():
    from src.platform.persistence.news_keys import news_title_key, titles_similar

    same_pairs = [
        ("日本央行将政策利率维持在1.25%不变，符合市场预期", "日本央行将政策利率维持在1.25%不变"),
        ("广期所多晶硅主力合约大跌6%，报34100元/吨", "广期所多晶硅主力合约大跌5%，报34460元/吨"),
        ("4只中证港股通消费主题指数ETF涨超3%", "4只国证港股通消费主题指数ETF涨超3%"),
    ]
    different_pairs = [
        ("现货白银跌超1%，现报51.38美元/盎司", "COMEX白银期货跌超1%，现报51.8美元/盎司"),
        ("广期所多晶硅主力合约大跌5%，报34460元/吨", "上期所沪锡主力合约大跌5%，报363170元/吨"),
        ("ETF开盘：交通运输ETF领涨3.10%，创业板人工智能ETF领跌", "ETF午评：酒ETF领涨3.65%，创业板人工智能ETF领跌3.2%"),
        ("期市早盘开盘，国内期货主力合约多数下跌", "股指期货早盘收盘，主力合约多数下跌"),
    ]
    for a, b in same_pairs:
        assert titles_similar(news_title_key(a), news_title_key(b)), (a, b)
    for a, b in different_pairs:
        assert not titles_similar(news_title_key(a), news_title_key(b)), (a, b)


def test_insert_news_skips_similar_title_within_window():
    db = _db()
    published = datetime(2026, 10, 30, 10, 0, tzinfo=SH)
    assert insert_news(db, {
        "digest": "日本央行将政策利率维持在1.25%不变，符合市场预期。此前一次会议加息25个基点",
        "news_time": published,
        "news_md5": "7" * 32,
        "sources": "ths",
    }) is not None
    assert insert_news(db, {
        "digest": "日本央行将政策利率维持在1.25%不变。（原标题：日本央行将政策利率维持在1.25%不变）",
        "news_time": published + timedelta(minutes=20),
        "news_md5": "8" * 32,
        "sources": "stock_star",
    }) is None
    assert insert_news(db, {
        "digest": "上期所沪锡主力合约大跌5%，报363170元/吨。",
        "news_time": published + timedelta(minutes=30),
        "news_md5": "9" * 32,
    }) is not None
    assert db.query(MarketNews).count() == 2


def test_insert_news_skips_cross_source_duplicate_title():
    db = _db()
    published = datetime(2026, 10, 30, 10, 0, tzinfo=SH)
    first = insert_news(db, {
        "digest": "佑驾创新与腾讯云达成战略合作。双方将在智能驾驶领域合作",
        "news_time": published,
        "news_md5": "1" * 32,
        "sources": "ths",
    })
    second = insert_news(db, {
        "digest": "佑驾创新与腾讯云达成战略合作。（原标题：佑驾创新与腾讯云达成战略合作） 人民财讯10月30日电",
        "news_time": published + timedelta(minutes=40),
        "news_md5": "2" * 32,
        "url": "https://stock.stockstar.com/x.shtml",
        "sources": "stock_star",
    })
    assert first is not None
    assert first.title_key == "佑驾创新与腾讯云达成战略合作"
    assert second is None
    assert db.query(MarketNews).count() == 1


def test_insert_news_keeps_daily_template_titles_a_day_apart():
    db = _db()
    today = datetime.now(SH).replace(microsecond=0)
    yesterday = insert_news(db, {
        "digest": "A股开盘：三大指数集体低开。上证指数低开1.25%",
        "news_time": today - timedelta(days=1),
        "news_md5": "3" * 32,
    })
    assert yesterday is not None
    # 昨天那条是昨天收进来的：入库时间也要拨回去，否则会被当成「今天才转载的旧文」。
    db.query(MarketNews).filter(MarketNews.id == yesterday.id).update(
        {MarketNews.created_at: today - timedelta(days=1)}
    )
    db.commit()
    assert insert_news(db, {
        "digest": "A股开盘：三大指数集体低开。上证指数低开0.40%",
        "news_time": today,
        "news_md5": "4" * 32,
    }) is not None


def test_insert_news_blocks_reprint_ingested_long_after_publication():
    """转载：隔十几小时才被另一个来源收进来，发布时间差远超窗口但仍是同一件事。"""
    db = _db()
    now = datetime.now(SH).replace(microsecond=0)
    assert insert_news(db, {
        "digest": "利好来了！北京，重磅发布！事关千亿产业。（原标题：利好来了！北京，重磅发布！事关千亿产业）",
        "news_time": now - timedelta(hours=1),
        "news_md5": "1" * 32,
    }) is not None
    # 19 小时前发布的同一件事、刚被另一来源转载进来 → 判重
    assert insert_news(db, {
        "digest": "利好来了！北京，重磅发布！事关氢能产业。（原标题：利好来了！北京，重磅发布！事关千亿产业）",
        "news_time": now - timedelta(hours=19),
        "news_md5": "2" * 32,
    }) is None
    assert db.query(MarketNews).count() == 1


def test_discarded_title_blocks_other_source_copy():
    db = _db()
    published = datetime(2026, 10, 30, 10, 0, tzinfo=SH)
    row = insert_news(db, {
        "digest": "中钢协重点企业超低排放改造后吨钢用能成本降低。数据显示",
        "news_time": published,
        "news_md5": "5" * 32,
        "sources": "ths",
    })
    assert row is not None
    row.analyzed = 1
    discard_news_row(db, row)
    db.commit()
    assert insert_news(db, {
        "digest": "中钢协重点企业超低排放改造后吨钢用能成本降低。（原标题：中钢协重点企业超低排放改造后吨钢用能成本降低）",
        "news_time": published + timedelta(hours=2),
        "news_md5": "6" * 32,
        "sources": "stock_star",
    }) is None


def test_is_weak_signal_treats_plus_minus_one_as_neutral():
    assert is_weak_signal(0) is True
    assert is_weak_signal(1) is True
    assert is_weak_signal(-1) is True
    assert is_weak_signal(None) is True
    assert is_weak_signal(2) is False
    assert is_weak_signal(-2) is False


def test_analyze_row_deletes_weak_signal_news():
    import asyncio
    from unittest.mock import patch

    from src.modules.market import news_analysis

    engine = create_engine("sqlite://")
    MarketNews.__table__.create(engine)
    MarketNewsDiscard.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    row = insert_news(db, {
        "digest": "企查查显示某公司设立全资子公司",
        "news_time": datetime(2026, 10, 30, 10, 0, tzinfo=SH),
        "news_md5": "w" * 32,
    })
    assert row is not None
    news_id = row.id
    weak = normalize_analysis({
        "digest": "常规设立子公司，影响有限",
        "tags": ["对外投资"],
        "relation_level": 3,
        "bullish_level": 1,
        "relations_stocks": [{"code": "301000", "name": "长联科技"}],
    }, "企查查显示某公司设立全资子公司")

    async def fake_analyze_one(_client, _row):
        return weak

    with patch.object(news_analysis, "SessionLocal", factory), patch.object(
        news_analysis, "analyze_one", side_effect=fake_analyze_one
    ):
        assert asyncio.run(news_analysis._analyze_row_by_id(object(), news_id)) is True

    db.expire_all()
    assert db.query(MarketNews).count() == 0
    assert db.query(MarketNewsDiscard).count() == 1


def test_search_news_hides_analyzed_weak_signal():
    db = _db()
    now = datetime(2026, 10, 30, 10, 0, tzinfo=SH)
    insert_news(db, {
        "digest": "两融参与人数小幅增加",
        "news_time": now,
        "news_md5": "p" * 32,
        "analyzed": 1,
        "relation_level": 3,
        "bullish_level": 1,
    })
    insert_news(db, {
        "digest": "券商获大额定增认购",
        "news_time": now - timedelta(minutes=1),
        "news_md5": "q" * 32,
        "analyzed": 1,
        "relation_level": 6,
        "bullish_level": 2,
    })
    digests = [item["digest"] for item in search_news(db, keyword="全部新闻")["items"]]
    assert digests == ["券商获大额定增认购"]


def test_prune_discards_drops_expired_rows():
    db = _db()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.add_all([
        MarketNewsDiscard(news_md5="o" * 32, url="", discarded_at=now - timedelta(hours=72)),
        MarketNewsDiscard(news_md5="n" * 32, url="", discarded_at=now - timedelta(hours=1)),
    ])
    db.commit()
    assert prune_discards(db) == 1
    assert [row.news_md5 for row in db.query(MarketNewsDiscard).all()] == ["n" * 32]


def test_topic_items_ignores_tags_from_irrelevant_news():
    db = _db()
    now = datetime.now(timezone.utc)
    for index in range(2):
        insert_news(db, {
            "digest": f"无关联快讯{index}",
            "tags": ["人道主义援助"],
            "news_time": now - timedelta(hours=index + 1),
            "news_md5": chr(ord("k") + index) * 32,
            "analyzed": 1,
            "relation_level": 0,
        })
    keys = [item["key"] for item in topic_items(db)]
    assert "人道主义援助" not in keys

