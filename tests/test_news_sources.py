from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.modules.market.news_store import insert_news, search_news
from src.modules.market.stockstar_news import (
    _decode_stockstar,
    _is_article_href,
    _parse_list_time,
    normalize_stockstar_item,
)
from src.modules.market.wallstreet_news import normalize_wallstreet_article
from src.platform.persistence.models import MarketNews, MarketNewsDiscard


def _db():
    engine = create_engine("sqlite://")
    MarketNews.__table__.create(engine)
    MarketNewsDiscard.__table__.create(engine)
    return sessionmaker(bind=engine)()


def test_normalize_wallstreet_article():
    item = normalize_wallstreet_article({
        "id": 3763450,
        "title": "美联储官员谈降息",
        "content_text": "市场关注后续路径。",
        "display_time": 1761468000,
        "url": "https://wallstreetcn.com/articles/3763450",
        "error": None,
    })
    assert item["sources"] == "wallstreet"
    assert item["url"].endswith("/3763450")
    assert "美联储" in item["digest"]
    assert item["analyzed"] == 0
    assert len(item["news_md5"]) == 32


def test_normalize_stockstar_report():
    item = normalize_stockstar_item(
        {
            "title": "半导体行业周报",
            "url": "https://stock.stockstar.com/IG2026102701.shtml",
            "publish_date": "10-27 09:30",
        },
        {
            "title": "半导体行业周报",
            "content": "设备招标扩大。",
            "publish_time": "2026-10-27 09:30:00",
            "url": "https://stock.stockstar.com/IG2026102701.shtml",
        },
        news_type="report",
    )
    assert item["sources"] == "stock_star"
    assert item["news_type"] == "report"
    assert "研报" in item["tags"]
    assert item["digest"].startswith("半导体")


def test_stockstar_article_href_filter():
    assert _is_article_href("https://stock.stockstar.com/IG2026102700023506.shtml")
    assert _is_article_href("/SS2026102200029046.shtml")
    assert not _is_article_href("https://stock.stockstar.com/list/6143.shtml")
    assert not _is_article_href("/list/2_1.shtml")


def test_stockstar_decode_prefers_gbk():
    raw = "日韩股市".encode("gbk")
    assert "日韩股市" in _decode_stockstar(raw)


def test_parse_stockstar_short_time():
    from zoneinfo import ZoneInfo

    stamp = _parse_list_time("10-27 14:05")
    assert stamp.tzinfo is not None
    local = stamp.astimezone(ZoneInfo("Asia/Shanghai"))
    assert local.month == 10
    assert local.day == 27
    assert local.hour == 14
    assert local.minute == 5


def test_insert_dedupes_by_url_across_sources():
    db = _db()
    first = insert_news(db, {
        "digest": "同一链接正文 A",
        "url": "https://wallstreetcn.com/articles/1",
        "news_time": datetime(2026, 10, 27, 1, 0, tzinfo=timezone.utc),
        "news_md5": "1" * 32,
        "sources": "wallstreet",
    })
    second = insert_news(db, {
        "digest": "同一链接正文 B",
        "url": "https://wallstreetcn.com/articles/1",
        "news_time": datetime(2026, 10, 27, 2, 0, tzinfo=timezone.utc),
        "news_md5": "2" * 32,
        "sources": "stock_star",
    })
    assert first is not None
    assert second is None
    page = search_news(db, keyword="全部新闻")
    assert page["total"] == 1


def test_ingest_window_keeps_last_24h_only():
    from src.modules.market.news_ingest import _insert_batch
    from src.modules.market.news_store import is_within_ingest_window

    now = datetime.now(timezone.utc)
    assert is_within_ingest_window(now - timedelta(hours=2), now=now)
    assert not is_within_ingest_window(now - timedelta(hours=25), now=now)
    assert not is_within_ingest_window(None, now=now)

    db = _db()
    fresh_ids, _ = _insert_batch(
        db,
        [{
            "digest": "今日快讯",
            "news_time": now - timedelta(hours=3),
            "news_md5": "a" * 32,
        }],
    )
    stale_ids, _ = _insert_batch(
        db,
        [{
            "digest": "前天快讯",
            "news_time": now - timedelta(hours=30),
            "news_md5": "b" * 32,
        }],
    )
    assert len(fresh_ids) == 1
    assert stale_ids == []
    assert db.query(MarketNews).count() == 1


def test_should_ingest_news_drops_noise_without_market_signal():
    from src.modules.market.news_prefilter import should_ingest_news

    assert should_ingest_news({
        "digest": "世界杯决赛加时进球",
        "title": "足球夜",
        "tags": ["体育"],
    }) is False
    assert should_ingest_news({
        "digest": "某球队教练换帅",
        "title": "联赛新闻",
    }) is False


def test_should_ingest_news_keeps_market_or_attached_stocks():
    from src.modules.market.news_prefilter import should_ingest_news

    assert should_ingest_news({
        "digest": "世界杯概念股涨停",
        "title": "体育产业",
    }) is True
    assert should_ingest_news({
        "digest": "无关键词正文",
        "relations_stocks": [{"code": "300124", "name": "汇川技术"}],
    }) is True
    assert should_ingest_news({
        "digest": "三季报净利增长",
        "news_type": "news",
    }) is True
    assert should_ingest_news({
        "digest": "行业周报正文",
        "news_type": "report",
    }) is True
    assert should_ingest_news({
        "digest": "央行官员谈货币政策",
    }) is True
    assert should_ingest_news({
        "digest": "持股平台完成过户",
    }) is True
    assert should_ingest_news({
        "digest": "世界杯概念股涨停",
        "title": "体育产业",
    }) is True
    assert should_ingest_news({
        "digest": "演唱会门票股民热议",
        "title": "娱乐夜",
    }) is False


def test_should_ingest_news_drops_party_building_without_market_signal():
    from src.modules.market.news_prefilter import should_ingest_news

    assert should_ingest_news({
        "digest": "谢春涛：《决定》第八部分对增强制度执行力专门提出要求，确保全面从严治党落到实处",
    }) is False
    assert should_ingest_news({
        "digest": "黄建发：《决定》部署突出抓好新兴领域党的建设",
    }) is False
    assert should_ingest_news({
        "digest": "某券商党建活动后宣布回购股份",
    }) is True
    assert should_ingest_news({
        "digest": "中国向古巴捐赠生活物资，帮助改善弱势群体生活",
    }) is True


def test_existing_urls_include_discarded_links():
    from src.modules.market.news_ingest import _existing_urls

    db = _db()
    insert_news(db, {
        "digest": "已入库快讯",
        "url": "https://example.com/kept",
        "news_time": datetime.now(timezone.utc),
        "news_md5": "k" * 32,
    })
    db.add(MarketNewsDiscard(news_md5="d" * 32, url="https://example.com/dropped"))
    db.commit()
    assert _existing_urls(db) == {"https://example.com/kept", "https://example.com/dropped"}


def test_insert_batch_skips_noise_news():
    from src.modules.market.news_ingest import _insert_batch

    now = datetime.now(timezone.utc)
    db = _db()
    created, _ = _insert_batch(
        db,
        [
            {
                "digest": "欧冠淘汰赛进球集锦",
                "news_time": now - timedelta(hours=1),
                "news_md5": "n" * 32,
            },
            {
                "digest": "证监会发布回购新规",
                "news_time": now - timedelta(hours=1),
                "news_md5": "m" * 32,
            },
        ],
    )
    rows = db.query(MarketNews).all()
    assert len(created) == 1
    assert len(rows) == 1
    assert "回购" in rows[0].digest
