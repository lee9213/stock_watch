"""策略新闻催化：从 market_news 汇总 metrics。"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.modules.strategy.strategy_engine import (
    _load_news_metrics,
    _market_news_importance,
)
from src.platform.scheduling.timezone import utc_now


def test_market_news_importance_mapping():
    assert _market_news_importance(8, 1) == 3
    assert _market_news_importance(5, 1) == 2
    assert _market_news_importance(2, 1) == 1
    assert _market_news_importance(0, 0) == 1


def test_load_news_metrics_from_market_news():
    now = utc_now()
    candidate = SimpleNamespace(
        stock_symbol="600519",
        stock_name="贵州茅台",
        stock_market="CN",
    )
    news = SimpleNamespace(
        digest="贵州茅台获机构增持，业绩增长超预期",
        relations_stocks=[{"code": "600519", "name": "贵州茅台"}],
        relation_level=8,
        bullish_level=7,
        analyzed=1,
        news_time=now - timedelta(hours=2),
    )

    db = MagicMock()
    market_q = MagicMock()
    db.query.return_value = market_q
    market_q.filter.return_value = market_q
    market_q.order_by.return_value = market_q
    market_q.limit.return_value = market_q
    market_q.all.return_value = [news]

    metrics = _load_news_metrics(db=db, candidates=[candidate], lookback_hours=72)
    assert "600519" in metrics
    m = metrics["600519"]
    assert m["news_count"] == 1
    assert m["high_importance_count"] == 1
    assert m["event_score"] > 0
    assert m["event_bias"] > 0
    assert m["event_tier"] in {"low", "medium", "high"}


def test_load_news_metrics_name_fallback_when_no_relations():
    now = utc_now()
    candidate = SimpleNamespace(
        stock_symbol="000001",
        stock_name="平安银行",
        stock_market="CN",
    )
    news = SimpleNamespace(
        digest="平安银行发布回购计划，利好股东",
        relations_stocks=[],
        relation_level=4,
        bullish_level=2,
        analyzed=1,
        news_time=now - timedelta(hours=1),
    )

    db = MagicMock()
    market_q = MagicMock()
    db.query.return_value = market_q
    market_q.filter.return_value = market_q
    market_q.order_by.return_value = market_q
    market_q.limit.return_value = market_q
    market_q.all.return_value = [news]

    metrics = _load_news_metrics(db=db, candidates=[candidate])
    assert "000001" in metrics
    assert metrics["000001"]["news_count"] == 1
    assert metrics["000001"]["event_bias"] > 0


def _metrics_for(news_list, candidate):
    db = MagicMock()
    market_q = MagicMock()
    db.query.return_value = market_q
    market_q.filter.return_value = market_q
    market_q.order_by.return_value = market_q
    market_q.limit.return_value = market_q
    market_q.all.return_value = news_list
    return _load_news_metrics(db=db, candidates=[candidate])


def test_llm_bias_not_overridden_by_keywords():
    """大西洋案例：营收增长但净利下降 29%，LLM 判 -4；关键词「增长」不能把它翻成利好。"""
    now = utc_now()
    candidate = SimpleNamespace(stock_symbol="600558", stock_name="大西洋", stock_market="CN")
    news = SimpleNamespace(
        digest="大西洋发布2026年三季报，第三季度营收9.85亿元同比增长2.12%，但归母净利润同比下降29.36%",
        relations_stocks=[{"code": "600558", "name": "大西洋"}],
        relation_level=5, bullish_level=-4, analyzed=1, news_time=now - timedelta(hours=2),
    )
    m = _metrics_for([news], candidate)["600558"]
    assert m["event_bias"] < 0


def test_direct_news_count_by_relation_level():
    now = utc_now()
    candidate = SimpleNamespace(stock_symbol="300059", stock_name="东方财富", stock_market="CN")
    generic = SimpleNamespace(
        digest="私募年内合计自购旗下产品18.59亿元", relations_stocks=[{"code": "300059"}],
        relation_level=3, bullish_level=2, analyzed=1, news_time=now - timedelta(hours=3),
    )
    direct = SimpleNamespace(
        digest="东方财富三季报净利增长", relations_stocks=[{"code": "300059"}],
        relation_level=7, bullish_level=4, analyzed=1, news_time=now - timedelta(hours=1),
    )
    assert _metrics_for([generic], candidate)["300059"]["direct_news_count"] == 0
    m = _metrics_for([generic, direct], candidate)["300059"]
    assert m["news_count"] == 2
    assert m["direct_news_count"] == 1


def test_generic_fanout_news_not_direct():
    """工行案例：一条新闻挂靠多家银行，即使 relation_level 高也不算个股直接新闻。"""
    now = utc_now()
    candidate = SimpleNamespace(stock_symbol="601398", stock_name="工商银行", stock_market="CN")
    news = SimpleNamespace(
        digest="多家银行获北向资金增持",
        relations_stocks=[
            {"code": "601398"},
            {"code": "601288"},
            {"code": "601939"},
            {"code": "601988"},
        ],
        relation_level=7,
        bullish_level=3,
        analyzed=1,
        news_time=now - timedelta(hours=1),
    )
    m = _metrics_for([news], candidate)["601398"]
    assert m["news_count"] == 1
    assert m["direct_news_count"] == 0


def test_direct_news_count_and_event_score_are_capped():
    now = utc_now()
    candidate = SimpleNamespace(stock_symbol="601398", stock_name="工商银行", stock_market="CN")
    news = [
        SimpleNamespace(
            digest=f"工商银行公告{i}",
            relations_stocks=[{"code": "601398"}],
            relation_level=7,
            bullish_level=5,
            analyzed=1,
            news_time=now - timedelta(hours=i + 1),
        )
        for i in range(6)
    ]
    m = _metrics_for(news, candidate)["601398"]
    assert m["news_count"] == 6
    assert m["direct_news_count"] == 2
    assert m["event_score"] <= 6.0


def test_duplicate_digest_counts_once_per_stock():
    """同一条新闻转载两次，对同一股票只计一次热度和偏向。"""
    now = utc_now()
    candidate = SimpleNamespace(stock_symbol="601398", stock_name="工商银行", stock_market="CN")
    first = SimpleNamespace(
        digest="工商银行获北向资金大幅增持",
        relations_stocks=[{"code": "601398"}],
        relation_level=7,
        bullish_level=5,
        analyzed=1,
        news_time=now - timedelta(hours=2),
    )
    reprint = SimpleNamespace(
        digest="工商银行获北向资金大幅增持",
        relations_stocks=[{"code": "601398"}],
        relation_level=7,
        bullish_level=5,
        analyzed=1,
        news_time=now - timedelta(hours=1),
    )
    once = _metrics_for([first], candidate)["601398"]
    twice = _metrics_for([first, reprint], candidate)["601398"]
    assert twice["news_count"] == 1
    assert twice["direct_news_count"] == 1
    assert twice["event_bias"] == once["event_bias"]


def test_name_only_digest_match_not_direct():
    """只在摘要里点到股票名、没有个股关联，不算直接新闻。"""
    now = utc_now()
    candidate = SimpleNamespace(stock_symbol="601398", stock_name="工商银行", stock_market="CN")
    news = SimpleNamespace(
        digest="工商银行等大盘蓝筹跟随指数反弹",
        relations_stocks=[],
        relation_level=8,
        bullish_level=4,
        analyzed=1,
        news_time=now - timedelta(hours=1),
    )
    m = _metrics_for([news], candidate)["601398"]
    assert m["news_count"] == 1
    assert m["direct_news_count"] == 0


def test_load_news_metrics_skips_unanalyzed_market_news():
    now = utc_now()
    candidate = SimpleNamespace(
        stock_symbol="600519",
        stock_name="贵州茅台",
        stock_market="CN",
    )
    news = SimpleNamespace(
        digest="贵州茅台获机构增持",
        relations_stocks=[{"code": "600519", "name": "贵州茅台"}],
        relation_level=8,
        bullish_level=7,
        analyzed=0,
        news_time=now - timedelta(hours=1),
    )

    db = MagicMock()
    market_q = MagicMock()
    db.query.return_value = market_q
    market_q.filter.return_value = market_q
    market_q.order_by.return_value = market_q
    market_q.limit.return_value = market_q
    market_q.all.return_value = [news]

    metrics = _load_news_metrics(db=db, candidates=[candidate])
    assert metrics == {}


def test_load_news_metrics_matches_spaced_stock_name():
    now = utc_now()
    candidate = SimpleNamespace(
        stock_symbol="000858",
        stock_name="五 粮 液",
        stock_market="CN",
    )
    news = SimpleNamespace(
        digest="五粮液三季报超预期",
        relations_stocks=[],
        relation_level=6,
        bullish_level=4,
        analyzed=1,
        news_time=now - timedelta(hours=1),
    )
    metrics = _metrics_for([news], candidate)
    assert "000858" in metrics
    assert metrics["000858"]["news_count"] == 1


def test_load_news_metrics_skips_neutral_news_in_real_query():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from src.platform.persistence.models import MarketNews

    engine = create_engine("sqlite://")
    MarketNews.__table__.create(engine)
    db = sessionmaker(bind=engine)()
    now = utc_now().replace(tzinfo=None)
    db.add_all([
        MarketNews(
            digest="青岛食品连收5个涨停板",
            relations_stocks=[{"code": "600962", "name": "青岛食品"}],
            relation_level=6,
            bullish_level=0,
            analyzed=1,
            news_md5="a" * 32,
            news_time=now - timedelta(hours=1),
        ),
        MarketNews(
            digest="青岛食品获大额订单",
            relations_stocks=[{"code": "600962", "name": "青岛食品"}],
            relation_level=6,
            bullish_level=5,
            analyzed=1,
            news_md5="b" * 32,
            news_time=now - timedelta(hours=2),
        ),
    ])
    db.commit()
    candidate = SimpleNamespace(stock_symbol="600962", stock_name="青岛食品", stock_market="CN")

    metrics = _load_news_metrics(db=db, candidates=[candidate], lookback_hours=72)

    assert metrics["600962"]["news_count"] == 1
    assert metrics["600962"]["event_bias"] > 0
    assert metrics["600962"]["news_impact"] > 0
