"""采集预过滤：明显非市快讯直接不入库，避免无效 LLM。"""

from __future__ import annotations

from src.modules.market.news_store import merge_stocks

# 有这些信号就入库，交给后续分析。
MARKET_TOKENS = (
    "股票",
    "股价",
    "持股",
    "个股",
    "股市",
    "概念股",
    "etf",
    "公告",
    "财报",
    "季报",
    "年报",
    "回购",
    "增持",
    "减持",
    "央行",
    "证监会",
    "降息",
    "加息",
    "降准",
    "板块",
    "上市",
    "退市",
    "研报",
    "中标",
    "订单",
    "业绩",
    "净利",
    "营收",
    "分红",
    "配股",
    "可转债",
    "期货",
    "债券",
    "汇率",
    "原油",
    "黄金",
    "有色",
    "半导体",
    "芯片",
    "券商",
    "基金",
    "ipo",
    "北向",
    "成交额",
    "涨停",
    "跌停",
)

# 仅在没有市场信号时才丢弃。
NOISE_TOKENS = (
    "足球",
    "篮球",
    "世界杯",
    "欧冠",
    "nba",
    "联赛",
    "进球",
    "教练",
    "演唱会",
    "综艺",
    "影帝",
    "影后",
    "绯闻",
    "出轨",
    "天气预报",
    "暴雨预警",
    "台风路径",
    "故意杀人",
    "交通事故",
    "火灾现场",
    # 党建 / 政治宣讲：措辞固定，基本没有可交易映射。地缘、事故类会影响原油黄金军工，不在此列。
    "从严治党",
    "党的建设",
    "党建",
    "党性",
    "党校",
    "反腐败",
    "纪检监察",
    "纪委",
    "组织部",
    "宣传部",
    "政策研究室",
)


def _item_text(item: dict) -> str:
    parts = [str(item.get("digest") or ""), str(item.get("title") or "")]
    for tag in item.get("tags") or []:
        parts.append(str(tag))
    return " ".join(parts).lower()


def has_market_signal(item: dict | None) -> bool:
    """True=落得到 A 股：来源已挂股票，或正文命中市场词。

    比 ``should_ingest_news`` 严格一档：后者对「既不噪音也不含市场词」的宏观快讯
    是默认放行的（老源数量少，放行的代价可控）；快讯源一轮几十条，宏观占比过半，
    默认放行会让 LLM 分析量翻数倍且绝大多数结果被判无用丢弃。所以快讯源用这个
    更严的口径做闸门（见 ``news_sources.NewsSourceSpec.collect`` 的 market_only）。
    """
    if not isinstance(item, dict):
        return False
    if merge_stocks(item.get("relations_stocks")):
        return True
    text = _item_text(item)
    return any(token.lower() in text for token in MARKET_TOKENS)


def should_ingest_news(item: dict | None) -> bool:
    """True=入库。来源已挂股票或含市场词则留；仅噪音且无市场信号则丢。不确定则留。"""
    if not isinstance(item, dict):
        return False
    if str(item.get("news_type") or "") == "report":
        return True
    if merge_stocks(item.get("relations_stocks")):
        return True
    text = _item_text(item)
    if not text.strip():
        return False
    if any(token.lower() in text for token in MARKET_TOKENS):
        return True
    if any(token.lower() in text for token in NOISE_TOKENS):
        return False
    return True
