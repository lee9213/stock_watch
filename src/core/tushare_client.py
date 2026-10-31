"""
TuShare Pro 盘前数据采集：指数、全市场日线统计、财经新闻。

@author 萨博
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# A 股主要指数
DEFAULT_INDEX_CODES: tuple[tuple[str, str], ...] = (
    ("000001.SH", "上证指数"),
    ("399001.SZ", "深证成指"),
    ("399006.SZ", "创业板指"),
    ("000300.SH", "沪深300"),
)


@dataclass
class TusharePremarketSnapshot:
    """TuShare 盘前快照。"""

    trade_date: str
    prev_trade_date: str
    indices: list[dict[str, Any]] = field(default_factory=list)
    market_stats: dict[str, Any] = field(default_factory=dict)
    news: list[dict[str, Any]] = field(default_factory=list)
    watchlist_daily: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_date": self.trade_date,
            "prev_trade_date": self.prev_trade_date,
            "indices": self.indices,
            "market_stats": self.market_stats,
            "news": self.news,
            "watchlist_daily": self.watchlist_daily,
            "errors": self.errors,
        }


def resolve_tushare_token() -> str:
    """
    解析 TuShare token：环境变量 > PanWatch 配置 > vnpy vt_setting。
    """
    token = (os.getenv("TUSHARE_TOKEN") or "").strip()
    if token:
        return token

    try:
        from src.config import load_config

        cfg_token = (getattr(load_config().settings, "tushare_token", None) or "").strip()
        if cfg_token:
            return cfg_token
    except Exception:
        pass

    vt_setting = Path.home() / ".vntrader" / "vt_setting.json"
    if vt_setting.exists():
        try:
            data = json.loads(vt_setting.read_text(encoding="utf-8"))
            pwd = (data.get("datafeed.password") or "").strip()
            if pwd:
                return pwd
        except Exception as exc:
            logger.debug("读取 vt_setting TuShare token 失败: %s", exc)

    return ""


def _format_yyyymmdd(value: date | datetime | str) -> str:
    if isinstance(value, str):
        return value.replace("-", "")[:8]
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    return value.strftime("%Y%m%d")


def _shift_days(yyyymmdd: str, days: int) -> str:
    dt = datetime.strptime(yyyymmdd, "%Y%m%d").date()
    return (dt + timedelta(days=days)).strftime("%Y%m%d")


class TushareClient:
    """TuShare Pro 轻量封装。"""

    def __init__(self, token: str = "") -> None:
        self.token = (token or resolve_tushare_token()).strip()
        self._pro: Any | None = None

    @property
    def available(self) -> bool:
        return bool(self.token)

    def _ensure_pro(self) -> Any:
        if self._pro is not None:
            return self._pro
        if not self.token:
            raise RuntimeError("未配置 TuShare token（TUSHARE_TOKEN 或 datafeed.password）")
        import tushare as ts

        ts.set_token(self.token)
        self._pro = ts.pro_api()
        return self._pro

    def get_prev_trade_date(self, on_or_before: date | None = None) -> str:
        """返回 on_or_before 之前（含当日若为交易日则优先）的最近一个交易日。"""
        pro = self._ensure_pro()
        anchor = on_or_before or date.today()
        end = _format_yyyymmdd(anchor)
        start = _shift_days(end, -14)
        cal = pro.trade_cal(
            exchange="SSE",
            start_date=start,
            end_date=end,
            is_open="1",
        )
        if cal is None or cal.empty:
            raise RuntimeError("trade_cal 无数据")
        open_days = sorted(cal["cal_date"].astype(str).tolist())
        if not open_days:
            raise RuntimeError("trade_cal 未返回开市日")
        if open_days[-1] == end:
            return open_days[-1]
        return open_days[-1]

    def fetch_index_daily(self, trade_date: str) -> list[dict[str, Any]]:
        pro = self._ensure_pro()
        rows: list[dict[str, Any]] = []
        for ts_code, name in DEFAULT_INDEX_CODES:
            try:
                df = pro.index_daily(ts_code=ts_code, trade_date=trade_date)
            except Exception as exc:
                logger.warning("index_daily 失败 %s: %s", ts_code, exc)
                continue
            if df is None or df.empty:
                continue
            row = df.iloc[0]
            rows.append(
                {
                    "ts_code": ts_code,
                    "name": name,
                    "close": float(row.get("close") or 0),
                    "pct_chg": float(row.get("pct_chg") or 0),
                    "amount": float(row.get("amount") or 0),
                    "trade_date": str(row.get("trade_date") or trade_date),
                }
            )
        return rows

    def fetch_market_stats(self, trade_date: str) -> dict[str, Any]:
        pro = self._ensure_pro()
        df = pro.daily(trade_date=trade_date)
        if df is None or df.empty:
            return {"trade_date": trade_date, "stock_count": 0}

        pct = df["pct_chg"].astype(float)
        up = int((pct > 0).sum())
        down = int((pct < 0).sum())
        flat = int((pct == 0).sum())
        limit_up = int((pct >= 9.8).sum())
        limit_down = int((pct <= -9.8).sum())
        amount_yi = float(df["amount"].astype(float).sum() / 1e5)  # TuShare amount 单位：千元

        return {
            "trade_date": trade_date,
            "stock_count": int(len(df)),
            "up": up,
            "down": down,
            "flat": flat,
            "limit_up": limit_up,
            "limit_down": limit_down,
            "amount_yi": round(amount_yi, 2),
            "avg_pct_chg": round(float(pct.mean()), 2),
        }

    def fetch_news(
        self,
        start_dt: datetime,
        end_dt: datetime,
        limit: int = 15,
    ) -> list[dict[str, Any]]:
        pro = self._ensure_pro()
        start = start_dt.strftime("%Y-%m-%d %H:%M:%S")
        end = end_dt.strftime("%Y-%m-%d %H:%M:%S")
        try:
            df = pro.news(src="sina", start_date=start, end_date=end)
        except Exception as exc:
            logger.warning("TuShare news 接口失败: %s", exc)
            return []
        if df is None or df.empty:
            return []

        items: list[dict[str, Any]] = []
        for _, row in df.head(limit).iterrows():
            items.append(
                {
                    "datetime": str(row.get("datetime") or ""),
                    "title": str(row.get("title") or ""),
                    "content": str(row.get("content") or "")[:300],
                    "src": "tushare/sina",
                }
            )
        return items

    def fetch_daily_for_symbols(
        self,
        symbols: list[str],
        trade_date: str,
    ) -> list[dict[str, Any]]:
        if not symbols:
            return []
        pro = self._ensure_pro()
        rows: list[dict[str, Any]] = []
        for sym in symbols:
            ts_code = _to_ts_code(sym)
            if not ts_code:
                continue
            try:
                df = pro.daily(ts_code=ts_code, trade_date=trade_date)
            except Exception as exc:
                logger.debug("daily 失败 %s: %s", ts_code, exc)
                continue
            if df is None or df.empty:
                continue
            row = df.iloc[0]
            rows.append(
                {
                    "symbol": sym,
                    "ts_code": ts_code,
                    "close": float(row.get("close") or 0),
                    "pct_chg": float(row.get("pct_chg") or 0),
                    "amount": float(row.get("amount") or 0),
                    "vol": float(row.get("vol") or 0),
                }
            )
        return rows

    def fetch_premarket_snapshot(
        self,
        watchlist_symbols: list[str] | None = None,
        news_hours: int = 18,
    ) -> TusharePremarketSnapshot:
        """
        采集盘前快照：上一交易日指数 + 全市场统计 + 隔夜新闻 + 自选股昨收。
        """
        snapshot = TusharePremarketSnapshot(trade_date="", prev_trade_date="")
        if not self.available:
            snapshot.errors.append("未配置 TuShare token")
            return snapshot

        try:
            prev_trade_date = self.get_prev_trade_date()
            snapshot.prev_trade_date = prev_trade_date
            snapshot.trade_date = _format_yyyymmdd(date.today())
        except Exception as exc:
            snapshot.errors.append(f"trade_cal: {exc}")
            return snapshot

        try:
            snapshot.indices = self.fetch_index_daily(prev_trade_date)
        except Exception as exc:
            snapshot.errors.append(f"index_daily: {exc}")

        try:
            snapshot.market_stats = self.fetch_market_stats(prev_trade_date)
        except Exception as exc:
            snapshot.errors.append(f"daily: {exc}")

        end_dt = datetime.now()
        start_dt = end_dt - timedelta(hours=news_hours)
        snapshot.news = self.fetch_news(start_dt, end_dt)

        if watchlist_symbols:
            try:
                snapshot.watchlist_daily = self.fetch_daily_for_symbols(
                    watchlist_symbols,
                    prev_trade_date,
                )
            except Exception as exc:
                snapshot.errors.append(f"watchlist daily: {exc}")

        return snapshot


def _to_ts_code(symbol: str) -> str | None:
    sym = (symbol or "").strip().upper()
    if not sym:
        return None
    if "." in sym:
        return sym
    if len(sym) == 6 and sym.isdigit():
        if sym.startswith(("5", "6", "9")):
            return f"{sym}.SH"
        return f"{sym}.SZ"
    return None


def fetch_premarket_snapshot(
    watchlist_symbols: list[str] | None = None,
    token: str = "",
) -> TusharePremarketSnapshot:
    """模块级便捷入口。"""
    return TushareClient(token=token).fetch_premarket_snapshot(watchlist_symbols)


def format_snapshot_for_prompt(snapshot: TusharePremarketSnapshot) -> str:
    """将 TuShare 快照格式化为 LLM 可读 Markdown 片段。"""
    lines: list[str] = []
    if snapshot.errors:
        lines.append("> TuShare 部分接口异常：" + "；".join(snapshot.errors[:3]))
        lines.append("")

    if snapshot.prev_trade_date:
        lines.append(f"数据基准：上一交易日 {snapshot.prev_trade_date}")
        lines.append("")

    if snapshot.indices:
        lines.append("### A 股主要指数（TuShare index_daily）")
        for idx in snapshot.indices:
            chg = float(idx.get("pct_chg") or 0)
            direction = "↑" if chg > 0 else "↓" if chg < 0 else "→"
            lines.append(
                f"- {idx.get('name')}：{float(idx.get('close') or 0):.2f} "
                f"{direction} {chg:+.2f}%"
            )
        lines.append("")

    stats = snapshot.market_stats or {}
    if stats.get("stock_count"):
        lines.append("### 全市场概况（TuShare daily 聚合）")
        lines.append(
            f"- 涨跌家数：涨 {stats.get('up', 0)} / 跌 {stats.get('down', 0)} / 平 {stats.get('flat', 0)}"
        )
        lines.append(
            f"- 涨跌停：涨停约 {stats.get('limit_up', 0)} 家，跌停约 {stats.get('limit_down', 0)} 家"
        )
        lines.append(f"- 两市成交额：约 {stats.get('amount_yi', 0):.2f} 亿元")
        lines.append(f"- 平均涨跌幅：{stats.get('avg_pct_chg', 0):+.2f}%")
        lines.append("")

    if snapshot.news:
        lines.append("### 财经新闻（TuShare news / 新浪）")
        for item in snapshot.news[:10]:
            title = item.get("title") or ""
            dt = item.get("datetime") or ""
            lines.append(f"- [{dt}] {title}")
            content = (item.get("content") or "").strip()
            if content:
                lines.append(f"  > {content[:120]}...")
        lines.append("")

    if snapshot.watchlist_daily:
        lines.append("### 自选股昨收（TuShare daily）")
        for row in snapshot.watchlist_daily:
            lines.append(
                f"- {row.get('symbol')}：收盘 {float(row.get('close') or 0):.2f}，"
                f"涨跌 {float(row.get('pct_chg') or 0):+.2f}%"
            )
        lines.append("")

    return "\n".join(lines).strip()
