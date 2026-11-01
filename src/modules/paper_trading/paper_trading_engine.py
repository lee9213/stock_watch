"""模拟盘引擎：自动按策略信号建仓/平仓，跟踪虚拟账户收益。"""

from __future__ import annotations

import asyncio
import logging
import os
from functools import wraps
from threading import RLock
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.platform.marketdata.marketdata_client import md_quote_rows
from src.platform.marketdata.models import MarketCode
from src.platform.marketdata.quote_display import quote_date_is_current
from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import (
    PaperTradingAccount,
    PaperTradingPosition,
    PaperTradingSettlement,
    PaperTradingTrade,
    StrategySignalRun,
)
from src.modules.strategy.backtest.cost_model import CostModel
from src.modules.paper_trading.paper_settlement import (
    buying_power, consume_cn_proceeds, record_sale, settle_due, settlement_date,
)

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


# 入场门禁：够格才自动开仓（可用环境变量覆盖）
MIN_RANK_SCORE_FOR_ENTRY = _env_float("PAPER_MIN_RANK_SCORE", 75.0)
REQUIRE_NEWS_CATALYST = _env_bool("PAPER_REQUIRE_NEWS", True)
# 新闻偏向下限：低于此值视为利空，不算催化
MIN_EVENT_BIAS_FOR_ENTRY = _env_float("PAPER_MIN_EVENT_BIAS", 0.0)
MAX_ENTRIES_PER_SCAN = max(1, _env_int("PAPER_MAX_ENTRIES_PER_SCAN", 2))
# 同时持仓只数上限
MAX_OPEN_POSITIONS = max(1, _env_int("PAPER_MAX_OPEN_POSITIONS", 6))
# 持仓成本合计占初始资金的上限
MAX_TOTAL_EXPOSURE = _env_float("PAPER_MAX_TOTAL_EXPOSURE", 0.50)
# 同板块（主题/上市板）最多持仓只数
MAX_POSITIONS_PER_SECTOR = max(1, _env_int("PAPER_MAX_POSITIONS_PER_SECTOR", 2))
# 平仓后多少个交易日内不再买同一只（0=不限制）
REENTRY_COOLDOWN_SESSIONS = max(0, _env_int("PAPER_REENTRY_COOLDOWN_SESSIONS", 2))
# 收盘前多少分钟起不再新开仓
ENTRY_CUTOFF_MINUTES = max(0, _env_int("PAPER_ENTRY_CUTOFF_MINUTES", 15))
# 当日首个交易时段开盘后多少分钟内不新开仓
ENTRY_OPEN_DELAY_MINUTES = max(0, _env_int("PAPER_ENTRY_OPEN_DELAY_MINUTES", 30))
# 止盈至少为止损距离的倍数
MIN_RR_RATIO = _env_float("PAPER_MIN_RR_RATIO", 1.5)
# 现价相对入场中轴最大偏离（串票/脏计划防护，默认 3 倍）
MAX_ENTRY_PLAN_PRICE_RATIO = _env_float("PAPER_MAX_ENTRY_PLAN_RATIO", 3.0)
# 入场区间相对容差（默认 ±1%）
ENTRY_BAND_TOLERANCE = _env_float("PAPER_ENTRY_BAND_TOLERANCE", 0.01)
# 信号快照最多允许落后几个交易日（0=仅当日，1=当日+上一交易日）
MAX_SIGNAL_AGE_SESSIONS = max(0, _env_int("PAPER_MAX_SIGNAL_AGE_SESSIONS", 1))
# A 股最低成交额（元）；港股/美股单独下限。缺报价不拦。
MIN_TURNOVER_CN = _env_float("PAPER_MIN_TURNOVER_CN", 80_000_000.0)
MIN_TURNOVER_HK = _env_float("PAPER_MIN_TURNOVER_HK", 8_000_000.0)
MIN_TURNOVER_US = _env_float("PAPER_MIN_TURNOVER_US", 5_000_000.0)
# 止损距买入价的允许区间（比例）；无效止损用默认值
STOP_MIN_PCT = _env_float("PAPER_STOP_MIN_PCT", 0.03)
STOP_MAX_PCT = _env_float("PAPER_STOP_MAX_PCT", 0.10)
STOP_DEFAULT_PCT = _env_float("PAPER_STOP_DEFAULT_PCT", 0.08)
# 止盈距买入价的允许区间（比例）；无效止盈用默认值
TARGET_MIN_PCT = _env_float("PAPER_TARGET_MIN_PCT", 0.05)
TARGET_MAX_PCT = _env_float("PAPER_TARGET_MAX_PCT", 0.30)
TARGET_DEFAULT_PCT = _env_float("PAPER_TARGET_DEFAULT_PCT", 0.15)

# The scheduler and manual requests share one cash account in the server process.
# Serialize reads and writes together so concurrent scans cannot spend one balance twice.
ACCOUNT_LOCK = RLock()


def _account_mutation(method):
    @wraps(method)
    def guarded(*args, **kwargs):
        with ACCOUNT_LOCK:
            return method(*args, **kwargs)
    return guarded

# 模拟盘交易成本(A股口径,Phase 1)。与回测共用同一成本模型。
COST_MODEL = CostModel()

# 建仓股数下限(A股一手)
FIXED_QUANTITY = 100

# 移动止损:浮盈超过 MIN_PROFIT_FOR_TRAILING 后启用,从持仓最高价回撤超 TRAILING_STOP_PCT 即离场,
# 且离场线不低于买入价(保本)。机会盘更早启动、回撤更紧。
MIN_PROFIT_FOR_TRAILING = _env_float("PAPER_TRAILING_MIN_PROFIT", 0.03)
TRAILING_STOP_PCT = _env_float("PAPER_TRAILING_STOP_PCT", 0.04)
# 时间止损:默认最多持有交易日，并与信号 holding_days 取较小值
DEFAULT_TIME_STOP_DAYS = max(1, _env_int("PAPER_TIME_STOP_DAYS", 5))
# 最新信号变成这些动作时按反转离场(看空信号一律 inactive,故不按状态过滤)
REVERSAL_ACTIONS = ("avoid", "sell", "reduce")


def _position_weight(rank_score: float) -> float:
    """按信号强度分配单笔资金占该市场预算的比例(rank_score 越高投越多)。"""
    s = float(rank_score or 0.0)
    if s >= 85:
        return 0.18
    if s >= 75:
        return 0.14
    if s >= 65:
        return 0.10
    return 0.08


def entry_window_open(market: str) -> bool:
    """当前是否还允许新开仓：开盘缓冲之后、尾盘截止之前。"""
    from src.platform.scheduling.trading_calendar import _now_in_market_tz, trading_sessions

    now = _now_in_market_tz(market)
    sessions = trading_sessions(market, now)
    if not sessions:
        return False
    first_open = datetime.combine(now.date(), sessions[0].start, tzinfo=now.tzinfo)
    if now < first_open + timedelta(minutes=ENTRY_OPEN_DELAY_MINUTES):
        return False
    close_at = datetime.combine(now.date(), sessions[-1].end, tzinfo=now.tzinfo)
    return now < close_at - timedelta(minutes=ENTRY_CUTOFF_MINUTES)


def trailing_stop_line(entry_price: float, highest_price: float) -> float | None:
    """浮盈达标后的移动止损线（不低于买入价）；未达标返回 None。"""
    if entry_price <= 0 or highest_price <= 0:
        return None
    if (highest_price - entry_price) / entry_price < MIN_PROFIT_FOR_TRAILING:
        return None
    return max(entry_price, highest_price * (1 - TRAILING_STOP_PCT))


def trading_days_held(market: str, opened_at: datetime, now: datetime) -> int:
    """买入日之后到今天（含）经过的交易日数，按市场本地日历。"""
    from src.platform.scheduling.trading_calendar import _market_tz, is_trading_day

    tz = _market_tz(market)
    opened = opened_at if opened_at.tzinfo else opened_at.replace(tzinfo=timezone.utc)
    start = opened.astimezone(tz).date()
    end = now.astimezone(tz).date()
    held = 0
    cursor = start + timedelta(days=1)
    while cursor <= end:
        if is_trading_day(market, cursor):
            held += 1
        cursor += timedelta(days=1)
    return held


def clamp_exit_levels(
    entry_price: float,
    stop_loss: float | None,
    target_price: float | None,
) -> tuple[float, float]:
    """把止损/止盈限制在买入价的合理距离内，返回 (stop_loss, target_price)。"""
    entry = float(entry_price)
    if stop_loss is None or stop_loss <= 0 or stop_loss >= entry:
        stop_dist = STOP_DEFAULT_PCT
    else:
        stop_dist = min(max((entry - stop_loss) / entry, STOP_MIN_PCT), STOP_MAX_PCT)
    if target_price is None or target_price <= entry:
        target_gain = TARGET_DEFAULT_PCT
    else:
        target_gain = min(max((target_price - entry) / entry, TARGET_MIN_PCT), TARGET_MAX_PCT)
    min_target = stop_dist * float(MIN_RR_RATIO)
    if target_gain < min_target:
        target_gain = min(min_target, TARGET_MAX_PCT)
    return round(entry * (1 - stop_dist), 4), round(entry * (1 + target_gain), 4)


def lot_size(symbol: str, market: str = "CN") -> int:
    """A 股科创板 200 股一手，其余默认 100。"""
    if (market or "CN").strip().upper() == "CN" and str(symbol or "").startswith("688"):
        return 200
    return FIXED_QUANTITY


def industry_bucket(symbol: str, name: str | None = None) -> str:
    """粗分主题/上市板，用于同板块持仓上限。"""
    text = (name or "").replace(" ", "").replace("\u3000", "")
    rules = (
        ("白酒", ("茅台", "五粮液", "汾酒", "洋河", "泸州", "古井", "今世缘", "水井坊")),
        ("银行", ("银行",)),
        ("证券", ("证券", "券商")),
        ("医药", ("药业", "医药", "生物", "制药")),
        ("通信", ("通信", "光电", "光纤", "光模块", "中际", "天孚", "新易盛", "中兴")),
        ("半导体", ("芯片", "半导体", "硅", "存储", "中芯", "寒武", "海光", "澜起", "长鑫")),
        ("机器人", ("机器人", "谐波", "伺服", "宇树", "汇川", "埃斯顿")),
        ("传媒", ("传媒", "数科", "网络", "影视", "游戏")),
    )
    for bucket, keywords in rules:
        if any(key in text for key in keywords):
            return bucket
    code = (symbol or "").strip()
    if code.startswith("688"):
        return "科创板"
    if code.startswith(("300", "301")):
        return "创业板"
    if code.startswith(("8", "4")) or code.startswith("92"):
        return "北交所"
    return "主板"


def _compute_quantity(
    *,
    rank_score: float,
    market_budget: float,
    price: float,
    available_cash: float,
    cost_model: CostModel,
    lot: int = FIXED_QUANTITY,
) -> int:
    """按信号强度 + 市场预算计算建仓股数(lot 整数倍),受可用现金(含买入费)约束。

    返回 0 表示连最小一手都买不起,应跳过。
    """
    if price <= 0:
        return 0
    target_cash = max(0.0, market_budget) * _position_weight(rank_score)
    qty = int((target_cash / price) // lot) * lot
    if qty < lot:
        qty = lot  # 至少一手
    while qty >= lot:
        outlay = -cost_model.fill("buy", price, qty).cash_delta
        if outlay <= available_cash:
            return qty
        qty -= lot
    return 0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


SELL_BLOCK_MESSAGES = {
    "paper_trading_t1_locked": "A 股实行 T+1：当日买入的持仓须到下一交易日才能卖出",
    "paper_trading_open_time_missing": "持仓买入时间缺失，无法确认可卖数量",
}


def sell_block_reason(pos: PaperTradingPosition, *, now: datetime | None = None) -> str | None:
    """Settlement eligibility only; fills still require an open market and valid quote.

    SQLite stores naive UTC timestamps. T+1 uses the Shanghai purchase date,
    not elapsed 24-hour periods. The existing calendar guards enforce sessions.
    """
    if pos.stock_market != "CN":
        return None
    if pos.opened_at is None:
        return "paper_trading_open_time_missing"
    opened = pos.opened_at
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=timezone.utc)
    current = now if now is not None else _utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    shanghai = ZoneInfo("Asia/Shanghai")
    if opened.astimezone(shanghai).date() >= current.astimezone(shanghai).date():
        return "paper_trading_t1_locked"
    return None


def _to_market(market: str) -> MarketCode:
    try:
        return MarketCode(market)
    except Exception:
        return MarketCode.CN


def _is_trading_time(market: str) -> bool:
    from src.platform.scheduling.trading_calendar import market_status
    return market_status(market) == "trading"


def _safe_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 分市场资金配置（投资比例 → 子池现金）
# ---------------------------------------------------------------------------

ALL_MARKETS: tuple[str, ...] = ("CN", "HK", "US")
# 默认资金全部给 A 股；港股/美股不自动开仓（账户可另行覆盖 market_allocations）。
DEFAULT_ALLOCATIONS: dict[str, float] = {"CN": 1.0, "HK": 0.0, "US": 0.0}


def normalize_allocations(raw: dict | None) -> dict[str, float]:
    """补齐三市场、clamp 到 [0,1]，返回 {market: ratio}。"""
    raw = raw or {}
    out: dict[str, float] = {}
    for m in ALL_MARKETS:
        try:
            v = float(raw.get(m, 0.0) or 0.0)
        except Exception:
            v = 0.0
        out[m] = min(1.0, max(0.0, v))
    return out


def market_allocations_or_default(account: Any) -> dict[str, float]:
    """账户未配置比例时回退默认配置，否则归一化已配置的比例。"""
    raw = getattr(account, "market_allocations", None) or {}
    if not raw:
        return dict(DEFAULT_ALLOCATIONS)
    return normalize_allocations(raw)


def allocations_from_excluded(excluded: list[str] | None) -> dict[str, float]:
    """迁移用：被排除市场比例置 0，其余市场按默认权重归一化到合计 1.0。"""
    excluded_set = {str(m).upper() for m in (excluded or [])}
    weights = {m: DEFAULT_ALLOCATIONS[m] for m in ALL_MARKETS if m not in excluded_set}
    total = sum(weights.values())
    if total <= 0:
        # 全部被排除：兜底投 A 股
        return {"CN": 1.0, "HK": 0.0, "US": 0.0}
    return {m: round(weights.get(m, 0.0) / total, 6) for m in ALL_MARKETS}


def compute_market_cash(
    initial_capital: float, ratio: float, realized_pnl: float, open_cost: float
) -> float:
    """某市场账面现金 = 总资金×比例 + 已实现盈亏 − 持仓含费投入。"""
    return initial_capital * ratio + realized_pnl - open_cost


def position_buy_cost(pos: PaperTradingPosition) -> float:
    return -COST_MODEL.fill("buy", pos.entry_price, pos.quantity).cash_delta


def market_realized_open(db: Session, market: str) -> tuple[float, float]:
    """返回 (该市场已实现盈亏合计, 该市场未平仓持仓成本合计)。"""
    realized = (
        db.query(func.coalesce(func.sum(PaperTradingTrade.pnl), 0.0))
        .filter(PaperTradingTrade.stock_market == market)
        .scalar()
    ) or 0.0
    positions = (
        db.query(PaperTradingPosition)
        .filter(
            PaperTradingPosition.status == "open",
            PaperTradingPosition.stock_market == market,
        )
        .all()
    )
    open_cost = sum(position_buy_cost(p) for p in positions)
    return float(realized), float(open_cost)


def market_available_cash(
    db: Session, account: PaperTradingAccount, market: str, alloc: dict | None = None
) -> float:
    """某市场当前可用现金（用于建仓门槛与展示）。"""
    alloc = alloc or market_allocations_or_default(account)
    ratio = alloc.get(market, 0.0)
    realized, open_cost = market_realized_open(db, market)
    cash = compute_market_cash(account.initial_capital, ratio, realized, open_cost)
    now = _utc_now()
    return min(buying_power(db, cash, market, now=now),
               buying_power(db, account.current_capital, market, now=now, all_markets=True))


def account_buying_power(db: Session, account: PaperTradingAccount) -> float:
    """Aggregate allocated limits, capped by the shared cash balance."""
    return min(buying_power(db, account.current_capital, "CN", now=_utc_now(), all_markets=True),
               sum(market_available_cash(db, account, m) for m in ALL_MARKETS))


def _serialize_position(pos: PaperTradingPosition) -> dict:
    """将 ORM Position 提取为 plain dict，避免 detached 问题。"""
    return {
        "id": pos.id,
        "stock_symbol": pos.stock_symbol,
        "stock_market": pos.stock_market,
        "stock_name": pos.stock_name or "",
        "quantity": pos.quantity,
        "entry_price": pos.entry_price,
        "stop_loss": pos.stop_loss,
        "target_price": pos.target_price,
        "current_price": pos.current_price,
        "unrealized_pnl": pos.unrealized_pnl,
        "status": pos.status,
        "strategy_code": pos.strategy_code or "",
    }


def _serialize_trade(trade: PaperTradingTrade) -> dict:
    """将 ORM Trade 提取为 plain dict。"""
    return {
        "id": trade.id,
        "stock_symbol": trade.stock_symbol,
        "stock_market": trade.stock_market,
        "stock_name": trade.stock_name or "",
        "quantity": trade.quantity,
        "entry_price": trade.entry_price,
        "exit_price": trade.exit_price,
        "pnl": trade.pnl,
        "pnl_pct": trade.pnl_pct,
        "exit_reason": trade.exit_reason,
        "holding_days": trade.holding_days,
        "strategy_code": trade.strategy_code or "",
    }


def _serialize_signal(sig: StrategySignalRun) -> dict:
    """将 ORM Signal 提取为 plain dict。"""
    return {
        "id": sig.id,
        "stock_symbol": sig.stock_symbol,
        "stock_market": sig.stock_market,
        "stock_name": sig.stock_name or "",
        "strategy_code": sig.strategy_code or "",
        "rank_score": sig.rank_score,
        "entry_low": sig.entry_low,
        "entry_high": sig.entry_high,
        "action": sig.action,
    }


def price_passes_entry_band(
    current_price: float,
    entry_low: float | None,
    entry_high: float | None,
    *,
    tolerance: float | None = None,
    max_plan_ratio: float | None = None,
) -> tuple[bool, str]:
    """现价须落在入场区间内，且相对计划中轴不能离谱（防串票脏计划）。"""
    if entry_low is None or entry_high is None:
        return False, "missing_entry_plan"
    price = float(current_price)
    low = float(entry_low)
    high = float(entry_high)
    if price <= 0 or low <= 0 or high <= 0:
        return False, "invalid_entry_plan"
    if low > high:
        low, high = high, low
    tol = float(ENTRY_BAND_TOLERANCE if tolerance is None else tolerance)
    band_low = low * (1.0 - max(0.0, tol))
    band_high = high * (1.0 + max(0.0, tol))
    if price < band_low or price > band_high:
        return False, "price_outside_entry_band"

    mid = (low + high) / 2.0
    ratio_cap = float(MAX_ENTRY_PLAN_PRICE_RATIO if max_plan_ratio is None else max_plan_ratio)
    if mid > 0 and ratio_cap > 1.0:
        ratio = max(price / mid, mid / price)
        if ratio > ratio_cap:
            return False, "entry_plan_price_mismatch"
    return True, "ok"


def allowed_signal_snapshot_dates(
    market: str,
    *,
    max_age_sessions: int | None = None,
    as_of: date | None = None,
) -> set[str]:
    """允许的信号 snapshot_date：当日（若为交易日）+ 往前 max_age_sessions 个交易日。"""
    from src.platform.scheduling.trading_calendar import _now_in_market_tz, is_trading_day

    age = int(MAX_SIGNAL_AGE_SESSIONS if max_age_sessions is None else max_age_sessions)
    age = max(0, age)
    cursor = as_of or _now_in_market_tz(market).date()
    allowed: set[str] = set()
    found = 0
    need = age + 1
    for _ in range(20):
        if is_trading_day(market, cursor):
            allowed.add(cursor.isoformat())
            found += 1
            if found >= need:
                break
        cursor -= timedelta(days=1)
    return allowed


def signal_snapshot_is_fresh(
    snapshot_date: str | None,
    market: str,
    *,
    max_age_sessions: int | None = None,
    as_of: date | None = None,
) -> tuple[bool, str]:
    """信号快照是否足够新。"""
    snap = (snapshot_date or "").strip()
    if not snap:
        return False, "missing_snapshot_date"
    allowed = allowed_signal_snapshot_dates(
        market, max_age_sessions=max_age_sessions, as_of=as_of
    )
    if snap not in allowed:
        return False, "signal_stale"
    return True, "ok"


def signal_passes_entry_gate(sig: StrategySignalRun) -> tuple[bool, str]:
    """判断信号是否满足模拟盘自动建仓门禁。

    硬条件：active + buy/add + 有入场区间 + rank_score 达标 + 快照时效。
    软条件（默认开启）：payload 里需有至少 1 条与个股直接相关的新闻，且新闻偏向不为利空，
    技术面 catalyst_score 不加分冒充新闻。
    """
    status = (sig.status or "").strip().lower()
    if status != "active":
        return False, "status_not_active"
    payload = sig.payload if isinstance(sig.payload, dict) else {}
    # 盘前轮次产出的"预演"信号只用于展示：它没有当日盘口确认，不能直接下单。
    if bool(payload.get("preview")):
        return False, "premarket_preview"
    action = (sig.action or "").strip().lower()
    if action not in ("buy", "add"):
        return False, "action_not_entry"
    if sig.entry_low is None or sig.entry_high is None:
        return False, "missing_entry_plan"
    rank = float(sig.rank_score or 0.0)
    if rank < float(MIN_RANK_SCORE_FOR_ENTRY):
        return False, "rank_below_threshold"

    market = (sig.stock_market or "CN").strip().upper() or "CN"
    fresh_ok, fresh_reason = signal_snapshot_is_fresh(sig.snapshot_date, market)
    if not fresh_ok:
        return False, fresh_reason

    if not REQUIRE_NEWS_CATALYST:
        return True, "ok"

    news_metric = payload.get("news_metric") if isinstance(payload.get("news_metric"), dict) else {}
    if int(news_metric.get("direct_news_count") or 0) < 1:
        return False, "missing_news_catalyst"
    event_bias = float(news_metric.get("event_bias") or 0.0)
    if event_bias < float(MIN_EVENT_BIAS_FOR_ENTRY):
        return False, "bearish_news"
    return True, "ok"


def quote_passes_tape_gate(
    quote: dict | None,
    symbol: str,
    market: str,
) -> tuple[bool, str]:
    """现价盘口：接近涨停或成交额过低则不开仓。缺涨跌幅/成交额不拦。"""
    from src.modules.strategy.entry_candidates import overheat_change_pct

    q = quote or {}
    mkt = (market or "CN").strip().upper() or "CN"
    pct = _safe_float(q.get("change_pct"))
    if mkt == "CN" and pct is not None and pct >= float(overheat_change_pct(symbol)):
        return False, "near_limit_up"
    if pct is not None and pct <= 0:
        return False, "intraday_unconfirmed"
    turnover = _safe_float(q.get("turnover"))
    floors = {"CN": MIN_TURNOVER_CN, "HK": MIN_TURNOVER_HK, "US": MIN_TURNOVER_US}
    floor = float(floors.get(mkt) or 0.0)
    if turnover is not None and floor > 0 and turnover < floor:
        return False, "low_liquidity"
    return True, "ok"


class PaperTradingEngine:
    """模拟盘扫描引擎。"""

    def _get_or_create_account(self, db: Session) -> PaperTradingAccount:
        account = db.query(PaperTradingAccount).first()
        if not account:
            account = PaperTradingAccount(
                initial_capital=1000000.0,
                current_capital=1000000.0,
                peak_capital=1000000.0,
            )
            db.add(account)
            db.commit()
            db.refresh(account)
        return account

    def _fetch_quotes_map(self, symbols_markets: list[tuple[str, str]]) -> dict[tuple[str, str], dict]:
        """批量获取报价，返回 {(market, symbol): quote_dict}

        通过 QuoteOrchestrator 调度,支持多 provider 主备故障转移。
        """
        grouped: dict[MarketCode, list[str]] = {}
        for symbol, market in symbols_markets:
            mc = _to_market(market)
            grouped.setdefault(mc, []).append(symbol)

        out: dict[tuple[str, str], dict] = {}
        for market, symbols in grouped.items():
            if not symbols:
                continue
            rows = md_quote_rows(symbols, market.value)
            by_symbol = {str(r.get("symbol")): r for r in rows}
            for sym in symbols:
                q = by_symbol.get(sym)
                if q:
                    out[(market.value, sym)] = q
        return out

    def _check_entries(
        self, db: Session, account: PaperTradingAccount,
    ) -> tuple[int, set[tuple[str, str]], list[tuple[PaperTradingPosition, StrategySignalRun | None]]]:
        """检查可入场的策略信号，自动建仓。返回 (建仓数, 新建仓股票key集合, 建仓事件列表)。"""
        active_markets = [
            market for market in ALL_MARKETS
            if _is_trading_time(market) and entry_window_open(market)
        ]
        if not active_markets:
            return 0, set(), []
        # 查询最新活跃买入信号（SQL 先滤硬条件，新闻催化在内存门禁再确认）
        query = (
            db.query(StrategySignalRun)
            .filter(
                StrategySignalRun.status == "active",
                StrategySignalRun.stock_market.in_(active_markets),
                StrategySignalRun.action.in_(["buy", "add"]),
                StrategySignalRun.entry_low.isnot(None),
                StrategySignalRun.entry_high.isnot(None),
                StrategySignalRun.rank_score >= float(MIN_RANK_SCORE_FOR_ENTRY),
            )
        )
        # 按投资比例排除不投入（比例为 0）的市场
        alloc = market_allocations_or_default(account)
        excluded = [m for m in ALL_MARKETS if alloc.get(m, 0.0) <= 0]
        if excluded:
            query = query.filter(StrategySignalRun.stock_market.notin_(excluded))
        signals = query.order_by(StrategySignalRun.rank_score.desc()).limit(80).all()
        entry_events: list[tuple[PaperTradingPosition, StrategySignalRun | None]] = []
        new_keys: set[tuple[str, str]] = set()
        if not signals:
            return 0, new_keys, entry_events

        # 已有 open position 的股票
        open_keys = set()
        open_positions = (
            db.query(PaperTradingPosition)
            .filter(PaperTradingPosition.status == "open")
            .all()
        )
        sector_counts: dict[str, int] = {}
        for p in open_positions:
            open_keys.add((p.stock_symbol, p.stock_market))
            bucket = industry_bucket(p.stock_symbol, p.stock_name)
            sector_counts[bucket] = sector_counts.get(bucket, 0) + 1

        entry_limit = min(int(MAX_ENTRIES_PER_SCAN), int(MAX_OPEN_POSITIONS) - len(open_positions))
        if entry_limit <= 0:
            return 0, new_keys, entry_events
        exposure_room = (
            account.initial_capital * float(MAX_TOTAL_EXPOSURE)
            - sum(position_buy_cost(p) for p in open_positions)
        )
        if exposure_room <= 0:
            return 0, new_keys, entry_events

        # 平仓冷却：近 REENTRY_COOLDOWN_SESSIONS 个交易日内平过仓的股票不再买入
        cooling: set[tuple[str, str]] = set()
        if REENTRY_COOLDOWN_SESSIONS > 0:
            now = _utc_now()
            recent = (
                db.query(PaperTradingTrade.stock_symbol, PaperTradingTrade.stock_market, PaperTradingTrade.closed_at)
                .filter(PaperTradingTrade.closed_at >= (now - timedelta(days=14)).replace(tzinfo=None))
                .all()
            )
            for sym, mkt, closed_at in recent:
                if closed_at and trading_days_held(mkt, closed_at, now) < REENTRY_COOLDOWN_SESSIONS:
                    cooling.add((sym, mkt))

        # 收集需要报价的信号（去重：同股票只取 rank_score 最高的一条；过门禁才入队）
        candidates = []
        seen = set()
        skipped_gate = 0
        for sig in signals:
            key = (sig.stock_symbol, sig.stock_market)
            if key in open_keys or key in cooling:
                continue
            if key in seen:
                continue
            bucket = industry_bucket(sig.stock_symbol, sig.stock_name)
            if sector_counts.get(bucket, 0) >= int(MAX_POSITIONS_PER_SECTOR):
                skipped_gate += 1
                logger.info(
                    "[模拟盘] 跳过入场 %s:%s reason=sector_cap sector=%s",
                    sig.stock_market,
                    sig.stock_symbol,
                    bucket,
                )
                continue
            ok, reason = signal_passes_entry_gate(sig)
            if not ok:
                skipped_gate += 1
                logger.debug(
                    "[模拟盘] 跳过入场 %s:%s reason=%s rank=%.1f",
                    sig.stock_market,
                    sig.stock_symbol,
                    reason,
                    float(sig.rank_score or 0.0),
                )
                continue
            seen.add(key)
            candidates.append(sig)

        if skipped_gate:
            logger.info(
                "[模拟盘] 入场门禁过滤: skipped=%s kept=%s min_rank=%.1f require_news=%s",
                skipped_gate,
                len(candidates),
                float(MIN_RANK_SCORE_FOR_ENTRY),
                bool(REQUIRE_NEWS_CATALYST),
            )

        if not candidates:
            return 0, new_keys, entry_events

        # 批量获取报价
        syms = [(s.stock_symbol, s.stock_market) for s in candidates]
        quotes = self._fetch_quotes_map(syms)

        # 预算各市场可用现金（建仓时按市场子池逐笔扣减）
        market_cash = {m: market_available_cash(db, account, m, alloc) for m in ALL_MARKETS}

        opened = 0
        for sig in candidates:
            if opened >= entry_limit:
                break
            if not _is_trading_time(sig.stock_market):
                continue
            key = (sig.stock_market, sig.stock_symbol)
            quote = quotes.get(key)
            if not quote or not quote_date_is_current(sig.stock_market, quote):
                continue
            current_price = _safe_float(quote.get("current_price"))
            if current_price is None or current_price <= 0:
                continue

            band_ok, band_reason = price_passes_entry_band(
                current_price, sig.entry_low, sig.entry_high
            )
            if not band_ok:
                logger.info(
                    "[模拟盘] 跳过入场 %s:%s reason=%s price=%.4f band=%.4f-%.4f",
                    sig.stock_market,
                    sig.stock_symbol,
                    band_reason,
                    current_price,
                    float(sig.entry_low or 0.0),
                    float(sig.entry_high or 0.0),
                )
                continue
            tape_ok, tape_reason = quote_passes_tape_gate(
                quote, sig.stock_symbol, sig.stock_market
            )
            if not tape_ok:
                logger.info(
                    "[模拟盘] 跳过入场 %s:%s reason=%s change_pct=%s turnover=%s",
                    sig.stock_market,
                    sig.stock_symbol,
                    tape_reason,
                    quote.get("change_pct"),
                    quote.get("turnover"),
                )
                continue

            # 用当前市价入场
            entry_price = current_price
            mkt = sig.stock_market
            if alloc.get(mkt, 0.0) <= 0:
                continue  # 该市场比例为 0，不投入
            bucket = industry_bucket(sig.stock_symbol, sig.stock_name)
            if sector_counts.get(bucket, 0) >= int(MAX_POSITIONS_PER_SECTOR):
                logger.info(
                    "[模拟盘] 跳过入场 %s:%s reason=sector_cap sector=%s",
                    sig.stock_market,
                    sig.stock_symbol,
                    bucket,
                )
                continue
            avail = min(market_cash.get(mkt, 0.0), exposure_room)

            # 仓位管理:按信号强度分配该市场预算(替换原固定 100 股)
            market_budget = account.initial_capital * alloc.get(mkt, 0.0)
            quantity = _compute_quantity(
                rank_score=float(sig.rank_score or 0.0),
                market_budget=market_budget,
                price=entry_price,
                available_cash=avail,
                cost_model=COST_MODEL,
                lot=lot_size(sig.stock_symbol, sig.stock_market),
            )
            if quantity <= 0:
                continue  # 子池额度不足以买入最小一手

            # 含交易成本的实际买入流出
            buy_fill = COST_MODEL.fill("buy", entry_price, quantity)
            buy_outlay = -buy_fill.cash_delta

            # 基于入场价计算止损/止盈
            # 优先用信号的止损/止盈比例，否则用默认 -8%/+15%
            stop_loss = sig.stop_loss
            target_price = sig.target_price
            if stop_loss and sig.entry_low and sig.entry_low > 0:
                # 保留信号的止损比例，映射到实际入场价
                orig_mid = (sig.entry_low + (sig.entry_high or sig.entry_low)) / 2
                if orig_mid > 0:
                    stop_ratio = (stop_loss - orig_mid) / orig_mid
                    target_ratio = ((target_price - orig_mid) / orig_mid) if target_price else 0.15
                    stop_loss = round(entry_price * (1 + stop_ratio), 4)
                    target_price = round(entry_price * (1 + target_ratio), 4) if target_price else None
            stop_loss, target_price = clamp_exit_levels(entry_price, stop_loss, target_price)

            pos = PaperTradingPosition(
                stock_symbol=sig.stock_symbol,
                stock_market=sig.stock_market,
                stock_name=sig.stock_name or "",
                quantity=quantity,
                entry_price=entry_price,
                stop_loss=stop_loss,
                target_price=target_price,
                current_price=current_price,
                highest_price=entry_price,
                unrealized_pnl=0.0,
                status="open",
                signal_run_id=sig.id,
                signal_snapshot_date=sig.snapshot_date or "",
                signal_action=sig.action or "",
                strategy_code=sig.strategy_code or "",
                opened_at=_utc_now(),
                settlement_date=settlement_date(sig.stock_market, _utc_now()),
            )
            db.add(pos)
            account.current_capital -= buy_outlay
            if mkt == "CN":
                consume_cn_proceeds(db, buy_outlay, now=_utc_now())
            market_cash[mkt] = market_cash.get(mkt, 0.0) - buy_outlay
            exposure_room -= buy_outlay
            # A different market's cached limit must also reflect this outlay.
            for other in ALL_MARKETS:
                if other != mkt:
                    market_cash[other] = min(market_cash[other], buying_power(
                        db, account.current_capital, other, now=_utc_now(), all_markets=True))
            open_keys.add((sig.stock_symbol, sig.stock_market))
            new_keys.add((sig.stock_symbol, sig.stock_market))
            filled_bucket = industry_bucket(sig.stock_symbol, sig.stock_name)
            sector_counts[filled_bucket] = sector_counts.get(filled_bucket, 0) + 1
            entry_events.append((pos, sig))
            opened += 1
            logger.info(
                "[模拟盘] 建仓: %s %s @ %.2f x%d, 止损=%.2f, 止盈=%s, 买入费=%.2f, 策略=%s",
                sig.stock_name or sig.stock_symbol,
                sig.stock_market,
                entry_price,
                quantity,
                stop_loss or 0,
                target_price or "无",
                buy_fill.explicit_fees + buy_fill.slippage_cost,
                sig.strategy_code,
            )

        if opened > 0:
            db.commit()
        return opened, new_keys, entry_events

    def _close_position(
        self,
        db: Session,
        account: PaperTradingAccount,
        pos: PaperTradingPosition,
        exit_price: float,
        exit_reason: str,
    ) -> PaperTradingTrade:
        """平仓单个持仓，返回交易记录。"""
        now = _utc_now()
        block = sell_block_reason(pos, now=now)
        if block:
            raise ValueError(block)
        # 含交易成本的净盈亏:卖出净回收 − 建仓含费投入(与建仓口径一致,资金守恒)
        buy_cost = -COST_MODEL.fill("buy", pos.entry_price, pos.quantity).cash_delta
        sell_fill = COST_MODEL.fill("sell", exit_price, pos.quantity)
        sell_proceeds = sell_fill.cash_delta
        pnl = round(sell_proceeds - buy_cost, 4)
        pnl_pct = (pnl / buy_cost * 100) if buy_cost > 0 else 0.0

        holding_days = 0
        if pos.opened_at:
            opened = pos.opened_at
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
            holding_days = max(0, (now - opened).days)

        trade = PaperTradingTrade(
            stock_symbol=pos.stock_symbol,
            stock_market=pos.stock_market,
            stock_name=pos.stock_name or "",
            quantity=pos.quantity,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            pnl=pnl,
            pnl_pct=round(pnl_pct, 2),
            exit_reason=exit_reason,
            signal_run_id=pos.signal_run_id,
            signal_snapshot_date=pos.signal_snapshot_date or "",
            strategy_code=pos.strategy_code or "",
            holding_days=holding_days,
            opened_at=pos.opened_at,
            closed_at=now,
        )
        db.add(trade)

        pos.status = "closed"
        pos.closed_at = now
        pos.current_price = exit_price
        pos.unrealized_pnl = pnl

        # 回收资金(卖出净回收,已扣卖出费)
        account.current_capital += sell_proceeds
        record_sale(db, trade, sell_proceeds)
        account.total_pnl += pnl
        account.total_trades += 1
        if pnl > 0:
            account.winning_trades += 1

        logger.info(
            "[模拟盘] 平仓: %s %s @ %.2f, 盈亏=%.2f (%.2f%%), 原因=%s",
            pos.stock_name or pos.stock_symbol,
            pos.stock_market,
            exit_price,
            pnl,
            pnl_pct,
            exit_reason,
        )
        return trade

    def _check_exits(
        self, db: Session, account: PaperTradingAccount, skip_keys: set[tuple[str, str]] | None = None,
    ) -> tuple[int, list[tuple[PaperTradingPosition, PaperTradingTrade]]]:
        """检查持仓止损/止盈/信号反转，自动平仓。skip_keys 中的股票跳过（本轮新建仓）。"""
        exit_events: list[tuple[PaperTradingPosition, PaperTradingTrade]] = []
        active_markets = [market for market in ALL_MARKETS if _is_trading_time(market)]
        if not active_markets:
            return 0, exit_events
        positions = (
            db.query(PaperTradingPosition)
            .filter(PaperTradingPosition.status == "open", PaperTradingPosition.stock_market.in_(active_markets))
            .all()
        )
        if not positions:
            return 0, exit_events

        # 批量获取报价
        syms = [(p.stock_symbol, p.stock_market) for p in positions]
        quotes = self._fetch_quotes_map(syms)

        closed = 0
        for pos in positions:
            if not _is_trading_time(pos.stock_market):
                continue
            # 跳过本轮刚建仓的持仓
            if skip_keys and (pos.stock_symbol, pos.stock_market) in skip_keys:
                continue
            key = (pos.stock_market, pos.stock_symbol)
            quote = quotes.get(key)
            if not quote or not quote_date_is_current(pos.stock_market, quote):
                continue
            current_price = _safe_float(quote.get("current_price"))

            if current_price is None or current_price <= 0:
                continue

            # 更新现价、净浮动盈亏(含若此刻平仓的双边成本)、持仓期最高价
            pos.current_price = current_price
            _buy_cost_u = -COST_MODEL.fill("buy", pos.entry_price, pos.quantity).cash_delta
            _sell_u = COST_MODEL.fill("sell", current_price, pos.quantity).cash_delta
            pos.unrealized_pnl = round(_sell_u - _buy_cost_u, 4)
            if pos.highest_price is None or current_price > pos.highest_price:
                pos.highest_price = current_price

            # Mark-to-market continues while today's A-share purchase is locked.
            # Every automatic exit shares this guard, including reversal/time exits.
            if sell_block_reason(pos):
                continue

            # 检查止损
            if pos.stop_loss and current_price <= pos.stop_loss:
                trade = self._close_position(db, account, pos, current_price, "stop_loss")
                exit_events.append((pos, trade))
                closed += 1
                continue

            # 检查止盈
            if pos.target_price and current_price >= pos.target_price:
                trade = self._close_position(db, account, pos, current_price, "target_price")
                exit_events.append((pos, trade))
                closed += 1
                continue

            # 移动止损:浮盈达标后,从持仓最高价回撤超阈值则离场(离场线不低于买入价)
            trail_line = trailing_stop_line(pos.entry_price, pos.highest_price or 0.0)
            if trail_line is not None and current_price <= trail_line:
                trade = self._close_position(db, account, pos, current_price, "trailing_stop")
                exit_events.append((pos, trade))
                closed += 1
                continue

            # 检查信号反转:取该股最新快照的信号,不按状态过滤
            if pos.signal_run_id:
                # no_autoflush: 信号查询是只读的,不要把本轮累积的持仓现价更新提前 flush——
                # 否则扫描中途会反复抢 SQLite 写锁,与其它调度器并发写时触发 "database is locked"。
                # 所有写入统一在本方法末尾 db.commit() 时一次性落盘。
                with db.no_autoflush:
                    latest = (
                        db.query(StrategySignalRun)
                        .filter(
                            StrategySignalRun.stock_symbol == pos.stock_symbol,
                            StrategySignalRun.stock_market == pos.stock_market,
                        )
                        .order_by(
                            StrategySignalRun.snapshot_date.desc(),
                            StrategySignalRun.updated_at.desc(),
                        )
                        .first()
                    )
                if latest and (latest.action or "") in REVERSAL_ACTIONS:
                    trade = self._close_position(db, account, pos, current_price, "signal_reversal")
                    exit_events.append((pos, trade))
                    closed += 1
                    continue

            # 时间止损:持有超过最大交易日离场(信号天数与机会盘上限取较小值)
            max_days = DEFAULT_TIME_STOP_DAYS
            if pos.signal_run_id:
                sig_hold = (
                    db.query(StrategySignalRun.holding_days)
                    .filter(StrategySignalRun.id == pos.signal_run_id)
                    .scalar()
                )
                if sig_hold and int(sig_hold) > 0:
                    max_days = min(int(sig_hold), int(DEFAULT_TIME_STOP_DAYS))
            if pos.opened_at:
                if trading_days_held(pos.stock_market, pos.opened_at, _utc_now()) >= max_days:
                    trade = self._close_position(db, account, pos, current_price, "time_stop")
                    exit_events.append((pos, trade))
                    closed += 1
                    continue

        self._update_account_metrics(db, account)
        db.commit()
        return closed, exit_events

    def _update_account_metrics(self, db: Session, account: PaperTradingAccount) -> None:
        """更新账户峰值和最大回撤。"""
        # 计算包含浮动盈亏的总资产
        open_positions = (
            db.query(PaperTradingPosition)
            .filter(PaperTradingPosition.status == "open")
            .all()
        )
        unrealized_total = sum(p.unrealized_pnl or 0 for p in open_positions)
        total_equity = account.current_capital + sum(
            (p.current_price or p.entry_price) * p.quantity for p in open_positions
        )

        if total_equity > account.peak_capital:
            account.peak_capital = total_equity

        if account.peak_capital > 0:
            drawdown = (account.peak_capital - total_equity) / account.peak_capital * 100
            if drawdown > account.max_drawdown_pct:
                account.max_drawdown_pct = round(drawdown, 2)

    @_account_mutation
    def _scan_sync(self) -> dict:
        """同步扫描（在线程中执行）。"""
        db = SessionLocal()
        try:
            account = self._get_or_create_account(db)
            settled = settle_due(db, now=_utc_now())
            db.commit()
            if not account.enabled:
                return {"status": "disabled", "settled": settled}

            opened, new_keys, entry_events = self._check_entries(db, account)
            closed, exit_events = self._check_exits(db, account, skip_keys=new_keys)

            # 在 db.close() 前将 ORM 对象序列化为 dict，避免 detached 问题
            serialized_entries = [
                {"pos_data": _serialize_position(pos), "sig_data": _serialize_signal(sig) if sig else None}
                for pos, sig in entry_events
            ]
            serialized_exits = [
                {"pos_data": _serialize_position(pos), "trade_data": _serialize_trade(trade)}
                for pos, trade in exit_events
            ]

            return {
                "status": "ok",
                "opened": opened,
                "closed": closed,
                "settled": settled,
                "entry_events": serialized_entries,
                "exit_events": serialized_exits,
            }
        except Exception as e:
            logger.exception(f"[模拟盘] 扫描异常: {e}")
            return {"status": "error", "error": str(e)}
        finally:
            db.close()

    async def scan_once(self) -> dict:
        """异步扫描入口。"""
        result = await asyncio.to_thread(self._scan_sync)
        # 发送通知（异步，失败不影响交易）
        await self._send_notifications(result)
        return result

    @_account_mutation
    def close_position_manual(self, position_id: int) -> dict:
        """手动平仓。"""
        db = SessionLocal()
        try:
            account = self._get_or_create_account(db)
            pos = (
                db.query(PaperTradingPosition)
                .filter(
                    PaperTradingPosition.id == position_id,
                    PaperTradingPosition.status == "open",
                )
                .first()
            )
            if not pos:
                return {"ok": False, "error": "持仓不存在或已平仓"}

            block = sell_block_reason(pos)
            if block:
                return {"ok": False, "error_code": block, "error": SELL_BLOCK_MESSAGES[block]}

            if not _is_trading_time(pos.stock_market):
                return {"ok": False, "error": "该市场当前非交易时段，无法成交"}

            # 获取最新报价(走 flag 门控的 md_quote_rows,支持故障转移)
            mc = _to_market(pos.stock_market)
            rows = md_quote_rows([pos.stock_symbol], mc.value)

            exit_price = _safe_float(rows[0].get("current_price")) if rows else None
            if not exit_price or exit_price <= 0 or not quote_date_is_current(pos.stock_market, rows[0]):
                return {"ok": False, "error": "暂无有效报价，无法成交"}
            if not _is_trading_time(pos.stock_market):
                return {"ok": False, "error": "该市场当前非交易时段，无法成交"}

            trade = self._close_position(db, account, pos, exit_price, "manual")
            settle_due(db, now=_utc_now())
            self._update_account_metrics(db, account)
            db.commit()
            # 序列化后返回，避免 db.close() 后 ORM 对象 detached
            return {
                "ok": True,
                "pos_data": _serialize_position(pos),
                "trade_data": _serialize_trade(trade),
            }
        finally:
            db.close()

    async def close_position_manual_async(self, position_id: int) -> dict:
        """异步手动平仓，含通知。"""
        result = await asyncio.to_thread(self.close_position_manual, position_id)
        if result.get("ok"):
            try:
                from src.modules.paper_trading.paper_trading_notifier import notify_exit
                pos_data = result.pop("pos_data", None)
                trade_data = result.pop("trade_data", None)
                if pos_data and trade_data:
                    await notify_exit(pos_data, trade_data)
            except Exception:
                logger.exception("[模拟盘] 手动平仓通知失败")
        return result

    async def _send_notifications(self, result: dict) -> None:
        """从扫描结果中取出序列化事件，发送通知。"""
        try:
            from src.modules.paper_trading.paper_trading_notifier import notify_entry, notify_exit

            for evt in result.pop("entry_events", []):
                await notify_entry(evt["pos_data"], evt.get("sig_data"))
            for evt in result.pop("exit_events", []):
                await notify_exit(evt["pos_data"], evt["trade_data"])
        except Exception:
            logger.exception("[模拟盘] 通知发送失败")

    @_account_mutation
    def reset_account(self) -> dict:
        """重置模拟盘（清空所有数据）。"""
        db = SessionLocal()
        try:
            db.query(PaperTradingSettlement).delete()
            db.query(PaperTradingPosition).delete()
            db.query(PaperTradingTrade).delete()
            account = db.query(PaperTradingAccount).first()
            if account:
                account.current_capital = account.initial_capital
                account.total_pnl = 0.0
                account.total_trades = 0
                account.winning_trades = 0
                account.max_drawdown_pct = 0.0
                account.peak_capital = account.initial_capital
                account.enabled = True
            db.commit()
            return {"ok": True}
        finally:
            db.close()


ENGINE = PaperTradingEngine()
