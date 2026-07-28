from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from src.domain import DataContractError
from src.golden_slice.deterministic_ledger_audit import (
    run_deterministic_ledger_audit,
)
from src.golden_slice.execution_snapshot import build_execution_snapshot
from src.golden_slice.manifest import GOLDEN_SLICE_SECURITY_IDS
from src.golden_slice.precomputed_signals import (
    FORBIDDEN_FUTURE_COLUMNS,
    SIGNAL_PROJECTION_COLUMNS,
    load_ordered_signal_binding,
    monthly_first_trading_days,
)
from src.golden_slice.run_execution_pipeline import (
    run_golden_slice_execution_pipeline,
)
from src.market_calendar import TradingCalendar


MANIFEST_PATH = Path(__file__).with_name("golden_slice_manifest.json")
SNAPSHOT_ID = "golden_slice_2026-07-28_EXECUTION"


class GoldenSliceExecutionPipelineTest(unittest.TestCase):
    def test_signal_adapter_physically_projects_only_signal_columns_and_binds_by_order(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            predictions_path = root / "predictions.parquet"
            manifest_path = root / "feature_manifest.json"
            rows = []
            for signal_date in ("2020-01-02", "2020-01-03"):
                for rank, security_id in enumerate(("000333", "000651"), start=1):
                    rows.append(
                        {
                            "signal_asof_ts": f"{signal_date}T15:00:00+08:00",
                            "score_asof_ts": f"{signal_date}T15:00:00+08:00",
                            "security_id": security_id,
                            "signal_status": "OK",
                            "momentum_score": 0.3 / rank,
                            "cross_sectional_rank": float(rank),
                            "future_return": 999.0,
                            "label_status": "FUTURE_ONLY",
                            "rank_ic": 1.0,
                        }
                    )
            pd.DataFrame(rows).to_parquet(predictions_path, index=False)
            manifest_path.write_text(
                json.dumps(
                    {
                        "signal_day_count": 2,
                        "signal_manifest_hashes": ["hash-day-1", "hash-day-2"],
                    }
                ),
                encoding="utf-8",
            )
            calendar = TradingCalendar(
                (date(2020, 1, 2), date(2020, 1, 3))
            )
            original_read_parquet = pd.read_parquet
            projected_columns: list[str] | None = None

            def recording_read_parquet(*args, **kwargs):
                nonlocal projected_columns
                projected_columns = kwargs.get("columns")
                return original_read_parquet(*args, **kwargs)

            with patch(
                "src.golden_slice.precomputed_signals.pd.read_parquet",
                side_effect=recording_read_parquet,
            ):
                binding = load_ordered_signal_binding(
                    predictions_path=predictions_path,
                    feature_manifest_path=manifest_path,
                    calendar=calendar,
                    start_date=date(2020, 1, 2),
                    end_date=date(2020, 1, 3),
                    expected_security_ids=("000333", "000651"),
                )

            self.assertEqual(projected_columns, list(SIGNAL_PROJECTION_COLUMNS))
            self.assertTrue(
                FORBIDDEN_FUTURE_COLUMNS.isdisjoint(binding.projected_columns)
            )
            self.assertEqual(
                binding.signal_manifest_hashes,
                ("hash-day-1", "hash-day-2"),
            )
            self.assertEqual(
                binding.signals_by_date[date(2020, 1, 2)].signal_manifest_hash,
                "hash-day-1",
            )
            self.assertEqual(
                binding.signals_by_date[date(2020, 1, 3)].signal_manifest_hash,
                "hash-day-2",
            )

    def test_monthly_rebalance_uses_first_real_calendar_session(self) -> None:
        calendar = TradingCalendar(
            (
                date(2020, 1, 31),
                date(2020, 2, 3),
                date(2020, 2, 4),
                date(2020, 3, 2),
            )
        )
        self.assertEqual(
            monthly_first_trading_days(
                calendar,
                date(2020, 1, 31),
                date(2020, 3, 2),
            ),
            (
                date(2020, 1, 31),
                date(2020, 2, 3),
                date(2020, 3, 2),
            ),
        )

    def test_deterministic_audit_routes_actual_cash_and_stock_semantics(self) -> None:
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
            calendar_dates = {
                pd.Timestamp(action["derived_available_at"]).date()
                for action in manifest["verified_corporate_actions"]
            }
            calendar_dates.update(
                pd.Timestamp(action["ex_date"]).date()
                for action in manifest["verified_corporate_actions"]
            )
            calendar = TradingCalendar(tuple(sorted(calendar_dates)))

            result = run_deterministic_ledger_audit(
                corporate_action_path=snapshot.corporate_action_path,
                calendar=calendar,
            )

            self.assertEqual(len(result.cash_dividends), 3)
            gree = next(
                item
                for item in result.cash_dividends
                if item.security_id == "000651"
                and item.ex_date == date(2021, 8, 23)
            )
            self.assertEqual(gree.actual_per_share, Decimal("3.0"))
            self.assertEqual(
                gree.ex_right_deduction_per_share,
                Decimal("2.784787"),
            )
            self.assertEqual(
                gree.expected_receivable_cash_delta,
                Decimal("3000.00"),
            )
            self.assertEqual(
                gree.actual_receivable_cash_delta,
                Decimal("3000.00"),
            )

            self.assertEqual(len(result.stock_dividends), 2)
            for stock in result.stock_dividends:
                self.assertEqual(stock.prior_position, 1000)
                self.assertEqual(stock.actual_share_delta, 200)
                self.assertEqual(stock.cost_basis_delta, Decimal("0.00"))
                self.assertEqual(
                    stock.total_cost_basis_before,
                    stock.total_cost_basis_after,
                )
                self.assertLess(
                    stock.per_share_cost_after,
                    stock.per_share_cost_before,
                )
                self.assertEqual(stock.new_lot_sellable_from, stock.ex_date)
                self.assertTrue(stock.new_lot_is_unlocked)

    def test_frozen_manifest_gate_runs_before_any_data_dependency(self) -> None:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        manifest["manifest_hash"] = "tampered"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tampered_path = root / "manifest.json"
            tampered_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(Exception, "hash mismatch"):
                run_golden_slice_execution_pipeline(
                    manifest_path=tampered_path,
                    l1_path=root / "missing_l1.parquet",
                    security_master_path=root / "missing_master.parquet",
                    calendar_path=root / "missing_calendar.parquet",
                    four_a_artifact_dir=root / "missing_4a",
                    artifacts_root=root / "artifacts",
                    run_date=date(2026, 7, 28),
                )
            self.assertFalse((root / "artifacts").exists())


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
