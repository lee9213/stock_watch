"""合并 ``market_news`` 里已经入库的「同一事件多条」。

判重规则的改动（``news_keys.contents_duplicate``）只对**新入库**的新闻生效；
页面上的历史重复还在库里。这个脚本按同一套口径回放存量，把重复的合并掉。

保留哪一条
----------
保留**最早入库**的那条，与实时链路「先到的留下、后到的丢弃」一致；被删行的
``sources`` 追加到保留行，多源报道的痕迹不丢（页面上会显示 ``cls+stock_star``）。

不碰的东西
----------
- 已分析的行不做重新判定（``analyzed``/``bullish_level``/``relations_stocks``
  保持原样），只删除被判为同一事件的多余副本；
- 判重窗口与线上一致（默认 12 小时，按 ``news_time`` 和 ``created_at`` 双锚）。

用法::

    python scripts/dedupe_market_news.py                  # 只打印，不改库
    python scripts/dedupe_market_news.py --apply          # 真正合并
    python scripts/dedupe_market_news.py --days 7 --apply # 只处理近 7 天
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.platform.persistence.database import SessionLocal  # noqa: E402
from src.platform.persistence.models import MarketNews  # noqa: E402
from src.platform.persistence.news_keys import (  # noqa: E402
    content_signature,
    contents_duplicate,
)

DEFAULT_DAYS = 7


def _source_set(value: str | None) -> set[str]:
    return {part.strip() for part in (value or "").replace(",", "+").split("+") if part.strip()}


def _merge_sources(keep: str | None, drop: str | None) -> str:
    merged = _source_set(keep) | _source_set(drop)
    return "+".join(sorted(merged))


def find_groups(rows: list[MarketNews], window_hours: int) -> list[list[MarketNews]]:
    """按时间滚动，把同一事件的多条聚成组（组内按入库先后排列）。"""
    window = timedelta(hours=window_hours)
    signatures = []
    groups: list[list[MarketNews]] = []
    for row in rows:
        row_signature = content_signature(row.digest)
        target: list[MarketNews] | None = None
        for index, head in enumerate(groups):
            if abs((row.news_time or row.created_at) - (head[0].news_time or head[0].created_at)) > window:
                continue
            if contents_duplicate(row_signature, signatures[index]):
                target = head
                break
        if target is None:
            groups.append([row])
            signatures.append(row_signature)
        else:
            target.append(row)
    return [group for group in groups if len(group) > 1]


def main() -> int:
    parser = argparse.ArgumentParser(description="合并已入库的重复新闻")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="只处理最近 N 天（默认 7）")
    parser.add_argument("--window-hours", type=int, default=12, help="判重时间窗（默认 12 小时）")
    parser.add_argument("--apply", action="store_true", help="真正写库；缺省只打印")
    args = parser.parse_args()

    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    db = SessionLocal()
    try:
        rows = (
            db.query(MarketNews)
            .filter(MarketNews.news_time >= since)
            .order_by(MarketNews.news_time, MarketNews.id)
            .all()
        )
        print(f"扫描 {len(rows)} 条（近 {args.days} 天）")
        groups = find_groups(rows, args.window_hours)
        print(f"识别出 {len(groups)} 组重复，涉及 {sum(len(g) for g in groups)} 条\n")

        removed = 0
        for group in groups:
            keep = group[0]
            print(f"[保留 #{keep.id}] {keep.sources or '-'}  {(keep.news_time or keep.created_at)}")
            print(f"    {keep.digest[:60].replace(chr(10), ' ')}")
            for dup in group[1:]:
                print(f"  [合并 #{dup.id}] {dup.sources or '-'}  {(dup.news_time or dup.created_at)}")
                print(f"      {dup.digest[:60].replace(chr(10), ' ')}")
                if args.apply:
                    keep.sources = _merge_sources(keep.sources, dup.sources)
                    db.delete(dup)
                    removed += 1
            print()

        if args.apply:
            db.commit()
            print(f"已合并：删除 {removed} 条，保留 {len(groups)} 条（sources 已合并）")
        else:
            print("dry-run：未改动数据库。确认无误后加 --apply 执行。")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
