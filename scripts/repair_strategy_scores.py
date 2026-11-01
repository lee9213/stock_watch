"""按当前口径重算已经入库的机会评分（strategy_signal_runs）。

为什么需要它：库里的行是旧代码打的，评分口径在这之后改过三次，于是同一列上
并存着几种互相矛盾的读数。实测 2026-11-01 的 42 行里：

1. ``rank_score`` 两种口径混在一个字段里。可执行行是"当日同市场百分位"（旧口径
   ``idx/(n-1)``，n=1 时除零退回 1.0，于是 600519 的 4 行全是 100 分），非可执行
   行则是封顶后的合成分（65/63.51/…）。两个数根本不是同一种东西。
2. ``absolute_score`` 一行两个值。``payload`` 写每行自己的 composite（136.05），
   ``score_breakdown`` 写该股票的最高 composite（145.78），接口按 breakdown 优先
   读，换个调用方式就换个数。
3. 非可执行行**没有** ``payload.absolute_score``，只有 breakdown 里有。

这里不重取行情，也不碰 ``frozen``（那是 point-in-time 证据，改了就等于事后改判），
``composite_score`` 保持入库原值——它是当时看到的输入，不该被今天的代码覆盖。
重算的只是"由 composite 派生、且派生规则已经变了"的字段：

- ``rank_score`` / ``weighted_score``：先按 ``_capped_display_score`` 还原封顶后的
  展示分，再让 ``_normalize_actionable_rank`` 对可执行行做中点百分位；
- ``absolute_score``：payload 与 breakdown 统一写同一个数；
- ``actionable``：按 ``_is_actionable`` 重新判定，和页面判据对齐。

用法::

    python scripts/repair_strategy_scores.py              # 只打印，不改库
    python scripts/repair_strategy_scores.py --apply      # 落库
    python scripts/repair_strategy_scores.py --apply --date 2026-11-01
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.modules.strategy.strategy_engine import (
    _capped_display_score,
    _is_actionable,
    _normalize_actionable_rank,
    _safe_float,
)
from src.platform.persistence.database import SessionLocal
from src.platform.persistence.json_safe import to_jsonable
from src.platform.persistence.models import StrategySignalRun


def _breakdown_of(row: StrategySignalRun) -> dict:
    payload = row.payload if isinstance(row.payload, dict) else {}
    breakdown = payload.get("score_breakdown")
    return dict(breakdown) if isinstance(breakdown, dict) else {}


def _composite_of(row: StrategySignalRun) -> float:
    """入库时的未封顶合成分。老行可能只有 absolute/rank，按可信度依次回退。"""
    breakdown = _breakdown_of(row)
    for key in ("composite_score", "absolute_score", "raw_score"):
        value = _safe_float(breakdown.get(key))
        if value is not None:
            return float(value)
    return float(row.rank_score or 0.0)


def repair_snapshot(db, snapshot_date: str, *, apply: bool = False) -> dict:
    rows = (
        db.query(StrategySignalRun)
        .filter(StrategySignalRun.snapshot_date == snapshot_date)
        .all()
    )
    stats = {
        "snapshot_date": snapshot_date,
        "rows": len(rows),
        "rank_changed": 0,
        "actionable_changed": 0,
        "absolute_synced": 0,
        "changes": [],
    }
    old_rank: dict[int, float] = {}
    old_absolute: dict[int, float | None] = {}
    for row in rows:
        breakdown = _breakdown_of(row)
        composite = _composite_of(row)
        has_entry = row.entry_low is not None or row.entry_high is not None

        payload = dict(row.payload) if isinstance(row.payload, dict) else {}
        # 旧值要在改写 payload 之前取，否则拿到的已经是新值，变化量统计会归零。
        old_rank[row.id] = round(float(row.rank_score or 0.0), 4)
        old_absolute[row.id] = _safe_float(payload.get("absolute_score"))

        capped = round(
            _capped_display_score(
                composite,
                status=row.status,
                action=(row.action or "watch").strip().lower() or "watch",
                has_entry=has_entry,
                is_holding=bool(row.is_holding_snapshot),
            ),
            4,
        )
        breakdown["weighted_score"] = capped
        breakdown["absolute_score"] = round(composite, 4)
        payload["score_breakdown"] = breakdown
        payload["absolute_score"] = round(composite, 4)
        # 旧行可能残留上一次归一化写下的百分位字段；先清掉，可执行行下面会重写，
        # 不可执行行不该带着一个"当日排名"——它没有参与排名。
        for stale in ("percentile_score", "rank_pct"):
            payload.pop(stale, None)
            breakdown.pop(stale, None)

        row.rank_score = capped
        row.payload = to_jsonable(payload)

        actionable = _is_actionable(row)
        if bool(row.actionable) != actionable:
            stats["actionable_changed"] += 1
        row.actionable = actionable

    # 统一重算可执行行的百分位（按市场分组，按股票去重，中点口径）。
    normalized = _normalize_actionable_rank(rows=rows)
    stats["normalized"] = normalized

    for row in rows:
        payload = row.payload if isinstance(row.payload, dict) else {}
        breakdown = payload.get("score_breakdown") or {}
        absolute = _safe_float(payload.get("absolute_score"))
        if _safe_float(breakdown.get("absolute_score")) != absolute:
            raise RuntimeError(f"{row.stock_symbol} {row.strategy_code} absolute_score 不一致，已中止")
        new_rank = round(float(row.rank_score or 0.0), 4)
        rank_moved = abs(old_rank.get(row.id, 0.0) - new_rank) > 1e-6
        absolute_moved = old_absolute.get(row.id) != absolute
        stats["rank_changed"] += int(rank_moved)
        stats["absolute_synced"] += int(absolute_moved)
        if rank_moved or absolute_moved:
            stats["changes"].append(
                {
                    "symbol": row.stock_symbol,
                    "strategy": row.strategy_code,
                    "old_rank": old_rank.get(row.id),
                    "new_rank": new_rank,
                    "old_absolute": old_absolute.get(row.id),
                    "new_absolute": absolute,
                }
            )

    if apply:
        db.commit()
    else:
        db.rollback()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="按当前口径重算已入库的机会评分")
    parser.add_argument("--apply", action="store_true", help="真正写库，缺省只打印")
    parser.add_argument("--date", default=None, help="只处理某个快照日，缺省全部")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        dates = [
            r[0]
            for r in db.query(StrategySignalRun.snapshot_date)
            .filter(StrategySignalRun.snapshot_date.isnot(None))
            .distinct()
            .order_by(StrategySignalRun.snapshot_date)
            .all()
        ]
        if args.date:
            dates = [d for d in dates if d == args.date]
        if not dates:
            print("没有需要处理的快照")
            return 0

        for snapshot_date in dates:
            stats = repair_snapshot(db, snapshot_date, apply=args.apply)
            print(f"\n=== {snapshot_date}  共 {stats['rows']} 行 ===")
            print(
                f"  rank_score 变化 {stats['rank_changed']} 行 / "
                f"actionable 变化 {stats['actionable_changed']} 行 / "
                f"absolute_score 同步 {stats['absolute_synced']} 行 / "
                f"参与百分位 {stats['normalized']} 行"
            )
            print("  变化的行（按 rank 变化幅度排序）:")
            ordered = sorted(
                stats["changes"],
                key=lambda x: abs(float(x["new_rank"]) - float(x["old_rank"] or 0.0)),
                reverse=True,
            )
            for item in ordered[:12]:
                print(
                    "    {symbol} {strategy:16s} rank {old_rank} -> {new_rank}"
                    "   absolute {old_absolute} -> {new_absolute}".format(**item)
                )
        print("\n已写库" if args.apply else "\n仅预览，未写库（加 --apply 落库）")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
