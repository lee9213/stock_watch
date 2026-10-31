"""单条新闻 LLM 分析：摘要、关联度、利好度、关联股票。

提示词对齐 AlphaLom `prompt_news_analysis.md` + `prompt_155th`，并保留原文已标注股票。
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from string import Template

from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import MarketNews
from src.modules.market.news_store import (
    MAX_ANALYZE_FAILS,
    attach_topic_tags,
    discard_news_row,
    is_weak_signal,
    merge_stocks,
)

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
# 超过视为板块综述：只留最直接标的，并压低关联度，避免一串板块票进催化。
MAX_RELATIONS_STOCKS = 3
GENERIC_RELATION_MAX = 4
# 分析队列 worker 数。
INGEST_ANALYZE_CONCURRENCY = 4
# 定时扫遗漏：只捞入库超过 CATCHUP_MIN_AGE_MINUTES 仍未分析的。
CATCHUP_ANALYZE_LIMIT = 8
CATCHUP_MIN_AGE_MINUTES = 5

ANALYSIS_SYSTEM = (
    "你是资深金融分析师，擅长评估新闻对可交易标的的影响。"
    "只输出一个合法 JSON 对象，不要解释，不要 Markdown 代码块。"
)


@lru_cache(maxsize=1)
def _load_prompt_template() -> Template:
    body = (_PROMPTS_DIR / "news_analysis.md").read_text(encoding="utf-8")
    policy = (_PROMPTS_DIR / "prompt_155th.md").read_text(encoding="utf-8")
    # 模板内含 JSON 花括号示例，必须用 safe_substitute，且先嵌入十五五片段。
    merged = body.replace("$prompt_155th", policy)
    return Template(merged)


def build_analysis_prompt(
    *,
    news_time: str,
    news_content: str,
    known_stocks: list | str | None = None,
) -> str:
    if isinstance(known_stocks, list):
        known_text = json.dumps(known_stocks, ensure_ascii=False) if known_stocks else "无"
    else:
        known_text = str(known_stocks or "无").strip() or "无"
    return _load_prompt_template().safe_substitute(
        news_time=news_time or "",
        news_content=news_content or "",
        known_stocks=known_text,
    )


def _extract_json(raw: str) -> dict | None:
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None


def _clamp(value, low: int, high: int, default: int = 0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _tighten_relations(stocks: list, relation_level: int) -> tuple[list, int]:
    """挂靠过多则截到 3 只，并把关联度压到板块综述上限。"""
    cleaned = merge_stocks(stocks)
    level = _clamp(relation_level, 0, 10)
    if len(cleaned) > MAX_RELATIONS_STOCKS:
        cleaned = cleaned[:MAX_RELATIONS_STOCKS]
        level = min(level, GENERIC_RELATION_MAX)
    return cleaned, level


def normalize_analysis(data: dict | None, fallback_digest: str) -> dict | None:
    if not isinstance(data, dict):
        return None
    digest = " ".join(str(data.get("digest") or fallback_digest or "").split())
    if not digest:
        return None
    tags = []
    for item in data.get("tags") or []:
        text = str(item).strip()
        if text and text not in tags:
            tags.append(text)
    stocks = merge_stocks(data.get("relations_stocks"))
    relation_level = _clamp(data.get("relation_level"), 0, 10)
    if stocks and relation_level <= 0:
        relation_level = 3
    stocks, relation_level = _tighten_relations(stocks, relation_level)
    if relation_level <= 0:
        stocks = []
    bullish = _clamp(data.get("bullish_level"), -10, 10)
    if relation_level <= 0:
        bullish = 0
    return {
        "digest": digest,
        "tags": attach_topic_tags(digest, tags, stocks),
        "relation_level": relation_level,
        "bullish_level": bullish,
        "relations_stocks": stocks,
        "bullish_level_reason": str(data.get("bullish_level_reason") or "").strip(),
        "relation_reason": str(data.get("relation_reason") or "").strip(),
        "analyzed": 1,
    }


async def _complete_analysis(client, prompt: str) -> str:
    from src.platform.ai.usage_recorder import llm_usage_context

    messages = [
        {"role": "system", "content": ANALYSIS_SYSTEM},
        {"role": "user", "content": prompt},
    ]
    extra: dict = {}
    if str(getattr(client, "model", "") or "").startswith("glm-5.3"):
        extra["reasoning_effort"] = "low"
        extra["response_format"] = {"type": "json_object"}
    with llm_usage_context(scene="news_analysis"):
        if hasattr(client, "chat_multi"):
            return await client.chat_multi(messages, temperature=0.2, **extra)
        return await client.chat(ANALYSIS_SYSTEM, prompt, temperature=0.2)


async def analyze_one(client, row: MarketNews) -> dict | None:
    known = merge_stocks(row.relations_stocks)
    prompt = build_analysis_prompt(
        news_time=row.news_time.isoformat() if row.news_time else "",
        known_stocks=known,
        news_content=f"digest: {getattr(row, 'raw_text', None) or row.digest or ''}",
    )
    raw = await _complete_analysis(client, prompt)
    result = normalize_analysis(_extract_json(raw), row.digest or "")
    if not result:
        return None
    result["relations_stocks"] = merge_stocks(known, result["relations_stocks"])
    if result["relations_stocks"] and result["relation_level"] <= 0:
        result["relation_level"] = 3
    result["relations_stocks"], result["relation_level"] = _tighten_relations(
        result["relations_stocks"], result["relation_level"]
    )
    if result["relation_level"] <= 0:
        result["bullish_level"] = 0
        result["relations_stocks"] = []
    return result


def _apply_analysis_result(row: MarketNews, result: dict | None) -> bool:
    """写回分析结果；解析失败保持未分析，供 catchup 重试。"""
    if not result:
        return False
    row.digest = result["digest"]
    row.tags = attach_topic_tags(result["digest"], result.get("tags") or [], result["relations_stocks"])
    row.relation_level = result["relation_level"]
    row.bullish_level = result["bullish_level"]
    row.relations_stocks = result["relations_stocks"]
    row.bullish_level_reason = str(result.get("bullish_level_reason") or "").strip()
    row.relation_reason = str(result.get("relation_reason") or "").strip()
    row.analyzed = 1
    row.analyze_fail_count = 0
    return True


def mark_analyze_failure(row: MarketNews) -> int:
    """解析/调用失败加一次计数，达到上限后不再进 catchup。"""
    current = int(getattr(row, "analyze_fail_count", 0) or 0)
    row.analyze_fail_count = current + 1
    return int(row.analyze_fail_count)


async def _analyze_row_by_id(client, news_id: int) -> bool:
    """单条分析：独立 Session，便于并发。成功写出结构化结果返回 True。"""
    db = SessionLocal()
    try:
        row = db.query(MarketNews).filter(MarketNews.id == news_id).first()
        if row is None or int(row.analyzed or 0) != 0:
            return False
        if int(getattr(row, "analyze_fail_count", 0) or 0) >= MAX_ANALYZE_FAILS:
            return False
        try:
            result = await analyze_one(client, row)
        except Exception:
            fails = mark_analyze_failure(row)
            db.commit()
            logger.exception("新闻分析失败 id=%s fails=%s", news_id, fails)
            return False
        ok = _apply_analysis_result(row, result)
        if not ok:
            fails = mark_analyze_failure(row)
            db.commit()
            logger.warning(
                "新闻分析结果无法解析，保持未分析 id=%s fails=%s",
                news_id,
                fails,
            )
            return False
        if is_weak_signal(row.bullish_level):
            discard_news_row(db, row)
            db.commit()
            logger.debug("新闻利多利空过弱，已删除 id=%s bullish=%s", news_id, row.bullish_level)
            return True
        db.commit()
        if result.get("relation_reason") or result.get("bullish_level_reason"):
            logger.debug(
                "新闻分析理由 id=%s relation=%s bullish=%s",
                news_id,
                (result.get("relation_reason") or "")[:80],
                (result.get("bullish_level_reason") or "")[:80],
            )
        return ok
    finally:
        db.close()


async def analyze_by_id(news_id: int, client=None) -> bool:
    """按新闻 id 分析一条；供分析队列 worker 调用。"""
    if client is None:
        db = SessionLocal()
        try:
            from src.platform.ai.ai_failover import get_configured_failover_client

            client = get_configured_failover_client(db)
        finally:
            db.close()
    if not getattr(client, "model", None):
        logger.info("新闻分析跳过：未配置默认模型 id=%s", news_id)
        return False
    return await _analyze_row_by_id(client, int(news_id))
