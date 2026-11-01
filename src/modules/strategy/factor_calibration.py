"""因子自校准(M2):把孤立的 IC/IR 接进因子权重的轻量标定闭环。

把 `factor_eval.evaluate_factor_ic` 算出的每因子 IC/IR,符号感知地转成目标权重,
EMA 平滑 + clamp 后写入 `FactorWeight`(并审计到 `FactorWeightHistory`)。
镜像 `strategy_engine.rebalance_strategy_weights` 的机制,但作用于因子级。

设计要点(见 .docs/factor-self-calibration-design-2026-06-20.md):
- IC 必须测在「原始因子」上(快照存 raw,权重只在合成时乘),否则闭环自我强化失真。
- 惩罚因子(risk/crowd)IC 预期为负:用 −IC 驱动,惩罚有效→提权,失效→降权。
- 只吃「持有期已走完」的 outcome(point-in-time),防未来函数。
"""

from __future__ import annotations

import logging

from src.modules.strategy.factor_eval import evaluate_factor_ic
from src.modules.strategy.factor_weights import (
    CALIBRATABLE_FACTORS,
    MARKETS,
    PENALTY_FACTORS,
    get_factor_weights,
)
from src.modules.strategy.opportunity_markets import OPPORTUNITY_MARKETS
from src.platform.scheduling.timezone import utc_now
from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import FactorWeight, FactorWeightHistory

logger = logging.getLogger(__name__)

# 归一化基准:一个「不错」的 IR / 一个「有意义」的单期 IC。
IR_REF = 0.5
IC_REF = 0.05
# |IC| 绝对闸门:低于该值视为「没有预测力」,无论 IR 多稳定都不提权。
# 只看 IR 会踩的坑:某因子每天 IC 都是 0.001、std 极小 → IR 可以到 10,
# 于是被顶到最高权重 1.4。那是「稳定地无效」,不是 alpha。
IC_MIN_ABS = 0.015


def compute_target(factor_code: str, ic, ir, *, beta: float = 0.4) -> float | None:
    """由 IC/IR 算目标权重;优先 IR(更稳),fallback IC;惩罚因子翻符号。

    返回 None 表示信息不足或没有预测力(|IC| 未过闸 / IC、IR 均缺失),应跳过该因子。
    term 归一化并 clamp 到 [-1, 1];target = 1 + beta·term。
    """
    # 先过 |IC| 闸:没有绝对预测力时,IR 再漂亮也只是「稳定地无效」。
    ic_abs = abs(float(ic)) if ic is not None else None
    if ic_abs is None or ic_abs < IC_MIN_ABS:
        return None
    if ir is not None:
        term = ir / IR_REF
    else:
        term = float(ic) / IC_REF
    if factor_code in PENALTY_FACTORS:
        term = -term  # 惩罚因子:IC 越负越该信
    term = max(-1.0, min(1.0, term))
    return 1.0 * (1.0 + beta * term)


def blend(old: float, target: float, *, alpha: float = 0.35,
          lo: float = 0.5, hi: float = 1.5) -> float:
    """EMA 平滑(防跳变)+ clamp 到 [lo, hi]。"""
    new = old * (1.0 - alpha) + target * alpha
    return max(lo, min(hi, new))


def calibrate_factor_weights(
    market: str, *, alpha: float = 0.35, beta: float = 0.4,
    clamp: tuple[float, float] = (0.5, 1.5),
    min_samples: int = 20, horizon: int = 5, days: int = 90, db=None,
) -> dict:
    """对单个市场跑一轮因子权重标定,写 FactorWeight + FactorWeightHistory。

    门控:is_pinned / auto_calibrate=False / 样本不足 / IC 缺失 → 跳过(不改权重)。
    每次都把最近观测(last_ic/ir/sample_size)写入 FactorWeight.meta 供 API 展示;
    History 只记录「实际发生的调整」(reason=auto),避免冷启动期审计噪声。
    """
    own = db is None
    db = db or SessionLocal()
    try:
        ic_result = evaluate_factor_ic(
            days=days, horizon=horizon, min_samples=min_samples, market=market, db=db
        )
        factors = ic_result.get("factors", {})
        get_factor_weights(market, db=db)  # 确保 5 个因子行存在

        lo, hi = float(clamp[0]), float(clamp[1])
        changed = 0
        skipped_no_samples = 0
        skipped_below_ic = 0
        rows_changed: list[dict] = []

        for code in CALIBRATABLE_FACTORS:
            row = (
                db.query(FactorWeight)
                .filter(FactorWeight.factor_code == code, FactorWeight.market == market)
                .first()
            )
            old = float(row.weight)
            stats = factors.get(code, {})
            ic = stats.get("ic")
            ir = stats.get("ir")
            n = int(stats.get("sample_size", 0))

            # 记录最近一次观测(供 API 展示),无论是否调整。
            row.meta = {
                **(row.meta or {}),
                "last_ic": ic, "last_ir": ir, "last_sample_size": n,
                "last_calibrated_at": utc_now().isoformat(),
            }

            if row.is_pinned or not row.auto_calibrate:
                continue
            if n < min_samples or ic is None:
                skipped_no_samples += 1
                continue
            target = compute_target(code, ic, ir, beta=beta)
            if target is None:
                # |IC| 没过闸:没有预测力,保持原权重而不是提权。
                skipped_below_ic += 1
                row.meta = {**(row.meta or {}), "last_skip_reason": "ic_below_floor"}
                continue
            new = round(blend(old, target, alpha=alpha, lo=lo, hi=hi), 4)
            if abs(new - old) < 0.01:
                continue

            row.weight = new
            row.reason = f"auto(ic={ic}, ir={ir}, n={n})"
            row.effective_from = utc_now()
            row.updated_at = utc_now()
            db.add(FactorWeightHistory(
                factor_code=code, market=market, old_weight=old, new_weight=new,
                ic=ic, ir=ir, sample_size=n, reason="auto",
                meta={"target": round(target, 4), "alpha": alpha},
            ))
            changed += 1
            rows_changed.append({
                "factor_code": code, "old_weight": old, "new_weight": new, "sample_size": n,
            })

        db.commit()
        return {"market": market, "checked": len(CALIBRATABLE_FACTORS),
                "changed": changed, "rows": rows_changed,
                "skipped_no_samples": skipped_no_samples,
                "skipped_below_ic": skipped_below_ic}
    except Exception as e:  # pragma: no cover - 防御性
        logger.warning(f"[因子标定] market={market} 失败: {e}")
        db.rollback()
        return {"market": market, "checked": 0, "changed": 0, "rows": [], "error": str(e)}
    finally:
        if own:
            db.close()


def calibrate_all_markets(
    *, db=None, markets: list[str] | None = None, **kwargs
) -> dict[str, dict]:
    """对机会页覆盖的市场各跑一轮因子标定;供调度器每日 outcome 评估后调用。

    只跑 ``OPPORTUNITY_MARKETS``(当前仅 CN)。改造前遍历 CN/HK/US 三个市场,
    但机会链路只产 A 股信号,港股/美股永远 0 样本——每轮都"成功"返回 changed=0
    并写 last_calibrated_at,运维看到的是"已标定",实际是空跑。

    kwargs 透传给 calibrate_factor_weights(alpha/beta/clamp/min_samples/horizon/days)。
    """
    own = db is None
    db = db or SessionLocal()
    try:
        targets = list(markets) if markets else list(OPPORTUNITY_MARKETS)
        return {m: calibrate_factor_weights(m, db=db, **kwargs) for m in targets}
    finally:
        if own:
            db.close()
