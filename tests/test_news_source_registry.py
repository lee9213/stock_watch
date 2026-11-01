"""事件驱动新闻源注册表：开关/顺序解析、字段归一化、相关性闸门。

与 ``test_news_sources.py``（各源的 HTML/字段解析）互补：这里测的是
「采哪些源、什么顺序、条目怎么归一化、什么时候该丢」。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from marketdata.types import FlashNews
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.modules.market.news_prefilter import has_market_signal
from src.modules.market.news_sources import (
    EVENT_NEWS_SOURCE_TYPE,
    NEWS_SOURCE_SPECS,
    NewsSourceSpec,
    _cls_collect,
    _flash_news_items,
    load_news_sources,
    source_summary,
)
from src.platform.persistence.models import (
    DataSource,
    MarketNews,
    MarketNewsDiscard,
    NewsDigest,
)


def _factory():
    engine = create_engine("sqlite://")
    DataSource.__table__.create(engine)
    return sessionmaker(bind=engine)


def _add(db, provider: str, *, enabled: bool = True, priority: int = 0, config: dict | None = None):
    db.add(
        DataSource(
            name=provider,
            type=EVENT_NEWS_SOURCE_TYPE,
            provider=provider,
            config=config or {},
            enabled=enabled,
            priority=priority,
        )
    )


def _flash(**kwargs) -> FlashNews:
    base = {
        "source": "cls",
        "external_id": "1",
        "title": "",
        "content": "",
        "publish_time": datetime.now(timezone.utc) - timedelta(minutes=3),
        "symbols": [],
        "importance": 0,
        "url": "",
    }
    base.update(kwargs)
    return FlashNews(**base)


# ---------------------------------------------------------------------------
# 开关与顺序
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_registry(monkeypatch) -> dict:
    """往注册表注入几个假源，让「开关/顺序」测试不依赖真实源列表。

    真实注册表当前只有同花顺一个源（其余已下线），直接拿它测顺序是测不出来的。
    """
    from src.modules.market import news_sources

    def _make(key: str) -> NewsSourceSpec:
        return NewsSourceSpec(
            key=key, label=key, description="", config={}, fetch=lambda dedup, cfg: []
        )

    registry = {key: _make(key) for key in ("alpha", "beta", "gamma")}
    monkeypatch.setattr(news_sources, "NEWS_SOURCE_SPECS", registry)
    return registry


def test_load_respects_enabled_and_orders_by_priority(fake_registry):
    db = _factory()()
    _add(db, "alpha", priority=10)
    _add(db, "beta", enabled=False, priority=0)
    _add(db, "gamma", priority=5)
    db.commit()

    plan = load_news_sources(db)

    assert [spec.key for spec, _ in plan] == ["gamma", "alpha"], "按 priority 升序，关闭的源不出现"
    db.close()


def test_load_falls_back_to_all_sources_when_table_empty(fake_registry):
    """老库还没 seed event_news：回退默认全量，升级瞬间不会停采。"""
    db = _factory()()
    plan = load_news_sources(db)

    assert [spec.key for spec, _ in plan] == list(fake_registry)
    db.close()


def test_all_sources_disabled_means_no_collection(fake_registry):
    """有配置但全部关闭时要真的不采（与「表为空」的兜底区分开）。"""
    db = _factory()()
    _add(db, "alpha", enabled=False)
    _add(db, "gamma", enabled=False)
    db.commit()

    assert load_news_sources(db) == []
    db.close()


def test_unknown_provider_is_skipped_not_fatal(fake_registry):
    db = _factory()()
    _add(db, "alpha", priority=1)
    _add(db, "some_removed_vendor", priority=0)
    db.commit()

    assert [spec.key for spec, _ in load_news_sources(db)] == ["alpha"]
    db.close()


def test_source_summary_exposes_disabled_sources_with_implemented_flag(fake_registry):
    db = _factory()()
    _add(db, "alpha", priority=1)
    _add(db, "gone", enabled=False, priority=2)
    db.commit()

    rows = source_summary(db)

    assert [(r["provider"], r["enabled"], r["implemented"]) for r in rows] == [
        ("alpha", True, True),
        ("gone", False, False),
    ]
    db.close()


def _ingest_factory():
    engine = create_engine("sqlite://")
    for model in (DataSource, MarketNews, MarketNewsDiscard, NewsDigest):
        model.__table__.create(engine)
    return sessionmaker(bind=engine)


def test_priority_decides_which_source_keeps_a_duplicate_event(monkeypatch):
    """同一条事件被两个源报到：priority 靠前的源入库，后到的被同轮判重跳过。

    这就是「顺序可调」的实际意义 —— 排在最前的源等于默认代表来源。
    """
    from src.modules.market import news_sources
    from src.modules.market.news_ingest import (
        ingest_news_sources,
        invalidate_dedup_index,
    )

    def _item(text: str, md5: str, source: str) -> dict:
        return {
            "digest": text,
            "news_time": datetime.now(timezone.utc) - timedelta(minutes=5),
            "news_md5": md5,
            "url": "",
            "sources": source,
        }

    def _make(key: str, text: str, md5: str) -> NewsSourceSpec:
        return NewsSourceSpec(
            key=key,
            label=key,
            description="",
            config={},
            fetch=lambda dedup, cfg, _t=text, _m=md5, _k=key: [_item(_t, _m, _k)],
        )

    monkeypatch.setitem(news_sources.NEWS_SOURCE_SPECS, "cls", _make("cls", "宁德时代业绩预增超预期", "a" * 32))
    monkeypatch.setitem(
        news_sources.NEWS_SOURCE_SPECS, "sina", _make("sina", "宁德时代业绩预增超预期。盘中快讯", "b" * 32)
    )

    db = _ingest_factory()()
    invalidate_dedup_index(db)
    _add(db, "sina", priority=0)  # 新浪排在最前 → 由它做代表
    _add(db, "cls", priority=5)
    db.commit()

    result = ingest_news_sources(db, parallel=False)

    assert len(result["created_ids"]) == 1
    assert result["sources"] == {"sina": 1, "cls": 0}
    assert db.query(MarketNews).one().sources == "sina"
    db.close()


# ---------------------------------------------------------------------------
# 字段归一化
# ---------------------------------------------------------------------------


def test_flash_item_keeps_only_a_share_codes():
    """新浪快讯的 symbols 混着外汇期货代码，不能当关联个股写进库。"""
    items = _flash_news_items(
        [
            _flash(
                title="某公司公告回购",
                content="某公司公告回购股份，金额 1 亿元。",
                symbols=["600000", "000001", "fx_susdinr", "hf_cl", "300750.SZ"],
            )
        ],
        source_tag="sina",
    )

    assert items[0]["relations_stocks"] == [
        {"code": "600000", "name": "600000"},
        {"code": "000001", "name": "000001"},
    ]


def test_flash_item_does_not_repeat_title_already_in_content():
    items = _flash_news_items(
        [_flash(title="央行宣布降准", content="央行宣布降准，释放长期资金约 5000 亿元。")],
        source_tag="cls",
    )

    assert items[0]["digest"] == "央行宣布降准，释放长期资金约 5000 亿元。"
    assert items[0]["raw_text"].startswith("央行宣布降准")


def test_flash_item_prepends_title_when_missing_from_content():
    items = _flash_news_items(
        [_flash(title="比亚迪：10月销量", content="10 月新能源汽车销量 49.02 万辆。")],
        source_tag="eastmoney",
    )

    assert items[0]["digest"] == "比亚迪：10月销量。10 月新能源汽车销量 49.02 万辆。"


def test_flash_item_drops_stale_and_empty_entries():
    """时间解析失败的条目会回退到 1970，绝不能当成新新闻入库。"""
    items = _flash_news_items(
        [
            _flash(title="新文", content="新文正文"),
            _flash(title="", content="", publish_time=datetime.now(timezone.utc)),
            _flash(
                title="太旧",
                content="太旧正文",
                publish_time=datetime(1970, 1, 1, tzinfo=timezone.utc),
            ),
        ],
        source_tag="cls",
    )

    assert len(items) == 1
    assert items[0]["title"] == "新文"


# ---------------------------------------------------------------------------
# 相关性闸门
# ---------------------------------------------------------------------------


def test_has_market_signal_accepts_stock_mapping_or_market_words():
    assert has_market_signal({"digest": "央行宣布降准", "relations_stocks": []})
    assert has_market_signal({"digest": "某公司公告", "relations_stocks": [{"code": "600000"}]})
    assert not has_market_signal({"digest": "泽连斯基称对俄发动远程打击", "relations_stocks": []})


def test_collect_filters_out_non_market_items_when_market_only():
    """快讯源默认开 market_only：海外政治类不入库，否则分析量翻数倍且结论多被丢弃。"""

    def _fetch(dedup, cfg):
        return [
            {"digest": "沪市前三季度806家公司营收利润双增长", "relations_stocks": []},
            {"digest": "塞尔维亚议会选举部分投票站重新投票", "relations_stocks": []},
        ]

    strict = NewsSourceSpec(
        key="eastmoney", label="", description="", config={"market_only": True}, fetch=_fetch
    )
    assert [item["digest"][:2] for item in strict.collect(None)] == ["沪市"]

    lax = NewsSourceSpec(
        key="eastmoney", label="", description="", config={"market_only": False}, fetch=_fetch
    )
    assert len(lax.collect(None)) == 2


def test_collect_swallows_source_failure():
    def _boom(dedup, cfg):
        raise RuntimeError("上游 502")

    spec = NewsSourceSpec(key="cls", label="", description="", config={}, fetch=_boom)
    assert spec.collect(None) == []


def test_cls_requested_limit_is_clamped_to_upstream_max(monkeypatch):
    """财联社 rn>50 直接返回空列表，表现为源静默停采；调大 limit 必须被夹回 50。"""
    from marketdata import registry

    captured: dict = {}

    class _Vendor:
        def fetch(self, symbols, config):
            captured.update(config)
            return []

    vendors = dict(registry.VENDOR_CLASSES_BY_TYPE["flash_news"])
    vendors["cls"] = _Vendor
    monkeypatch.setitem(registry.VENDOR_CLASSES_BY_TYPE, "flash_news", vendors)

    assert _cls_collect(None, {"limit": 999}) == []
    assert captured["days"] == 50

    _cls_collect(None, {"limit": 30})
    assert captured["days"] == 30
