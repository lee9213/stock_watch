"""事件驱动新闻源注册表：各源采集适配器 + 开关/顺序解析。

每个适配器统一返回 ``news_store`` 期望的条目字典（``digest`` / ``raw_text`` /
``news_md5`` / ``title`` / ``tags`` / ``relations_stocks`` / ``news_time`` /
``news_type`` / ``url`` / ``sources``），源与源之间的字段差异只在这一层收敛。

**开关与顺序不在代码里写死**，由 ``data_sources`` 表 ``type='event_news'`` 的行决定：

- ``enabled=False`` 的源整轮跳过，连一次 HTTP 都不发；
- ``priority`` 越小越先入库。同一事件被多个源同时报到时，先入库的那条留下，
  后到的被同轮共享的判重快照挡掉 —— 所以排最前的源等于「默认代表来源」。

表里一条 ``event_news`` 都没有（老库尚未 seed）时回退到 ``NEWS_SOURCE_SPECS``
的默认全量，避免升级瞬间停采。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from src.modules.market.news_prefilter import has_market_signal
from src.modules.market.news_store import is_within_ingest_window, news_md5
from src.modules.market.stockstar_news import (
    fetch_stockstar_news,
    fetch_stockstar_reports,
    fetch_stockstar_roll,
)
from src.modules.market.ths_news import fetch_ths_flash
from src.platform.persistence.models import DataSource

logger = logging.getLogger(__name__)

# data_sources.type 的取值：事件驱动新闻流专用（与 marketdata 的 flash_news 区分，
# 后者只做「快讯接口连通性测试」，不参与事件驱动入库）。
EVENT_NEWS_SOURCE_TYPE = "event_news"

# A 股/北交所代码：60/68 沪市、00/30 深市、8x/4x 北交所、9x B 股。
# 新浪快讯的 symbols 里混着 fx_susdinr / hf_cl 这类外汇期货代码，必须过滤，
# 否则会被当成「关联个股」写进 market_news.relations_stocks。
_A_SHARE_CODE = re.compile(r"^(?:60|68|00|30|8\d|4\d|9\d)\d{4}$")


def _a_share_stocks(codes) -> list[dict[str, str]]:
    """把来源给的代码列表收敛成 ``[{"code","name"}]``，只留 A 股。"""
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in codes or []:
        code = str(raw or "").strip()
        if not code or code in seen or not _A_SHARE_CODE.match(code):
            continue
        seen.add(code)
        # 快讯接口只给代码不给名称；name 用代码兜底，下游按 code 为主键用。
        rows.append({"code": code, "name": code})
    return rows


def _compose_digest(title: str, body: str) -> str:
    """标题+正文拼接，正文已包含标题时不重复（与同花顺源口径一致）。"""
    title = (title or "").strip()
    body = (body or "").strip()
    if not title:
        return body
    if not body or title in body:
        return body or title
    return f"{title}。{body}"


def _flash_news_items(news, *, source_tag: str, dedup=None) -> list[dict]:
    """``marketdata.FlashNews`` 列表 → 统一 item dict。"""
    items: list[dict] = []
    for entry in news or []:
        title = (getattr(entry, "title", "") or "").strip()
        body = (getattr(entry, "content", "") or "").strip()
        digest = _compose_digest(title, body)
        moment = getattr(entry, "publish_time", None)
        if not digest or not is_within_ingest_window(moment):
            continue
        # 抓取阶段就按标题判重：快讯不抓详情，省下的只是一次入库判重，
        # 但能让「同一事件两源同轮报到」时少一条进 prepared 列表。
        if dedup is not None and dedup.is_duplicate_title(title or digest, moment):
            continue
        items.append(
            {
                "external_id": str(getattr(entry, "external_id", "") or ""),
                "digest": digest,
                "raw_text": body or digest,
                # 去重键沿用「仅正文」口径，避免同一正文配上不同标题后被当成新条目。
                "news_md5": news_md5(body or title or digest),
                "title": title,
                "tags": [],
                "relations_stocks": _a_share_stocks(getattr(entry, "symbols", None)),
                "news_time": moment,
                "news_type": "news",
                "url": (getattr(entry, "url", "") or "").strip(),
                "sources": source_tag,
            }
        )
    return items


# ---------------------------------------------------------------------------
# 采集适配器：统一签名 (dedup, config) -> list[dict]
# ---------------------------------------------------------------------------


def _fetch_ths(dedup, config: dict) -> list[dict]:
    pages = max(int(config.get("pages") or 1), 1)
    pagesize = int(config.get("pagesize") or 50)
    items: list[dict] = []
    for page in range(1, pages + 1):
        items.extend(fetch_ths_flash(page=page, pagesize=pagesize))
    return items


def _fetch_stockstar(dedup, config: dict) -> list[dict]:
    news_limit = int(config.get("news_limit") or 6)
    items: list[dict] = list(
        fetch_stockstar_news(
            limit=news_limit,
            skip_urls=dedup.urls,
            precheck=dedup.precheck,
        )
    )
    skip = set(dedup.urls) | {item.get("url") or "" for item in items}
    items.extend(
        fetch_stockstar_reports(
            limit=int(config.get("report_limit") or 4),
            skip_urls=skip,
            precheck=dedup.precheck,
        )
    )
    if config.get("include_roll"):
        skip |= {item.get("url") or "" for item in items}
        items.extend(
            fetch_stockstar_roll(
                limit=max(2, news_limit // 2),
                skip_urls=skip,
                precheck=dedup.precheck,
            )
        )
    return items


def _from_flash_vendor(
    vendor_name: str, source_tag: str, dedup, config: dict, max_limit: int = 100
) -> list[dict]:
    """复用 marketdata 的快讯 vendor（接口细节已在那里解析），只做字段转换。

    惰性 import：marketdata 是独立包，事件驱动链路不启动时不必加载。
    vendor 的 config 键名是历史遗留的 ``days``，实际语义为「条数」。

    ``max_limit``：上游对 page_size 有硬上限，超限不一定报错——财联社 rn>50 直接
    返回空列表，表现为「源静默停采」。所以这里按源夹住，用户把 limit 调大也不会
    把源弄哑。
    """
    from marketdata.registry import VENDOR_CLASSES_BY_TYPE

    limit = max(min(int(config.get("limit") or 50), max_limit), 1)
    vendor_cls = VENDOR_CLASSES_BY_TYPE["flash_news"][vendor_name]
    news = vendor_cls().fetch([], {"days": limit})
    return _flash_news_items(news, source_tag=source_tag, dedup=dedup)


def _cls_collect(dedup, config: dict) -> list[dict]:
    # 实测 cls 的 rn=50 正好返回 50 条，rn=100 返回 0 条（签名参数合法但被上游拒）。
    return _from_flash_vendor("cls", "cls", dedup, config, max_limit=50)


def _sina_collect(dedup, config: dict) -> list[dict]:
    return _from_flash_vendor("sina", "sina", dedup, config)


def _eastmoney_collect(dedup, config: dict) -> list[dict]:
    return _from_flash_vendor("eastmoney", "eastmoney", dedup, config)


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NewsSourceSpec:
    """一个可开关、可排序的事件驱动新闻源。"""

    key: str  # 与 data_sources.provider 一致
    label: str
    description: str
    config: dict
    fetch: Callable[..., list[dict]]

    def collect(self, dedup, config: dict | None = None) -> list[dict]:
        """按「默认 config ← 数据库 config」覆盖后采集；单源异常只影响自己。

        ``market_only=True`` 时只保留落得到 A 股的条目（含市场词或带股票标注）。
        快讯源一轮几十条、宏观占一半以上，不设闸门会让分析量翻数倍且结论多被丢弃。
        """
        merged = {**self.config, **(config or {})}
        try:
            items = self.fetch(dedup, merged) or []
        except Exception:
            logger.exception("事件驱动新闻源采集失败 source=%s", self.key)
            return []
        if merged.get("market_only"):
            items = [item for item in items if has_market_signal(item)]
        return items


# 顺序 = 默认采集与入库顺序（= seed 里的 priority 顺序）：
# 时效快的在前，让「同事件多源报到」时由最快的源做代表。
#
# ``market_only`` 默认只给「宏观为主、一轮几十条」的快讯源开启：实测一轮 50 条里
# 新浪只有 22%、东财 48%、财联社 52% 含市场词，剩下全是海外政治/赛事/天气类，
# 分析后基本被丢弃。判据见 news_prefilter.has_market_signal。
#
# ``limit`` 统一 50：上游接口的 page_size 上限就是 50 左右，再大也拿不到更多。
NEWS_SOURCE_SPECS: dict[str, NewsSourceSpec] = {
    "cls": NewsSourceSpec(
        key="cls",
        label="财联社电报",
        description="秒级电报，A 股首发最快；本地签名、免 key。不带个股标注。",
        config={"limit": 50, "market_only": True},
        fetch=_cls_collect,
    ),
    "eastmoney": NewsSourceSpec(
        key="eastmoney",
        label="东方财富快讯",
        description="东财 7×24 快讯，个股公告与宏观都覆盖。不带个股标注。",
        config={"limit": 50, "market_only": True},
        fetch=_eastmoney_collect,
    ),
    "sina": NewsSourceSpec(
        key="sina",
        label="新浪财经7x24",
        description="新浪 7×24 直播，部分条目自带关联个股（ext.stocks）。宏观占比最高。",
        config={"limit": 50, "market_only": True},
        fetch=_sina_collect,
    ),
    "ths": NewsSourceSpec(
        key="ths",
        label="同花顺快讯",
        description="同花顺股票快讯，唯一稳定带个股代码的源；会抓详情页补全文。",
        config={"pages": 1, "pagesize": 50},
        fetch=_fetch_ths,
    ),
    "stock_star": NewsSourceSpec(
        key="stock_star",
        label="证券之星",
        description="证券之星资讯/研报列表，会抓详情页；列表页常回填旧文，时效偏慢。",
        # 每条都要抓详情页且串行限速（delay 0.35s），50 条约需 40s，是本轮的耗时瓶颈。
        config={"news_limit": 40, "report_limit": 10, "include_roll": False},
        fetch=_fetch_stockstar,
    ),
}


def load_news_sources(db: Session) -> list[tuple[NewsSourceSpec, dict]]:
    """读启用的事件驱动新闻源，按 priority 升序（顺序即入库顺序）。

    表里没有任何 ``event_news`` 行时回退默认全量：老库升级后不会突然停采。
    有行但全部 disabled 时返回空 —— 用户主动全关就该真的不采。
    """
    rows = (
        db.query(DataSource)
        .filter(DataSource.type == EVENT_NEWS_SOURCE_TYPE)
        .order_by(DataSource.priority, DataSource.id)
        .all()
    )
    if not rows:
        logger.info("data_sources 无 event_news 配置，回退默认全量源")
        return [(spec, {}) for spec in NEWS_SOURCE_SPECS.values()]

    sources: list[tuple[NewsSourceSpec, dict]] = []
    for row in rows:
        spec = NEWS_SOURCE_SPECS.get(row.provider)
        if spec is None:
            logger.warning("未知的事件驱动新闻源 provider=%s，已跳过", row.provider)
            continue
        if not row.enabled:
            continue
        sources.append((spec, dict(row.config or {})))
    return sources


def source_summary(db: Session) -> list[dict]:
    """给接口/前端看的所有源状态（含被关闭的），按 priority 排序。"""
    rows = (
        db.query(DataSource)
        .filter(DataSource.type == EVENT_NEWS_SOURCE_TYPE)
        .order_by(DataSource.priority, DataSource.id)
        .all()
    )
    if not rows:
        return [
            {"provider": spec.key, "name": spec.label, "enabled": True, "priority": i * 5}
            for i, spec in enumerate(NEWS_SOURCE_SPECS.values())
        ]
    out: list[dict] = []
    for row in rows:
        spec = NEWS_SOURCE_SPECS.get(row.provider)
        out.append(
            {
                "id": row.id,
                "provider": row.provider,
                "name": row.name or (spec.label if spec else row.provider),
                "enabled": bool(row.enabled),
                "priority": int(row.priority or 0),
                "implemented": spec is not None,
            }
        )
    return out
