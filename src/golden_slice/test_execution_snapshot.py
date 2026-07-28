from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from src.domain import DataContractError
from src.golden_slice.execution_snapshot import (
    build_execution_snapshot,
    cash_field_split_diagnostics,
    load_execution_snapshot,
)
from src.golden_slice.manifest import GOLDEN_SLICE_SECURITY_IDS


MANIFEST_PATH = Path(__file__).with_name("golden_slice_manifest.json")
SNAPSHOT_ID = "golden_slice_2026-07-28_EXECUTION"


class GoldenSliceExecutionSnapshotTest(unittest.TestCase):
    def test_execution_snapshot_routes_actual_and_deduction_cash_fields(self) -> None:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            l1_path = root / "daily.parquet"
            master_path = root / "master.parquet"
            _write_l1(l1_path)
            _write_master(master_path)

            snapshot = build_execution_snapshot(
                manifest,
                l1_path=l1_path,
                security_master_path=master_path,
                output_dir=root / "snapshot",
                snapshot_id=SNAPSHOT_ID,
            )
            actions = pd.read_parquet(snapshot.corporate_action_path)
            diagnostics = cash_field_split_diagnostics(actions)

            self.assertEqual(diagnostics["different_count"], 22)
            self.assertEqual(diagnostics["same_count"], 54)
            self.assertEqual(
                diagnostics["maximum_difference"],
                {
                    "security_id": "000651",
                    "ex_date": "2021-08-23",
                    "cash_dividend_per_share": "3.0",
                    "ex_right_cash_deduction_per_share": "2.784787",
                    "absolute_difference": "0.215213",
                },
            )
            self.assertNotIn("available_at", actions.columns)
            self.assertTrue(actions["disclosure_time_known"].eq(False).all())  # noqa: E712

            master = pd.read_parquet(snapshot.security_master_path)
            self.assertEqual(len(master), 12)
            self.assertEqual(set(master["snapshot_id"]), {SNAPSHOT_ID})
            self.assertEqual(
                set(master["source_snapshot_id"]),
                {"source_security_master"},
            )
            self.assertEqual(snapshot.security_master_row_count, 12)
            self.assertEqual(
                snapshot.source_security_master_snapshot_ids,
                ("source_security_master",),
            )
            self.assertEqual(
                load_execution_snapshot(
                    output_dir=snapshot.root_dir,
                    snapshot_id=SNAPSHOT_ID,
                ),
                snapshot,
            )

    def test_execution_snapshot_rejects_adjustment_only_id(self) -> None:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(DataContractError, "EXECUTION"):
            build_execution_snapshot(
                manifest,
                l1_path=Path("not_read.parquet"),
                security_master_path=Path("not_read_master.parquet"),
                output_dir=Path("not_written"),
                snapshot_id="golden_slice_2026-07-28_ADJUSTMENT_ONLY",
            )


def _write_l1(path: Path) -> None:
    pd.DataFrame(
        [
            {
                "security_id": security_id,
                "trade_date": "2020-01-02",
                "open": 10.0,
                "close": 10.0,
                "amount": 1_000_000.0,
                "trade_status": "正常",
                "event_ts": "2020-01-02T15:00:00+08:00",
                "available_at": "2020-01-02T15:00:00+08:00",
                "price_basis": "RAW_UNADJUSTED",
                "snapshot_id": "source_l1",
            }
            for security_id in GOLDEN_SLICE_SECURITY_IDS
        ]
    ).to_parquet(path, index=False)


def _write_master(path: Path) -> None:
    pd.DataFrame(
        [
            {
                "security_id": security_id,
                "board": "主板",
                "list_date": "2000-01-01",
                "available_at": "2000-01-01T15:00:00+08:00",
                "snapshot_id": "source_security_master",
            }
            for security_id in GOLDEN_SLICE_SECURITY_IDS
        ]
    ).to_parquet(path, index=False)


if __name__ == "__main__":
    unittest.main()
