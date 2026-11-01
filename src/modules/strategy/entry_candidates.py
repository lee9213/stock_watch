from __future__ import annotations

from src.platform.marketdata.outcome_prices import forward_bars
from src.modules.strategy.plan_outcome import evaluate_plan_path, outcome_day_str

import asyncio
import logging
from datetime import date, datetime, timedelta

from sqlalchemy import String, and_, case, cast, func, or_

from src.modules.automation.agent_catalog import RETIRED_AGENT_NAMES
from src.modules.strategy.scoring_config import (
    ACTION_BASE_SCORE,
    ACTIVE_SCORE_MARKET_SCAN,
    ACTIVE_SCORE_WATCHLIST,
    CRASH_CHANGE_PCT,
    FREEZE_PHASES,
    INTRADAY_CONFIRM_CHANGE_PCT,
    INTRADAY_CONFIRM_TAGS,
    INTRADAY_CONFIRM_VOLUME_RATIO,
    INTRADAY_PHASES,
    KLINE_FETCH_WORKERS,
    MIN_PLAN_QUALITY_FOR_ACTIVE,
    MIN_TURNOVER_FOR_BUY,
    OVERHEAT_DELTA_PCT,
    PLAN_ATR_BAND_MULT,
    PLAN_ATR_STOP_MULT,
    PLAN_ATR_TARGET_MULT,
    PLAN_BAND_HALF_MAX,
    PLAN_BAND_HALF_MIN,
    PLAN_BAND_WIDTH_MAX,
    PLAN_BAND_WIDTH_MIN,
    PLAN_MID_PRICE_RATIO_MAX,
    PLAN_MIN_RISK_REWARD,
    PLAN_STOP_MAX_PCT,
    PLAN_STOP_MIN_PCT,
    PLAN_TARGET_MAX_PCT,
    POINT_IN_TIME_START,
    STALE_QUOTE_MINUTES,
)
from src.platform.runtime.config import Settings
from src.platform.scheduling import trading_calendar
from src.platform.scheduling.timezone import app_now
from src.platform.marketdata.collectors.discovery_collector import EastMoneyDiscoveryCollector
from src.platform.marketdata.collectors.kline_collector import KlineCollector
from src.platform.persistence.json_safe import to_jsonable
from src.platform.marketdata.marketdata_client import md_stock_data
from src.platform.notifications.notifier import get_global_proxy
from src.platform.scheduling.timezone import to_iso_with_tz, utc_now
from src.platform.marketdata.models import MarketCode
from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import (
    EntryCandidate,
    EntryCandidateOutcome,
    MarketScanSnapshot,
    PaperTradingPosition,
    Position,
    Stock,
    StockSuggestion,
)

logger = logging.getLogger(__name__)


AGENT_LABELS: dict[str, str] = {
    "premarket_outlook": "盘前分析",
    "intraday_monitor": "盘中监测",
    "daily_report": "收盘复盘",
    "market_scan": "市场扫描",
}


CANDIDATE_SOURCE_LABELS: dict[str, str] = {
    "watchlist": "关注池",
    "market_scan": "市场池",
    "mixed": "市场+关注",
}


STRATEGY_LABELS: dict[str, str] = {
    "trend_follow": "趋势延续",
    "macd_golden": "MACD金叉",
    "volume_breakout": "放量突破",
    "momentum": "动量强化",
    "pullback": "回踩确认",
    "rebound": "超跌反弹",
    "watchlist_agent": "Agent建议",
}

MARKET_SCAN_SEED_SYMBOLS: dict[str, list[str]] = {
    "CN": [
        "600519",
        "601318",
        "601127",
        "300750",
        "300308",
        "000333",
        "002594",
        "601899",
        "600036",
        "600900",
        "000858",
        "601288",
        "600276",
        "601888",
        "000651",
    ],
    "HK": [
        "00700",
        "09988",
        "03690",
        "01810",
        "02318",
        "01299",
        "00941",
        "00388",
        "00883",
        "09999",
        "01398",
        "02628",
    ],
    "US": [
        "AAPL",
        "MSFT",
        "NVDA",
        "AMZN",
        "GOOGL",
        "META",
        "TSLA",
        "AMD",
        "NFLX",
        "INTC",
        "BABA",
        "PDD",
        "NIO",
        "TSM",
        "QQQ",
    ],
}


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _safe_float(value) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _to_market(value: str | None) -> MarketCode:
    try:
        return MarketCode((value or "CN").strip().upper())
    except Exception:
        return MarketCode.CN


def _resolve_market_scan_proxy() -> str | None:
    try:
        proxy = (get_global_proxy() or "").strip()
        if proxy:
            return proxy
    except Exception:
        pass
    try:
        proxy = (Settings().http_proxy or "").strip()
        if proxy:
            return proxy
    except Exception:
        pass
    return None


def _run_async(coro):
    try:
        return asyncio.run(coro)
    except RuntimeError:
        # In case we're in an already running loop, create an isolated one.
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


def _candidate_sort_key(item: dict) -> tuple[int, float, float]:
    meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
    source = str(meta.get("source") or "")
    source_priority = {
        "market_scan": 0,
        "market_scan_history": 1,
        "market_scan_seed_universe": 2,
        "market_scan_seed_watchlist": 3,
    }
    quote = item.get("quote_seed") if isinstance(item.get("quote_seed"), dict) else {}
    turnover = _safe_float(quote.get("turnover")) or 0.0
    change_abs = abs(_safe_float(quote.get("change_pct")) or 0.0)
    return (
        source_priority.get(source, 9),
        -float(turnover),
        -float(change_abs),
    )


def _extract_price_from_meta(meta: dict | None) -> dict:
    data = meta if isinstance(meta, dict) else {}
    quote = data.get("quote") if isinstance(data.get("quote"), dict) else {}
    context = data.get("context") if isinstance(data.get("context"), dict) else {}
    context_quote = (
        context.get("quote") if isinstance(context.get("quote"), dict) else {}
    )
    kline = data.get("kline") if isinstance(data.get("kline"), dict) else {}
    context_kline = (
        context.get("kline") if isinstance(context.get("kline"), dict) else {}
    )
    plan = data.get("plan") if isinstance(data.get("plan"), dict) else {}

    price = (
        _safe_float(quote.get("current_price"))
        or _safe_float(context_quote.get("current_price"))
        or _safe_float(data.get("trigger_price"))
        or _safe_float(data.get("current_price"))
        or _safe_float(kline.get("last_close"))
        or _safe_float(kline.get("close"))
        or _safe_float(context_kline.get("last_close"))
        or _safe_float(context_kline.get("close"))
        or _safe_float(plan.get("entry_price"))
    )
    change_pct = (
        _safe_float(quote.get("change_pct"))
        or _safe_float(context_quote.get("change_pct"))
        or _safe_float(data.get("change_pct"))
    )
    result = {}
    if price is not None:
        result["current_price"] = price
    if change_pct is not None:
        result["change_pct"] = change_pct
    return result


def _normalize_kline_summary(data: dict | None) -> dict:
    raw = data if isinstance(data, dict) else {}
    if not raw:
        return {}
    out: dict[str, object] = {}
    text_keys = ("trend", "macd_cross", "rsi_status", "kdj_status", "volume_trend")
    num_keys = (
        "volume_ratio",
        "support",
        "resistance",
        "support_s",
        "support_m",
        "support_l",
        "resistance_s",
        "resistance_m",
        "resistance_l",
        "last_close",
        "close",
        # 计划构造需要波动率与结构位：旧白名单把这些全丢了，_build_plan 只能
        # 用现价 ±1% 拍一个区间，导致止损/目标与 ATR 完全脱钩。
        "atr",
        "atr_pct",
        "amplitude",
        "amplitude_avg5",
        "boll_upper",
        "boll_mid",
        "boll_lower",
        "ma5",
        "ma10",
        "ma20",
        "ma60",
        "change_5d",
        "change_20d",
        "rsi6",
    )
    for key in text_keys:
        val = raw.get(key)
        if val is None:
            continue
        text = str(val).strip()
        if text:
            out[key] = text
    for key in num_keys:
        val = _safe_float(raw.get(key))
        if val is not None:
            out[key] = val
    return out


def _extract_kline_from_meta(meta: dict | None) -> dict:
    data = meta if isinstance(meta, dict) else {}
    if not data:
        return {}

    candidates: list[dict] = []
    for key in ("kline", "kline_summary"):
        item = data.get(key)
        if isinstance(item, dict):
            candidates.append(item)
    context = data.get("context")
    if isinstance(context, dict):
        for key in ("kline", "kline_summary"):
            item = context.get(key)
            if isinstance(item, dict):
                candidates.append(item)
    source_meta = data.get("source_meta")
    if isinstance(source_meta, dict):
        for key in ("kline", "kline_summary"):
            item = source_meta.get(key)
            if isinstance(item, dict):
                candidates.append(item)

    for item in candidates:
        normalized = _normalize_kline_summary(item)
        if normalized:
            return normalized
    return {}


def _load_quote_fallbacks(
    keys: set[str], *, lookback_days: int = 2, max_rows: int = 4000
) -> dict[str, dict]:
    if not keys:
        return {}

    symbols = sorted({k.split(":", 1)[1] for k in keys if ":" in k})
    if not symbols:
        return {}

    cutoff = utc_now() - timedelta(days=max(1, int(lookback_days)))
    db = SessionLocal()
    try:
        rows = (
            db.query(StockSuggestion)
            .filter(
                StockSuggestion.stock_symbol.in_(symbols),
                StockSuggestion.agent_name == "intraday_monitor",
                StockSuggestion.created_at >= cutoff,
            )
            .order_by(StockSuggestion.id.desc())
            .limit(max_rows)
            .all()
        )
    finally:
        db.close()

    fallback: dict[str, dict] = {}
    for row in rows:
        key = f"{_to_market(row.stock_market).value}:{row.stock_symbol}"
        if key not in keys or key in fallback:
            continue
        quote = _extract_price_from_meta(row.meta or {})
        if _safe_float(quote.get("current_price")) is not None:
            # 带上采集时间，供 quote_is_stale 判断这份补位行情还能不能用来开仓
            if row.created_at is not None:
                quote["quote_at"] = to_iso_with_tz(row.created_at)
            fallback[key] = quote
        if len(fallback) >= len(keys):
            break
    return fallback


def _load_kline_fallbacks(
    keys: set[str], *, lookback_days: int = 7, max_rows: int = 6000
) -> dict[str, dict]:
    if not keys:
        return {}
    symbols = sorted({k.split(":", 1)[1] for k in keys if ":" in k})
    if not symbols:
        return {}

    cutoff_day = (app_now().date() - timedelta(days=max(1, int(lookback_days)))).strftime("%Y-%m-%d")
    cutoff_time = utc_now() - timedelta(days=max(1, int(lookback_days)))
    fallback: dict[str, dict] = {}

    db = SessionLocal()
    try:
        rows = (
            db.query(EntryCandidate)
            .filter(
                EntryCandidate.stock_symbol.in_(symbols),
                EntryCandidate.snapshot_date >= cutoff_day,
            )
            .order_by(EntryCandidate.snapshot_date.desc(), EntryCandidate.updated_at.desc())
            .limit(max_rows)
            .all()
        )
        for row in rows:
            key = f"{_to_market(row.stock_market).value}:{row.stock_symbol}"
            if key not in keys or key in fallback:
                continue
            kline = _extract_kline_from_meta(row.meta if isinstance(row.meta, dict) else {})
            if not kline:
                continue
            fallback[key] = kline
            if len(fallback) >= len(keys):
                return fallback

        srows = (
            db.query(StockSuggestion)
            .filter(
                StockSuggestion.stock_symbol.in_(symbols),
                StockSuggestion.created_at >= cutoff_time,
                StockSuggestion.agent_name.in_(("intraday_monitor", "premarket_outlook", "daily_report")),
            )
            .order_by(StockSuggestion.id.desc())
            .limit(max_rows)
            .all()
        )
        for row in srows:
            key = f"{_to_market(row.stock_market).value}:{row.stock_symbol}"
            if key not in keys or key in fallback:
                continue
            kline = _extract_kline_from_meta(row.meta if isinstance(row.meta, dict) else {})
            if not kline:
                continue
            fallback[key] = kline
            if len(fallback) >= len(keys):
                break
    finally:
        db.close()

    return fallback


# 候选层结构信号打分表（两个候选池共用）。
#
# 改造前自选/AI 池与热榜池各有一份公式：同一个"均线多头排列"在自选池值 +8、
# 在热榜池值 +5；"当日涨幅适中"一个用 abs(pct)、一个只认上涨；热榜还多算成交额
# 与策略标签。结果同一张机会列表里混着两种尺度的分数，只能靠不同的 active 门槛
# （62 / 55）去补偿——门槛能补偿，**排序补偿不了**。
#
# 这里统一成一份权重表，两个池都用它。池间差异只保留在"AI 建议自身的质量/新鲜度"
# 与 strategy_engine 的 source_bonus，那两项确实来源不同而非尺度不同。
STRUCT_SCORE_WEIGHTS = {
    "trend_bull": 6.5,
    "trend_bear": -7.0,
    "macd_golden": 4.5,
    "macd_dead": -4.5,
    "rsi_weak": 2.0,
    "rsi_overbought": -3.0,
    "kdj_golden": 2.5,
    "kdj_dead": -2.5,
    "volume_strong": 3.5,
    "volume_mild": 2.0,
    "momentum_healthy": 2.0,
    "overheat": -8.0,
    "wild_swing": -3.0,
    "turnover_huge": 3.0,
    "turnover_large": 1.0,
    "tag_each": 2.0,
    "tag_cap": 6.0,
}


def _score_structure_signals(
    *,
    quote: dict | None,
    kline: dict | None,
    symbol: str = "",
    strategy_tags: list[str] | None = None,
) -> tuple[float, list[str]]:
    """趋势 / 动量 / 量能等结构信号的统一打分；两个候选池共用同一份权重。"""
    w = STRUCT_SCORE_WEIGHTS
    score = 0.0
    evidence: list[str] = []
    q = quote or {}
    k = kline or {}

    tags = [t for t in (strategy_tags or []) if t]
    if tags:
        score += min(float(w["tag_cap"]), len(tags) * float(w["tag_each"]))
        evidence.append("策略信号: " + " / ".join(_strategy_labels(tags[:3])))

    pct = _safe_float(q.get("change_pct"))
    if pct is not None:
        if pct >= float(overheat_change_pct(symbol)):
            score += float(w["overheat"])
            evidence.append(f"涨幅过热({pct:+.2f}%)")
        elif 0.5 <= pct <= 6.0:
            score += float(w["momentum_healthy"])
            evidence.append(f"价格动量({pct:+.2f}%)")
        elif pct <= CRASH_CHANGE_PCT:
            score += float(w["wild_swing"])
            evidence.append(f"当日大跌({pct:+.2f}%)")

    turnover = _safe_float(q.get("turnover"))
    if turnover is not None:
        if turnover >= 3e9:
            score += float(w["turnover_huge"])
            evidence.append("成交额高")
        elif turnover >= 1e9:
            score += float(w["turnover_large"])

    trend = (k.get("trend") or "").strip()
    if trend == "多头排列":
        score += float(w["trend_bull"])
        evidence.append("均线多头排列")
    elif trend == "空头排列":
        score += float(w["trend_bear"])
        evidence.append("均线空头排列")

    macd = (k.get("macd_cross") or "").strip()
    if macd == "金叉":
        score += float(w["macd_golden"])
        evidence.append("MACD 金叉")
    elif macd == "死叉":
        score += float(w["macd_dead"])
        evidence.append("MACD 死叉")

    rsi_status = (k.get("rsi_status") or "").strip()
    if rsi_status in ("超卖", "偏弱"):
        score += float(w["rsi_weak"])
        evidence.append(f"RSI {rsi_status}")
    elif rsi_status == "超买":
        score += float(w["rsi_overbought"])
        evidence.append(f"RSI {rsi_status}")

    kdj_status = (k.get("kdj_status") or "").strip()
    if "金叉" in kdj_status:
        score += float(w["kdj_golden"])
        evidence.append("KDJ 金叉")
    elif "死叉" in kdj_status:
        score += float(w["kdj_dead"])
        evidence.append("KDJ 死叉")

    vol_ratio = _safe_float(k.get("volume_ratio"))
    if vol_ratio is not None:
        if vol_ratio >= 2:
            score += float(w["volume_strong"])
            evidence.append(f"量比放大({vol_ratio:.1f}x)")
        elif vol_ratio >= 1.3:
            score += float(w["volume_mild"])
            evidence.append(f"量比温和放大({vol_ratio:.1f}x)")

    return score, evidence


def _score_suggestion(
    *,
    action: str,
    suggestion: StockSuggestion,
    quote: dict | None,
    kline: dict | None,
    strategy_tags: list[str] | None = None,
    symbol: str = "",
) -> tuple[float, list[str]]:
    score = ACTION_BASE_SCORE.get((action or "").strip().lower(), 45.0)
    struct_score, evidence = _score_structure_signals(
        quote=quote, kline=kline, symbol=symbol, strategy_tags=strategy_tags
    )
    score += struct_score

    if suggestion.signal:
        evidence.append(f"建议信号: {suggestion.signal}")
    if suggestion.reason:
        evidence.append(f"建议依据: {suggestion.reason}")

    # 以下两项是 AI 建议独有的信号，热榜票没有对应信息，不属于"尺度不一致"。
    q = suggestion.meta or {}
    quality = _safe_float((q.get("context_quality_score") if isinstance(q, dict) else None))
    if quality is not None:
        quality_bonus = _clamp((quality - 60.0) / 10.0, -3.0, 5.0)
        score += quality_bonus
        evidence.append(f"上下文质量分 {quality:.0f}")

    created_at = suggestion.created_at
    if created_at:
        try:
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=utc_now().tzinfo)
            hours = (utc_now() - created_at).total_seconds() / 3600.0
            if hours <= 6:
                score += 3
                evidence.append("建议新鲜度高(6h内)")
            elif hours >= 48:
                score -= 3
                evidence.append("建议时效偏旧(48h+)")
        except Exception:
            pass

    score = _clamp(score, 0.0, 100.0)
    return score, evidence[:8]


def _build_plan(
    *,
    action: str,
    quote: dict | None,
    kline: dict | None,
    suggestion_meta: dict | None = None,
) -> dict:
    meta = suggestion_meta if isinstance(suggestion_meta, dict) else {}
    meta_quote = meta.get("quote") if isinstance(meta.get("quote"), dict) else {}
    context = meta.get("context") if isinstance(meta.get("context"), dict) else {}
    context_quote = (
        context.get("quote") if isinstance(context.get("quote"), dict) else {}
    )
    meta_plan = meta.get("plan") if isinstance(meta.get("plan"), dict) else {}

    price = (
        _safe_float((quote or {}).get("current_price"))
        or _safe_float(meta_quote.get("current_price"))
        or _safe_float(context_quote.get("current_price"))
        or _safe_float(meta.get("trigger_price"))
        or _safe_float(meta.get("current_price"))
        or _safe_float(meta_plan.get("entry_price"))
        or _safe_float((kline or {}).get("last_close"))
    )
    support = _safe_float((kline or {}).get("support_m")) or _safe_float((kline or {}).get("support"))
    resistance = _safe_float((kline or {}).get("resistance_m")) or _safe_float((kline or {}).get("resistance"))
    support_s = _safe_float((kline or {}).get("support_s"))
    resistance_s = _safe_float((kline or {}).get("resistance_s"))
    trend = (kline or {}).get("trend") or ""

    entry_low = None
    entry_high = None
    stop_loss = None
    target_price = None
    invalidation = ""
    atr_pct = None
    plan_rr_ok = False

    if price is not None and price > 0:
        atr = _safe_float((kline or {}).get("atr"))
        atr_pct = _safe_float((kline or {}).get("atr_pct"))
        if atr is not None and atr > 0:
            atr_pct = atr / price
        elif atr_pct is None or atr_pct <= 0:
            # 没有 ATR 时退化为振幅，再退化为 2% 的固定波动假设
            amplitude = _safe_float((kline or {}).get("amplitude"))
            atr_pct = (amplitude / 100.0) if amplitude else 0.02
        atr_pct = _clamp(float(atr_pct), 0.004, 0.12)
        atr_value = atr if (atr is not None and atr > 0) else price * atr_pct

        if support is None:
            support = price * (1.0 - max(0.03, atr_pct * 1.5))
        if resistance is None:
            resistance = price * (1.0 + max(0.04, atr_pct * 2.0))

        if action in ("buy", "add"):
            # 区间宽度跟随波动率：原来的固定 ±1% 对高波动票太窄（开盘就被扫过），
            # 对低波动票又太宽（永远等不到回踩）。半宽 = ATR% × 系数，再夹住上下限。
            half = _clamp(atr_pct * float(PLAN_ATR_BAND_MULT), float(PLAN_BAND_HALF_MIN), float(PLAN_BAND_HALF_MAX))
            entry_low = price * (1.0 - half)
            entry_high = price * (1.0 + half)
            # 短线支撑若就在现价脚下（1.5×ATR 之内），把区间下沿抬到支撑上方：
            # 回踩确认的买点应该在支撑附近，而不是一根均匀的对称带。
            anchor = support_s if (support_s is not None and support_s < price) else support
            if anchor is not None and 0 < (price - anchor) / price <= atr_pct * 1.5:
                entry_low = min(price * (1.0 + half), max(anchor * 1.003, price * (1.0 - half)))

            mid = (entry_low + entry_high) / 2.0
            # 风险基准统一用区间上沿（最差成交价）。回踩买入时"高开回落"最先在
            # entry_high 成交，用中点当成本会系统性低估风险：实测同一计划按 mid 算
            # R:R=1.50 达标、按 entry_high 算只有 1.19。
            fill_ref = entry_high

            # 止损：支撑外扩与 ATR 止损取更靠外（更低）的一个，让结构位与波动率都不被击穿。
            stop_by_atr = fill_ref - float(PLAN_ATR_STOP_MULT) * atr_value
            stop_by_support = support * 0.99 if support else None
            stop_struct = min(x for x in (stop_by_atr, stop_by_support, fill_ref) if x is not None)
            stop_far = fill_ref * (1.0 - float(PLAN_STOP_MAX_PCT))  # 允许的最远止损
            stop_near = fill_ref * (1.0 - float(PLAN_STOP_MIN_PCT))  # 允许的最近止损
            stop_struct = _clamp(float(stop_struct), stop_far, stop_near)

            # 目标由市场结构决定（阻力位 / ATR 目标），不再为了凑 R:R 往上抬。
            # 改造前 target = max(结构目标, mid + 1.5×risk)，等于"先定风险再倒推目标"，
            # 实测产出 目标 +12% / 止损 -8% / R:R 恰好 1.50 的计划——三个数字全是常量
            # 边界，目标成了公式产物，与上方真实阻力无关（曾把目标抬到阻力之上 6.4%）。
            anchor_res = (
                resistance_s
                if (resistance_s is not None and resistance_s > fill_ref)
                else resistance
            )
            target_struct = price + float(PLAN_ATR_TARGET_MULT) * atr_value
            if anchor_res is not None and anchor_res > fill_ref:
                target_struct = max(target_struct, anchor_res * 0.99)
            target_price = min(
                float(target_struct), price * (1.0 + float(PLAN_TARGET_MAX_PCT))
            )

            # 目标定下来之后反推"止损最多能放多远"；结构止损太远就收紧到刚好达标，
            # 收不动（要近过 PLAN_STOP_MIN_PCT）说明上方空间不足，本就不该买 → 降级。
            #
            # 反推时留 0.2% 余量：止损与目标随后都要 round 到 4 位小数，恰好卡在门槛上
            # 会算出 1.4998 这种值，_plan_quality 只能靠容差放过，边界样本随机成败。
            plan_rr_ok = False
            rr_target = float(PLAN_MIN_RISK_REWARD) * 1.002
            budget = (float(target_price) - fill_ref) / rr_target
            if budget > 0:
                stop_for_rr = fill_ref - budget
                if stop_struct >= stop_for_rr:
                    # 结构止损本身就够近，不用动
                    stop_loss = stop_struct
                    plan_rr_ok = True
                elif stop_for_rr <= stop_near:
                    # 收紧到刚好达标，且仍不为日内噪音级别
                    stop_loss = stop_for_rr
                    plan_rr_ok = True
                else:
                    # 要收进 2% 以内才达标：上方空间不足
                    stop_loss = stop_struct
                    plan_rr_ok = False
            else:
                stop_loss = stop_struct
                plan_rr_ok = False

            stop_loss = round(float(stop_loss), 4)
            target_price = round(float(target_price), 4)
            invalidation = f"若跌破 {float(stop_loss):.2f} 则失效"
            if not plan_rr_ok:
                invalidation += "（上方空间不足，风险回报比不达标）"
        elif action in ("hold", "watch"):
            stop_loss = (support * 0.98) if support else (price * 0.94)
            target_price = (resistance * 0.99) if resistance else (price * 1.04)
            invalidation = f"若跌破 {stop_loss:.2f} 则转防守"
        else:
            invalidation = "当前以风险控制为主，不建议新开仓"

    return {
        "entry_low": entry_low,
        "entry_high": entry_high,
        "stop_loss": stop_loss,
        "target_price": target_price,
        "invalidation": invalidation,
        "trend": trend,
        "support": support,
        "resistance": resistance,
        "atr_pct": round(float(atr_pct), 6) if price is not None else None,
        # 风险回报比（按最差成交价 entry_high 计）。plan_rr_ok=False 表示上方空间不足、
        # 收紧止损也凑不出门槛，计划本身不成立——下游据此封顶 quality、不进 active。
        "risk_reward": _plan_risk_reward(entry_high, stop_loss, target_price),
        "plan_rr_ok": bool(plan_rr_ok),
    }


def _plan_risk_reward(
    fill_ref: float | None, stop_loss: float | None, target_price: float | None
) -> float | None:
    """按最差成交价算风险回报比；任一缺失或风险非正时返回 None。"""
    ref = _safe_float(fill_ref)
    stop = _safe_float(stop_loss)
    target = _safe_float(target_price)
    if ref is None or stop is None or target is None or ref <= 0:
        return None
    risk = ref - stop
    reward = target - ref
    if risk <= 0:
        return None
    return round(reward / risk, 4)


def _candidate_source_label(source: str) -> str:
    return CANDIDATE_SOURCE_LABELS.get((source or "").strip(), source or "")


def _strategy_labels(tags: list[str] | None) -> list[str]:
    out: list[str] = []
    for t in tags or []:
        if not t:
            continue
        out.append(STRATEGY_LABELS.get(t, t))
    return out


# 热榜至少有这么多活数据时，不再用历史/种子蓝筹补位
LIVE_SCAN_FALLBACK_MIN = 1
# 其余阈值统一收口在 scoring_config（可用 PW_* 环境变量覆盖）：
# 过热 / 当日大跌 / 最低成交额 / 盘中确认 / 计划结构 / active 定级。


def _plan_quality(plan: dict | None, *, quote: dict | None = None) -> int:
    """计划完整度 + 结构合理性。结构不合法时封顶 <90，避免误进 active。"""
    p = plan if isinstance(plan, dict) else {}
    score = 0
    entry_low = _safe_float(p.get("entry_low"))
    entry_high = _safe_float(p.get("entry_high"))
    stop_loss = _safe_float(p.get("stop_loss"))
    target_price = _safe_float(p.get("target_price"))

    if entry_low is not None or entry_high is not None:
        score += 30
    if stop_loss is not None:
        score += 30
    if target_price is not None:
        score += 30
    if str(p.get("invalidation") or "").strip():
        score += 10

    structural_ok = True
    if entry_low is not None and entry_high is not None:
        if entry_low <= 0 or entry_high <= 0 or entry_low > entry_high:
            structural_ok = False
        else:
            mid = (entry_low + entry_high) / 2.0
            width = (entry_high - entry_low) / mid if mid > 0 else 0.0
            if width < PLAN_BAND_WIDTH_MIN or width > PLAN_BAND_WIDTH_MAX:
                structural_ok = False
            if stop_loss is not None and stop_loss >= mid:
                structural_ok = False
            if target_price is not None and target_price <= mid:
                structural_ok = False
            price = _safe_float((quote or {}).get("current_price"))
            if price is not None and price > 0 and mid > 0:
                ratio = max(price / mid, mid / price)
                if ratio > PLAN_MID_PRICE_RATIO_MAX:
                    structural_ok = False
            # 风险回报比：目标太近 / 止损太远的计划即使字段齐全也不该满分。
            # 改造前只查结构（目标>中价、止损<中价），"赚 0.5% 赔 6% " 能拿 100 分进
            # active，后验评估里目标几乎必中、止损几乎打不到，胜率因此虚高。
            #
            # 风险基准用 entry_high（最差成交价），与 _build_plan、路径评估口径一致；
            # 用 mid 会低估风险（实测同一计划 mid 口径 1.50、entry_high 口径 1.19）。
            # 门槛留 0.1% 容差：_build_plan 是"恰好达标"地产出止损，浮点误差会让
            # 0.744/0.496 算成 1.4999…，不加容差会把边界计划随机误杀。
            if stop_loss is not None and target_price is not None:
                risk = float(entry_high) - float(stop_loss)
                reward = float(target_price) - float(entry_high)
                if risk <= 0 or reward <= 0:
                    structural_ok = False
                elif (reward / risk) < float(PLAN_MIN_RISK_REWARD) * 0.999:
                    structural_ok = False
            # 计划显式声明"上方空间不足、凑不出 R:R"时直接否决，避免下游漏判。
            if p.get("plan_rr_ok") is False:
                structural_ok = False
    elif entry_low is None and entry_high is None:
        # 无入场区间时不做结构否决（观望类可无区间）
        pass
    else:
        structural_ok = False

    if not structural_ok:
        # 保证达不到 active 门槛（quality >= 90）
        score = min(score, 89)
    return int(_clamp(float(score), 0, 100))


def _load_holding_keys() -> set[str]:
    db = SessionLocal()
    try:
        rows = (
            db.query(Stock.market, Stock.symbol)
            .join(Position, Position.stock_id == Stock.id)
            .all()
        )
        keys = {
            f"{(m or 'CN').strip().upper()}:{(s or '').strip()}"
            for m, s in rows
            if s
        }
        paper_rows = (
            db.query(PaperTradingPosition.stock_market, PaperTradingPosition.stock_symbol)
            .filter(PaperTradingPosition.status == "open")
            .all()
        )
        for market, symbol in paper_rows:
            code = (symbol or "").strip()
            if not code:
                continue
            keys.add(f"{(market or 'CN').strip().upper()}:{code}")
        return keys
    finally:
        db.close()


def _parse_day(value: str | None) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except Exception:
            continue
    return None


def _pick_close_on_or_before(klines: list, target: date) -> float | None:
    if not klines:
        return None
    rows: list[tuple[date, float]] = []
    for k in klines:
        d = _parse_day(getattr(k, "date", None))
        c = getattr(k, "close", None)
        if d is None or c is None:
            continue
        try:
            rows.append((d, float(c)))
        except Exception:
            continue
    if not rows:
        return None
    rows.sort(key=lambda x: x[0])
    for d, c in reversed(rows):
        if d <= target:
            return c
    return None


def normalize_stock_name(name: str | None) -> str:
    """去掉名称里的空格/全角空格，便于新闻匹配（五 粮 液 → 五粮液）。"""
    if not name:
        return ""
    return (
        str(name)
        .replace(" ", "")
        .replace("\u3000", "")
        .replace("\xa0", "")
        .strip()
    )


def cn_limit_pct(symbol: str | None) -> float:
    """A 股涨跌停幅度：主板 10、创业/科创 20、北交 30。"""
    code = (symbol or "").strip()
    if code.startswith("688") or code.startswith(("300", "301")):
        return 20.0
    if code.startswith(("8", "4")) or code.startswith("92"):
        return 30.0
    return 10.0


def overheat_change_pct(symbol: str | None) -> float:
    """过热阈值：涨停幅度减 OVERHEAT_DELTA_PCT 个百分点。"""
    return cn_limit_pct(symbol) - float(OVERHEAT_DELTA_PCT)


def market_phase(market: str | None) -> str:
    """当前市场所处阶段：trading / break / pre_market / after_hours / closed / unknown。"""
    try:
        return trading_calendar.market_status(market or "CN")
    except Exception:
        return "unknown"


def phase_has_intraday_data(phase: str) -> bool:
    """当日是否已产生可用于"盘中确认"的数据。

    盘前（pre_market）和非交易日（closed/unknown）拿到的 quote 实际是上一交易日的
    收盘残留，此时用"当日涨幅 + 量比"做否决会把所有 buy/add 全部打成"盘中未确认"，
    09:15 那轮因此长期产出 0 条机会。
    """
    return phase in INTRADAY_PHASES


def phase_allows_freeze(phase: str) -> bool:
    """只有盘中/午休轮次可以冻结 point-in-time 状态。

    收盘后（after_hours）已经包含当日涨跌结果，用它冻结就是前视；盘前更是昨天的数据。
    """
    return phase in FREEZE_PHASES


def _parse_stamp(value) -> datetime | None:
    """把各种时间写法（datetime / date / ISO 字符串）统一成带时区 datetime。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=utc_now().tzinfo)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=utc_now().tzinfo)
    text = str(value).strip().replace("/", "-")
    if not text:
        return None
    try:
        if len(text) == 10:
            return datetime.fromisoformat(text).replace(tzinfo=utc_now().tzinfo)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=utc_now().tzinfo)
    except ValueError:
        return None


def quote_is_stale(quote: dict | None, seed_meta: dict | None = None) -> bool:
    """行情是否已过期。

    补位行情来自历史候选或盘中建议的 meta，行情源挂掉时可能是几小时前的价格，
    拿它算出的"回踩支撑 / 建仓区间"会误导，因此超过 STALE_QUOTE_MINUTES 就不允许开仓。
    时间戳缺失时不做拦截（否则会把所有历史数据一律判死）。
    """
    q = quote if isinstance(quote, dict) else {}
    m = seed_meta if isinstance(seed_meta, dict) else {}
    stamp = (
        _parse_stamp(q.get("quote_at"))
        or _parse_stamp(m.get("collected_at"))
        or _parse_stamp(m.get("fallback_snapshot_date"))
    )
    if stamp is None:
        return False
    age_minutes = (utc_now() - stamp).total_seconds() / 60.0
    return age_minutes > float(STALE_QUOTE_MINUTES)


def _compact_seed_meta(meta: dict | None) -> dict:
    """落库前裁剪上游 meta（与 strategy_engine._compact_source_meta 的白名单一致）。

    上游建议的 meta 可能带整份 context/kline，逐日累积会把 entry_candidates 撑大；
    这里只留下游真正会用到的字段。
    """
    raw = meta if isinstance(meta, dict) else {}
    if not raw:
        return {}
    out: dict[str, object] = {}
    for key in (
        "trace_id",
        "context_quality_score",
        "trigger_price",
        "entry_low",
        "entry_high",
        "stop_loss",
        "target_price",
        "invalidation",
        "source",
        "fill_source",
        "collected_at",
        "fallback_snapshot_date",
    ):
        value = raw.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            text = value.strip()
            if not text:
                continue
            out[key] = text[:240]
        else:
            out[key] = value
    quote = raw.get("quote") if isinstance(raw.get("quote"), dict) else {}
    if quote:
        out["quote"] = {
            "current_price": _safe_float(quote.get("current_price")),
            "change_pct": _safe_float(quote.get("change_pct")),
            "turnover": _safe_float(quote.get("turnover")),
            "volume": _safe_float(quote.get("volume")),
        }
    plan = raw.get("plan") if isinstance(raw.get("plan"), dict) else {}
    if plan:
        out["plan"] = {
            "entry_low": _safe_float(plan.get("entry_low")),
            "entry_high": _safe_float(plan.get("entry_high")),
            "stop_loss": _safe_float(plan.get("stop_loss")),
            "target_price": _safe_float(plan.get("target_price")),
            "invalidation": str(plan.get("invalidation") or "").strip()[:160],
            # R:R 判定结果要跟着 plan 一起走，否则从 seed 恢复的计划无法辨别
            # "上方空间不足"而被下游误当成可执行。
            "risk_reward": _safe_float(plan.get("risk_reward")),
            "plan_rr_ok": plan.get("plan_rr_ok"),
        }
    return out


def _kline_unavailable(kline: dict | None) -> bool:
    """采集失败或日 K 不够用：不能据此打回避。"""
    k = kline or {}
    if k.get("error"):
        return True
    trend = (k.get("trend") or "").strip()
    macd = (k.get("macd_cross") or "").strip()
    return (not trend or trend == "数据不足") and not macd


def has_intraday_confirm(
    strategy_tags: list[str] | None,
    quote: dict | None = None,
    kline: dict | None = None,
) -> bool:
    """日 K 多头/金叉之外，还要有当日动量、放量或回踩，或涨幅+量比同时达标。"""
    tags = {str(x) for x in (strategy_tags or []) if x}
    if tags & INTRADAY_CONFIRM_TAGS:
        return True
    q = quote or {}
    k = kline or {}
    pct = _safe_float(q.get("change_pct"))
    vol_ratio = _safe_float(k.get("volume_ratio"))
    if vol_ratio is None:
        vol_ratio = _safe_float(q.get("volume_ratio"))
    return (
        pct is not None
        and pct >= float(INTRADAY_CONFIRM_CHANGE_PCT)
        and vol_ratio is not None
        and vol_ratio >= float(INTRADAY_CONFIRM_VOLUME_RATIO)
    )


def _derive_market_scan_decision(
    quote: dict | None, kline: dict | None, *, symbol: str = ""
) -> dict:
    q = quote or {}
    k = kline or {}
    points = 0
    tags: list[str] = []
    reasons: list[str] = []
    heat_line = overheat_change_pct(symbol)

    trend = (k.get("trend") or "").strip()
    if trend == "多头排列":
        points += 2
        tags.append("trend_follow")
        reasons.append("均线多头排列")
    elif trend == "空头排列":
        points -= 2

    macd = (k.get("macd_cross") or "").strip()
    if macd == "金叉":
        points += 2
        tags.append("macd_golden")
        reasons.append("MACD金叉")
    elif macd == "死叉":
        points -= 2

    pct = _safe_float(q.get("change_pct"))
    vol_ratio = _safe_float(k.get("volume_ratio"))
    if vol_ratio is not None:
        if vol_ratio >= 1.8 and (pct is None or pct > 0):
            points += 1
            tags.append("volume_breakout")
            reasons.append(f"放量({vol_ratio:.1f}x)")
        elif vol_ratio <= 0.7:
            points -= 1

    turnover = _safe_float(q.get("turnover"))
    thin_tape = turnover is not None and turnover < float(MIN_TURNOVER_FOR_BUY)
    if thin_tape:
        points -= 2
        reasons.append(f"成交额过低({turnover / 1e8:.2f}亿)")
    overheated = pct is not None and pct >= float(heat_line)
    crashed = pct is not None and pct <= float(CRASH_CHANGE_PCT)
    if pct is not None:
        if overheated:
            points -= 3
            reasons.append(f"涨幅过热({pct:+.2f}%)")
        elif crashed:
            points -= 3
            reasons.append(f"当日大跌({pct:+.2f}%)")
        elif 1.5 <= pct <= 8.5:
            points += 1
            tags.append("momentum")
            reasons.append(f"涨幅{pct:+.2f}%")
        elif pct <= -5.5:
            points += 1
            tags.append("rebound")
            reasons.append("短线超跌")

    support = _safe_float(k.get("support_m")) or _safe_float(k.get("support"))
    last_close = _safe_float(k.get("last_close")) or _safe_float(q.get("current_price"))
    if support and last_close and 0 < support < last_close <= support * 1.03:
        points += 1
        tags.append("pullback")
        reasons.append("回踩支撑附近")

    action = "watch"
    action_label = "观望"
    if overheated:
        action = "watch"
        action_label = "过热观望"
    elif crashed:
        action = "watch"
        action_label = "大跌观望"
    elif thin_tape:
        action = "watch"
        action_label = "流动性观望"
    elif points >= 3 and not has_intraday_confirm(tags, q, k):
        action = "watch"
        action_label = "盘中未确认"
        reasons.append("日K形态缺少当日盘口确认")
    elif points >= 4:
        action = "buy"
        action_label = "建仓"
    elif points >= 3:
        action = "add"
        action_label = "准备加仓"
    elif points <= -3:
        if _kline_unavailable(k):
            action = "watch"
            action_label = "K线不足"
            reasons.append("日K未取到，不按回避处理")
        else:
            # 空头+死叉只是没有买点，不是风险回避
            action = "watch"
            action_label = "形态偏弱"

    signal = "，".join(_strategy_labels(tags[:3])) if tags else "暂无明确信号"
    reason = "，".join(reasons[:3]) if reasons else "等待更高确定性信号后介入"
    return {
        "action": action,
        "action_label": action_label,
        "signal": signal,
        "reason": reason,
        "strategy_tags": tags,
        "points": points,
    }


def _score_market_scan_candidate(
    *,
    action: str,
    quote: dict | None,
    kline: dict | None,
    strategy_tags: list[str] | None,
    symbol: str = "",
) -> tuple[float, list[str]]:
    score = ACTION_BASE_SCORE.get((action or "").strip().lower(), 45.0)
    struct_score, evidence = _score_structure_signals(
        quote=quote, kline=kline, symbol=symbol, strategy_tags=strategy_tags
    )
    score += struct_score
    score = _clamp(score, 0.0, 100.0)
    return score, evidence[:8]


def _load_market_scan_history_inputs(
    *, market: str, limit: int, max_days: int = 7
) -> dict[str, dict]:
    if limit <= 0:
        return {}

    mkt = (market or "CN").strip().upper()
    cutoff = (app_now().date() - timedelta(days=max(1, int(max_days)))).strftime("%Y-%m-%d")
    db = SessionLocal()
    try:
        rows = (
            db.query(EntryCandidate)
            .filter(
                EntryCandidate.candidate_source.in_(("market_scan", "mixed")),
                EntryCandidate.stock_market == mkt,
                EntryCandidate.snapshot_date >= cutoff,
            )
            .order_by(
                case((EntryCandidate.status == "active", 0), else_=1),
                EntryCandidate.snapshot_date.desc(),
                EntryCandidate.score.desc(),
                EntryCandidate.updated_at.desc(),
            )
            .limit(max(1, int(limit)) * 8)
            .all()
        )
    finally:
        db.close()

    out: dict[str, dict] = {}
    for row in rows:
        symbol = (row.stock_symbol or "").strip()
        if not symbol:
            continue
        key = f"{mkt}:{symbol}"
        if key in out:
            continue

        meta = row.meta if isinstance(row.meta, dict) else {}
        quote = meta.get("quote") if isinstance(meta.get("quote"), dict) else {}
        quote_seed = {
            "current_price": _safe_float(quote.get("current_price")),
            "change_pct": _safe_float(quote.get("change_pct")),
            "turnover": _safe_float(quote.get("turnover")),
            "volume": _safe_float(quote.get("volume")),
        }
        if quote_seed["current_price"] is None:
            if row.entry_low is not None and row.entry_high is not None:
                quote_seed["current_price"] = (float(row.entry_low) + float(row.entry_high)) / 2
            elif row.entry_high is not None:
                quote_seed["current_price"] = float(row.entry_high)
            elif row.entry_low is not None:
                quote_seed["current_price"] = float(row.entry_low)

        out[key] = {
            "symbol": symbol,
            "market": mkt,
            "stock_name": (row.stock_name or symbol).strip(),
            "candidate_source": "market_scan",
            "source_agent": (row.source_agent or "market_scan"),
            "source_suggestion_id": row.source_suggestion_id,
            "source_trace_id": row.source_trace_id or "",
            "quote_seed": quote_seed,
            "action": (row.action or "").strip().lower(),
            "action_label": (row.action_label or "").strip(),
            "signal": (row.signal or "").strip(),
            "reason": (row.reason or "").strip(),
            "strategy_tags_seed": list(row.strategy_tags or []),
            "plan_seed": row.plan if isinstance(row.plan, dict) else {},
                "meta": {
                    "source": "market_scan_history",
                    "fill_source": "history",
                    "fallback_snapshot_date": row.snapshot_date,
                    "fallback_candidate_id": row.id,
                    "fallback_status": row.status or "",
                },
        }
        if len(out) >= int(limit):
            break

    return out


def _load_market_scan_snapshot_inputs(
    *, market: str, limit: int, max_days: int = 7
) -> dict[str, dict]:
    if limit <= 0:
        return {}

    mkt = (market or "CN").strip().upper()
    cutoff = (app_now().date() - timedelta(days=max(1, int(max_days)))).strftime("%Y-%m-%d")
    db = SessionLocal()
    try:
        rows = (
            db.query(MarketScanSnapshot)
            .filter(
                MarketScanSnapshot.stock_market == mkt,
                MarketScanSnapshot.snapshot_date >= cutoff,
            )
            .order_by(
                MarketScanSnapshot.snapshot_date.desc(),
                MarketScanSnapshot.score_seed.desc(),
                MarketScanSnapshot.updated_at.desc(),
            )
            .limit(max(1, int(limit)) * 8)
            .all()
        )
    finally:
        db.close()

    out: dict[str, dict] = {}
    for row in rows:
        symbol = (row.stock_symbol or "").strip()
        if not symbol:
            continue
        key = f"{mkt}:{symbol}"
        if key in out:
            continue
        quote_seed = row.quote if isinstance(row.quote, dict) else {}
        out[key] = {
            "symbol": symbol,
            "market": mkt,
            "stock_name": (row.stock_name or symbol).strip(),
            "candidate_source": "market_scan",
            "source_agent": "market_scan",
            "source_suggestion_id": None,
            "source_trace_id": "",
            "quote_seed": quote_seed,
            "meta": {
                "source": "market_scan_snapshot",
                "fill_source": "snapshot",
                "fallback_snapshot_date": row.snapshot_date,
                "fallback_score_seed": _safe_float(row.score_seed),
                "fallback_source": row.source or "market_scan",
            },
        }
        if len(out) >= int(limit):
            break

    return out


def _load_market_scan_seed_inputs(*, market: str, limit: int) -> dict[str, dict]:
    if limit <= 0:
        return {}
    mkt = (market or "CN").strip().upper()
    symbols = list(
        dict.fromkeys(
            [str(s).strip() for s in MARKET_SCAN_SEED_SYMBOLS.get(mkt, []) if str(s).strip()]
        )
    )
    if not symbols:
        return {}
    try:
        rows = md_stock_data(symbols, _to_market(mkt).value)
    except Exception as e:
        logger.warning(f"市场扫描种子池拉取失败({mkt}): {e}")
        rows = []
    out: dict[str, dict] = {}
    for row in rows or []:
        symbol = (getattr(row, "symbol", "") or "").strip()
        if not symbol:
            continue
        key = f"{mkt}:{symbol}"
        quote = {
            "current_price": _safe_float(getattr(row, "current_price", None)),
            "change_pct": _safe_float(getattr(row, "change_pct", None)),
            "turnover": _safe_float(getattr(row, "turnover", None)),
            "volume": _safe_float(getattr(row, "volume", None)),
        }
        if quote["current_price"] is None:
            continue
        out[key] = {
            "symbol": symbol,
            "market": mkt,
            "stock_name": (getattr(row, "name", "") or symbol).strip(),
            "candidate_source": "market_scan",
            "source_agent": "market_scan",
            "source_suggestion_id": None,
            "source_trace_id": "",
            "quote_seed": quote,
            "meta": {
                "source": "market_scan_seed_universe",
                "fill_source": "seed",
                "collected_at": to_iso_with_tz(utc_now()),
            },
        }
        if len(out) >= int(limit):
            break
    return out


def _merge_market_scan_seed(
    base: dict[str, dict],
    incoming: dict[str, dict],
    *,
    market: str,
    limit_per_market: int,
) -> int:
    if not incoming:
        return 0
    mkt = (market or "CN").strip().upper()
    added = 0
    for key, item in incoming.items():
        if not key.startswith(f"{mkt}:"):
            continue
        if key in base:
            exist_quote = (
                base[key].get("quote_seed")
                if isinstance(base[key].get("quote_seed"), dict)
                else {}
            )
            item_quote = (
                item.get("quote_seed")
                if isinstance(item.get("quote_seed"), dict)
                else {}
            )
            if (_safe_float(exist_quote.get("turnover")) or 0.0) < (
                _safe_float(item_quote.get("turnover")) or 0.0
            ):
                base[key] = item
            continue
        base[key] = item
        added += 1
    keys = [k for k in base.keys() if k.startswith(f"{mkt}:")]
    if len(keys) > int(limit_per_market):
        keys_sorted = sorted(keys, key=lambda k: _candidate_sort_key(base.get(k) or {}))
        keep = set(keys_sorted[: int(limit_per_market)])
        for k in keys:
            if k not in keep:
                base.pop(k, None)
    return added


def _is_st_name(name: str | None) -> bool:
    """ST / *ST / S*ST 等风险警示股。"""
    return "ST" in normalize_stock_name(name).upper()


def _is_crash_change(change_pct: float | None) -> bool:
    """热榜采集：当日大跌票直接丢掉，不占扫描名额。"""
    return change_pct is not None and change_pct <= float(CRASH_CHANGE_PCT)


_CN_LIST_PREFIXES = (
    "000",
    "001",
    "002",
    "003",
    "300",
    "301",
    "600",
    "601",
    "603",
    "605",
    "688",
    "689",
    "8",
    "4",
    "92",
)


def is_cn_list_symbol(symbol: str | None) -> bool:
    """A 股/北交所 6 位数字代码。过滤热榜备源里的 0.920306 这类脏码。"""
    code = (symbol or "").strip()
    if not code.isdigit() or len(code) != 6:
        return False
    return code.startswith(_CN_LIST_PREFIXES)


def _load_market_scan_inputs(limit_per_market: int = 60, *, markets: list[str] | None = None) -> dict[str, dict]:
    collector = EastMoneyDiscoveryCollector(
        proxy=_resolve_market_scan_proxy(),
    )
    result: dict[str, dict] = {}
    safe_limit = max(20, int(limit_per_market))

    from src.modules.strategy.opportunity_markets import OPPORTUNITY_MARKETS

    for market in (OPPORTUNITY_MARKETS if markets is None else markets):
        fetch_limit = min(100, safe_limit + 30)
        try:
            turnover = _run_async(
                collector.fetch_hot_stocks(
                    market=market,
                    mode="turnover",
                    limit=fetch_limit,
                )
            )
        except Exception as e:
            logger.warning(f"市场扫描成交榜失败({market}): {e}")
            turnover = []
        try:
            gainers = _run_async(
                collector.fetch_hot_stocks(
                    market=market,
                    mode="gainers",
                    limit=fetch_limit,
                )
            )
        except Exception as e:
            logger.warning(f"市场扫描涨幅榜失败({market}): {e}")
            gainers = []

        # 成交榜 / 涨幅榜按配额各占一半：此前把两榜拼在一起遍历，成交榜一条就能
        # 把 safe_limit 占满并 break，涨幅榜形同废榜（fetch_limit 恒 > limit）。
        quota_turnover = max(1, int(round(safe_limit * 0.6)))
        quota_gainers = max(1, safe_limit - quota_turnover)
        dropped_crash = 0
        dropped_junk = 0

        def _absorb(rows, quota: int) -> int:
            """从 rows 里吸收最多 quota 只新票，返回实际新增数。"""
            nonlocal dropped_crash, dropped_junk
            added = 0
            for row in rows or []:
                if added >= quota:
                    break
                symbol = (getattr(row, "symbol", "") or "").strip()
                if not symbol:
                    continue
                quote = {
                    "current_price": _safe_float(getattr(row, "price", None)),
                    "change_pct": _safe_float(getattr(row, "change_pct", None)),
                    "turnover": _safe_float(getattr(row, "turnover", None)),
                    "volume": _safe_float(getattr(row, "volume", None)),
                }
                if _is_crash_change(quote.get("change_pct")):
                    dropped_crash += 1
                    continue
                if market == "CN" and not is_cn_list_symbol(symbol):
                    dropped_junk += 1
                    continue
                key = f"{market}:{symbol}"
                if key in result:
                    exist_quote = result[key].get("quote_seed") or {}
                    if (
                        _safe_float(exist_quote.get("turnover")) is None
                        and quote.get("turnover") is not None
                    ):
                        result[key]["quote_seed"] = quote
                    continue
                result[key] = {
                    "symbol": symbol,
                    "market": market,
                    "stock_name": (getattr(row, "name", "") or symbol).strip(),
                    "candidate_source": "market_scan",
                    "source_agent": "market_scan",
                    "source_suggestion_id": None,
                    "source_trace_id": "",
                    "quote_seed": quote,
                    "meta": {
                        "source": "market_scan",
                        "fill_source": "live",
                        "collected_at": to_iso_with_tz(utc_now()),
                    },
                }
                added += 1
            return added

        added_turnover = _absorb(turnover, quota_turnover)
        added_gainers = _absorb(gainers, quota_gainers)
        # 两榜都不足额时互相回填，尽量把名额用满（仍按成交榜优先）。
        if len([k for k in result if k.startswith(f"{market}:")]) < safe_limit:
            _absorb(turnover, safe_limit)
        if len([k for k in result if k.startswith(f"{market}:")]) < safe_limit:
            _absorb(gainers, safe_limit)
        if not (turnover or gainers):
            logger.warning("市场扫描热榜全空: market=%s，将回退历史/快照/种子", market)
        logger.debug(
            "市场扫描热榜合并: market=%s turnover=%s gainers=%s",
            market,
            added_turnover,
            added_gainers,
        )
        if dropped_crash:
            logger.info("市场扫描剔除当日大跌: market=%s dropped=%s", market, dropped_crash)
        if dropped_junk:
            logger.info("市场扫描剔除脏代码: market=%s dropped=%s", market, dropped_junk)

        market_count = len([k for k in result if k.startswith(f"{market}:")])
        if market_count < int(LIVE_SCAN_FALLBACK_MIN):
            fallback_map = _load_market_scan_history_inputs(
                market=market,
                limit=max(0, safe_limit - market_count),
                max_days=7,
            )
            added = _merge_market_scan_seed(
                result,
                fallback_map,
                market=market,
                limit_per_market=safe_limit,
            )
            if added > 0:
                logger.info(
                    "市场扫描回退补全: market=%s added=%s current=%s",
                    market,
                    added,
                    len([k for k in result if k.startswith(f"{market}:")]),
                )

        market_count = len([k for k in result if k.startswith(f"{market}:")])
        if market_count < int(LIVE_SCAN_FALLBACK_MIN):
            snapshot_map = _load_market_scan_snapshot_inputs(
                market=market,
                limit=max(0, safe_limit - market_count),
                max_days=14,
            )
            snap_added = _merge_market_scan_seed(
                result,
                snapshot_map,
                market=market,
                limit_per_market=safe_limit,
            )
            if snap_added > 0:
                logger.info(
                    "市场扫描快照补全: market=%s added=%s current=%s",
                    market,
                    snap_added,
                    len([k for k in result if k.startswith(f'{market}:')]),
                )

        market_count = len([k for k in result if k.startswith(f"{market}:")])
        if market_count < int(LIVE_SCAN_FALLBACK_MIN):
            seed_map = _load_market_scan_seed_inputs(
                market=market,
                limit=max(0, safe_limit - market_count),
            )
            seed_added = _merge_market_scan_seed(
                result,
                seed_map,
                market=market,
                limit_per_market=safe_limit,
            )
            if seed_added > 0:
                logger.info(
                    "市场扫描种子补全: market=%s added=%s current=%s",
                    market,
                    seed_added,
                    len([k for k in result if k.startswith(f"{market}:")]),
                )

    st_keys = [k for k, v in result.items() if _is_st_name(v.get("stock_name"))]
    for key in st_keys:
        result.pop(key, None)
    if st_keys:
        logger.info("市场扫描剔除 ST: %s 只", len(st_keys))

    # Final per-market cap and stable ordering.
    for market in (OPPORTUNITY_MARKETS if markets is None else markets):
        keys = [k for k in result.keys() if k.startswith(f"{market}:")]
        if len(keys) <= safe_limit:
            continue
        keys_sorted = sorted(keys, key=lambda k: _candidate_sort_key(result.get(k) or {}))
        keep = set(keys_sorted[:safe_limit])
        for key in keys:
            if key not in keep:
                result.pop(key, None)

    return result


def _persist_market_scan_snapshot(snapshot: str, market_scan_map: dict[str, dict], *, markets: list[str] | None = None) -> None:
    if not snapshot:
        return
    db = SessionLocal()
    try:
        db.query(MarketScanSnapshot).filter(
            MarketScanSnapshot.snapshot_date == snapshot,
            MarketScanSnapshot.stock_market.in_(markets) if markets is not None else True,
        ).delete(synchronize_session=False)
        rows = sorted(
            market_scan_map.values(),
            key=lambda x: (
                str(x.get("market") or ""),
                _candidate_sort_key(x),
            ),
        )
        for item in rows:
            symbol = str(item.get("symbol") or "").strip()
            market = str(item.get("market") or "CN").strip().upper() or "CN"
            if not symbol:
                continue
            quote = item.get("quote_seed") if isinstance(item.get("quote_seed"), dict) else {}
            meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
            db.add(
                MarketScanSnapshot(
                    snapshot_date=snapshot,
                    stock_symbol=symbol,
                    stock_market=market,
                    stock_name=str(item.get("stock_name") or symbol).strip(),
                    source=str(meta.get("source") or "market_scan"),
                    score_seed=float(_safe_float(item.get("score_seed")) or 0.0),
                    quote=to_jsonable(quote),
                    meta=to_jsonable(meta),
                )
            )
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning(f"写入市场池快照失败: {e}")
    finally:
        db.close()


def _format_candidate_row(row: EntryCandidate) -> dict:
    def _fmt(dt):
        if not dt:
            return ""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=utc_now().tzinfo)
        return to_iso_with_tz(dt)

    plan_data = row.plan if isinstance(row.plan, dict) else {}
    entry_low = row.entry_low if row.entry_low is not None else _safe_float(plan_data.get("entry_low"))
    entry_high = row.entry_high if row.entry_high is not None else _safe_float(plan_data.get("entry_high"))
    stop_loss = row.stop_loss if row.stop_loss is not None else _safe_float(plan_data.get("stop_loss"))
    target_price = row.target_price if row.target_price is not None else _safe_float(plan_data.get("target_price"))

    return {
        "id": row.id,
        "stock_symbol": row.stock_symbol,
        "stock_market": row.stock_market,
        "stock_name": row.stock_name,
        "snapshot_date": row.snapshot_date,
        "status": row.status,
        "score": round(float(row.score or 0), 2),
        "confidence": round(float(row.confidence or 0), 3) if row.confidence is not None else None,
        "action": row.action,
        "action_label": row.action_label,
        "candidate_source": row.candidate_source or "watchlist",
        "candidate_source_label": _candidate_source_label(row.candidate_source or "watchlist"),
        "strategy_tags": row.strategy_tags or [],
        "strategy_labels": _strategy_labels(row.strategy_tags or []),
        "is_holding_snapshot": bool(row.is_holding_snapshot),
        "plan_quality": int(row.plan_quality or 0),
        "signal": row.signal or "",
        "reason": row.reason or "",
        "entry_low": entry_low,
        "entry_high": entry_high,
        "stop_loss": stop_loss,
        "target_price": target_price,
        "invalidation": row.invalidation or "",
        "source_agent": row.source_agent or "",
        "source_agent_label": AGENT_LABELS.get(
            (row.source_agent or "").strip(), row.source_agent or ""
        ),
        "source_suggestion_id": row.source_suggestion_id,
        "source_trace_id": row.source_trace_id or "",
        "evidence": row.evidence or [],
        "plan": row.plan or {},
        "meta": row.meta or {},
        "frozen": row.frozen or {},
        "preview": bool((row.meta or {}).get("preview")) if isinstance(row.meta, dict) else False,
        "created_at": _fmt(row.created_at),
        "updated_at": _fmt(row.updated_at),
    }


def _load_latest_suggestions(limit: int = 300) -> list[StockSuggestion]:
    db = SessionLocal()
    try:
        now = utc_now()
        subquery = (
            db.query(
                StockSuggestion.stock_symbol,
                StockSuggestion.stock_market,
                func.max(StockSuggestion.id).label("max_id"),
            )
            .filter(StockSuggestion.agent_name.notin_(RETIRED_AGENT_NAMES))
            .group_by(StockSuggestion.stock_symbol, StockSuggestion.stock_market)
            .subquery()
        )
        rows = (
            db.query(StockSuggestion)
            .join(
                subquery,
                and_(
                    StockSuggestion.id == subquery.c.max_id,
                    StockSuggestion.stock_symbol == subquery.c.stock_symbol,
                    StockSuggestion.stock_market == subquery.c.stock_market,
                ),
            )
            .filter(
                or_(
                    StockSuggestion.expires_at.is_(None),
                    StockSuggestion.expires_at > now,
                )
            )
            .order_by(StockSuggestion.created_at.desc(), StockSuggestion.id.desc())
            .limit(max(1, limit))
            .all()
        )
        return rows
    finally:
        db.close()


def refresh_entry_candidates(
    *,
    max_inputs: int = 300,
    snapshot_date: str | None = None,
    market_scan_limit: int = 60,
    max_kline_symbols: int = 72,
    markets: list[str] | None = None,
) -> dict:
    from src.modules.strategy.opportunity_markets import resolve_opportunity_markets

    snapshot = (snapshot_date or app_now().date().strftime("%Y-%m-%d")).strip()
    markets = resolve_opportunity_markets(markets)
    if markets == []:
        return {"snapshot_date": snapshot, "count": 0, "items": []}
    # 按市场判断当前阶段：盘前/非交易日拿不到当日盘口，收盘后已含当日结果。
    market_phases = {m: market_phase(m) for m in markets}
    suggestions = _load_latest_suggestions(limit=max_inputs)
    suggestions = [s for s in suggestions if s.stock_market in markets]
    scan_kwargs = {"markets": markets}
    market_scan_map = _load_market_scan_inputs(limit_per_market=max(20, int(market_scan_limit)), **scan_kwargs)
    _persist_market_scan_snapshot(snapshot, market_scan_map, **scan_kwargs)
    holding_keys = _load_holding_keys()

    input_map: dict[str, dict] = dict(market_scan_map)
    for s in suggestions:
        market = _to_market(s.stock_market).value
        symbol = (s.stock_symbol or "").strip()
        if not symbol:
            continue
        key = f"{market}:{symbol}"
        seed_quote = _extract_price_from_meta(s.meta or {})
        seed = {
            "symbol": symbol,
            "market": market,
            "stock_name": (s.stock_name or symbol).strip(),
            "candidate_source": "mixed" if key in market_scan_map else "watchlist",
            "source_agent": s.agent_name or "",
            "source_suggestion_id": s.id,
            "source_trace_id": str((s.meta or {}).get("trace_id") or ""),
            "quote_seed": seed_quote,
            "action": (s.action or "watch").strip().lower(),
            "action_label": (s.action_label or "观望").strip(),
            "signal": (s.signal or "").strip(),
            "reason": (s.reason or "").strip(),
            "meta": to_jsonable(s.meta or {}),
            "suggestion_obj": s,
            "strategy_tags_seed": ["watchlist_agent"],
        }
        if key in input_map:
            base = input_map[key]
            if _safe_float((seed_quote or {}).get("current_price")) is None:
                seed["quote_seed"] = base.get("quote_seed") or seed_quote
            seed["candidate_source"] = "mixed"
            if not seed.get("plan_seed") and isinstance(base.get("plan_seed"), dict):
                seed["plan_seed"] = base.get("plan_seed")
            seed["meta"] = to_jsonable(
                {
                    **(
                        base.get("meta")
                        if isinstance(base.get("meta"), dict)
                        else {}
                    ),
                    **(seed.get("meta") or {}),
                    "source_mix": ["market_scan", "watchlist"],
                }
            )
        input_map[key] = seed

    if not input_map:
        return {"snapshot_date": snapshot, "count": 0, "items": []}

    key_set = set(input_map.keys())
    by_market: dict[MarketCode, list[str]] = {}
    for key in key_set:
        market, symbol = key.split(":", 1)
        by_market.setdefault(_to_market(market), []).append(symbol)

    quotes: dict[str, dict] = {}
    for market, symbols in by_market.items():
        uniq = sorted(set([x for x in symbols if x]))
        if not uniq:
            continue
        try:
            rows = md_stock_data(uniq, market.value)
        except Exception as e:
            logger.warning(f"入场候选行情采集失败({market.value}): {e}")
            rows = []
        for q in rows or []:
            quotes[f"{market.value}:{q.symbol}"] = {
                "current_price": getattr(q, "current_price", None),
                "change_pct": getattr(q, "change_pct", None),
                "turnover": getattr(q, "turnover", None),
            }

    quote_fallbacks = _load_quote_fallbacks(key_set)
    for key, value in quote_fallbacks.items():
        current = quotes.get(key) or {}
        if _safe_float(current.get("current_price")) is None:
            quotes[key] = value

    for key, seed in input_map.items():
        current = quotes.get(key) or {}
        if _safe_float(current.get("current_price")) is None:
            seed_quote = seed.get("quote_seed") if isinstance(seed.get("quote_seed"), dict) else {}
            if _safe_float(seed_quote.get("current_price")) is not None:
                quotes[key] = dict(seed_quote)

    kline_summary_map: dict[str, dict] = _load_kline_fallbacks(
        key_set,
        lookback_days=7,
        max_rows=8000,
    )
    fetch_candidates = [k for k in key_set if k not in kline_summary_map]
    if fetch_candidates:
        safe_cap = max(0, int(max_kline_symbols))
        fetch_candidates.sort(
            key=lambda k: (
                0 if input_map.get(k, {}).get("source_suggestion_id") else 1,
                _candidate_sort_key(input_map.get(k) or {}),
                k,
            )
        )
        if safe_cap > 0:
            fetch_candidates = fetch_candidates[:safe_cap]

        # 日 K 原来逐只串行拉，首轮刷新要 20-40s。KlineCollector 内部自带按标的
        # 的并发合并锁与正/负缓存，这里用少量线程并发即可，不需要额外去重。
        def _fetch_one(key: str) -> tuple[str, dict]:
            market, symbol = key.split(":", 1)
            try:
                return key, KlineCollector(_to_market(market)).get_kline_summary(symbol)
            except Exception:
                return key, {}

        workers = max(1, min(int(KLINE_FETCH_WORKERS), len(fetch_candidates)))
        if workers <= 1:
            for key in fetch_candidates:
                k, summary = _fetch_one(key)
                kline_summary_map[k] = summary
        else:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="kline") as pool:
                for k, summary in pool.map(_fetch_one, fetch_candidates):
                    kline_summary_map[k] = summary
    for key in key_set:
        if key not in kline_summary_map:
            kline_summary_map[key] = {}

    db = SessionLocal()
    items: list[dict] = []
    try:
        # 按 (symbol, market, snapshot) upsert，保留稳定 id，避免删建后 SQLite 复用 id 引发串票。
        existing_rows = (
            db.query(EntryCandidate)
            .filter(
                EntryCandidate.snapshot_date == snapshot,
                EntryCandidate.stock_market.in_(markets) if markets is not None else True,
            )
            .all()
        )
        existing_by_key: dict[str, EntryCandidate] = {}
        for row in existing_rows:
            ek = f"{(row.stock_market or 'CN').strip()}:{(row.stock_symbol or '').strip()}"
            if ek in existing_by_key and row.id and existing_by_key[ek].id:
                # 同键重复时丢掉较旧行
                prev = existing_by_key[ek]
                prev_ts = prev.updated_at.timestamp() if prev.updated_at else 0.0
                row_ts = row.updated_at.timestamp() if row.updated_at else 0.0
                drop = row if row_ts < prev_ts else prev
                keep = prev if drop is row else row
                existing_by_key[ek] = keep
                db.delete(drop)
            else:
                existing_by_key[ek] = row
        touched_keys: set[str] = set()

        for key, inp in input_map.items():
            market, symbol = key.split(":", 1)
            quote = dict(quotes.get(key, {}) or {})
            if _safe_float(quote.get("current_price")) is None:
                quote.update(
                    {
                        k: v
                        for k, v in _extract_price_from_meta(inp.get("meta") or {}).items()
                        if v is not None and quote.get(k) is None
                    }
                )
            kline = kline_summary_map.get(key, {}) or {}
            candidate_source = (inp.get("candidate_source") or "watchlist").strip()
            is_holding = key in holding_keys

            suggestion_obj = inp.get("suggestion_obj")
            strategy_tags: list[str] = list(inp.get("strategy_tags_seed") or [])
            if suggestion_obj is not None:
                action = (inp.get("action") or "watch").strip().lower()
                action_label = (inp.get("action_label") or "观望").strip()
                signal = (inp.get("signal") or "").strip()
                reason = (inp.get("reason") or "").strip()
                # 从 K 线反推的策略标签要先补齐再打分：两个候选池共用同一张
                # 结构信号权重表，标签也是其中一项，否则自选池的标签不参与计分、
                # 热榜池参与，尺度又分叉了。
                if (kline.get("trend") or "").strip() == "多头排列":
                    strategy_tags.append("trend_follow")
                if (kline.get("macd_cross") or "").strip() == "金叉":
                    strategy_tags.append("macd_golden")
                if (_safe_float(kline.get("volume_ratio")) or 0) >= 1.8:
                    strategy_tags.append("volume_breakout")
                score, evidence = _score_suggestion(
                    action=action,
                    suggestion=suggestion_obj,
                    quote=quote,
                    kline=kline,
                    strategy_tags=strategy_tags,
                    symbol=symbol,
                )
            else:
                seeded_action = (inp.get("action") or "").strip().lower()
                decision = _derive_market_scan_decision(
                    quote=quote, kline=kline, symbol=symbol
                )
                if seeded_action in ACTION_BASE_SCORE:
                    action = seeded_action
                    action_label = (inp.get("action_label") or decision.get("action_label") or "观望").strip()
                    signal = (inp.get("signal") or decision.get("signal") or "").strip()
                    reason = (inp.get("reason") or decision.get("reason") or "").strip()
                    strategy_tags = list(
                        dict.fromkeys(
                            [
                                x
                                for x in (
                                    inp.get("strategy_tags_seed")
                                    or decision.get("strategy_tags")
                                    or []
                                )
                                if x
                            ]
                        )
                    )
                else:
                    action = decision["action"]
                    action_label = decision["action_label"]
                    signal = decision["signal"]
                    reason = decision["reason"]
                    strategy_tags = list(decision.get("strategy_tags") or [])
                score, evidence = _score_market_scan_candidate(
                    action=action,
                    quote=quote,
                    kline=kline,
                    strategy_tags=strategy_tags,
                    symbol=symbol,
                )

            if is_holding and action in ("buy", "add"):
                action = "add"
                action_label = "持有/加仓"
            if (not is_holding) and action == "add":
                action = "buy"
                action_label = "建仓"

            strategy_tags = list(dict.fromkeys([x for x in strategy_tags if x]))
            change_pct = _safe_float(quote.get("change_pct"))
            # 放量只在当日上涨时算突破；下跌放量是出货，不打 volume_breakout
            if change_pct is not None and change_pct <= 0:
                strategy_tags = [x for x in strategy_tags if x != "volume_breakout"]
            plan = _build_plan(
                action=action,
                quote=quote,
                kline=kline,
                suggestion_meta=(inp.get("meta") or {}),
            )
            seed_plan = inp.get("plan_seed") if isinstance(inp.get("plan_seed"), dict) else {}
            if seed_plan and _plan_quality(plan, quote=quote) < 90:
                merged = dict(seed_plan)
                for k, v in (plan or {}).items():
                    if v is None:
                        continue
                    if isinstance(v, str) and not v.strip():
                        continue
                    merged[k] = v
                plan = merged
            quality = _plan_quality(plan, quote=quote)
            confidence = round(score / 100.0, 3)

            phase = market_phases.get(market, "unknown")
            quote_stale = quote_is_stale(quote, inp.get("meta") or {})

            # 过热/大跌/薄票硬否决：即使上游 seed 给了 buy，也不进 active
            veto = None
            turnover = _safe_float(quote.get("turnover"))
            if change_pct is not None and change_pct >= float(overheat_change_pct(symbol)):
                veto = ("过热观望", f"涨幅过热({change_pct:+.2f}%)")
            elif change_pct is not None and change_pct <= float(CRASH_CHANGE_PCT):
                veto = ("大跌观望", f"当日大跌({change_pct:+.2f}%)")
            elif turnover is not None and turnover < float(MIN_TURNOVER_FOR_BUY):
                veto = ("流动性观望", f"成交额过低({turnover / 1e8:.2f}亿)")
            elif quote_stale and phase_has_intraday_data(phase):
                # 只在"本该有当日行情"的时段判定过期：盘前/非交易日的补位行情
                # 天然是上一交易日的，不能据此否决。
                veto = ("行情过期", f"行情超过 {int(STALE_QUOTE_MINUTES)} 分钟未更新")
            elif phase_has_intraday_data(phase) and not has_intraday_confirm(
                strategy_tags, quote, kline
            ):
                # 只在当日已有盘口数据时要求确认：盘前/非交易日拿到的是上一交易日
                # 的残留数据，用它否决会让 09:15 那轮永远 0 条机会。
                veto = ("盘中未确认", "日K形态缺少当日盘口确认")
            fill_source = str((inp.get("meta") or {}).get("fill_source") or "live")
            if fill_source != "live" and action in ("buy", "add"):
                veto = ("补位观望", "历史/种子补位不作为买入机会")
            if veto and action in ("buy", "add"):
                action = "watch"
                action_label = veto[0]
                evidence = list(evidence or [])
                if veto[1] not in evidence:
                    evidence.append(veto[1])

            # 盘前轮次：保留为"今日预演"名单（标注待确认），不写 frozen，
            # 开盘后由后一轮刷新重新判定；paper trading 侧另有 preview 拦截。
            preview = phase == "pre_market" and action in ("buy", "add")

            status = "inactive"
            threshold = (
                ACTIVE_SCORE_MARKET_SCAN
                if candidate_source in ("market_scan", "mixed")
                else ACTIVE_SCORE_WATCHLIST
            )
            if (
                action in ("buy", "add")
                and quality >= MIN_PLAN_QUALITY_FOR_ACTIVE
                and score >= threshold
            ):
                status = "active"
                if preview:
                    action_label = f"{action_label}(盘前待确认)"
                    evidence = list(evidence or [])
                    if "盘前预演：开盘后需当日盘口确认" not in evidence:
                        evidence.append("盘前预演：开盘后需当日盘口确认")

            row = existing_by_key.get(key)
            if row is None:
                row = EntryCandidate(
                    stock_symbol=symbol,
                    stock_market=market,
                    snapshot_date=snapshot,
                )
                db.add(row)
                existing_by_key[key] = row

            row.stock_symbol = symbol
            row.stock_market = market
            row.stock_name = (inp.get("stock_name") or symbol).strip()
            row.snapshot_date = snapshot
            row.status = status
            row.score = score
            row.confidence = confidence
            row.action = action
            row.action_label = action_label
            row.signal = signal
            row.reason = reason
            row.candidate_source = candidate_source
            row.strategy_tags = to_jsonable(strategy_tags)
            row.is_holding_snapshot = bool(is_holding)
            row.plan_quality = quality
            row.entry_low = _safe_float(plan.get("entry_low"))
            row.entry_high = _safe_float(plan.get("entry_high"))
            row.stop_loss = _safe_float(plan.get("stop_loss"))
            row.target_price = _safe_float(plan.get("target_price"))
            row.invalidation = str(plan.get("invalidation") or "")
            row.source_agent = (inp.get("source_agent") or "")
            row.source_suggestion_id = inp.get("source_suggestion_id")
            row.source_trace_id = str(inp.get("source_trace_id") or "")
            row.evidence = to_jsonable(evidence)
            row.plan = to_jsonable(plan)
            row.meta = to_jsonable(
                {
                    "candidate_source": candidate_source,
                    "fill_source": fill_source,
                    "quote": quote,
                    "kline": {
                        "trend": kline.get("trend"),
                        "macd_cross": kline.get("macd_cross"),
                        "rsi_status": kline.get("rsi_status"),
                        "kdj_status": kline.get("kdj_status"),
                        "volume_ratio": kline.get("volume_ratio"),
                        "support": kline.get("support"),
                        "resistance": kline.get("resistance"),
                        # 策略层 alpha 因子要用独立量价信息，不能再拿 base_score 线性映射：
                        # 这几个字段是 alpha 的原料，缺了就只能退化成"候选分的拷贝"。
                        "atr_pct": kline.get("atr_pct"),
                        "change_5d": kline.get("change_5d"),
                        "change_20d": kline.get("change_20d"),
                        "ma20": kline.get("ma20"),
                    },
                    "strategy_tags": strategy_tags,
                    "is_holding_snapshot": bool(is_holding),
                    "phase": phase,
                    "preview": bool(preview),
                    "quote_stale": bool(quote_stale),
                    "source_meta": _compact_seed_meta(inp.get("meta")),
                }
            )
            # point-in-time 冻结：只认当日首个"盘中轮次"的最终可执行状态，
            # 盘前/收盘后都不写，后验评估因此不会吃到收盘后才成立的信息。
            if not isinstance(row.frozen, dict) or not row.frozen:
                if phase_allows_freeze(phase) and status == "active":
                    row.frozen = to_jsonable(
                        {
                            "status": status,
                            "action": action,
                            "action_label": action_label,
                            "score": round(float(score or 0.0), 4),
                            "plan_quality": int(quality),
                            "entry_low": _safe_float(plan.get("entry_low")),
                            "entry_high": _safe_float(plan.get("entry_high")),
                            "stop_loss": _safe_float(plan.get("stop_loss")),
                            "target_price": _safe_float(plan.get("target_price")),
                            "phase": phase,
                            "snapshot_date": snapshot,
                            "frozen_at": to_iso_with_tz(utc_now()),
                        }
                    )
            row.updated_at = utc_now()
            touched_keys.add(key)
            items.append(_format_candidate_row(row))

        stale_ids = [
            int(row.id)
            for ek, row in existing_by_key.items()
            if row.id is not None and ek not in touched_keys
        ]
        if stale_ids:
            db.query(EntryCandidate).filter(EntryCandidate.id.in_(stale_ids)).delete(
                synchronize_session=False
            )

        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"刷新入场候选失败: {e}")
        raise
    finally:
        db.close()

    items.sort(
        key=lambda x: (
            0
            if x.get("candidate_source") == "market_scan"
            else 1
            if x.get("candidate_source") == "mixed"
            else 2,
            -(x.get("score") or 0),
            x.get("stock_market") or "",
            x.get("stock_symbol") or "",
        )
    )
    return {"snapshot_date": snapshot, "count": len(items), "items": items}


def list_entry_candidates(
    *,
    market: str = "",
    status: str = "active",
    min_score: float = 0,
    limit: int = 20,
    snapshot_date: str = "",
    source: str = "",
    holding: str = "",
    strategy: str = "",
) -> dict:
    def _query_rows():
        db = SessionLocal()
        try:
            snapshot = (snapshot_date or "").strip()
            if not snapshot:
                latest = (
                    db.query(EntryCandidate.snapshot_date)
                    .order_by(EntryCandidate.snapshot_date.desc())
                    .first()
                )
                snapshot = latest[0] if latest else ""
            if not snapshot:
                return "", []

            q = db.query(EntryCandidate).filter(EntryCandidate.snapshot_date == snapshot)
            from src.modules.strategy.opportunity_markets import resolve_opportunity_market_query

            allowed = resolve_opportunity_market_query(market)
            if allowed is None:
                return snapshot, []
            q = q.filter(EntryCandidate.stock_market.in_(allowed))
            st = (status or "").strip().lower()
            if st and st != "all":
                q = q.filter(EntryCandidate.status == st)
            src = (source or "").strip().lower()
            if src and src != "all":
                if src == "market_scan":
                    q = q.filter(EntryCandidate.candidate_source.in_(("market_scan", "mixed")))
                elif src == "watchlist":
                    q = q.filter(EntryCandidate.candidate_source == "watchlist")
                else:
                    q = q.filter(EntryCandidate.candidate_source == src)
            h = (holding or "").strip().lower()
            if h == "held":
                q = q.filter(EntryCandidate.is_holding_snapshot.is_(True))
            elif h == "unheld":
                q = q.filter(EntryCandidate.is_holding_snapshot.is_(False))
            q = q.filter(EntryCandidate.score >= float(min_score or 0))
            tag = (strategy or "").strip()
            if tag:
                # strategy_tags 是 JSON 数组，SQLite 下按文本粗筛，再由调用方做精确比对。
                # 之前只在 limit 之后用 Python 过滤，选具体策略 + 高 min_score 时
                # 实际返回条数会明显少于 limit。
                q = q.filter(
                    func.replace(cast(EntryCandidate.strategy_tags, String), " ", "").like(
                        f'%"{tag}"%'
                    )
                )
            rows = (
                q.order_by(
                    case(
                        (EntryCandidate.candidate_source == "market_scan", 0),
                        (EntryCandidate.candidate_source == "mixed", 1),
                        else_=2,
                    ),
                    EntryCandidate.score.desc(),
                    EntryCandidate.updated_at.desc(),
                )
                .limit(max(20, int(limit) * 2))
                .all()
            )
            return snapshot, rows
        finally:
            db.close()

    snapshot, rows = _query_rows()

    tag = (strategy or "").strip()
    if tag:
        rows = [r for r in rows if tag in (r.strategy_tags or [])]

    rows = rows[: max(1, int(limit))]
    items = [_format_candidate_row(r) for r in rows]
    return {"snapshot_date": snapshot, "count": len(items), "items": items}




def resolve_evaluation_state(row) -> dict | None:
    """后验评估该用哪一版状态：优先当天首个盘中轮次的 frozen，其次当前值。

    返回 ``None`` 表示该行在 T 日不构成"机会"，不应参与评估。

    - 有 frozen：只认冻结那一刻的判定（真正 point-in-time）。
    - 无 frozen 且快照日 >= 冻结上线日：**排除**。这些行是"当时不是 active、收盘后
      才被 22:00 那轮改判成 active"的，用它们参与评估等于把收盘结果泄漏进决策。
    - 无 frozen 且快照日更早：迁移前的历史数据，回退到当前字段并标记
      ``point_in_time=False``，保持可评估但可区分。
    """
    frozen = getattr(row, "frozen", None)
    frozen = frozen if isinstance(frozen, dict) else {}
    if frozen:
        if (frozen.get("status") or "") != "active":
            return None
        return {
            "status": "active",
            "action": frozen.get("action") or getattr(row, "action", "") or "",
            "action_label": frozen.get("action_label") or getattr(row, "action_label", "") or "",
            "score": _safe_float(frozen.get("score")),
            "entry_low": _safe_float(frozen.get("entry_low")),
            "entry_high": _safe_float(frozen.get("entry_high")),
            "stop_loss": _safe_float(frozen.get("stop_loss")),
            "target_price": _safe_float(frozen.get("target_price")),
            "point_in_time": True,
            "frozen_at": frozen.get("frozen_at") or "",
            "frozen_phase": frozen.get("phase") or "",
        }
    if (getattr(row, "status", "") or "") != "active":
        return None
    snapshot = str(getattr(row, "snapshot_date", "") or "").strip()
    if POINT_IN_TIME_START and snapshot >= POINT_IN_TIME_START:
        return None
    return {
        "status": getattr(row, "status", ""),
        "action": getattr(row, "action", "") or "",
        "action_label": getattr(row, "action_label", "") or "",
        "score": _safe_float(getattr(row, "score", None)),
        "entry_low": getattr(row, "entry_low", None),
        "entry_high": getattr(row, "entry_high", None),
        "stop_loss": getattr(row, "stop_loss", None),
        "target_price": getattr(row, "target_price", None),
        "point_in_time": False,
        "frozen_at": "",
        "frozen_phase": "",
    }


def prefetch_evaluation_klines(
    requests: dict[tuple[str, str], int], *, max_workers: int | None = None
) -> dict[tuple[str, str], list]:
    """按 (symbol, market) 并发拉取评估所需日线。

    ``requests`` 的 value 是需要回溯的日历天数（同一 symbol 取最大值）。
    评估阶段此前是串行逐只拉，limit 400/800 条信号时要跑几分钟；刷新阶段早已改成
    线程池，这里补齐。候选层与策略层的评估共用本函数，同一批票只拉一次。
    """
    out: dict[tuple[str, str], list] = {}
    if not requests:
        return out

    def _fetch(item: tuple[tuple[str, str], int]) -> tuple[tuple[str, str], list]:
        key, lookback = item
        symbol, market = key
        if not symbol:
            return key, []
        try:
            rows = KlineCollector(_to_market(market)).get_klines(
                symbol, days=min(max(1, int(lookback)), 600)
            )
            return key, rows
        except Exception:
            return key, []

    items = list(requests.items())
    workers = max_workers if max_workers is not None else int(KLINE_FETCH_WORKERS)
    workers = max(1, min(int(workers), len(items)))
    if workers <= 1:
        for item in items:
            k, v = _fetch(item)
            out[k] = v
        return out
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="eval-kline") as pool:
        for k, v in pool.map(_fetch, items):
            out[k] = v
    return out


def evaluate_entry_candidate_outcomes(
    *,
    horizons: tuple[int, ...] = (1, 3, 5, 10),
    snapshot_days: int = 45,
    limit: int = 400,
) -> dict:
    stats = {
        "total_candidates": 0,
        "eligible": 0,
        "evaluated": 0,
        "skipped_not_due": 0,
        "skipped_no_price": 0,
        "skipped_no_base_price": 0,
        "skipped_not_filled": 0,
    }
    safe_horizons = sorted({max(1, int(h)) for h in horizons if int(h) > 0})
    if not safe_horizons:
        safe_horizons = [1, 3, 5]

    db = SessionLocal()
    try:
        cutoff = app_now().date() - timedelta(days=max(7, int(snapshot_days)))
        # 评估对象 = "当日首个盘中轮次判定为 active" 的票（frozen），
        # 兼容迁移前没有 frozen 的历史数据（回退到当前 status）。
        candidates = (
            db.query(EntryCandidate)
            .filter(
                EntryCandidate.snapshot_date >= cutoff.strftime("%Y-%m-%d"),
                or_(
                    EntryCandidate.status == "active",
                    func.length(func.coalesce(EntryCandidate.frozen, "")) > 2,
                ),
            )
            .order_by(EntryCandidate.snapshot_date.desc(), EntryCandidate.score.desc())
            .limit(max(1, int(limit)) * 4)
            .all()
        )
        stats["total_candidates"] = len(candidates)
        if not candidates:
            return stats

        existing_rows = (
            db.query(EntryCandidateOutcome.candidate_id, EntryCandidateOutcome.horizon_days)
            .filter(EntryCandidateOutcome.candidate_id.in_([c.id for c in candidates]))
            .all()
        )
        existing = {(int(cid), int(h)) for cid, h in existing_rows}

        today = app_now().date()
        # 先扫一遍收集 (symbol, market) 与所需回溯天数，并发预取。
        # 评估循环里按需串行拉 K 线时，limit=400 的场景要跑好几分钟。
        kline_requests: dict[tuple[str, str], int] = {}
        for c in candidates:
            snap_day = _parse_day(c.snapshot_date)
            if snap_day is None:
                continue
            if resolve_evaluation_state(c) is None:
                continue
            key = ((c.stock_symbol or "").strip(), (c.stock_market or "CN").strip().upper())
            lookback = max(120, (today - snap_day).days + 30)
            kline_requests[key] = max(kline_requests.get(key, 0), lookback)
        kline_cache = prefetch_evaluation_klines(kline_requests)
        pending = 0  # 分批提交计数,缩短写事务窗口

        for c in candidates:
            snap_day = _parse_day(c.snapshot_date)
            if snap_day is None:
                continue

            frozen = c.frozen if isinstance(c.frozen, dict) else {}
            eval_state = resolve_evaluation_state(c)
            if eval_state is None:
                # 当天盘中首个快照并非 active（或迁移前数据也不是 active）：
                # 这只票在 T 日不构成机会，不能用收盘后重刷出来的 active 结果参与评估。
                continue
            eval_status = eval_state["status"]
            eval_action = eval_state["action"]
            eval_action_label = eval_state["action_label"]
            eval_score = eval_state["score"]
            eval_entry_low = eval_state["entry_low"]
            eval_entry_high = eval_state["entry_high"]
            eval_stop = eval_state["stop_loss"]
            eval_target = eval_state["target_price"]
            point_in_time = bool(eval_state["point_in_time"])

            key = ((c.stock_symbol or "").strip(), (c.stock_market or "CN").strip().upper())
            klines = kline_cache.get(key, [])

            for horizon in safe_horizons:
                if (c.id, horizon) in existing:
                    continue
                # 逐 bar 取路径：能同时回答"到底有没有成交"和"先触目标还是先触止损"。
                bars = forward_bars(klines, snap_day, horizon, c.stock_market)
                if bars is None:
                    stats["skipped_not_due"] += 1
                    continue
                stats["eligible"] += 1
                outcome = evaluate_plan_path(
                    bars,
                    entry_low=eval_entry_low,
                    entry_high=eval_entry_high,
                    stop_loss=eval_stop,
                    target_price=eval_target,
                )
                target_day = outcome["outcome_day"] or bars[-1]["date"]
                status = outcome["status"]
                ret = outcome["return_pct"]
                base_price = outcome["fill_price"]
                outcome_price = outcome["outcome_price"]
                filled = bool(outcome["filled"])
                hit_target = True if status == "hit_target" else (False if filled else None)
                hit_stop = True if status == "hit_stop" else (False if filled else None)
                if status == "not_filled":
                    # 价格从没回到入场区间：记一行用于诊断"计划挂太高/太窄"，
                    # 但 base_price/收益留空，不参与胜率分母。
                    stats["skipped_not_filled"] = int(stats.get("skipped_not_filled", 0)) + 1

                row = EntryCandidateOutcome(
                    candidate_id=c.id,
                    snapshot_date=c.snapshot_date,
                    stock_symbol=c.stock_symbol,
                    stock_market=c.stock_market,
                    candidate_source=c.candidate_source or "watchlist",
                    strategy_tags=to_jsonable(c.strategy_tags or []),
                    horizon_days=horizon,
                    target_date=target_day.strftime("%Y-%m-%d"),
                    base_price=base_price,
                    outcome_price=outcome_price,
                    outcome_return_pct=ret,
                    hit_target=hit_target,
                    hit_stop=hit_stop,
                    outcome_status=status,
                    meta=to_jsonable(
                        {
                            "horizon_unit": "trading_days",
                            "candidate_score": float(eval_score or 0),
                            "action": eval_action,
                            "action_label": eval_action_label,
                            "point_in_time": point_in_time,
                            "frozen_at": (frozen.get("frozen_at") if frozen else ""),
                            "frozen_phase": (frozen.get("phase") if frozen else ""),
                            # 路径判定留痕：filled=False 的样本收益不可用；
                            # first_hit 记录是哪一侧先被触发，便于核对胜率口径。
                            "path_aware": True,
                            "filled": filled,
                            "fill_day": outcome_day_str(outcome["fill_day"]),
                            "first_hit": outcome["first_hit"] or "",
                            "exit_day": target_day.strftime("%Y-%m-%d"),
                        }
                    ),
                    evaluated_at=utc_now(),
                )
                db.add(row)
                stats["evaluated"] += 1
                existing.add((c.id, horizon))
                pending += 1

            # 分批提交:累计到阈值即落盘,缩短写事务,避免与 60s 调度器并发写长时间持锁
            if pending >= 50:
                db.commit()
                pending = 0

        db.commit()
        return stats
    except Exception as e:
        db.rollback()
        logger.warning(f"候选后验评估失败: {e}")
        return stats
    finally:
        db.close()


def get_entry_candidate_stats(*, days: int = 30) -> dict:
    days = max(1, min(int(days or 30), 365))
    since = utc_now() - timedelta(days=days)
    db = SessionLocal()
    try:
        latest_snapshot_row = (
            db.query(EntryCandidate.snapshot_date)
            .order_by(EntryCandidate.snapshot_date.desc())
            .first()
        )
        latest_snapshot = latest_snapshot_row[0] if latest_snapshot_row else ""
        coverage = {
            "snapshot_date": latest_snapshot,
            "total_snapshot_candidates": 0,
            "total_active": 0,
            "market_scan_active": 0,
            "watchlist_active": 0,
            "held_active": 0,
            "unheld_active": 0,
            "new_active_from_prev": 0,
            "dropped_from_prev": 0,
            "previous_snapshot_date": "",
            "observing_candidates": 0,
        }
        if latest_snapshot:
            snapshot_rows = (
                db.query(EntryCandidate.snapshot_date)
                .distinct()
                .order_by(EntryCandidate.snapshot_date.desc())
                .limit(2)
                .all()
            )
            previous_snapshot = (
                str(snapshot_rows[1][0])
                if len(snapshot_rows) > 1 and snapshot_rows[1] and snapshot_rows[1][0]
                else ""
            )
            total_active = (
                db.query(func.count(EntryCandidate.id))
                .filter(
                    EntryCandidate.snapshot_date == latest_snapshot,
                    EntryCandidate.status == "active",
                )
                .scalar()
            ) or 0
            total_snapshot_candidates = (
                db.query(func.count(EntryCandidate.id))
                .filter(EntryCandidate.snapshot_date == latest_snapshot)
                .scalar()
            ) or 0
            market_active = (
                db.query(func.count(EntryCandidate.id))
                .filter(
                    EntryCandidate.snapshot_date == latest_snapshot,
                    EntryCandidate.status == "active",
                    EntryCandidate.candidate_source.in_(("market_scan", "mixed")),
                )
                .scalar()
            ) or 0
            held_active = (
                db.query(func.count(EntryCandidate.id))
                .filter(
                    EntryCandidate.snapshot_date == latest_snapshot,
                    EntryCandidate.status == "active",
                    EntryCandidate.is_holding_snapshot.is_(True),
                )
                .scalar()
            ) or 0
            unheld_active = (
                db.query(func.count(EntryCandidate.id))
                .filter(
                    EntryCandidate.snapshot_date == latest_snapshot,
                    EntryCandidate.status == "active",
                    EntryCandidate.is_holding_snapshot.is_(False),
                )
                .scalar()
            ) or 0
            new_active_from_prev = 0
            dropped_from_prev = 0
            if previous_snapshot:
                latest_keys = set(
                    db.query(EntryCandidate.stock_market, EntryCandidate.stock_symbol)
                    .filter(
                        EntryCandidate.snapshot_date == latest_snapshot,
                        EntryCandidate.status == "active",
                    )
                    .all()
                )
                prev_keys = set(
                    db.query(EntryCandidate.stock_market, EntryCandidate.stock_symbol)
                    .filter(
                        EntryCandidate.snapshot_date == previous_snapshot,
                        EntryCandidate.status == "active",
                    )
                    .all()
                )
                new_active_from_prev = len(latest_keys - prev_keys)
                dropped_from_prev = len(prev_keys - latest_keys)
            watch_active = max(0, int(total_active) - int(market_active))
            coverage = {
                "snapshot_date": latest_snapshot,
                "total_snapshot_candidates": int(total_snapshot_candidates),
                "total_active": int(total_active),
                "market_scan_active": int(market_active),
                "watchlist_active": int(watch_active),
                "held_active": int(held_active),
                "unheld_active": int(unheld_active),
                "new_active_from_prev": int(new_active_from_prev),
                "dropped_from_prev": int(dropped_from_prev),
                "previous_snapshot_date": previous_snapshot,
                "observing_candidates": max(
                    0, int(total_snapshot_candidates) - int(total_active)
                ),
                "market_scan_share_pct": round((market_active / total_active * 100.0), 2)
                if total_active
                else 0.0,
            }

        outcome_rows = (
            db.query(
                EntryCandidateOutcome.horizon_days,
                EntryCandidateOutcome.candidate_source,
                func.count(EntryCandidateOutcome.id).label("total"),
                func.sum(case((EntryCandidateOutcome.outcome_return_pct > 0, 1), else_=0)).label("wins"),
                func.avg(EntryCandidateOutcome.outcome_return_pct).label("avg_ret"),
            )
            .filter(
                EntryCandidateOutcome.created_at >= since,
                EntryCandidateOutcome.outcome_status.in_(("evaluated", "hit_target", "hit_stop")),
            )
            .group_by(EntryCandidateOutcome.horizon_days, EntryCandidateOutcome.candidate_source)
            .all()
        )
        outcome_summary = []
        for h, src, total_eval, wins, avg_ret in outcome_rows:
            total_eval = int(total_eval or 0)
            wins = int(wins or 0)
            outcome_summary.append(
                {
                    "horizon_days": int(h or 0),
                    "source": src or "watchlist",
                    "source_label": _candidate_source_label(src or "watchlist"),
                    "total": total_eval,
                    "wins": wins,
                    "win_rate": round((wins / total_eval * 100.0), 2) if total_eval > 0 else 0.0,
                    "avg_return_pct": round(float(avg_ret or 0.0), 3),
                }
            )
        outcome_summary.sort(key=lambda x: (x["horizon_days"], x["total"]), reverse=True)

        return {
            "window_days": days,
            "coverage": coverage,
            "outcomes": outcome_summary,
        }
    finally:
        db.close()
