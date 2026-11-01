"""热榜合并：成交榜与涨幅榜必须都进候选池（配额合并回归）。

改造前两榜 concat 后统一遍历、达到 safe_limit 就 break，成交榜一条就能占满名额，
涨幅榜形同废榜。
"""

from __future__ import annotations

from types import SimpleNamespace

import src.modules.strategy.entry_candidates as ec


class _FakeCollector:
    def __init__(self, *args, **kwargs):
        pass

    async def fetch_hot_stocks(self, *, market="CN", mode="turnover", limit=20):
        if mode == "turnover":
            return [
                SimpleNamespace(
                    symbol=f"6000{i:02d}",
                    name=f"成交{i}",
                    price=10.0 + i,
                    change_pct=2.0,
                    turnover=1_000_000_000.0,
                    volume=1_000_000.0,
                )
                for i in range(min(limit, 100))
            ]
        return [
            SimpleNamespace(
                symbol=f"3000{i:02d}",
                name=f"涨幅{i}",
                price=20.0 + i,
                change_pct=9.0,
                turnover=500_000_000.0,
                volume=2_000_000.0,
            )
            for i in range(min(limit, 100))
        ]


def test_gainers_list_survives_turnover_filling_the_quota(monkeypatch):
    monkeypatch.setattr(ec, "EastMoneyDiscoveryCollector", _FakeCollector)
    result = ec._load_market_scan_inputs(limit_per_market=60, markets=["CN"])

    turnover_kept = [k for k in result if k.startswith("CN:600")]
    gainers_kept = [k for k in result if k.startswith("CN:300")]
    assert len(result) <= 60
    assert len(turnover_kept) >= 30
    # 关键断言：成交榜给满的情况下，涨幅榜仍有名额
    assert len(gainers_kept) >= 20
    assert result[f"CN:300000"]["quote_seed"]["change_pct"] == 9.0


def test_gainers_backfill_when_turnover_is_short(monkeypatch):
    class _ShortCollector(_FakeCollector):
        async def fetch_hot_stocks(self, *, market="CN", mode="turnover", limit=20):
            rows = await super().fetch_hot_stocks(market=market, mode=mode, limit=limit)
            return rows[:3] if mode == "turnover" else rows

    monkeypatch.setattr(ec, "EastMoneyDiscoveryCollector", _ShortCollector)
    result = ec._load_market_scan_inputs(limit_per_market=20, markets=["CN"])
    assert len([k for k in result if k.startswith("CN:600")]) == 3
    # 成交榜只有 3 条时，涨幅榜应回填剩余名额（不浪费扫描配额）
    assert len(result) >= 20
