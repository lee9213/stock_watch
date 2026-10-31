"""小时级新闻流速览：把窗口内新闻交给默认模型，压成三条头条后落库。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from src.platform.persistence.database import SessionLocal
from src.modules.market.news_store import (
    KEEP_DAYS,
    is_weak_signal,
    latest_digest,
    latest_digest_input_hash,
    list_news_in_window,
    next_refresh_at,
    prune_digests,
    prune_discards,
    prune_market_news,
    save_digest,
)

logger = logging.getLogger(__name__)

WINDOW_STEPS_HOURS = (1, 2, 4, 8, 24)
MIN_NEWS_FOR_DIGEST = 5
MAX_NEWS_IN_PROMPT = 50
MAX_HEADLINES = 3
MAX_KEYWORDS = 8
MAX_HIGHLIGHTS = 5
DIGEST_TITLE = "新闻流速览"
DIGEST_REFRESH_COOLDOWN_SEC = 120

DIGEST_SYSTEM = "你是财经新闻编辑。只输出一个合法 JSON 对象，不要解释，不要代码块。"
DIGEST_USER = """把过去一段时间的新闻流压缩成读者 10 秒能看完的速览。读者是要快速掌握市场在关注什么的投资者。

速览日期：{digest_date}
统计窗口：{window_desc}
窗口内新闻条数：{news_count}

新闻流（按时间倒序）：
{news_list}

输出 JSON：
{{
  "title": "新闻流速览",
  "headlines": [
    {{
      "topic": "市场焦点",
      "keywords": ["固态电池", "创新药"],
      "summary": "一句话总结发生了什么以及对谁有影响"
    }}
  ],
  "highlights": ["排不进前三条但仍有信息量的要点"]
}}

硬性约束：
- headlines 必须正好 3 条；topic 从 市场焦点、隔夜外盘、政策动向、产业动向、资金面、个股要闻、风险提示 中选，2-6 个字。
- keywords 每条 3-8 个，每个 2-6 字，贴近原文板块/概念，禁止上涨/利好/关注这类空词。
- summary 不超过 40 字，只复述输入里已有的事实，不要预测涨跌。
- highlights 0-5 条，每条不超过 40 字；没有就 []。
- headlines[0] 最重要：先看关联度，再看是否有政策/公司动作。
"""

_refresh_lock_until = 0.0


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


def normalize_digest_result(data: Any) -> dict | None:
    if not isinstance(data, dict):
        return None
    headlines = []
    for item in data.get("headlines") or []:
        if not isinstance(item, dict):
            continue
        keywords = []
        raw_keywords = item.get("keywords")
        if isinstance(raw_keywords, str):
            raw_keywords = raw_keywords.replace("，", "、").replace(",", "、").split("、")
        if isinstance(raw_keywords, list):
            for keyword in raw_keywords:
                text = str(keyword).strip().strip("、，,;； ")
                if text and text not in keywords:
                    keywords.append(text)
        summary = " ".join(str(item.get("summary") or "").split())
        if not keywords and not summary:
            continue
        headlines.append({
            "topic": (str(item.get("topic") or "").strip() or "重点")[:10],
            "keywords": keywords[:MAX_KEYWORDS],
            "summary": summary,
        })
        if len(headlines) >= MAX_HEADLINES:
            break
    if not headlines:
        return None
    highlights = []
    for item in data.get("highlights") or []:
        text = " ".join(str(item).split())
        if text:
            highlights.append(text)
        if len(highlights) >= MAX_HIGHLIGHTS:
            break
    return {
        "title": (str(data.get("title") or "").strip() or DIGEST_TITLE)[:60],
        "headlines": headlines,
        "highlights": highlights,
    }


def _format_news_lines(items: list[dict]) -> str:
    lines = []
    for index, item in enumerate(items, start=1):
        raw_time = item.get("news_time") or ""
        hhmm = raw_time[11:16] if isinstance(raw_time, str) and len(raw_time) >= 16 else "--:--"
        digest = " ".join((item.get("digest") or "").split())
        tags = "、".join(str(tag) for tag in (item.get("tags") or [])[:6])
        parts = [f"{index}. [{hhmm}] {digest}"]
        if tags:
            parts.append(f"标签:{tags}")
        level = item.get("relation_level")
        bullish = item.get("bullish_level")
        parts.append(f"关联度:{level if isinstance(level, int) else '-'}")
        parts.append(f"利好度:{bullish:+d}" if isinstance(bullish, int) else "利好度:-")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def _is_analyzed(item: dict) -> bool:
    try:
        return int(item.get("analyzed") or 0) == 1
    except (TypeError, ValueError):
        return False


def _select_for_prompt(items: list[dict]) -> list[dict]:
    def level(item: dict) -> int:
        try:
            return int(item.get("relation_level") or 0)
        except (TypeError, ValueError):
            return 0

    analyzed = [item for item in items if _is_analyzed(item)]
    pending = [item for item in items if not _is_analyzed(item)]
    relevant = sorted([item for item in analyzed if level(item) >= 3], key=level, reverse=True)
    weak = [item for item in analyzed if level(item) < 3]
    picked = relevant[:35] + weak
    remain = max(0, MAX_NEWS_IN_PROMPT - len(picked))
    if remain:
        picked = picked + pending[:remain]
    return picked[:MAX_NEWS_IN_PROMPT]


def collect_digest_items(
    db,
    now: datetime,
    *,
    force: bool = False,
) -> tuple[int | None, list[dict]]:
    """先扩窗口凑已分析条数；不够再补未分析。够 5 条已分析则不再混入 pending。"""
    analyzed: list[dict] = []
    pending: list[dict] = []
    used_hours: int | None = None
    for hours in WINDOW_STEPS_HOURS:
        start = now - timedelta(hours=hours)
        analyzed = _dedupe([
            item
            for item in list_news_in_window(db, start, now, analyzed=1)
            if not is_weak_signal(item.get("bullish_level"))
        ])
        pending = _dedupe([
            item
            for item in list_news_in_window(db, start, now, analyzed=0)
            if item.get("analysis_status") != "failed"
        ])
        if len(analyzed) >= MIN_NEWS_FOR_DIGEST:
            used_hours = hours
            pending = []
            break
        if len(analyzed) + len(pending) >= MIN_NEWS_FOR_DIGEST:
            used_hours = hours
            break
    if used_hours is None:
        if not force:
            logger.warning("新闻速览跳过：近 %s 小时仅 %s 条", WINDOW_STEPS_HOURS[-1], len(analyzed) + len(pending))
            return None, []
        used_hours = WINDOW_STEPS_HOURS[-1]
    return used_hours, _dedupe(analyzed + pending)


def digest_input_hash(items: list[dict]) -> str:
    payload = [
        {
            "id": str(item.get("id") or ""),
            "digest": item.get("digest") or "",
            "analyzed": int(item.get("analyzed") or 0),
            "relation_level": int(item.get("relation_level") or 0),
            "bullish_level": int(item.get("bullish_level") or 0),
        }
        for item in items
    ]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _dedupe(items: list[dict]) -> list[dict]:
    seen = set()
    unique = []
    for item in items:
        key = " ".join((item.get("digest") or "").split())
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


async def _complete_digest(client, prompt: str) -> str:
    from src.platform.ai.usage_recorder import llm_usage_context

    messages = [
        {"role": "system", "content": DIGEST_SYSTEM},
        {"role": "user", "content": prompt},
    ]
    extra: dict = {}
    if str(getattr(client, "model", "") or "").startswith("glm-5.3"):
        extra["reasoning_effort"] = "low"
        extra["response_format"] = {"type": "json_object"}
    with llm_usage_context(scene="news_digest"):
        if hasattr(client, "chat_multi"):
            return await client.chat_multi(messages, temperature=0.2, **extra)
        return await client.chat(DIGEST_SYSTEM, prompt, temperature=0.2)


async def _ask_digest(prompt: str) -> tuple[dict | None, str | None]:
    from src.platform.ai.usage_recorder import llm_usage_context

    db = SessionLocal()
    try:
        from src.platform.ai.ai_failover import get_configured_failover_client

        client = get_configured_failover_client(db)
        model_name = getattr(client, "model", None)
        if not model_name:
            logger.warning("新闻速览跳过：未配置模型")
            return None, None
        raw = await _complete_digest(client, prompt)
        result = normalize_digest_result(_extract_json(raw))
        if result:
            return result, model_name
        logger.warning("新闻速览 JSON 模式未解析成功，改用文本重试。片段：%s", (raw or "")[:200])
        with llm_usage_context(scene="news_digest"):
            raw = await client.chat(DIGEST_SYSTEM + " 必须只输出 JSON。", prompt, temperature=0.1)
        return normalize_digest_result(_extract_json(raw)), model_name
    except Exception:
        logger.exception("新闻速览模型调用失败")
        return None, None
    finally:
        db.close()


async def build_news_digest(trigger_type: str = "auto", force: bool = False) -> dict | None:
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        used_hours, items = collect_digest_items(db, now, force=force)
        if used_hours is None or not items:
            return None
        window_start = now - timedelta(hours=used_hours)
        selected = _select_for_prompt(items)
        fingerprint = digest_input_hash(selected)
        if not force and fingerprint and latest_digest_input_hash(db) == fingerprint:
            logger.info("新闻速览跳过：输入未变 hash=%s", fingerprint[:8])
            return latest_digest(db)
        prompt = DIGEST_USER.format(
            digest_date=now.astimezone().strftime("%Y-%m-%d %H:%M"),
            window_desc=f"{window_start.strftime('%m-%d %H:%M')} ~ {now.strftime('%m-%d %H:%M')}（近 {used_hours} 小时）",
            news_count=len(selected),
            news_list=_format_news_lines(selected),
        )
    finally:
        db.close()

    result, model_name = await _ask_digest(prompt)
    if not result:
        return None
    db = SessionLocal()
    try:
        saved = save_digest(
            db,
            headlines=result["headlines"],
            highlights=result["highlights"],
            title=result["title"],
            window_start=window_start,
            window_end=now,
            news_count=len(selected),
            model=model_name,
            trigger_type=trigger_type,
            generated_at=now,
            input_hash=fingerprint,
        )
        if saved:
            logger.info("新闻速览已生成：窗口 %sh / %s 条 / 头条 %s", used_hours, len(selected), len(saved.get("headlines") or []))
        return saved
    finally:
        db.close()


async def job_news_digest_hourly() -> None:
    try:
        await build_news_digest(trigger_type="auto")
        db = SessionLocal()
        try:
            prune_digests(db, KEEP_DAYS)
            prune_market_news(db, KEEP_DAYS)
            prune_discards(db)
        finally:
            db.close()
    except Exception:
        logger.exception("新闻速览定时任务失败")


def digest_payload(db) -> dict:
    latest = latest_digest(db)
    nxt = next_refresh_at()
    return {
        "digest": latest,
        "next_refresh_at": nxt.strftime("%Y-%m-%d %H:%M:%S"),
        "refresh_rule": {
            "hour_start": 7,
            "hour_end": 23,
            "minute": 30,
        },
        "timezone": "Asia/Shanghai",
    }


def acquire_refresh_lock() -> int | None:
    import time

    global _refresh_lock_until
    now = time.monotonic()
    if now < _refresh_lock_until:
        return max(1, int(_refresh_lock_until - now))
    _refresh_lock_until = now + DIGEST_REFRESH_COOLDOWN_SEC
    return None


def release_refresh_lock() -> None:
    global _refresh_lock_until
    _refresh_lock_until = 0.0
