from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from src.domain import DataContractError
from src.golden_slice.manifest import GOLDEN_SLICE_SECURITY_IDS
from src.golden_slice.snapshot import (
    build_adjustment_only_snapshot,
    load_adjustment_only_snapshot,
)


MANIFEST_PATH = Path(__file__).with_name("golden_slice_manifest.json")
SNAPSHOT_ID = "golden_slice_2026-07-27_ADJUSTMENT_ONLY"


class GoldenSliceSnapshotTest(unittest.TestCase):
    def test_snapshot_preserves_l1_lineage_and_maps_verified_ca_fields(self) -> None:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            l1_path = root / "daily_bar_raw.parquet"
            pd.DataFrame(
                [
                    {
                        "security_id": security_id,
                        "trade_date": "2020-01-02",
                        "open": 10.0,
                        "close": 10.0,
                        "event_ts": "2020-01-02T15:00:00+08:00",
                        "available_at": "2020-01-02T15:00:00+08:00",
                        "price_basis": "RAW_UNADJUSTED",
                        "snapshot_id": "source_l1_snapshot",
                    }
                    for security_id in GOLDEN_SLICE_SECURITY_IDS
                ]
            ).to_parquet(l1_path, index=False)

            snapshot = build_adjustment_only_snapshot(
                manifest,
                l1_path=l1_path,
                output_dir=root / "snapshot",
                snapshot_id=SNAPSHOT_ID,
            )

            bars = pd.read_parquet(snapshot.daily_bar_path)
            actions = pd.read_parquet(snapshot.corporate_action_path)
            self.assertEqual(snapshot.l1_row_count, 12)
            self.assertEqual(snapshot.ca_row_count, 76)
            self.assertEqual(snapshot.source_snapshot_ids, ("source_l1_snapshot",))
            self.assertEqual(set(bars["snapshot_id"]), {SNAPSHOT_ID})
            self.assertEqual(
                set(bars["source_snapshot_id"]),
                {"source_l1_snapshot"},
            )
            self.assertNotIn("available_at", actions.columns)
            self.assertEqual(
                actions["action_type"].value_counts().to_dict(),
                {"CASH_DIVIDEND": 73, "STOCK_DIVIDEND": 3},
            )

            gree = actions.loc[
                actions["security_id"].eq("000651")
                & pd.to_datetime(actions["disclosure_date"])
                .dt.date.eq(pd.Timestamp("2021-08-14").date())
            ].iloc[0]
            self.assertEqual(gree["cash_dividend_per_share"], 2.784787)
            self.assertEqual(gree["cash_dividend_actual_per_share"], 3.0)

            reloaded = load_adjustment_only_snapshot(
                output_dir=snapshot.root_dir,
                snapshot_id=SNAPSHOT_ID,
            )
            self.assertEqual(reloaded, snapshot)

    def test_snapshot_rejects_id_without_adjustment_only_marker(self) -> None:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(DataContractError, "ADJUSTMENT_ONLY"):
            build_adjustment_only_snapshot(
                manifest,
                l1_path=Path("not_read.parquet"),
                output_dir=Path("not_written"),
                snapshot_id="golden_slice_2026-07-27",
            )


if __name__ == "__main__":
    unittest.main()
