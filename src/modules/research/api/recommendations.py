"""推荐相关 API（入场候选榜）。"""

import logging
import threading

from fastapi import APIRouter, Query

from src.modules.strategy.entry_candidates import (
    evaluate_entry_candidate_outcomes,
    get_entry_candidate_stats,
    list_entry_candidates,
    refresh_entry_candidates,
)
from src.modules.strategy.strategy_catalog import list_strategy_catalog
from src.modules.strategy.strategy_engine import (
    evaluate_strategy_outcomes,
    get_strategy_factor_snapshot,
    get_strategy_stats,
    list_market_regime_snapshots,
    list_portfolio_risk_snapshots,
    list_strategy_signals,
    list_strategy_weight_history,
    rebalance_strategy_weights,
    refresh_strategy_signals,
)
from src.modules.strategy.factor_eval import evaluate_factor_ic
from src.modules.strategy import refresh_state
from src.modules.research.signal_explain import enrich_signal
from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import StrategySignalRun

router = APIRouter()
logger = logging.getLogger(__name__)

# 刷新互斥与状态统一由 strategy.refresh_state 维护（定时任务、手动刷新、启动补跑共用），
# 避免两处各自持一份"running"导致同一分钟双重刷新、争抢同一批 snapshot 行。


def _latest_strategy_snapshot() -> str:
    db = SessionLocal()
    try:
        row = (
            db.query(StrategySignalRun.snapshot_date)
            .order_by(StrategySignalRun.snapshot_date.desc())
            .first()
        )
        return row[0] if row else ""
    finally:
        db.close()


def _refresh_worker(
    *,
    snapshot_date: str,
    rebuild_candidates: bool,
    max_inputs: int,
    market_scan_limit: int,
    max_kline_symbols: int,
    limit_candidates: int,
):
    try:
        result = refresh_strategy_signals(
            snapshot_date=snapshot_date,
            rebuild_candidates=rebuild_candidates,
            max_inputs=max_inputs,
            market_scan_limit=market_scan_limit,
            max_kline_symbols=max_kline_symbols,
            limit_candidates=limit_candidates,
        )
        refresh_state.finish(
            snapshot_date=result.get("snapshot_date") or "",
            count=int(result.get("count") or 0),
        )
    except Exception as e:
        logger.exception("后台刷新策略信号失败: %s", e)
        refresh_state.finish(error=str(e))


def _start_refresh_job(**kwargs) -> tuple[bool, dict]:
    started, state = refresh_state.try_begin(source="api")
    if not started:
        return False, state
    thread = threading.Thread(
        target=_refresh_worker,
        kwargs=kwargs,
        daemon=True,
        name="strategy-signals-refresh",
    )
    thread.start()
    return True, refresh_state.snapshot()




def _run_evaluation_locked(fn, *, label: str, **kwargs) -> dict:
    """后验评估的互斥包装。

    API 手动触发与夜间定时任务此前可以并行：两边各自查"已评估"集合都不含对方
    刚插入的行，于是同时 INSERT 撞唯一约束 → 整批 rollback，统计接口读到半成品。
    这里与定时任务共用 ``refresh_state`` 的 evaluate 锁。
    """
    started, state = refresh_state.try_begin(source="api", name="evaluate")
    if not started:
        return {
            "queued": False,
            "running": True,
            "skipped": True,
            "message": f"{label}正在执行中，本次跳过",
            "state": state,
        }
    try:
        result = fn(**kwargs)
        evaluated = int((result or {}).get("evaluated") or 0)
        refresh_state.finish(count=evaluated, name="evaluate")
        return result
    except Exception as e:  # noqa: BLE001 - 记录后原样抛出，保留调用方 500 语义
        refresh_state.finish(error=str(e), name="evaluate")
        raise


def _reject_if_refreshing() -> dict | None:
    """同步刷新入口的前置检查：已有刷新在跑就不再开一条。

    ``POST /entry-candidates/refresh`` 和 ``GET /entry-candidates?refresh=true``
    原先完全绕开锁，会和定时任务/后台刷新同时拉一遍全市场行情、争抢同一批
    snapshot 行（SQLite 写锁）。现在这两个入口也走同一个 refresh 锁。
    """
    if refresh_state.is_running():
        return {
            "queued": False,
            "running": True,
            "skipped": True,
            "message": "已有机会刷新在执行中，本次跳过",
            "state": refresh_state.snapshot(),
        }
    return None


@router.get("/entry-candidates")
def get_entry_candidates(
    market: str = Query("", description="市场代码: CN/HK/US"),
    status: str = Query("active", description="状态: active/inactive/all"),
    min_score: float = Query(0, ge=0, le=100),
    limit: int = Query(20, ge=1, le=500),
    refresh: bool = Query(False, description="是否先刷新候选再返回"),
    snapshot_date: str = Query("", description="快照日期 YYYY-MM-DD，默认最新"),
    source: str = Query("", description="来源: market_scan/watchlist/mixed/all"),
    holding: str = Query("", description="持仓过滤: held/unheld/all"),
    strategy: str = Query("", description="策略标签过滤"),
):
    if refresh:
        skipped = _reject_if_refreshing()
        if skipped is not None:
            payload = list_entry_candidates(
                market=market,
                status=status,
                min_score=min_score,
                limit=limit,
                snapshot_date=snapshot_date,
                source=source,
                holding=holding,
                strategy=strategy,
            )
            payload["refresh_skipped"] = True
            payload["refresh_message"] = skipped["message"]
            return payload
        started, _state = refresh_state.try_begin(source="api-sync")
        if started:
            try:
                refresh_entry_candidates()
                refresh_state.finish(snapshot_date="", count=0)
            except Exception as e:  # noqa: BLE001
                refresh_state.finish(error=str(e))
                raise
    return list_entry_candidates(
        market=market,
        status=status,
        min_score=min_score,
        limit=limit,
        snapshot_date=snapshot_date,
        source=source,
        holding=holding,
        strategy=strategy,
    )


@router.post("/entry-candidates/refresh")
def refresh_candidates(
    max_inputs: int = Query(300, ge=10, le=1000),
    market_scan_limit: int = Query(60, ge=20, le=300),
):
    skipped = _reject_if_refreshing()
    if skipped is not None:
        return skipped
    started, _state = refresh_state.try_begin(source="api-sync")
    if not started:
        return {
            "queued": False,
            "running": True,
            "skipped": True,
            "message": "已有机会刷新在执行中，本次跳过",
            "state": refresh_state.snapshot(),
        }
    try:
        cand = refresh_entry_candidates(
            max_inputs=max_inputs,
            market_scan_limit=market_scan_limit,
        )
        # 同步刷新策略信号层，保持前端机会页一致。
        refresh_strategy_signals(
            snapshot_date=cand.get("snapshot_date", ""),
            rebuild_candidates=False,
        )
    except Exception as e:  # noqa: BLE001
        refresh_state.finish(error=str(e))
        raise
    refresh_state.finish(
        snapshot_date=str(cand.get("snapshot_date") or ""),
        count=int(cand.get("count") or 0),
    )
    return cand




@router.get("/entry-candidates/stats")
def candidate_stats(days: int = Query(30, ge=1, le=365)):
    return get_entry_candidate_stats(days=days)


@router.post("/entry-candidates/outcomes/evaluate")
def evaluate_candidate_outcomes(
    limit: int = Query(400, ge=20, le=2000),
    snapshot_days: int = Query(45, ge=7, le=365),
):
    return _run_evaluation_locked(
        evaluate_entry_candidate_outcomes,
        label="候选后验评估",
        horizons=(1, 3, 5, 10),
        snapshot_days=snapshot_days,
        limit=limit,
    )


@router.get("/strategy-catalog")
def get_strategy_catalog(enabled_only: bool = Query(True, description="仅返回启用策略")):
    return {"items": list_strategy_catalog(enabled_only=enabled_only)}


@router.get("/strategy-signals")
def get_strategy_signal_list(
    market: str = Query("", description="市场代码: CN/HK/US"),
    status: str = Query("all", description="状态: active/inactive/all"),
    min_score: float = Query(0, ge=0, le=100),
    limit: int = Query(50, ge=1, le=500),
    snapshot_date: str = Query("", description="快照日期 YYYY-MM-DD，默认最新"),
    source_pool: str = Query("", description="来源池: market_scan/watchlist/mixed/all"),
    holding: str = Query("", description="持仓过滤: held/unheld/all"),
    strategy_code: str = Query("", description="策略代码"),
    risk_level: str = Query("", description="风险等级: low/medium/high/all"),
    include_payload: bool = Query(False, description="是否返回完整 payload（默认否，提升性能）"),
):
    result = list_strategy_signals(
        market=market,
        status=status,
        min_score=min_score,
        limit=limit,
        snapshot_date=snapshot_date,
        source_pool=source_pool,
        holding=holding,
        strategy_code=strategy_code,
        risk_level=risk_level,
        include_payload=include_payload,
    )
    # Phase 3: 注入 1-10 可解释评分 + 正负因子拆解
    for _it in result.get("items", []):
        enrich_signal(_it)
    return result


@router.get("/strategy-regimes")
def get_strategy_regimes(
    snapshot_date: str = Query("", description="快照日期 YYYY-MM-DD"),
    market: str = Query("", description="市场过滤: CN/HK/US"),
    limit: int = Query(100, ge=1, le=1000),
):
    return list_market_regime_snapshots(
        snapshot_date=snapshot_date,
        market=market,
        limit=limit,
    )


@router.get("/strategy-risk-snapshots")
def get_strategy_risk_snapshots(
    snapshot_date: str = Query("", description="快照日期 YYYY-MM-DD"),
    market: str = Query("", description="市场过滤: CN/HK/US"),
    limit: int = Query(100, ge=1, le=1000),
):
    return list_portfolio_risk_snapshots(
        snapshot_date=snapshot_date,
        market=market,
        limit=limit,
    )


@router.get("/strategy-factors/{signal_run_id}")
def get_strategy_factor(signal_run_id: int):
    return get_strategy_factor_snapshot(signal_run_id)


@router.post("/strategy-signals/refresh")
def refresh_strategy_signal_list(
    rebuild_candidates: bool = Query(True, description="是否先重算候选池"),
    snapshot_date: str = Query("", description="指定快照日期，不传则用最新"),
    max_inputs: int = Query(500, ge=20, le=2000),
    market_scan_limit: int = Query(80, ge=20, le=500),
    max_kline_symbols: int = Query(72, ge=0, le=300),
    limit_candidates: int = Query(2000, ge=50, le=10000),
    wait: bool = Query(False, description="是否同步等待刷新完成（默认后台执行）"),
):
    if wait:
        return refresh_strategy_signals(
            snapshot_date=snapshot_date,
            rebuild_candidates=rebuild_candidates,
            max_inputs=max_inputs,
            market_scan_limit=market_scan_limit,
            max_kline_symbols=max_kline_symbols,
            limit_candidates=limit_candidates,
        )

    started, state = _start_refresh_job(
        snapshot_date=snapshot_date,
        rebuild_candidates=rebuild_candidates,
        max_inputs=max_inputs,
        market_scan_limit=market_scan_limit,
        max_kline_symbols=max_kline_symbols,
        limit_candidates=limit_candidates,
    )
    latest_snapshot = _latest_strategy_snapshot()
    return {
        "queued": True,
        "running": True,
        "accepted": bool(started),
        "message": "已提交后台执行" if started else "刷新任务已在执行中",
        "snapshot_date": latest_snapshot or state.get("last_snapshot_date") or "",
        "count": 0,
        "items": [],
    }


@router.get("/strategy-signals/refresh-status")
def strategy_signal_refresh_status():
    state = refresh_state.snapshot()
    latest_snapshot = _latest_strategy_snapshot()
    return {
        "running": bool(state.get("running")),
        "source": state.get("source") or "",
        "started_at": state.get("started_at") or "",
        "finished_at": state.get("finished_at") or "",
        "last_error": state.get("last_error") or "",
        "last_snapshot_date": latest_snapshot or state.get("last_snapshot_date") or "",
        "last_count": int(state.get("last_count") or 0),
        "runs": int(state.get("runs") or 0),
    }


@router.post("/strategy-signals/outcomes/evaluate")
def evaluate_strategy_signal_outcomes(
    limit: int = Query(800, ge=20, le=5000),
    snapshot_days: int = Query(60, ge=7, le=365),
):
    return _run_evaluation_locked(
        evaluate_strategy_outcomes,
        label="策略后验评估",
        horizons=(1, 3, 5, 10),
        snapshot_days=snapshot_days,
        limit=limit,
    )


@router.post("/strategy-weights/rebalance")
def rebalance_strategy_weights_api(
    window_days: int = Query(45, ge=7, le=365),
    min_samples: int = Query(8, ge=3, le=500),
    alpha: float = Query(0.35, ge=0.05, le=0.95),
):
    return rebalance_strategy_weights(
        window_days=window_days,
        min_samples=min_samples,
        alpha=alpha,
        regime="default",
    )


@router.get("/strategy-stats")
def strategy_stats(days: int = Query(45, ge=1, le=365)):
    return get_strategy_stats(days=days)


@router.get("/strategy-factor-ic")
def strategy_factor_ic(
    days: int = Query(90, ge=7, le=365, description="回看快照天数"),
    horizon: int = Query(5, ge=1, le=60, description="持有期(交易日)"),
):
    """各因子的 IC/IR 有效性评估(StrategyFactorSnapshot × StrategyOutcome)。"""
    return evaluate_factor_ic(days=days, horizon=horizon)


@router.get("/strategy-weight-history")
def strategy_weight_history(
    strategy_code: str = Query("", description="策略代码过滤"),
    market: str = Query("", description="市场过滤"),
    limit: int = Query(200, ge=1, le=2000),
):
    return list_strategy_weight_history(
        strategy_code=strategy_code,
        market=market,
        limit=limit,
    )
