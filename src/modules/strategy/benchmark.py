"""同期基准收益：把"信号自己赚了多少"换算成"比基准多赚多少"。

为什么需要这个模块：``strategy_outcomes.outcome_return_pct`` 单独看没有意义。
大盘涨 3% 时一条 +2% 的信号其实是跑输的，而一条「+1% 但大盘跌 2%」的信号
才是真正的选股能力。没有基准对照，所有"胜率""平均收益"都无法回答
「这套打分到底有没有 alpha」。

基准标的选 510300（沪深300ETF）而不是 000300 指数：实测东财对 000300 直接
断连、同花顺返回 404，指数日线取不到；510300 可稳定拉取，且它是实物 ETF、
有真实成交价，跟踪误差千分级，用来判断"有没有跑赢大盘"足够。另需注意
``000001`` 在 K 线接口里是平安银行而不是上证指数，不能拿来当基准。

口径（与 ``plan_outcome.evaluate_plan_path`` 的持仓区间对齐）：
- 起点 = snapshot_date 之后第一根已完成日线的**开盘价**（T 日出信号、T+1 开盘买入）
- 终点 = 第 horizon 根日线的**收盘价**
- 未到期（bar 不足 horizon 根）返回 None，不写库
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from sqlalchemy.orm import Session

from src.platform.marketdata.collectors.kline_collector import KlineCollector
from src.platform.marketdata.models import MarketCode
from src.platform.marketdata.outcome_prices import forward_bars
from src.platform.persistence.models import StrategyBenchmark
from src.platform.scheduling.timezone import utc_now

logger = logging.getLogger(__name__)

# 按优先级排列：第一个能拉到数据的就用作基准。
BENCHMARK_CANDIDATES: dict[str, tuple[tuple[str, str], ...]] = {
    "CN": (
        ("510300", "沪深300ETF"),
        ("510500", "中证500ETF"),
        ("510050", "上证50ETF"),
    ),
}
DEFAULT_BENCHMARK = "510300"


def _to_market(value: str | None) -> MarketCode:
    try:
        return MarketCode((value or "CN").strip().upper())
    except Exception:
        return MarketCode.CN


def _parse_day(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10].replace("/", "-"))
    except ValueError:
        return None


def benchmark_symbol(market: str = "CN") -> str:
    """该市场使用的基准标的代码（仅用于展示/记录，不保证能拉到数据）。"""
    candidates = BENCHMARK_CANDIDATES.get((market or "CN").upper())
    return candidates[0][0] if candidates else DEFAULT_BENCHMARK


def fetch_benchmark_klines(
    market: str = "CN", *, days: int = 160
) -> tuple[str, list, str]:
    """按优先级拉基准日线，返回 ``(symbol, klines, name)``。

    全部候选都拉不到时返回 ``("", [], "")``——调用方据此跳过基准回填，
    而不是把基准收益记成 0（记 0 会让"超额"等于原始收益，等于假装有 alpha）。
    """
    code = _to_market(market)
    for symbol, name in BENCHMARK_CANDIDATES.get((market or "CN").upper(), ()):
        try:
            rows = KlineCollector(code).get_klines(symbol, days=days)
        except Exception as exc:  # pragma: no cover - 网络/数据源异常
            logger.warning("[基准] %s(%s) 拉取失败: %s", symbol, name, exc)
            continue
        if rows:
            return symbol, rows, name
    return "", [], ""


def compute_benchmark_return(
    klines: list, snapshot_day: date, horizon: int, market: str = "CN"
) -> dict | None:
    """算同期基准收益；bar 不足 horizon 根（未到期）返回 None。"""
    bars = forward_bars(klines, snapshot_day, horizon, _to_market(market))
    if not bars:
        return None
    try:
        base = float(bars[0]["open"])
        end = float(bars[-1]["close"])
    except (KeyError, TypeError, ValueError):
        return None
    if base <= 0 or end <= 0:
        return None
    return {
        "base_price": round(base, 4),
        "end_price": round(end, 4),
        "return_pct": round((end / base - 1.0) * 100.0, 4),
        "target_date": bars[-1]["date"].strftime("%Y-%m-%d"),
        "bars": len(bars),
    }


def compute_excess(stock_return_pct, benchmark_return_pct) -> float | None:
    """超额收益 = 信号收益 - 基准收益。任一缺失则返回 None（不猜）。"""
    if stock_return_pct is None or benchmark_return_pct is None:
        return None
    try:
        return round(float(stock_return_pct) - float(benchmark_return_pct), 4)
    except (TypeError, ValueError):
        return None


def load_benchmark_returns(
    db: Session,
    keys: set[tuple[str, int]],
    *,
    market: str = "CN",
    persist: bool = True,
    sample_sizes: dict[tuple[str, int], int] | None = None,
) -> dict[tuple[str, int], dict]:
    """批量取 (snapshot_date, horizon) → 基准收益，缺的现算并落库。

    同一批评估只拉一次基准 K 线。已有记录直接复用（基准不会随时间变化），
    避免每轮评估都为同一日期重复联网。

    返回的 dict 只包含**已到期**的组合；未到期的键不出现，调用方按缺失处理。
    """
    out: dict[tuple[str, int], dict] = {}
    wanted = {(str(d).strip(), int(h)) for d, h in keys if str(d or "").strip() and int(h) > 0}
    if not wanted:
        return out

    normalized_market = (market or "CN").strip().upper()
    existing = (
        db.query(StrategyBenchmark)
        .filter(
            StrategyBenchmark.stock_market == normalized_market,
            StrategyBenchmark.snapshot_date.in_({d for d, _ in wanted}),
        )
        .all()
    )
    for row in existing:
        key = (row.snapshot_date, int(row.horizon_days))
        if key in wanted and row.benchmark_return_pct is not None:
            out[key] = {
                "base_price": row.benchmark_base_price,
                "end_price": row.benchmark_end_price,
                "return_pct": row.benchmark_return_pct,
                "target_date": row.target_date or "",
                "symbol": row.benchmark_symbol or "",
            }

    missing = sorted(wanted - set(out))
    if not missing:
        return out

    symbol, klines, name = fetch_benchmark_klines(normalized_market)
    if not klines:
        logger.warning("[基准] 市场 %s 无可用基准标的，跳过基准回填", normalized_market)
        return out

    for day_str, horizon in missing:
        snap_day = _parse_day(day_str)
        if snap_day is None:
            continue
        result = compute_benchmark_return(klines, snap_day, horizon, normalized_market)
        if result is None:
            continue  # 未到期
        result["symbol"] = symbol
        out[(day_str, horizon)] = result
        if not persist:
            continue
        db.add(
            StrategyBenchmark(
                snapshot_date=day_str,
                stock_market=normalized_market,
                horizon_days=horizon,
                benchmark_symbol=symbol,
                benchmark_base_price=result["base_price"],
                benchmark_end_price=result["end_price"],
                benchmark_return_pct=result["return_pct"],
                target_date=result["target_date"],
                sample_size=int((sample_sizes or {}).get((day_str, horizon), 0)),
                meta={"benchmark_name": name, "caliber": "t1_open_to_horizon_close"},
                computed_at=utc_now(),
            )
        )
    if persist:
        try:
            db.commit()
        except Exception as exc:  # pragma: no cover - 并发唯一键冲突
            db.rollback()
            logger.warning("[基准] 落库失败（可能并发写入）: %s", exc)
    return out
