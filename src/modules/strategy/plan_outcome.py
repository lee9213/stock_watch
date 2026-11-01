"""入场计划的后验评估：成交判定 + 逐 bar 路径判定。

改造前后验评估只用"horizon 末日的收盘价"跟目标价/止损价比一次，隐含两个乐观假设：

1. **一定能成交**——基准价取入场区间中点，第二天跳空高开 5%、价格从没回到区间，
   回测照样按中点算收益；
2. **路径无关**——期间跌破止损后又涨回来，只要末日收盘价没低于止损就算"没打损"，
   反之亦能拿到"达标"。目标价离得近时，这套算法几乎必然记一次命中。

这里同时修掉两件事：先判断"价格有没有真的进过区间"（未成交记 ``not_filled``，
不计入胜率分母），再按日线 OHLC 逐根判定"先触目标还是先触止损"。

**成交当日的 bar 内时序**：日线只有 OHLC，无法表达"高点发生在成交前还是成交后"。
但成交方向是确定的，据此可以推出当日只剩哪一侧可观测：

- 高开回落成交（``fall_back``，成交价 = 区间上沿）：当日 high 一定发生在成交之前，
  **当日只能判止损，不能判目标**。改造前会把成交前冲高的那一下记成"目标命中"，
  实测 10.5 开盘冲 10.8 再跌回 10.1 成交，会被记成 +4.95% 的 hit_target。
- 低开反弹成交（``bounce_up``，成交价 = 区间下沿）：当日 low 一定发生在成交之前，
  **当日只能判目标，不能判止损**。改造前会把开盘那一下记成"止损"，实测 9.5 低开
  反弹到 9.9 成交、随后收在 10.2，会被记成 −2.02% 的 hit_stop。
- 开盘即在区间内（``open_in_band``）：成交就在开盘，high/low 都在成交之后，两侧都可判。

这两个误判方向相反，且第二种（高开回落）恰好是"回踩买入"最常见的成交形态，
会让胜率被系统性抬高。从成交次日开始的 bar 恢复双侧判定。
"""

from __future__ import annotations

from datetime import date


def _touch_entry(bar: dict, entry_low: float, entry_high: float) -> tuple[float, str] | None:
    """当日是否与入场区间相交；相交则返回 ``(限价单成交价, 成交方向)``。"""
    open_px = float(bar["open"])
    high = float(bar["high"])
    low = float(bar["low"])
    if low > entry_high or high < entry_low:
        return None
    if entry_low <= open_px <= entry_high:
        # 开盘就在区间内，直接以开盘价成交
        return open_px, "open_in_band"
    if open_px > entry_high:
        # 高开：只有回落到区间上沿以内才算成交；当日 high 在成交之前
        return (entry_high, "fall_back") if low <= entry_high else None
    # 低开：反弹到区间下沿即成交（限价买）；当日 low 在成交之前
    return (entry_low, "bounce_up") if high >= entry_low else None


def _hit_side(bar: dict, stop_loss: float | None, target_price: float | None) -> str | None:
    """判断这一根 bar 先触哪一侧。"""
    open_px = float(bar["open"])
    high = float(bar["high"])
    low = float(bar["low"])
    hit_stop = stop_loss is not None and low <= float(stop_loss)
    hit_target = target_price is not None and high >= float(target_price)
    if hit_stop and hit_target:
        # 同日双侧都触及：跳空方向能判断就按跳空方向，否则保守判先止损。
        if open_px >= float(target_price):
            return "target"
        return "stop"
    if hit_stop:
        return "stop"
    if hit_target:
        return "target"
    return None


def _result(
    *,
    filled: bool,
    fill_day,
    fill_price,
    status: str,
    first_hit: str | None,
    outcome_price,
    outcome_day,
    return_pct,
    holding_days: int | None,
    fill_mode: str,
) -> dict:
    return {
        "filled": filled,
        "fill_day": fill_day,
        "fill_price": fill_price,
        "fill_mode": fill_mode,
        "status": status,
        "first_hit": first_hit,
        "outcome_price": outcome_price,
        "outcome_day": outcome_day,
        "return_pct": return_pct,
        # 成交日到出场日的 bar 数（含成交日）。horizon 内才成交的样本持有期很短，
        # 收益口径与整段持有的样本不可混算，留痕供统计侧分组。
        "holding_days": holding_days,
    }


def evaluate_plan_path(
    bars: list[dict] | None,
    *,
    entry_low: float | None,
    entry_high: float | None,
    stop_loss: float | None,
    target_price: float | None,
) -> dict:
    """对一组已完成日线做路径评估。

    ``bars`` 应是从快照日之后开始、长度等于 horizon 的日线序列。
    """
    rows = list(bars or [])
    if not rows:
        return _result(
            filled=False,
            fill_day=None,
            fill_price=None,
            status="not_due",
            first_hit=None,
            outcome_price=None,
            outcome_day=None,
            return_pct=None,
            holding_days=None,
            fill_mode="",
        )

    last = rows[-1]
    has_band = entry_low is not None and entry_high is not None
    low_bound = float(entry_low) if entry_low is not None else None
    high_bound = float(entry_high) if entry_high is not None else None

    fill_price: float | None = None
    fill_index: int | None = None
    fill_mode = "open_in_band"
    if has_band and low_bound is not None and high_bound is not None:
        if low_bound > high_bound:
            low_bound, high_bound = high_bound, low_bound
        for idx, bar in enumerate(rows):
            hit = _touch_entry(bar, low_bound, high_bound)
            if hit is not None:
                fill_price, fill_mode = hit
                fill_index = idx
                break
    else:
        # 没有入场区间（观望类计划）：按首个交易日开盘价作为参考成本，
        # 这类样本只用于观察，前端胜率统计需按 status 过滤。
        fill_price = float(rows[0]["open"])
        fill_index = 0
        fill_mode = "open_in_band"

    if fill_price is None or fill_price <= 0 or fill_index is None:
        return _result(
            filled=False,
            fill_day=None,  # 从未成交，没有成交日
            fill_price=None,
            status="not_filled",
            first_hit=None,
            outcome_price=float(last["close"]),
            outcome_day=last["date"],
            return_pct=None,
            holding_days=0,
            fill_mode="",
        )

    for idx in range(fill_index, len(rows)):
        bar = rows[idx]
        # 成交当日只允许判定"成交之后才可能发生"的那一侧（见模块 docstring）。
        allow_stop = idx > fill_index or fill_mode != "bounce_up"
        allow_target = idx > fill_index or fill_mode != "fall_back"
        side = _hit_side(
            bar,
            stop_loss if allow_stop else None,
            target_price if allow_target else None,
        )
        if side == "stop":
            exit_px = float(stop_loss)
            return _result(
                filled=True,
                fill_day=rows[fill_index]["date"],
                fill_price=fill_price,
                status="hit_stop",
                first_hit="stop",
                outcome_price=exit_px,
                outcome_day=bar["date"],
                return_pct=(exit_px - fill_price) / fill_price * 100.0,
                holding_days=idx - fill_index + 1,
                fill_mode=fill_mode,
            )
        if side == "target":
            exit_px = float(target_price)
            return _result(
                filled=True,
                fill_day=rows[fill_index]["date"],
                fill_price=fill_price,
                status="hit_target",
                first_hit="target",
                outcome_price=exit_px,
                outcome_day=bar["date"],
                return_pct=(exit_px - fill_price) / fill_price * 100.0,
                holding_days=idx - fill_index + 1,
                fill_mode=fill_mode,
            )

    close_px = float(last["close"])
    return _result(
        filled=True,
        fill_day=rows[fill_index]["date"],
        fill_price=fill_price,
        status="evaluated",
        first_hit=None,
        outcome_price=close_px,
        outcome_day=last["date"],
        return_pct=(close_px - fill_price) / fill_price * 100.0,
        holding_days=len(rows) - fill_index,
        fill_mode=fill_mode,
    )


def outcome_day_str(value) -> str:
    day = value if isinstance(value, date) else None
    return day.strftime("%Y-%m-%d") if day else ""
