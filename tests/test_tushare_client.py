"""TuShare 盘前数据采集单元测试。"""

import unittest
from unittest.mock import MagicMock, patch

from src.core.tushare_client import (
    TushareClient,
    _to_ts_code,
    format_snapshot_for_prompt,
    resolve_tushare_token,
)


class TestTushareHelpers(unittest.TestCase):
    def test_to_ts_code(self):
        self.assertEqual(_to_ts_code("600519"), "600519.SH")
        self.assertEqual(_to_ts_code("000001"), "000001.SZ")
        self.assertEqual(_to_ts_code("600519.SH"), "600519.SH")

    @patch.dict("os.environ", {"TUSHARE_TOKEN": "abc123"}, clear=False)
    def test_resolve_token_from_env(self):
        self.assertEqual(resolve_tushare_token(), "abc123")


class TestFormatSnapshot(unittest.TestCase):
    def test_format_includes_sections(self):
        from src.core.tushare_client import TusharePremarketSnapshot

        snap = TusharePremarketSnapshot(
            trade_date="20250826",
            prev_trade_date="20250825",
            indices=[{"name": "上证指数", "close": 3000, "pct_chg": 1.2}],
            market_stats={"stock_count": 5000, "up": 3000, "down": 1500, "flat": 500,
                          "limit_up": 50, "limit_down": 10, "amount_yi": 9000, "avg_pct_chg": 0.5},
            news=[{"datetime": "2025-08-26 07:00:00", "title": "测试新闻", "content": "正文"}],
        )
        text = format_snapshot_for_prompt(snap)
        self.assertIn("上证指数", text)
        self.assertIn("全市场概况", text)
        self.assertIn("测试新闻", text)


class TestTushareClientFetch(unittest.TestCase):
    def test_fetch_premarket_snapshot(self):
        client = TushareClient(token="test-token")
        pro = MagicMock()
        client._pro = pro

        import pandas as pd

        pro.trade_cal.return_value = pd.DataFrame({"cal_date": ["20250822", "20250825"]})
        pro.index_daily.return_value = pd.DataFrame(
            [{"ts_code": "000001.SH", "close": 3000, "pct_chg": 1.0, "amount": 100, "trade_date": "20250825"}]
        )
        pro.daily.return_value = pd.DataFrame(
            {"pct_chg": [1.0, -2.0, 0.0], "amount": [1e6, 2e6, 3e6]}
        )
        pro.news.return_value = pd.DataFrame(
            [{"datetime": "2025-08-26 07:00:00", "title": "头条", "content": "内容"}]
        )

        snap = client.fetch_premarket_snapshot(watchlist_symbols=["600519"])

        self.assertEqual(snap.prev_trade_date, "20250825")
        self.assertEqual(len(snap.indices), 4)
        self.assertEqual(snap.market_stats.get("stock_count"), 3)
        self.assertEqual(len(snap.news), 1)


if __name__ == "__main__":
    unittest.main()
