#!/usr/bin/env python3
"""
盘前/盘中市场简报（直连，不依赖 TuShare）。

数据源：
- 指数 / 自选：腾讯 qt.gtimg.cn
- 涨跌分布：东财 push2ex getTopicZDFenBu
- 快讯：财联社 roll list

用法（在 PanWatch 根目录）:
  python scripts/premarket_brief_direct.py
  python scripts/premarket_brief_direct.py --symbols 300124,688126 --no-ai

说明：交易时段内为「当日累计」口径；未开盘时指数多为昨收附近。

@author 萨博
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import ssl
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TZ = ZoneInfo("Asia/Shanghai")
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
SSL_CTX = ssl.create_default_context()

INDEX_CODES = (
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
    ("sh000300", "沪深300"),
)

PROMPT = """你是 A 股盘前分析师。根据下方直连行情与快讯，撰写今日盘前简报。

要求：
1. 先用 150 字概括市场温度（指数、涨跌家数、成交额、情绪）
2. 解读 2-3 条重要新闻及其对今日开盘/盘中的潜在影响
3. 给出今日操作关注点（板块/风格/风险），80 字内
4. 使用 Markdown，简洁专业，不要编造未提供的数据
"""


def _http_get(url: str, headers: dict[str, str] | None = None, timeout: int = 12) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, **(headers or {})},
    )
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
        return resp.read()


def _to_tencent_code(symbol: str) -> str | None:
    sym = (symbol or "").strip().lower()
    if not sym:
        return None
    if sym.startswith(("sh", "sz", "bj")) and len(sym) >= 8:
        return sym
    if len(sym) == 6 and sym.isdigit():
        if sym.startswith(("5", "6", "9")):
            return f"sh{sym}"
        if sym.startswith(("4", "8")):
            return f"bj{sym}"
        return f"sz{sym}"
    return None


def fetch_tencent_quotes(codes: list[str]) -> list[dict]:
    if not codes:
        return []
    url = "https://qt.gtimg.cn/q=" + ",".join(codes)
    text = _http_get(url).decode("gbk", errors="replace")
    rows: list[dict] = []
    for line in text.strip().split(";"):
        if "~" not in line:
            continue
        body = line.split("=", 1)[-1].strip().strip('"')
        f = body.split("~")
        if len(f) < 38:
            continue
        try:
            price = float(f[3] or 0)
            prev = float(f[4] or 0)
            pct = float(f[32] or 0)
            # 指数成交额字段约等于「万元」
            amount_wan = float(f[37] or 0)
        except ValueError:
            continue
        rows.append(
            {
                "code": f[2],
                "name": f[1],
                "price": price,
                "prev_close": prev,
                "pct_chg": pct,
                "amount_yi": round(amount_wan / 10000.0, 2),
            }
        )
    return rows


def fetch_market_fenbu() -> dict:
    url = (
        "https://push2ex.eastmoney.com/getTopicZDFenBu"
        "?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
    )
    data = json.loads(
        _http_get(url, headers={"Referer": "https://quote.eastmoney.com/"}).decode()
    )
    payload = data.get("data") or {}
    fenbu_list = payload.get("fenbu") or []
    buckets: dict[int, int] = {}
    for item in fenbu_list:
        if not isinstance(item, dict) or not item:
            continue
        key = next(iter(item.keys()))
        try:
            buckets[int(key)] = int(item[key])
        except (TypeError, ValueError):
            continue

    up = sum(v for k, v in buckets.items() if k > 0)
    down = sum(v for k, v in buckets.items() if k < 0)
    flat = buckets.get(0, 0)
    # 10/11、-10/-11 近似涨跌停附近家数
    limit_up = buckets.get(10, 0) + buckets.get(11, 0)
    limit_down = buckets.get(-10, 0) + buckets.get(-11, 0)
    return {
        "qdate": str(payload.get("qdate") or ""),
        "up": up,
        "down": down,
        "flat": flat,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "stock_count": up + down + flat,
        "buckets": buckets,
    }


def fetch_cls_news(limit: int = 15) -> list[dict]:
    params = {
        "appName": "CailianpressWeb",
        "os": "web",
        "sv": "7.7.5",
        "last_time": "",
        "refresh_type": 1,
        "rn": str(limit),
    }
    qs = "&".join(f"{k}={params[k]}" for k in sorted(params))
    sign = hashlib.md5(hashlib.sha1(qs.encode()).hexdigest().encode()).hexdigest()
    url = "https://www.cls.cn/v1/roll/get_roll_list?" + urllib.parse.urlencode(
        {**params, "sign": sign}
    )
    data = json.loads(
        _http_get(url, headers={"Referer": "https://www.cls.cn/"}).decode()
    )
    items = ((data.get("data") or {}).get("roll_data") or [])[:limit]
    out: list[dict] = []
    for item in items:
        title = (item.get("title") or item.get("content") or "").strip()
        if not title:
            continue
        ts = item.get("ctime") or item.get("time") or ""
        try:
            dt = datetime.fromtimestamp(int(ts), tz=TZ).strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError, OSError):
            dt = str(ts)
        content = (item.get("content") or "").strip()
        if content == title:
            content = ""
        out.append(
            {
                "datetime": dt,
                "title": title[:120],
                "content": content[:200],
                "src": "cls",
            }
        )
    return out


def build_snapshot(symbols: list[str]) -> dict:
    errors: list[str] = []
    now = datetime.now(TZ)

    indices: list[dict] = []
    sh_amt = 0.0
    sz_amt = 0.0
    try:
        index_codes = [c for c, _ in INDEX_CODES]
        quotes = {q["code"]: q for q in fetch_tencent_quotes(index_codes)}
        for code, name in INDEX_CODES:
            q = quotes.get(code[2:]) or quotes.get(code)
            # 腾讯返回的 code 字段通常是 000001 / 399001
            if not q:
                for item in quotes.values():
                    if item.get("name") == name:
                        q = item
                        break
            if not q:
                continue
            indices.append(
                {
                    "code": code,
                    "name": name,
                    "price": q["price"],
                    "prev_close": q["prev_close"],
                    "pct_chg": q["pct_chg"],
                    "amount_yi": q["amount_yi"],
                }
            )
            if code.startswith("sh"):
                sh_amt = q["amount_yi"]
            if code == "sz399001":
                sz_amt = q["amount_yi"]
    except Exception as exc:
        errors.append(f"腾讯指数: {exc}")

    market_stats: dict = {}
    try:
        market_stats = fetch_market_fenbu()
        market_stats["amount_yi"] = round(sh_amt + sz_amt, 2)
    except Exception as exc:
        errors.append(f"东财涨跌分布: {exc}")

    news: list[dict] = []
    try:
        news = fetch_cls_news(limit=15)
    except Exception as exc:
        errors.append(f"财联社快讯: {exc}")

    watchlist: list[dict] = []
    tencent_codes = []
    for sym in symbols:
        tc = _to_tencent_code(sym)
        if tc:
            tencent_codes.append(tc)
    if tencent_codes:
        try:
            for q in fetch_tencent_quotes(tencent_codes):
                watchlist.append(
                    {
                        "symbol": q["code"],
                        "name": q["name"],
                        "price": q["price"],
                        "pct_chg": q["pct_chg"],
                        "amount_yi": q["amount_yi"],
                    }
                )
        except Exception as exc:
            errors.append(f"自选行情: {exc}")

    return {
        "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
        "indices": indices,
        "market_stats": market_stats,
        "news": news,
        "watchlist": watchlist,
        "errors": errors,
    }


def format_snapshot_markdown(snapshot: dict) -> str:
    lines: list[str] = []
    errors = snapshot.get("errors") or []
    if errors:
        lines.append("> 部分数据源异常：" + "；".join(errors[:3]))
        lines.append("")

    lines.append(f"数据时间：{snapshot.get('as_of')}（直连：腾讯 / 东财 / 财联社）")
    lines.append("")

    indices = snapshot.get("indices") or []
    if indices:
        lines.append("### A 股主要指数")
        for idx in indices:
            chg = float(idx.get("pct_chg") or 0)
            direction = "↑" if chg > 0 else "↓" if chg < 0 else "→"
            lines.append(
                f"- {idx.get('name')}：{float(idx.get('price') or 0):.2f} "
                f"{direction} {chg:+.2f}%（额约 {float(idx.get('amount_yi') or 0):.0f} 亿）"
            )
        lines.append("")

    stats = snapshot.get("market_stats") or {}
    if stats.get("stock_count"):
        lines.append("### 全市场涨跌分布（东财）")
        lines.append(f"- 统计日：{stats.get('qdate') or '-'}")
        lines.append(
            f"- 涨跌家数：涨 {stats.get('up', 0)} / 跌 {stats.get('down', 0)} / 平 {stats.get('flat', 0)}"
        )
        lines.append(
            f"- 涨跌停附近：约 {stats.get('limit_up', 0)} / {stats.get('limit_down', 0)} 家"
        )
        if stats.get("amount_yi"):
            lines.append(f"- 上证+深成指成交额合计约：{stats.get('amount_yi'):.0f} 亿元")
        lines.append("")

    news = snapshot.get("news") or []
    if news:
        lines.append("### 财经快讯（财联社）")
        for item in news[:10]:
            lines.append(f"- [{item.get('datetime')}] {item.get('title')}")
            content = (item.get("content") or "").strip()
            if content:
                lines.append(f"  > {content[:120]}")
        lines.append("")

    watchlist = snapshot.get("watchlist") or []
    if watchlist:
        lines.append("### 自选股")
        for row in watchlist:
            lines.append(
                f"- {row.get('name')}({row.get('symbol')})："
                f"{float(row.get('price') or 0):.2f}，"
                f"涨跌 {float(row.get('pct_chg') or 0):+.2f}%"
            )
        lines.append("")

    return "\n".join(lines).strip()


async def main() -> None:
    parser = argparse.ArgumentParser(description="直连盘前/盘中市场简报（无 TuShare）")
    parser.add_argument(
        "--symbols",
        default="",
        help="逗号分隔自选股代码，如 300124,688126",
    )
    parser.add_argument(
        "--no-ai",
        action="store_true",
        help="只打印原始 Markdown，不调用 AI",
    )
    args = parser.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    snapshot = build_snapshot(symbols)
    user_content = format_snapshot_markdown(snapshot)
    if not user_content or (
        not snapshot.get("indices")
        and not snapshot.get("news")
        and not snapshot.get("market_stats")
    ):
        print("未拉取到可用数据")
        if snapshot.get("errors"):
            print("错误：", "; ".join(snapshot["errors"]))
        sys.exit(2)

    if args.no_ai:
        print(user_content)
        sys.exit(0)

    try:
        from src.config import load_config
        from src.core.ai_client import AIClient
    except Exception as exc:
        print("--- 直连数据（无法加载 AI 客户端，仅输出原始摘要）---\n")
        print(user_content)
        print(f"\n(AI 加载失败: {exc})")
        sys.exit(0)

    cfg = load_config().settings
    if not cfg.ai_api_key or args.no_ai:
        print("--- 直连数据（未配置 AI，仅输出原始摘要）---\n")
        print(user_content)
        sys.exit(0)

    ai = AIClient(cfg.ai_base_url, cfg.ai_api_key, cfg.ai_model, cfg.http_proxy)
    brief = await ai.chat(PROMPT, user_content)
    print(brief)


if __name__ == "__main__":
    asyncio.run(main())
