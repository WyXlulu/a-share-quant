from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from src.data.pit_data_portal import PITDataPortal
from src.domain import DataContractError


ASOF_BEFORE = "2026-06-30T14:59:59+08:00"
ASOF_AFTER = "2026-06-30T15:00:01+08:00"
VISIBLE_AT = "2026-06-30T15:00:00+08:00"
FUTURE_AT = "2026-07-01T15:00:00+08:00"
SECURITY_MASTER_ROW_VISIBLE_AT = "2001-08-27T15:00:00+08:00"


class PITDataPortalTest(unittest.TestCase):
    def test_future_filtering_excludes_before_and_includes_after_available_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            table_path = Path(tmpdir) / "daily_bar_raw.parquet"
            _write_daily_bar_fixture(table_path)
            portal = PITDataPortal({"daily_bar_raw": table_path, "security_master": table_path})

            before = portal.query("daily_bar_raw", ASOF_BEFORE)
            after = portal.query("daily_bar_raw", ASOF_AFTER)

            self.assertTrue(before.empty)
            self.assertEqual(after["security_id"].tolist(), ["600519"])

    def test_lt001_injected_future_row_is_physically_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            table_path = Path(tmpdir) / "daily_bar_raw.parquet"
            rows = _daily_bar_rows()
            rows.append(
                {
                    "security_id": "000001",
                    "trade_date": "2026-07-01",
                    "close": 999.0,
                    "event_ts": FUTURE_AT,
                    "available_at": FUTURE_AT,
                    "snapshot_id": "fixture",
                }
            )
            pd.DataFrame(rows).to_parquet(table_path, index=False)
            portal = PITDataPortal({"daily_bar_raw": table_path, "security_master": table_path})

            result = portal.query("daily_bar_raw", ASOF_AFTER)

            self.assertNotIn("000001", result["security_id"].astype(str).tolist())
            self.assertFalse(result["close"].eq(999.0).any())

    def test_missing_available_at_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            table_path = Path(tmpdir) / "missing_available_at.parquet"
            pd.DataFrame([{"security_id": "600519", "close": 100.0}]).to_parquet(
                table_path, index=False
            )
            portal = PITDataPortal({"daily_bar_raw": table_path, "security_master": table_path})

            with self.assertRaises(DataContractError):
                portal.query("daily_bar_raw", ASOF_AFTER)

    def test_security_master_taint_companions_and_attrs_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            table_path = Path(tmpdir) / "security_master.parquet"
            _write_security_master_fixture(table_path)
            portal = PITDataPortal({"daily_bar_raw": table_path, "security_master": table_path})

            result = portal.query(
                "security_master",
                ASOF_AFTER,
                security_ids=["600519"],
                columns=["security_id", "is_st", "status"],
            )

            self.assertEqual(result["security_id"].tolist(), ["600519"])
            self.assertIn("is_st_available_at", result.columns)
            self.assertIn("status_available_at", result.columns)
            self.assertIn("is_st_point_in_time_capability", result.columns)
            self.assertIn("status_point_in_time_capability", result.columns)
            self.assertTrue(result["is_st_available_at"].eq(VISIBLE_AT).all())
            self.assertTrue(result["status_available_at"].eq(VISIBLE_AT).all())
            self.assertTrue(result["is_st_point_in_time_capability"].eq("CURRENT_SNAPSHOT_ONLY").all())
            self.assertTrue(result["status_point_in_time_capability"].eq("CURRENT_SNAPSHOT_ONLY").all())
            self.assertEqual(
                result.attrs["field_capabilities"]["is_st"]["point_in_time_capability"],
                "CURRENT_SNAPSHOT_ONLY",
            )
            self.assertEqual(
                result.attrs["field_capabilities"]["status"]["evidence_level"],
                "EXPLORATORY_TAINTED",
            )

    def test_security_master_current_snapshot_fields_are_hidden_before_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            table_path = Path(tmpdir) / "security_master.parquet"
            _write_security_master_fixture(table_path)
            portal = PITDataPortal({"daily_bar_raw": table_path, "security_master": table_path})

            result = portal.query(
                "security_master",
                ASOF_BEFORE,
                security_ids=["600519"],
                columns=["security_id", "board", "is_st", "status"],
            )

            self.assertEqual(result["security_id"].tolist(), ["600519"])
            self.assertEqual(result["board"].tolist(), ["main_board"])
            self.assertTrue(result["is_st"].isna().all())
            self.assertTrue(result["status"].isna().all())
            self.assertTrue(result["is_st_available_at"].isna().all())
            self.assertTrue(result["status_available_at"].isna().all())
            self.assertTrue(result["is_st_point_in_time_capability"].eq("CURRENT_SNAPSHOT_ONLY").all())
            self.assertTrue(result["status_evidence_level"].eq("EXPLORATORY_TAINTED").all())


def _daily_bar_rows() -> list[dict[str, object]]:
    return [
        {
            "security_id": "600519",
            "trade_date": "2026-06-30",
            "close": 1185.49,
            "event_ts": VISIBLE_AT,
            "available_at": VISIBLE_AT,
            "snapshot_id": "fixture",
        }
    ]


def _write_daily_bar_fixture(path: Path) -> None:
    pd.DataFrame(_daily_bar_rows()).to_parquet(path, index=False)


def _write_security_master_fixture(path: Path) -> None:
    pd.DataFrame(
        [
            {
                "security_id": "600519",
                "name": "Kweichow Moutai",
                "available_at": SECURITY_MASTER_ROW_VISIBLE_AT,
                "board": "main_board",
                "board_available_at": SECURITY_MASTER_ROW_VISIBLE_AT,
                "is_st": False,
                "status": "normal",
                "is_st_available_at": VISIBLE_AT,
                "is_st_point_in_time_capability": "CURRENT_SNAPSHOT_ONLY",
                "is_st_evidence_level": "EXPLORATORY_TAINTED",
                "status_available_at": VISIBLE_AT,
                "status_point_in_time_capability": "CURRENT_SNAPSHOT_ONLY",
                "status_evidence_level": "EXPLORATORY_TAINTED",
                "snapshot_id": "fixture",
            }
        ]
    ).to_parquet(path, index=False)


if __name__ == "__main__":
    unittest.main()
