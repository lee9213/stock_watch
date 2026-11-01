"""批量分析、采集前预过滤、强信号推送的回归测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.modules.market import news_analysis
from src.modules.market.news_analysis import (
    NewsSnapshot,
    analyze_many,
    build_batch_prompt,
    build_analysis_prompt,
)
from src.modules.market.news_ingest import IngestDedupIndex
from src.modules.market.news_store import insert_news
from src.platform.persistence.models import MarketNews, MarketNewsDiscard, NewsDigest

SH = ZoneInfo("Asia/Shanghai")


@pytest.fixture(autouse=True)
def _isolate_breaker():
    """熔断是模块级状态，用例之间必须隔离（否则 worker 会真的 sleep 退避）。"""
    news_analysis.reset_analysis_breaker()
    yield
    news_analysis.reset_analysis_breaker()


@pytest.fixture(autouse=True)
def _no_cross_round_linger(monkeypatch):
    """跨轮攒批在非交易时段是 15-30 分钟，测试里一律关闭，避免用例挂住。

    需要验证攒批行为的用例自己在用例内覆盖这个 patch。
    """
    from src.modules.market import news_flow_scheduler as nfs

    monkeypatch.setattr(nfs, "batch_linger_sec", lambda *a, **k: 0.0)


def _client():
    """带 model 的假客户端：没有 model 时 analyze_many 会直接跳过（不消耗重试额度）。"""
    from types import SimpleNamespace

    return SimpleNamespace(model="glm-4")


def _factory():
    engine = create_engine("sqlite://")
    MarketNews.__table__.create(engine)
    MarketNewsDiscard.__table__.create(engine)
    NewsDigest.__table__.create(engine)
    return sessionmaker(bind=engine)


def _seed(factory, count: int = 3) -> list[int]:
    db = factory()
    ids: list[int] = []
    now = datetime.now(timezone.utc)
    for index in range(count):
        row = insert_news(db, {
            "digest": f"测试新闻{index}",
            "news_time": now - timedelta(minutes=5 + index),
            "news_md5": f"{index + 1}" * 32,
        })
        assert row is not None
        ids.append(int(row.id))
    db.close()
    return ids


def _batch_payload(count: int) -> str:
    return json.dumps(
        {
            "results": [
                {
                    "index": index,
                    "digest": f"批量摘要{index}",
                    "tags": ["半导体"],
                    "relation_level": 5,
                    "bullish_level": 3,
                    "bullish_level_reason": "订单增长",
                    "relations_stocks": [{"code": "600519", "name": "贵州茅台"}],
                    "relation_reason": "正文点名",
                }
                for index in range(1, count + 1)
            ]
        },
        ensure_ascii=False,
    )


def test_build_batch_prompt_reuses_rules_and_lists_every_item():
    moment = datetime(2026, 11, 1, 2, 0, tzinfo=timezone.utc)
    snapshots = [
        NewsSnapshot(id=i, digest=f"新闻{i}", raw_text=f"正文{i}", news_time=moment)
        for i in range(1, 4)
    ]
    prompt = build_batch_prompt(snapshots)
    single = build_analysis_prompt(news_time="", news_content="正文", known_stocks=[])

    assert "### 1" in prompt and "### 3" in prompt
    assert prompt.count("digest: 正文") == 3
    # 规则段与单条分析同源（不重复维护两份提示词）
    assert "市场关联度" in prompt and "bullish_level_reason" in prompt
    # 固定指令只发一次：3 条的批量 prompt 明显短于 3 次单条
    assert len(prompt) < len(single) * 3


def test_analyze_many_uses_one_call_and_writes_every_row():
    factory = _factory()
    ids = _seed(factory, 3)
    prompts: list[str] = []

    async def fake_complete(_client, prompt):
        prompts.append(prompt)
        return _batch_payload(3)

    with patch.object(news_analysis, "SessionLocal", factory), patch.object(
        news_analysis, "_complete_analysis", side_effect=fake_complete
    ):
        done = asyncio.run(analyze_many(_client(), ids, batch_size=5))

    assert len(prompts) == 1
    assert set(done) == set(ids)
    db = factory()
    rows = db.query(MarketNews).order_by(MarketNews.id).all()
    assert [row.analyzed for row in rows] == [1, 1, 1]
    assert rows[0].digest.startswith("批量摘要")
    db.close()


def test_partial_batch_results_are_retried_as_one_more_batch():
    """5 条只回了 3 条：缺的 2 条合成一次调用，而不是各打一次单条。"""
    factory = _factory()
    ids = _seed(factory, 5)
    prompts: list[str] = []

    async def fake_complete(_client, prompt):
        prompts.append(prompt)
        # 第一次只回前 3 条；补跑时回剩下的 2 条
        return _batch_payload(3) if len(prompts) == 1 else _batch_payload(2)

    with patch.object(news_analysis, "SessionLocal", factory), patch.object(
        news_analysis, "_complete_analysis", side_effect=fake_complete
    ):
        done = asyncio.run(analyze_many(_client(), ids, batch_size=5))

    assert len(prompts) == 2, "缺失条目应再组一次批量，而不是逐条重打"
    assert set(done) == set(ids)
    db = factory()
    assert [row.analyzed for row in db.query(MarketNews).order_by(MarketNews.id).all()] == [1] * 5
    db.close()


def test_analyze_many_falls_back_to_single_when_batch_unparsable():
    factory = _factory()
    ids = _seed(factory, 2)
    calls: list[str] = []

    async def fake_complete(_client, prompt):
        calls.append(prompt)
        if len(calls) == 1:
            return "抱歉，我无法完成"  # 整批不可解析 → 逐条回退
        return json.dumps(
            {
                "digest": "单条摘要",
                "tags": ["半导体"],
                "relation_level": 4,
                "bullish_level": -3,
                "relations_stocks": [{"code": "000001", "name": "平安银行"}],
            },
            ensure_ascii=False,
        )

    with patch.object(news_analysis, "SessionLocal", factory), patch.object(
        news_analysis, "_complete_analysis", side_effect=fake_complete
    ):
        done = asyncio.run(analyze_many(_client(), ids, batch_size=5))

    assert len(calls) == 3  # 1 次批量失败 + 2 次单条
    assert set(done) == set(ids)
    db = factory()
    assert {row.digest for row in db.query(MarketNews).all()} == {"单条摘要"}
    db.close()


def test_ingest_dedup_index_flags_same_event_title():
    factory = _factory()
    db = factory()
    now = datetime.now(timezone.utc)
    insert_news(db, {
        "digest": "国家发改委批复新建三条特高压直流工程",
        "news_time": now - timedelta(minutes=20),
        "news_md5": "d" * 32,
    })
    index = IngestDedupIndex(db)
    assert index.is_duplicate_title(
        "国家发改委批复新建三条特高压直流工程", now
    )
    assert not index.is_duplicate_title("某公司发布三季度业绩预告", now)
    # 超出 ±12h 窗口不算重复（每日模板标题不应互相压制）
    assert not index.is_duplicate_title(
        "国家发改委批复新建三条特高压直流工程", now + timedelta(hours=20)
    )
    db.close()


def test_wallstreet_skips_detail_for_old_and_duplicate_items():
    from src.modules.market import wallstreet_news as wn

    now = datetime.now(timezone.utc)
    metas = [
        {"id": 1, "title": "三天前的旧闻", "display_time": int((now - timedelta(days=3)).timestamp())},
        {"id": 2, "title": "已知重复事件", "display_time": int(now.timestamp())},
        {"id": 3, "title": "全新事件", "display_time": int(now.timestamp())},
    ]
    fetched: list[int] = []

    def fake_article(article_id, timeout=10.0):
        fetched.append(article_id)
        return {
            "id": article_id,
            "title": "全新事件",
            "content_text": "正文内容",
            "display_time": int(now.timestamp()),
            "url": wn.ARTICLE_PAGE.format(article_id),
        }

    with patch.object(wn, "fetch_hot_items", return_value=metas), patch.object(
        wn, "fetch_article", side_effect=fake_article
    ):
        items = wn.fetch_wallstreet_hot(
            limit=5,
            delay=0,
            precheck=lambda title, _moment: title == "已知重复事件",
        )

    assert fetched == [3]
    assert len(items) == 1


def test_stockstar_skips_detail_for_duplicate_titles():
    from src.modules.market import stockstar_news as sn

    rows = [
        {"title": "重复的同一件事", "url": "https://stock.stockstar.com/IG1.shtml", "publish_date": "2026-11-01 09:30"},
        {"title": "全新的公告解读", "url": "https://stock.stockstar.com/IG2.shtml", "publish_date": "2026-11-01 09:20"},
    ]
    fetched: list[str] = []

    def fake_detail(url, timeout=10.0):
        fetched.append(url)
        return {"title": "全新的公告解读", "publish_time": "2026-11-01 09:20", "content": "正文", "url": url}

    with patch.object(sn, "fetch_list_page", return_value=rows), patch.object(
        sn, "fetch_article_content", side_effect=fake_detail
    ), patch.object(sn, "is_within_ingest_window", return_value=True):
        items = sn.fetch_stockstar_feed(
            list_urls=("https://stock.stockstar.com/list/2_1.shtml",),
            limit=2,
            delay=0,
            precheck=lambda title, _moment: title == "重复的同一件事",
        )

    assert fetched == ["https://stock.stockstar.com/IG2.shtml"]
    assert len(items) == 1


def test_worker_batches_queued_ids_into_one_analysis_call(monkeypatch):
    from src.modules.market import news_flow_scheduler as nfs

    # 周末/夜间会走跨轮攒批（1800s），测试里压到最小。
    monkeypatch.setattr(nfs, "batch_linger_sec", lambda *a, **k: 0.05)

    async def _run():
        sched = nfs.NewsFlowScheduler()
        batches: list[list[int]] = []

        async def fake_analyze(_client, news_ids, **_kwargs):
            batches.append([int(x) for x in news_ids])
            return [int(x) for x in news_ids]

        with patch(
            "src.modules.market.news_flow_scheduler.analyze_many",
            side_effect=fake_analyze,
        ):
            worker = asyncio.create_task(sched._analyze_worker(0))
            try:
                sched._enqueue_news_ids(list(range(1, 13)), log_label="test")
                await sched._analyze_queue.join()
            finally:
                worker.cancel()
                try:
                    await worker
                except asyncio.CancelledError:
                    pass
        # 12 条按批量大小切块：5 + 5 + 2
        assert [len(batch) for batch in batches] == [5, 5, 2]
        assert sorted(x for batch in batches for x in batch) == list(range(1, 13))

    asyncio.run(_run())


def test_workers_do_not_split_one_burst_into_many_single_calls(monkeypatch):
    """同一次采集入队的一批 id 必须并成一次调用。

    改前 4 个 worker 各自 ``get()`` 抢一条，一次入队 5 条会变成 5 次单条调用；
    单条 prompt 里有 96% 是固定指令，等于把这份成本付 5 遍。
    """
    from src.modules.market import news_flow_scheduler as nfs

    monkeypatch.setattr(nfs, "batch_linger_sec", lambda *a, **k: 0.05)

    async def _run():
        sched = nfs.NewsFlowScheduler()
        batches: list[list[int]] = []

        async def fake_analyze(_client, news_ids, **_kwargs):
            batches.append([int(x) for x in news_ids])
            return [int(x) for x in news_ids]

        with patch.object(nfs, "analyze_many", side_effect=fake_analyze):
            workers = [asyncio.create_task(sched._analyze_worker(i)) for i in range(2)]
            try:
                sched._enqueue_news_ids([1, 2, 3, 4, 5], log_label="burst")
                await asyncio.wait_for(sched._analyze_queue.join(), timeout=3)
            finally:
                for worker in workers:
                    worker.cancel()
                for worker in workers:
                    try:
                        await worker
                    except asyncio.CancelledError:
                        pass
        return batches

    batches = asyncio.run(_run())
    assert batches == [[1, 2, 3, 4, 5]], f"一次入队的 5 条应合并为一次调用，实际 {batches}"


def test_next_batch_does_not_wait_forever_on_lone_item(monkeypatch):
    """队列里只有一条时最多等 BATCH_LINGER_SEC，不能无限攒批。"""
    import time as _time

    from src.modules.market import news_flow_scheduler as nfs

    monkeypatch.setattr(nfs, "batch_linger_sec", lambda *a, **k: 0.1)

    async def _run():
        sched = nfs.NewsFlowScheduler()
        sched._analyze_queue.put_nowait(7)
        started = _time.monotonic()
        batch = await sched._next_batch()
        return batch, _time.monotonic() - started

    batch, elapsed = asyncio.run(_run())
    assert batch == [7]
    assert 0.08 <= elapsed < 0.9, f"攒批等待应在配置范围内，实际 {elapsed:.2f}s"


def test_catchup_leaves_half_of_queue_for_fresh_news():
    from src.modules.market.news_flow_scheduler import (
        ANALYZE_QUEUE_MAXSIZE,
        NewsFlowScheduler,
    )

    sched = NewsFlowScheduler()
    with patch.object(sched, "_enqueue_unanalyzed") as spy:
        sched._enqueue_catchup()
    limit = spy.call_args.kwargs["limit"]
    assert 0 < limit <= ANALYZE_QUEUE_MAXSIZE // 2


def test_strong_signal_thresholds_and_message():
    from src.modules.market.news_signal_alert import _format, is_strong_signal

    assert is_strong_signal(7, 6)
    assert is_strong_signal(9, -8)
    assert not is_strong_signal(6, 9)
    assert not is_strong_signal(8, 5)

    row = MarketNews(
        digest="某龙头宣布上调产品价格 20%",
        relation_level=8,
        bullish_level=-7,
        bullish_level_reason="提价挤压下游利润",
        relations_stocks=[{"code": "600519", "name": "贵州茅台"}],
        url="https://example.com/news/1",
    )
    title, content = _format(row)
    assert "利空" in title
    assert "-7" in content and "贵州茅台" in content and "https://example.com/news/1" in content


def test_batch_call_failure_does_not_retry_each_row():
    """调用本身失败（超时/连接/5xx）时整批放弃，不逐条重试、也不扣重试额度。

    逐条重试只对「400/422 可能是某条内容触发」有意义；对整批性故障，
    逐条重试等于把一个失败放大成 N 倍，还会把每条的重试额度一起烧掉。
    """
    factory = _factory()
    ids = _seed(factory, 3)
    calls: list[str] = []

    async def fake_complete(_client, prompt):
        calls.append(prompt)
        raise RuntimeError("Connection reset by peer")

    with patch.object(news_analysis, "SessionLocal", factory), patch.object(
        news_analysis, "_complete_analysis", side_effect=fake_complete
    ):
        done = asyncio.run(analyze_many(_client(), ids, batch_size=5))

    assert done == []
    assert len(calls) == 1, "批量失败不应逐条重试"

    db = factory()
    rows = db.query(MarketNews).order_by(MarketNews.id).all()
    assert [int(row.analyzed or 0) for row in rows] == [0, 0, 0]
    # 整批性故障不消耗单条的重试额度，配好模型后仍能被补扫捞回来
    assert [int(row.analyze_fail_count or 0) for row in rows] == [0, 0, 0]
    db.close()


def test_bad_request_still_falls_back_to_single_rows():
    """400/422 可能是某一条内容触发：仍逐条定位，把其余条目救回来。"""
    from openai import BadRequestError

    factory = _factory()
    ids = _seed(factory, 2)
    calls: list[str] = []

    async def fake_complete(_client, prompt):
        calls.append(prompt)
        if len(calls) == 1:
            raise BadRequestError(
                "content policy", response=_fake_response(), body=None
            )
        return json.dumps(
            {
                "digest": "单条摘要",
                "tags": [],
                "relation_level": 4,
                "bullish_level": 2,
                "relations_stocks": [],
            },
            ensure_ascii=False,
        )

    with patch.object(news_analysis, "SessionLocal", factory), patch.object(
        news_analysis, "_complete_analysis", side_effect=fake_complete
    ):
        done = asyncio.run(analyze_many(_client(), ids, batch_size=5))

    assert len(calls) == 3  # 1 次批量 400 + 2 次单条
    assert set(done) == set(ids)
    factory().close()


def _fake_response():
    import httpx

    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    return httpx.Response(400, request=request)


def test_breaker_opens_after_repeated_call_failures():
    factory = _factory()
    ids = _seed(factory, 3)

    async def fake_complete(_client, _prompt):
        raise RuntimeError("upstream 502")

    with patch.object(news_analysis, "SessionLocal", factory), patch.object(
        news_analysis, "_complete_analysis", side_effect=fake_complete
    ):
        for _ in range(news_analysis.BREAKER_FAIL_THRESHOLD):
            asyncio.run(analyze_many(_client(), ids, batch_size=5))

    # 连续失败达到阈值后必须熔断，worker 才会停下来等待而不是空打
    assert news_analysis.analysis_breaker_remaining() > 0


def test_slow_call_hits_own_timeout_instead_of_provider_default(monkeypatch):
    """provider 默认 600s×3 次重试，分析侧必须有自己的上限，且超时不逐条重试。"""
    factory = _factory()
    ids = _seed(factory, 3)
    calls: list[int] = []

    async def slow_complete(_client, _prompt):
        calls.append(1)
        await asyncio.sleep(10)
        return "{}"

    monkeypatch.setattr(news_analysis, "ANALYZE_TIMEOUT_SEC", 0.05)
    with patch.object(news_analysis, "SessionLocal", factory), patch.object(
        news_analysis, "_complete_analysis", side_effect=slow_complete
    ):
        done = asyncio.run(analyze_many(_client(), ids, batch_size=5))

    assert done == []
    assert len(calls) == 1, "超时属于整批性故障，不应逐条重试"
    assert news_analysis.analysis_breaker_remaining() >= 0


def test_single_item_call_failure_does_not_burn_retry_quota():
    """单条路径上的调用级故障（超时/连接/5xx）同样不能扣这条新闻的重试额度。

    队列里只剩一条时 chunk 长度就是 1，走的是 _analyze_snapshot；如果那里也
    按「失败 +1」处理，模型抖动三轮就把新闻永久标成失效，恢复后也不再分析。
    """
    factory = _factory()
    ids = _seed(factory, 1)

    async def fake_complete(_client, _prompt):
        raise RuntimeError("Connection reset by peer")

    with patch.object(news_analysis, "SessionLocal", factory), patch.object(
        news_analysis, "_complete_analysis", side_effect=fake_complete
    ):
        done = asyncio.run(analyze_many(_client(), ids, batch_size=5))

    assert done == []
    db = factory()
    row = db.query(MarketNews).one()
    assert int(row.analyzed or 0) == 0
    assert int(row.analyze_fail_count or 0) == 0, "调用级故障不应消耗重试额度"
    db.close()


def test_unparsable_batch_splits_instead_of_replaying_each_row():
    """整批不可解析时先对半拆（1 + 2 次），而不是 1 + N 次逐条重打。"""
    factory = _factory()
    ids = _seed(factory, 4)
    sizes: list[int] = []

    async def fake_complete(_client, prompt):
        count = prompt.count("digest: 测试新闻")
        sizes.append(count)
        if count > 2:  # 大批量时“输出被截断”
            return "抱歉，我无法完成这个请求"
        return _batch_payload(count)

    with patch.object(news_analysis, "SessionLocal", factory), patch.object(
        news_analysis, "_complete_analysis", side_effect=fake_complete
    ):
        done = asyncio.run(analyze_many(_client(), ids, batch_size=5))

    assert sizes == [4, 2, 2], "应拆成两半各调一次，而不是逐条重打 4 次"
    assert set(done) == set(ids)
    db = factory()
    assert {row.analyzed for row in db.query(MarketNews).all()} == {1}
    db.close()


def test_split_failure_leaves_rows_pending_without_burning_quota():
    """拆小后仍不可解析：整批留给下轮补扫，不计失败次数、不逐条重打。"""
    factory = _factory()
    ids = _seed(factory, 4)
    calls: list[int] = []

    async def fake_complete(_client, _prompt):
        calls.append(1)
        return "模型罢工了"

    with patch.object(news_analysis, "SessionLocal", factory), patch.object(
        news_analysis, "_complete_analysis", side_effect=fake_complete
    ):
        done = asyncio.run(analyze_many(_client(), ids, batch_size=5))

    assert done == []
    assert len(calls) == 3, "1 次整批 + 2 次半批，不应再逐条打 4 次"
    db = factory()
    rows = db.query(MarketNews).all()
    assert [int(row.analyzed or 0) for row in rows] == [0, 0, 0, 0]
    assert [int(row.analyze_fail_count or 0) for row in rows] == [0, 0, 0, 0]
    db.close()
