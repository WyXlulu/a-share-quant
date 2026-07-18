from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

from src.data import PITDataPortal
from src.data.pit_data_portal import (
    resolve_ca_available_at as core_resolve_ca_available_at,
)
from src.domain import DataContractError
from src.engine.backtest_runner import (
    CachedPITDataPortal,
    resolve_ca_available_at as cached_resolve_ca_available_at,
)
from src.market_calendar import trading_calendar_from_dates


class CorporateActionAvailabilitySentinelTest(unittest.TestCase):
    def test_lt_ca_vis_01_unknown_time_uses_next_trading_day_and_fast_slow_match(self) -> None:
        calendar = trading_calendar_from_dates(
            [date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6)]
        )
        row = _ca_row(
            disclosure_time_known=False,
            disclosure_date="2026-01-02",
            ex_date="2026-01-06T00:00:00+08:00",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_ca_fixture(Path(tmpdir), [row])
            paths = {"corporate_actions": path}
            slow = PITDataPortal(paths, calendar)
            fast = CachedPITDataPortal(paths, calendar)

            slow_friday = slow.query("corporate_actions", "2026-01-02T15:00:00+08:00")
            fast_friday = fast.query("corporate_actions", "2026-01-02T15:00:00+08:00")
            slow_monday = slow.query("corporate_actions", "2026-01-05T09:30:00+08:00")
            fast_monday = fast.query("corporate_actions", "2026-01-05T09:30:00+08:00")

        self.assertEqual(calendar.next_trading_day(date(2026, 1, 2)), date(2026, 1, 5))
        self.assertNotEqual(calendar.next_trading_day(date(2026, 1, 2)), date(2026, 1, 3))
        self.assertIs(core_resolve_ca_available_at, cached_resolve_ca_available_at)
        self.assertTrue(slow_friday.empty)
        self.assertTrue(fast_friday.empty)
        self.assertEqual(len(slow_monday), 1)
        self.assertEqual(
            pd.Timestamp(slow_monday.iloc[0]["available_at"]),
            pd.Timestamp("2026-01-05T09:30:00+08:00"),
        )
        pd.testing.assert_frame_equal(slow_friday, fast_friday, check_dtype=True)
        pd.testing.assert_frame_equal(slow_monday, fast_monday, check_dtype=True)

    def test_lt_ca_vis_02_unknown_time_at_or_after_ex_date_fails_closed(self) -> None:
        calendar = trading_calendar_from_dates([date(2026, 1, 2), date(2026, 1, 5)])
        row = _ca_row(
            disclosure_time_known=False,
            disclosure_date="2026-01-02",
            ex_date="2026-01-05T00:00:00+08:00",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_ca_fixture(Path(tmpdir), [row])
            portal = PITDataPortal({"corporate_actions": path}, calendar)

            # Golden-slice volume is small: an unverifiable boundary is sent to manual
            # review. A future full-market ledger may add graceful BLOCK propagation.
            # This boundary is intentionally limited to disclosure_time_known=False;
            # the corresponding known-time boundary remains a full-market follow-up.
            with self.assertRaisesRegex(DataContractError, "cannot be verified before ex_date"):
                portal.query("corporate_actions", "2026-01-05T15:00:00+08:00")

    def test_lt_ca_vis_03_missing_legacy_and_disclosure_availability_fails_closed(self) -> None:
        row = _ca_row(ex_date="2026-01-06T00:00:00+08:00")

        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_ca_fixture(Path(tmpdir), [row])
            portal = PITDataPortal({"corporate_actions": path})

            with self.assertRaisesRegex(
                DataContractError,
                "neither disclosure_time_known nor available_at",
            ):
                portal.query("corporate_actions", "2026-01-06T15:00:00+08:00")

    def test_lt_ca_vis_04_known_time_is_preserved_and_date_only_value_is_rejected(self) -> None:
        precise = _ca_row(
            disclosure_time_known=True,
            disclosure_ts="2024-06-14T18:27:00+08:00",
            ex_date="2024-06-21T00:00:00+08:00",
        )
        midnight = _ca_row(
            disclosure_time_known=True,
            disclosure_ts="2024-06-14T00:00:00+08:00",
            ex_date="2024-06-21T00:00:00+08:00",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            precise_path = _write_ca_fixture(Path(tmpdir), [precise], name="precise.parquet")
            precise_portal = PITDataPortal({"corporate_actions": precise_path})
            visible = precise_portal.query(
                "corporate_actions",
                "2024-06-14T18:27:00+08:00",
            )

            midnight_path = _write_ca_fixture(Path(tmpdir), [midnight], name="midnight.parquet")
            midnight_portal = PITDataPortal({"corporate_actions": midnight_path})
            with self.assertRaisesRegex(DataContractError, "date-only midnight"):
                midnight_portal.query(
                    "corporate_actions",
                    "2024-06-14T15:00:00+08:00",
                )

        self.assertEqual(len(visible), 1)
        self.assertEqual(
            pd.Timestamp(visible.iloc[0]["available_at"]),
            pd.Timestamp("2024-06-14T18:27:00+08:00"),
        )


def _ca_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "security_id": "600519",
        "action_type": "CASH_DIVIDEND",
        "cash_dividend_per_share": 0.50,
        "share_ratio": 0.0,
        "source_id": "lt_ca_visibility_fixture",
        "snapshot_id": "lt_ca_visibility_fixture",
    }
    row.update(overrides)
    return row


def _write_ca_fixture(
    tmpdir: Path,
    rows: list[dict[str, object]],
    *,
    name: str = "corporate_actions.parquet",
) -> Path:
    path = tmpdir / name
    pd.DataFrame(rows).to_parquet(path, index=False)
    return path


if __name__ == "__main__":
    unittest.main()
