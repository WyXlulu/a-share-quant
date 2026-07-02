from __future__ import annotations

import json
import unittest
from pathlib import Path

import pandas as pd

from src.data.akshare_adapter import normalize_vendor_daily_frame
from src.domain import DAILY_BAR_REQUIRED_COLUMNS, BarFrequency, PriceBasis, TradeStatus


class L1RawContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.l1_path = Path("data/l1_raw/daily_bar_raw.parquet")
        self.manifest_path = Path("data/l1_raw/manifest.json")
        self.quarantined_hfq_path = Path("data/quarantine/vendor_adjusted/hs300_daily_10y.parquet")
        for path in (self.l1_path, self.manifest_path, self.quarantined_hfq_path):
            if not path.exists():
                self.skipTest(f"本地数据文件不存在: {path}")
        self.l1 = pd.read_parquet(self.l1_path)
        self.hfq = normalize_vendor_daily_frame(None, pd.read_parquet(self.quarantined_hfq_path))

    def test_l1_raw_price_basis_is_unadjusted_only(self) -> None:
        self.assertEqual(set(self.l1["price_basis"].dropna().unique()), {PriceBasis.RAW_UNADJUSTED.value})
        self.assertTrue(set(DAILY_BAR_REQUIRED_COLUMNS).issubset(self.l1.columns))
        self.assertEqual(set(self.l1["bar_frequency"].dropna().unique()), {BarFrequency.DAILY.value})

    def test_l1_manifest_records_current_hs300_tainted_semantics(self) -> None:
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["price_basis"], PriceBasis.RAW_UNADJUSTED.value)
        self.assertEqual(manifest["bar_frequency"], BarFrequency.DAILY.value)
        self.assertIn("EXPLORATORY_TAINTED", manifest["universe"]["source"])
        self.assertEqual(manifest["snapshot_id"], "20260630_akshare_raw")

    def test_trade_status_is_explicit(self) -> None:
        statuses = set(self.l1["trade_status"].dropna().unique())
        self.assertTrue(statuses.issubset({status.value for status in TradeStatus}))
        self.assertIn(TradeStatus.NORMAL.value, statuses)
        self.assertIn(TradeStatus.MISSING.value, statuses)

    def test_l1_range_and_row_count_are_consistent_with_quarantined_hfq(self) -> None:
        active_l1 = self.l1[self.l1["trade_status"].ne(TradeStatus.MISSING.value)].copy()
        active_l1["trade_date"] = pd.to_datetime(active_l1["trade_date"])
        hfq = self.hfq.copy()
        hfq["trade_date"] = pd.to_datetime(hfq["trade_date"])

        l1_stats = active_l1.groupby("security_id")["trade_date"].agg(["min", "max", "count"])
        hfq_stats = hfq.groupby("security_id")["trade_date"].agg(["min", "max", "count"])
        common = sorted(set(l1_stats.index) & set(hfq_stats.index))

        self.assertEqual(len(common), 300)
        for security_id in common:
            start_gap_days = abs(
                (l1_stats.loc[security_id, "min"] - hfq_stats.loc[security_id, "min"]).days
            )
            end_gap_days = abs(
                (l1_stats.loc[security_id, "max"] - hfq_stats.loc[security_id, "max"]).days
            )
            self.assertLessEqual(start_gap_days, 180)
            self.assertLessEqual(end_gap_days, 5)
            ratio = l1_stats.loc[security_id, "count"] / hfq_stats.loc[security_id, "count"]
            self.assertGreater(ratio, 0.80)
            self.assertLess(ratio, 1.30)


if __name__ == "__main__":
    unittest.main()
