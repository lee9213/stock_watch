from __future__ import annotations

from src.platform.marketdata.outcome_prices import completed_outcome_bar

import asyncio
import logging
from datetime import date, datetime, timedelta

from sqlalchemy import and_, case, func, or_

from src.modules.automation.agent_catalog import RETIRED_AGENT_NAMES
from src.platform.runtime.config import Settings
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


ACTION_BASE_SCORE: dict[str, float] = {
    "buy": 78.0,
    "add": 72.0,
    "hold": 58.0,
    "watch": 52.0,
    "alert": 45.0,
    "reduce": 30.0,
    "sell": 20.0,
    "avoid": 15.0,
}

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
    text_keys = ("trend", "macd_cross", "rsi_status", "kdj_status")
    num_keys = (
        "volume_ratio",
        "support",
        "resistance",
        "support_m",
        "resistance_m",
        "last_close",
        "close",
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

    cutoff_day = (date.today() - timedelta(days=max(1, int(lookback_days)))).strftime("%Y-%m-%d")
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


def _score_suggestion(
    *,
    action: str,
    suggestion: StockSuggestion,
    quote: dict | None,
    kline: dict | None,
) -> tuple[float, list[str]]:
    score = ACTION_BASE_SCORE.get((action or "").strip().lower(), 45.0)
    evidence: list[str] = []

    if suggestion.signal:
        evidence.append(f"建议信号: {suggestion.signal}")
    if suggestion.reason:
        evidence.append(f"建议依据: {suggestion.reason}")

    if quote:
        pct = _safe_float(quote.get("change_pct"))
        if pct is not None:
            if 0.5 <= abs(pct) <= 6:
                score += 2
                evidence.append(f"当日波动适中({pct:+.2f}%)")
            elif abs(pct) >= 10:
                score -= 3
                evidence.append(f"当日波动过大({pct:+.2f}%)")

    if kline:
        trend = (kline.get("trend") or "").strip()
        if trend == "多头排列":
            score += 8
            evidence.append("均线多头排列")
        elif trend == "空头排列":
            score -= 8
            evidence.append("均线空头排列")

        macd = (kline.get("macd_cross") or "").strip()
        if macd == "金叉":
            score += 5
            evidence.append("MACD 金叉")
        elif macd == "死叉":
            score -= 5
            evidence.append("MACD 死叉")

        rsi_status = (kline.get("rsi_status") or "").strip()
        if rsi_status in ("超卖", "偏弱"):
            score += 2
            evidence.append(f"RSI {rsi_status}")
        elif rsi_status in ("超买",):
            score -= 3
            evidence.append(f"RSI {rsi_status}")

        kdj_status = (kline.get("kdj_status") or "").strip()
        if "金叉" in kdj_status:
            score += 3
            evidence.append("KDJ 金叉")
        elif "死叉" in kdj_status:
            score -= 3
            evidence.append("KDJ 死叉")

        vol_ratio = _safe_float(kline.get("volume_ratio"))
        if vol_ratio is not None:
            if vol_ratio >= 2:
                score += 4
                evidence.append(f"量比放大({vol_ratio:.1f}x)")
            elif vol_ratio >= 1.3:
                score += 2
                evidence.append(f"量比温和放大({vol_ratio:.1f}x)")

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
    trend = (kline or {}).get("trend") or ""

    entry_low = None
    entry_high = None
    stop_loss = None
    target_price = None
    invalidation = ""

    if price is not None:
        if support is None:
            support = price * 0.95
        if resistance is None:
            resistance = price * 1.06

        if action in ("buy", "add"):
            entry_low = price * 0.99
            entry_high = price * 1.01
            stop_loss = (support * 0.985) if support else (price * 0.95)
            if resistance and resistance > price:
                target_price = resistance * 0.99
            else:
                target_price = price * 1.06
            invalidation = f"若跌破 {stop_loss:.2f} 则失效"
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
    }


def _candidate_source_label(source: str) -> str:
    return CANDIDATE_SOURCE_LABELS.get((source or "").strip(), source or "")


def _strategy_labels(tags: list[str] | None) -> list[str]:
    out: list[str] = []
    for t in tags or []:
        if not t:
            continue
        out.append(STRATEGY_LABELS.get(t, t))
    return out


# 涨幅过热硬阈值（%）——达到后不再给 buy/add active
OVERHEAT_CHANGE_PCT = 9.5
# 热榜至少有这么多活数据时，不再用历史/种子蓝筹补位
LIVE_SCAN_FALLBACK_MIN = 1
# 当日大跌硬阈值（%）——跌幅达到后不再给 buy/add active（日 K 技术标签反映的是昨天，接不住今天的下跌）
CRASH_CHANGE_PCT = -7.0
# A 股买入最低成交额（元）；低于此值视为薄票，不给 buy/add
MIN_TURNOVER_FOR_BUY = 80_000_000.0
# 日 K 多头/金叉必须叠加当日盘口，否则只是昨天的形态
INTRADAY_CONFIRM_TAGS = frozenset({"momentum", "volume_breakout", "pullback"})
INTRADAY_CONFIRM_CHANGE_PCT = 1.5
INTRADAY_CONFIRM_VOLUME_RATIO = 1.2
# 入场区间相对现价的合理宽度上下限
_PLAN_BAND_WIDTH_MIN = 0.002  # 0.2%
_PLAN_BAND_WIDTH_MAX = 0.08   # 8%
# 计划中轴相对现价最大偏离倍数
_PLAN_MID_PRICE_RATIO_MAX = 1.5


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
            if width < _PLAN_BAND_WIDTH_MIN or width > _PLAN_BAND_WIDTH_MAX:
                structural_ok = False
            if stop_loss is not None and stop_loss >= mid:
                structural_ok = False
            if target_price is not None and target_price <= mid:
                structural_ok = False
            price = _safe_float((quote or {}).get("current_price"))
            if price is not None and price > 0 and mid > 0:
                ratio = max(price / mid, mid / price)
                if ratio > _PLAN_MID_PRICE_RATIO_MAX:
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
    """过热阈值：涨停幅度减 0.5 个百分点。"""
    return cn_limit_pct(symbol) - 0.5


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
    evidence: list[str] = []

    q = quote or {}
    k = kline or {}
    tags = strategy_tags or []

    if tags:
        score += min(8, len(tags) * 2)
        evidence.append("策略信号: " + " / ".join(_strategy_labels(tags[:3])))

    pct = _safe_float(q.get("change_pct"))
    if pct is not None:
        if pct >= float(overheat_change_pct(symbol)):
            score -= 8
            evidence.append(f"涨幅过热({pct:+.2f}%)")
        elif 1 <= pct <= 7:
            score += 2
            evidence.append(f"价格动量({pct:+.2f}%)")

    turnover = _safe_float(q.get("turnover"))
    if turnover is not None:
        if turnover >= 3e9:
            score += 3
            evidence.append("成交额高")
        elif turnover >= 1e9:
            score += 1

    trend = (k.get("trend") or "").strip()
    if trend == "多头排列":
        score += 5
        evidence.append("均线多头排列")
    elif trend == "空头排列":
        score -= 6
        evidence.append("均线空头排列")

    score = _clamp(score, 0.0, 100.0)
    return score, evidence[:8]


def _load_market_scan_history_inputs(
    *, market: str, limit: int, max_days: int = 7
) -> dict[str, dict]:
    if limit <= 0:
        return {}

    mkt = (market or "CN").strip().upper()
    cutoff = (date.today() - timedelta(days=max(1, int(max_days)))).strftime("%Y-%m-%d")
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
    cutoff = (date.today() - timedelta(days=max(1, int(max_days)))).strftime("%Y-%m-%d")
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

        merged = list(turnover or []) + list(gainers or [])
        if not merged:
            logger.warning("市场扫描热榜全空: market=%s，将回退历史/快照/种子", market)
        dropped_crash = 0
        dropped_junk = 0
        for row in merged:
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
                if _safe_float(exist_quote.get("turnover")) is None and quote.get("turnover") is not None:
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
            if len([k for k in result if k.startswith(f"{market}:")]) >= safe_limit:
                break
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

    snapshot = (snapshot_date or date.today().strftime("%Y-%m-%d")).strip()
    markets = resolve_opportunity_markets(markets)
    if markets == []:
        return {"snapshot_date": snapshot, "count": 0, "items": []}
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
        for key in fetch_candidates:
            market, symbol = key.split(":", 1)
            try:
                kline_summary_map[key] = KlineCollector(_to_market(market)).get_kline_summary(symbol)
            except Exception:
                kline_summary_map[key] = {}
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
                score, evidence = _score_suggestion(
                    action=action,
                    suggestion=suggestion_obj,
                    quote=quote,
                    kline=kline,
                )
                if (kline.get("trend") or "").strip() == "多头排列":
                    strategy_tags.append("trend_follow")
                if (kline.get("macd_cross") or "").strip() == "金叉":
                    strategy_tags.append("macd_golden")
                if (_safe_float(kline.get("volume_ratio")) or 0) >= 1.8:
                    strategy_tags.append("volume_breakout")
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

            # 过热/大跌/薄票硬否决：即使上游 seed 给了 buy，也不进 active
            veto = None
            turnover = _safe_float(quote.get("turnover"))
            if change_pct is not None and change_pct >= float(overheat_change_pct(symbol)):
                veto = ("过热观望", f"涨幅过热({change_pct:+.2f}%)")
            elif change_pct is not None and change_pct <= float(CRASH_CHANGE_PCT):
                veto = ("大跌观望", f"当日大跌({change_pct:+.2f}%)")
            elif turnover is not None and turnover < float(MIN_TURNOVER_FOR_BUY):
                veto = ("流动性观望", f"成交额过低({turnover / 1e8:.2f}亿)")
            elif not has_intraday_confirm(strategy_tags, quote, kline):
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

            status = "inactive"
            threshold = 62 if candidate_source in ("market_scan", "mixed") else 55
            if action in ("buy", "add") and quality >= 90 and score >= threshold:
                status = "active"

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
                    },
                    "strategy_tags": strategy_tags,
                    "is_holding_snapshot": bool(is_holding),
                    "source_meta": inp.get("meta") or {},
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
                .limit(max(20, int(limit) * 4))
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
    }
    safe_horizons = sorted({max(1, int(h)) for h in horizons if int(h) > 0})
    if not safe_horizons:
        safe_horizons = [1, 3, 5]

    db = SessionLocal()
    try:
        cutoff = date.today() - timedelta(days=max(7, int(snapshot_days)))
        candidates = (
            db.query(EntryCandidate)
            .filter(
                EntryCandidate.status == "active",
                EntryCandidate.snapshot_date >= cutoff.strftime("%Y-%m-%d"),
            )
            .order_by(EntryCandidate.snapshot_date.desc(), EntryCandidate.score.desc())
            .limit(max(1, int(limit)))
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

        today = date.today()
        kline_cache: dict[tuple[str, str], list] = {}
        pending = 0  # 分批提交计数,缩短写事务窗口

        for c in candidates:
            snap_day = _parse_day(c.snapshot_date)
            if snap_day is None:
                continue

            key = ((c.stock_symbol or "").strip(), (c.stock_market or "CN").strip().upper())
            if key not in kline_cache:
                try:
                    lookback = max(120, (today - snap_day).days + 30)
                    kline_cache[key] = KlineCollector(_to_market(key[1])).get_klines(
                        key[0], days=min(lookback, 600)
                    )
                except Exception:
                    kline_cache[key] = []
            klines = kline_cache[key]

            for horizon in safe_horizons:
                if (c.id, horizon) in existing:
                    continue
                target_day = snap_day + timedelta(days=horizon)
                if target_day > today:
                    stats["skipped_not_due"] += 1
                    continue

                bar = completed_outcome_bar(klines, snap_day, horizon, c.stock_market)
                if bar is None:
                    stats["skipped_not_due"] += 1
                    continue
                target_day, outcome_price = bar
                stats["eligible"] += 1

                base_price = None
                if c.entry_low is not None and c.entry_high is not None:
                    base_price = (float(c.entry_low) + float(c.entry_high)) / 2
                elif c.entry_high is not None:
                    base_price = float(c.entry_high)
                elif c.entry_low is not None:
                    base_price = float(c.entry_low)
                if base_price is None:
                    meta = c.meta if isinstance(c.meta, dict) else {}
                    quote = meta.get("quote") if isinstance(meta.get("quote"), dict) else {}
                    base_price = _safe_float(quote.get("current_price"))
                if base_price is None:
                    base_price = _pick_close_on_or_before(klines, snap_day)
                if base_price is None or base_price <= 0:
                    stats["skipped_no_base_price"] += 1
                    status = "no_base_price"
                    ret = None
                else:
                    ret = (outcome_price - base_price) / base_price * 100.0
                    if c.target_price is not None and outcome_price >= float(c.target_price):
                        status = "hit_target"
                    elif c.stop_loss is not None and outcome_price <= float(c.stop_loss):
                        status = "hit_stop"
                    else:
                        status = "evaluated"

                hit_target = (
                    bool(c.target_price is not None and outcome_price >= float(c.target_price))
                    if status != "no_base_price"
                    else None
                )
                hit_stop = (
                    bool(c.stop_loss is not None and outcome_price <= float(c.stop_loss))
                    if status != "no_base_price"
                    else None
                )

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
                            "candidate_score": float(c.score or 0),
                            "action": c.action or "",
                            "action_label": c.action_label or "",
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
