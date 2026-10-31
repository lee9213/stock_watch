#!/usr/bin/env python3
"""
场景一：TuShare 盘前数据 + AI 简报（CLI）。

用法（在 PanWatch 根目录）:
  python scripts/premarket_brief.py
  python scripts/premarket_brief.py --symbols 600519,000001

@author 萨博
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import load_config
from src.core.ai_client import AIClient
from src.core.tushare_client import (
    TushareClient,
    format_snapshot_for_prompt,
)

PROMPT = """你是 A 股盘前分析师。根据 TuShare 提供的上一交易日指数、全市场统计与隔夜新闻，撰写今日盘前简报。

要求：
1. 先用 150 字概括市场温度（指数、涨跌家数、成交额、情绪）
2. 解读 2-3 条重要新闻及其对今日开盘的潜在影响
3. 给出今日操作关注点（板块/风格/风险），80 字内
4. 使用 Markdown，简洁专业，不要编造未提供的数据
"""


async def main() -> None:
    parser = argparse.ArgumentParser(description="TuShare 盘前简报")
    parser.add_argument(
        "--symbols",
        default="",
        help="逗号分隔自选股代码，如 600519,000001",
    )
    args = parser.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    client = TushareClient()
    if not client.available:
        print("错误：未找到 TuShare token。请设置 TUSHARE_TOKEN 或配置 ~/.vntrader/vt_setting.json")
        sys.exit(1)

    snapshot = client.fetch_premarket_snapshot(watchlist_symbols=symbols)
    user_content = format_snapshot_for_prompt(snapshot)
    if not user_content:
        print("TuShare 未返回可用数据")
        if snapshot.errors:
            print("错误：", "; ".join(snapshot.errors))
        sys.exit(2)

    cfg = load_config().settings
    if not cfg.ai_api_key:
        print("--- TuShare 数据（未配置 AI，仅输出原始摘要）---\n")
        print(user_content)
        sys.exit(0)

    ai = AIClient(cfg.ai_base_url, cfg.ai_api_key, cfg.ai_model, cfg.http_proxy)
    brief = await ai.chat(PROMPT, user_content)
    print(brief)


if __name__ == "__main__":
    asyncio.run(main())
