from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd

from src.domain import TradeStatus


class L1AmountUnitsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.l1_path = Path("data/l1_raw/daily_bar_raw.parquet")
        if not self.l1_path.exists():
            self.skipTest(f"本地数据文件不存在: {self.l1_path}")
        self.l1 = pd.read_parquet(
            self.l1_path,
            columns=[
                "security_id",
                "trade_date",
                "open",
                "close",
                "volume",
                "amount",
                "trade_status",
            ],
        )
        self.l1["security_id"] = self.l1["security_id"].astype(str).str.zfill(6)
        self.l1["trade_date"] = pd.to_datetime(self.l1["trade_date"], errors="raise").dt.date

    def test_amount_is_cny_and_volume_is_shares_for_capacity_adv(self) -> None:
        samples = [
            ("600519", date(2020, 12, 22), "main_board"),
            ("300750", date(2022, 6, 20), "chinext"),
            ("688981", date(2023, 7, 3), "star_market"),
        ]

        for security_id, trade_date, board_label in samples:
            with self.subTest(security_id=security_id, board_label=board_label):
                row = self._sample_row(security_id, trade_date)
                average_price = (Decimal(str(row["open"])) + Decimal(str(row["close"]))) / Decimal("2")
                notional_from_price_volume = average_price * Decimal(str(row["volume"]))
                ratio = Decimal(str(row["amount"])) / notional_from_price_volume

                self.assertEqual(row["trade_status"], TradeStatus.NORMAL.value)
                self.assertGreater(row["open"], 0)
                self.assertGreater(row["close"], 0)
                self.assertGreater(row["volume"], 0)
                self.assertGreater(row["amount"], 0)
                self.assertGreater(
                    ratio,
                    Decimal("0.85"),
                    msg=f"{security_id} ratio too small; amount may be in thousands of CNY",
                )
                self.assertLess(
                    ratio,
                    Decimal("1.15"),
                    msg=f"{security_id} ratio too large; volume may be in hands/lots",
                )

    def _sample_row(self, security_id: str, trade_date: date) -> pd.Series:
        rows = self.l1.loc[
            self.l1["security_id"].eq(security_id)
            & self.l1["trade_date"].eq(trade_date)
        ]
        if rows.empty:
            self.fail(f"missing fixture row for {security_id} on {trade_date}")
        return rows.iloc[0]


if __name__ == "__main__":
    unittest.main()
