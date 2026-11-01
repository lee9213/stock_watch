"""新闻 LLM 分析：摘要、关联度、利好度、关联股票。

提示词对齐 AlphaLom `prompt_news_analysis.md` + `prompt_155th`，并保留原文已标注股票。
支持批量分析（一次调用多条，省掉重复的固定指令 token）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
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
# 分析队列 worker 数。实测新闻到达约 5 条/小时、每轮采集 0.25 条，单 worker
# 的处理能力（约 6 条/分钟）已经远超供给；4 个 worker 只是空转，还各自持有一份
# 60s 过期的模型客户端缓存（每分钟各重载一次 DB 会话）。留 2 个用于覆盖
# 补扫与突发，够用。
INGEST_ANALYZE_CONCURRENCY = 2
# 一次 LLM 调用最多分析几条新闻（固定指令只发一次，实测省约 45% token）。
BATCH_ANALYZE_SIZE = 5
# 定时扫遗漏：只捞入库超过 CATCHUP_MIN_AGE_MINUTES 仍未分析的。
CATCHUP_ANALYZE_LIMIT = 8
CATCHUP_MIN_AGE_MINUTES = 5
# 「未配置模型」提示的打印间隔：逐条打印会在没配 AI 时刷满日志表。
MODEL_MISSING_LOG_INTERVAL_SEC = 300

# 单次分析调用自己的超时。AsyncOpenAI 默认 timeout=600s 且 max_retries=2，
# 模型侧挂起时一个 worker 能被钉住半小时，200 条队列只涨不跌；必须自己设上限。
ANALYZE_TIMEOUT_SEC = 90
# 批量结果整体不可解析时，条数超过这个值就先对半拆再试，而不是逐条重打：
# 一次批量失败逐条重打等于把 1 次失败放大成 N 次，还会把每条的重试额度一起烧掉。
PARSE_FALLBACK_MAX_ITEMS = 2
# 连续 N 次「调用本身失败」（超时/连接/5xx/鉴权）就熔断，避免 4 个 worker 空打
# 同一组必挂的模型。退避 30s 起、最长 5 分钟，任何一次调用成功即清零。
BREAKER_FAIL_THRESHOLD = 3
BREAKER_MIN_BACKOFF_SEC = 30.0
BREAKER_MAX_BACKOFF_SEC = 300.0

ANALYSIS_SYSTEM = (
    "你是资深金融分析师，擅长评估新闻对可交易标的的影响。"
    "只输出一个合法 JSON 对象，不要解释，不要 Markdown 代码块。"
)

# 批量提示词复用单条模板的规则部分（切在这一行之前），只替换输出格式与输入段。
_RULES_SPLIT_MARKER = "## 输出格式"

# 命中这些词才注入「十五五重点方向」清单（见 _policy_focus_block）。
POLICY_FOCUS_HINTS = (
    "政策",
    "规划",
    "方案",
    "意见",
    "通知",
    "部委",
    "国务院",
    "发改委",
    "工信部",
    "财政部",
    "证监会",
    "央行",
    "十五五",
    "试点",
    "补贴",
    "专项",
    "指导意见",
    "工业母机",
    "高端仪器",
    "基础软件",
    "6G",
    "具身智能",
    "氢能",
    "核聚变",
    "脑机接口",
    "生物制造",
    "低空经济",
    "人工智能",
)


def _needs_policy_focus(text: str) -> bool:
    return any(hint in (text or "") for hint in POLICY_FOCUS_HINTS)

# 输出格式说明压到最短：JSON 结构本身已经说明了字段，重复的「不要加解释」
# 「不要 Markdown 代码块」对模型没有额外约束力（_extract_json 也容错）。
# 原版 445 字里约 260 字是重复强调，固定指令占单次调用成本 82%，值得压。
BATCH_OUTPUT_SPEC = """## 输出格式
仅输出 JSON，不要 Markdown 代码块：
{"results":[{"index":1,"digest":"概要","tags":["关键词"],"relation_level":0,"bullish_level":0,"bullish_level_reason":"理由","relations_stocks":[{"code":"代码","name":"名称"}],"relation_reason":"理由"}]}
results 条数必须等于新闻条数，index 必须对应；字段齐全，判断不了给 0 与 []；各条独立判断，不合并同类事件。
"""

_model_missing_logged_at = 0.0

# 调用级熔断状态。放在模块级是刻意的：4 个 worker 共享同一份计数，
# 天然形成「全局熔断」，而不是每个 worker 各自撞一遍同样的墙。
_breaker_until = 0.0
_fail_streak = 0


def analysis_breaker_remaining() -> float:
    """熔断剩余秒数；>0 时调用方应等待而不是继续消费队列。"""
    return max(0.0, _breaker_until - time.monotonic())


def reset_analysis_breaker() -> None:
    """清空熔断状态（测试隔离 / 手动恢复用）。"""
    global _breaker_until, _fail_streak
    _breaker_until = 0.0
    _fail_streak = 0


def _reset_breaker() -> None:
    global _breaker_until, _fail_streak
    if _fail_streak or _breaker_until:
        _fail_streak = 0
        _breaker_until = 0.0


def _trip_breaker() -> float:
    """记一次调用失败；达到阈值后开熔断，返回本次退避秒数（未熔断返回 0）。"""
    global _breaker_until, _fail_streak
    _fail_streak += 1
    if _fail_streak < BREAKER_FAIL_THRESHOLD:
        return 0.0
    backoff = min(
        BREAKER_MAX_BACKOFF_SEC,
        BREAKER_MIN_BACKOFF_SEC * (2 ** (_fail_streak - BREAKER_FAIL_THRESHOLD)),
    )
    _breaker_until = time.monotonic() + backoff
    return backoff


def _is_item_specific_error(exc: Exception) -> bool:
    """批量调用失败是否可能由「某一条内容」引起。

    只有 400/422 这类参数或内容问题才值得逐条重试定位——换一条能过。
    超时、连接失败、5xx、鉴权失效都是整批性故障，逐条重试只会把同一个失败
    放大成 N 倍（还会把每条的重试额度一起耗掉）。
    """
    try:
        from openai import BadRequestError
    except Exception:  # pragma: no cover — openai 一定装得上
        return False
    return isinstance(exc, BadRequestError)


@lru_cache(maxsize=4)
def _render_template(policy_focus: str) -> Template:
    """按「是否注入政策清单」两个版本缓存模板。"""
    base = (_PROMPTS_DIR / "news_analysis.md").read_text(encoding="utf-8")
    policy = (_PROMPTS_DIR / "prompt_155th.md").read_text(encoding="utf-8")
    merged = base.replace("$prompt_155th", policy).replace("$policy_focus", policy_focus)
    # 模板内含 JSON 花括号示例，必须用 safe_substitute。
    return Template(merged)


@lru_cache(maxsize=4)
def _load_batch_rules(policy_focus: str = "") -> str:
    """单条模板里「输出格式」之前的规则段，直接复用，避免两套提示词漂移。"""
    base = (_PROMPTS_DIR / "news_analysis.md").read_text(encoding="utf-8")
    policy = (_PROMPTS_DIR / "prompt_155th.md").read_text(encoding="utf-8")
    rules, _, _ = base.partition(_RULES_SPLIT_MARKER)
    merged = rules.replace("$prompt_155th", policy).replace("$policy_focus", policy_focus)
    return merged.strip()


@lru_cache(maxsize=1)
def _policy_focus_text() -> str:
    return (_PROMPTS_DIR / "prompt_policy_focus.md").read_text(encoding="utf-8").strip()


def _policy_focus_block(text: str) -> str:
    """政策清单只在相关新闻上注入：它只影响多标的间的排序偏好，
    对「某公司发债」「某药企获批」这类新闻无作用（实测占单条 prompt 14%）。"""
    return _policy_focus_text() if _needs_policy_focus(text) else ""


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
    policy = _policy_focus_block(f"{news_content or ''} {known_text}")
    return _render_template(policy).safe_substitute(
        news_time=news_time or "",
        news_content=news_content or "",
        known_stocks=known_text,
    )


def build_batch_prompt(items: list["NewsSnapshot"]) -> str:
    """把 N 条新闻拼成一次调用；规则段与单条分析完全一致。"""
    combined = "\n".join((item.raw_text or item.digest or "") for item in items)
    policy = _policy_focus_block(combined)
    blocks: list[str] = [_load_batch_rules(policy), BATCH_OUTPUT_SPEC.strip(), ""]
    blocks.append(f"## 待分析新闻（共 {len(items)} 条）")
    for index, item in enumerate(items, start=1):
        known = json.dumps(item.relations_stocks, ensure_ascii=False) if item.relations_stocks else "无"
        moment = item.news_time.isoformat() if item.news_time else ""
        blocks.extend(
            [
                "",
                f"### {index}",
                f"发布时间：{moment}",
                f"原文已标注股票（优先保留，可补充，勿无故删除）：{known}",
                "内容：",
                f"digest: {item.raw_text or item.digest}",
            ]
        )
    return "\n".join(blocks).strip() + "\n"


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


def _merge_known(result: dict, known: list) -> dict:
    """保留来源已标注的股票，再压关联度。"""
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


async def _complete_with_timeout(
    client, prompt: str, *, timeout: float | None = None
) -> str:
    """包一层自己的超时，别让 worker 被 provider 的 600s 默认超时钉死。"""
    limit = ANALYZE_TIMEOUT_SEC if timeout is None else float(timeout)
    return await asyncio.wait_for(_complete_analysis(client, prompt), timeout=limit)


@dataclass
class NewsSnapshot:
    """分析所需字段的只读快照：LLM 调用期间不再持有数据库会话。"""

    id: int
    digest: str
    raw_text: str = ""
    news_time: datetime | None = None
    relations_stocks: list = field(default_factory=list)


def _snapshot(row: MarketNews) -> NewsSnapshot:
    return NewsSnapshot(
        id=int(row.id),
        digest=row.digest or "",
        raw_text=getattr(row, "raw_text", None) or row.digest or "",
        news_time=row.news_time,
        relations_stocks=merge_stocks(row.relations_stocks),
    )


def _load_pending(news_ids: list[int]) -> list[NewsSnapshot]:
    """读待分析行的快照后立刻关会话；已分析 / 超过失败上限的直接跳过。"""
    ids = [int(x) for x in news_ids if x is not None]
    if not ids:
        return []
    db = SessionLocal()
    try:
        rows = db.query(MarketNews).filter(MarketNews.id.in_(ids)).all()
        by_id = {int(row.id): row for row in rows}
        snapshots: list[NewsSnapshot] = []
        for news_id in ids:
            row = by_id.get(news_id)
            if row is None or int(row.analyzed or 0) != 0:
                continue
            if int(getattr(row, "analyze_fail_count", 0) or 0) >= MAX_ANALYZE_FAILS:
                continue
            snapshots.append(_snapshot(row))
        return snapshots
    finally:
        db.close()


def _record_failure(news_id: int) -> int:
    """调用/解析失败加一次计数；独立短会话，不占着连接。"""
    db = SessionLocal()
    try:
        row = db.get(MarketNews, int(news_id))
        if row is None:
            return 0
        fails = mark_analyze_failure(row)
        db.commit()
        return fails
    finally:
        db.close()


async def analyze_one(client, row) -> dict | None:
    known = merge_stocks(row.relations_stocks)
    prompt = build_analysis_prompt(
        news_time=row.news_time.isoformat() if row.news_time else "",
        known_stocks=known,
        news_content=f"digest: {getattr(row, 'raw_text', None) or row.digest or ''}",
    )
    raw = await _complete_with_timeout(client, prompt)
    result = normalize_analysis(_extract_json(raw), row.digest or "")
    if not result:
        return None
    return _merge_known(result, known)


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


def _save_result(snapshot: NewsSnapshot, result: dict | None) -> bool:
    """在新会话里写回结果；失败只加计数，等下轮补扫。"""
    db = SessionLocal()
    try:
        row = db.get(MarketNews, int(snapshot.id))
        if row is None:
            return False
        ok = _apply_analysis_result(row, result)
        if not ok:
            fails = mark_analyze_failure(row)
            db.commit()
            logger.warning(
                "新闻分析结果无法解析，保持未分析 id=%s fails=%s",
                snapshot.id,
                fails,
            )
            return False
        if is_weak_signal(row.bullish_level):
            discard_news_row(db, row)
            db.commit()
            logger.debug(
                "新闻利多利空过弱，已删除 id=%s bullish=%s",
                snapshot.id,
                row.bullish_level,
            )
            return True
        db.commit()
        if result and (result.get("relation_reason") or result.get("bullish_level_reason")):
            logger.debug(
                "新闻分析理由 id=%s relation=%s bullish=%s",
                snapshot.id,
                (result.get("relation_reason") or "")[:80],
                (result.get("bullish_level_reason") or "")[:80],
            )
        return ok
    finally:
        db.close()


async def _analyze_snapshot(client, snapshot: NewsSnapshot) -> bool:
    """单条分析：先取快照 → 调模型（无会话）→ 新会话写回。"""
    try:
        result = await analyze_one(client, snapshot)
    except Exception as exc:
        if not _is_item_specific_error(exc):
            # 超时/连接/5xx/鉴权是调用级故障，不是这条新闻的问题：**不扣它的
            # 重试额度**。否则模型侧抖动三次就把新闻永久标成失效
            # （MAX_ANALYZE_FAILS=3），恢复之后也再不会被分析。
            backoff = _trip_breaker()
            logger.warning(
                "新闻分析调用失败，不计入重试额度、留待下轮补扫 id=%s%s: %s: %s",
                snapshot.id,
                f"，熔断 {backoff:.0f}s" if backoff else "",
                type(exc).__name__,
                str(exc)[:200],
            )
            return False
        fails = _record_failure(snapshot.id)
        logger.warning(
            "新闻分析被模型拒绝（400/422，计入重试额度）id=%s fails=%s/%s: %s: %s",
            snapshot.id,
            fails,
            MAX_ANALYZE_FAILS,
            type(exc).__name__,
            str(exc)[:200],
        )
        return False
    # 调用成功说明模型链是通的，清掉之前的连续失败计数（解析失败由
    # _save_result 单独计，不在这里算调用故障）。
    _reset_breaker()
    return _save_result(snapshot, result)


def _parse_batch_results(raw: str, chunk: list[NewsSnapshot]) -> dict[int, dict]:
    """解析批量结果，按 index 映射回新闻 id；解析不出的条目交给逐条回退。"""
    data = _extract_json(raw)
    rows = None
    if isinstance(data, dict):
        for key in ("results", "items", "data"):
            if isinstance(data.get(key), list):
                rows = data[key]
                break
    if not isinstance(rows, list):
        return {}

    results: dict[int, dict] = {}
    for position, item in enumerate(rows):
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index")) - 1
        except (TypeError, ValueError):
            index = position
        if not 0 <= index < len(chunk):
            if position < len(chunk):
                index = position
            else:
                continue
        snapshot = chunk[index]
        if snapshot.id in results:
            continue
        normalized = normalize_analysis(item, snapshot.digest)
        if normalized is None:
            continue
        results[snapshot.id] = _merge_known(normalized, snapshot.relations_stocks)
    return results


async def _retry_per_item(client, chunk: list[NewsSnapshot]) -> list[int]:
    """逐条重打：只有「可能是某一条内容触发」时才值得这么做。"""
    done: list[int] = []
    for snapshot in chunk:
        if await _analyze_snapshot(client, snapshot):
            done.append(snapshot.id)
    return done


async def _recover_missing(client, missing: list[NewsSnapshot]) -> list[int]:
    """批量只回了部分条目：把缺的再合一次调用，仍缺的才逐条补。

    原实现直接逐条重打——5 条只回 3 条时，剩下 2 条要付 2 遍固定指令
    （单条约是 5 条批量里一条的 3 倍成本）。缺的条目重新拼一次调用更划算。
    """
    if len(missing) < 2:
        return await _retry_per_item(client, missing)
    try:
        raw = await _complete_with_timeout(client, build_batch_prompt(missing))
        batch_results = _parse_batch_results(raw, missing)
        _reset_breaker()
    except Exception as exc:
        if not _is_item_specific_error(exc):
            # 整批性故障：再逐条打只会把同一个失败放大成 N 倍，留给下轮补扫。
            backoff = _trip_breaker()
            logger.warning(
                "批量补跑缺失条目失败，留待下轮补扫%s：%s: %s",
                f"，熔断 {backoff:.0f}s" if backoff else "",
                type(exc).__name__,
                str(exc)[:200],
            )
            return []
        logger.warning(
            "批量补跑缺失条目返回内容错误，改逐条（%s 条）：%s: %s",
            len(missing),
            type(exc).__name__,
            str(exc)[:200],
        )
        return await _retry_per_item(client, missing)

    done: list[int] = []
    for snapshot in missing:
        result = batch_results.get(snapshot.id)
        if result is not None and _save_result(snapshot, result):
            done.append(snapshot.id)
    still_missing = [snapshot for snapshot in missing if snapshot.id not in set(done)]
    if still_missing:
        done.extend(await _retry_per_item(client, still_missing))
    return done


async def _analyze_chunk(
    client, chunk: list[NewsSnapshot], *, depth: int = 0
) -> list[int]:
    if len(chunk) == 1:
        return [chunk[0].id] if await _analyze_snapshot(client, chunk[0]) else []

    batch_results: dict[int, dict] = {}
    try:
        raw = await _complete_with_timeout(client, build_batch_prompt(chunk))
        batch_results = _parse_batch_results(raw, chunk)
        _reset_breaker()
    except Exception as exc:
        if _is_item_specific_error(exc):
            # 400/422：可能是某一条内容触发，逐条定位能救回其余条目。
            logger.warning(
                "批量分析返回参数/内容错误，改逐条定位（%s 条）：%s: %s",
                len(chunk),
                type(exc).__name__,
                str(exc)[:200],
            )
            return await _retry_per_item(client, chunk)
        # 整批性故障：不逐条重试、也不扣每条的重试额度，整批留待下轮补扫。
        backoff = _trip_breaker()
        logger.warning(
            "批量分析调用失败，整批留待下轮补扫（不逐条重试、不扣额度）%s 条%s：%s: %s",
            len(chunk),
            f"，熔断 {backoff:.0f}s" if backoff else "",
            type(exc).__name__,
            str(exc)[:200],
        )
        return []

    if not batch_results:
        # 一条都没解析出来：多半是输出被截断或格式整体跑偏（批量越大越容易）。
        # 逐条重打会把 1 次失败放大成 N 次，先对半拆再试一次；拆完仍不行就
        # 整批留给下轮补扫，别把这批新闻的重试额度白烧掉。
        if depth == 0 and len(chunk) <= PARSE_FALLBACK_MAX_ITEMS:
            return await _retry_per_item(client, chunk)
        if depth == 0:
            middle = len(chunk) // 2
            logger.warning(
                "批量分析结果整体不可解析（%s 条），对半拆分重试", len(chunk)
            )
            return [
                *await _analyze_chunk(client, chunk[:middle], depth=1),
                *await _analyze_chunk(client, chunk[middle:], depth=1),
            ]
        logger.warning(
            "批量分析结果整体不可解析（%s 条），整批留待下轮补扫（不逐条重打）",
            len(chunk),
        )
        return []

    done: list[int] = []
    missing: list[NewsSnapshot] = []
    for snapshot in chunk:
        result = batch_results.get(snapshot.id)
        if result is not None:
            if _save_result(snapshot, result):
                done.append(snapshot.id)
        else:
            missing.append(snapshot)
    if missing:
        done.extend(await _recover_missing(client, missing))
    return done


async def analyze_many(
    client, news_ids: list[int], *, batch_size: int = BATCH_ANALYZE_SIZE
) -> list[int]:
    """批量分析一组新闻，返回成功写出的 id 列表。

    一次 LLM 调用最多处理 ``batch_size`` 条；批量结果里缺失或解析失败的条目
    自动回退单条分析，保证不会因为模型输出格式问题整批丢失。

    没有可用模型时直接返回空：不能进 ``_record_failure``，否则每条都会扣掉一次
    重试额度，等用户配好模型时新闻已经「失败到上限」不再分析。
    """
    if client is None or not getattr(client, "model", None):
        _log_model_missing(news_ids[0] if news_ids else 0)
        return []
    pending = _load_pending(list(news_ids))
    if not pending:
        return []
    size = max(1, int(batch_size or 1))
    done: list[int] = []
    for start in range(0, len(pending), size):
        done.extend(await _analyze_chunk(client, pending[start : start + size]))
    return done


async def _analyze_row_by_id(client, news_id: int) -> bool:
    """单条分析：独立 Session，便于并发。成功写出结构化结果返回 True。"""
    pending = _load_pending([news_id])
    if not pending:
        return False
    return await _analyze_snapshot(client, pending[0])


def _build_client():
    db = SessionLocal()
    try:
        from src.platform.ai.ai_failover import get_configured_failover_client

        return get_configured_failover_client(db)
    finally:
        db.close()


async def analyze_by_id(news_id: int, client=None) -> bool:
    """按新闻 id 分析一条；供分析队列 worker 调用。"""
    if client is None:
        client = _build_client()
    if not getattr(client, "model", None):
        _log_model_missing(news_id)
        return False
    return await _analyze_row_by_id(client, int(news_id))


def _log_model_missing(news_id: int) -> None:
    """未配置模型时每条都会走到这里：按间隔降噪，不逐条写日志表。"""
    global _model_missing_logged_at
    now = time.monotonic()
    if now - _model_missing_logged_at < MODEL_MISSING_LOG_INTERVAL_SEC:
        logger.debug("新闻分析跳过：未配置默认模型 id=%s", news_id)
        return
    _model_missing_logged_at = now
    logger.warning(
        "新闻分析跳过：未配置默认模型（%ss 内同类提示只打一次，新闻留在库里等补扫）id=%s",
        MODEL_MISSING_LOG_INTERVAL_SEC,
        news_id,
    )
