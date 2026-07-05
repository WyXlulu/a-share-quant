from __future__ import annotations

import inspect
import tempfile
import unittest
from dataclasses import astuple
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from src.data import PITDataPortal
from src.domain import PriceBasis, TradeStatus
from src.engine import LockedOrder, T1OpenExecutor
from src.engine.event_clock import AsofDataPortal, ClockContext
from src.engine.execution import LimitRuleTable
from src.engine.portfolio_ledger import CashState, PortfolioLedger, PositionLot, PositionState
from src.features.cross_sectional_momentum import (
    CrossSectionalMomentumExclusion,
    CrossSectionalMomentumPoint,
    CrossSectionalMomentumSignal,
    ExclusionReasonCount,
)
from src.features.pit_adjustment_service import (
    AdjustedReturnStatus,
    CumulativeAdjustedReturnResult,
    EVIDENCE_STATUS,
    PITAdjustmentService,
)
from src.market_calendar import trading_calendar_from_dates
from src.portfolio import momentum_strategy
from src.portfolio.momentum_strategy import (
    MomentumOrderIntent,
    MomentumStrategyConfig,
    SignalDrivenMomentumStrategy,
    build_equal_weight_targets,
)


ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")


class MomentumStrategyTest(unittest.TestCase):
    def test_signal_driven_order_flow_uses_existing_lock_and_execution_boundary(self) -> None:
        trade_dates = _business_dates(date(2025, 1, 20), 255)
        decision_date = trade_dates[-3]
        execution_date = trade_dates[-2]
        scores = {
            "000001": Decimal("0.20"),
            "000002": Decimal("0.10"),
            "000003": Decimal("-0.10"),
        }
        daily_rows = _daily_rows_for_scores(trade_dates, decision_date, scores)
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _write_fixture(tmpdir, daily_rows=daily_rows)
            portal = PITDataPortal(paths)
            calendar = trading_calendar_from_dates(trade_dates)
            ledger = _ledger(calendar, initial_cash=Decimal("100000.00"))
            strategy = SignalDrivenMomentumStrategy(
                ("000001", "000002", "000003"),
                PITAdjustmentService(portal, calendar),
                ledger,
                MomentumStrategyConfig(
                    top_n=2,
                    max_single_name_weight=Decimal("0.40"),
                    min_cash_buffer=Decimal("0"),
                ),
            )
            ctx = _ctx(portal, decision_date)

            intents = strategy.on_bar(ctx)

            self.assertTrue(all(isinstance(intent, MomentumOrderIntent) for intent in intents))
            self.assertEqual([(intent.security_id, intent.side, intent.quantity) for intent in intents], [
                ("000001", "buy", 4000),
                ("000002", "buy", 4000),
            ])
            self.assertTrue(all(intent.evidence_status == "EXPLORATORY_TAINTED" for intent in intents))
            self.assertTrue(all(intent.signal_manifest_hash == strategy.latest_signal.signal_manifest_hash for intent in intents))

            locked_orders = _lock_intents(calendar, portal, ledger, intents, execution_date)
            mutated_portal = PITDataPortal(
                _write_fixture(
                    tmpdir,
                    daily_rows=_daily_rows_for_scores(
                        trade_dates,
                        decision_date,
                        scores,
                        t1_open_override=Decimal("99"),
                    ),
                    suffix="mutated",
                )
            )
            mutated_locked = _lock_intents(
                calendar,
                mutated_portal,
                _ledger(calendar, initial_cash=Decimal("100000.00")),
                intents,
                execution_date,
            )
            self.assertEqual(_locked_snapshot(locked_orders), _locked_snapshot(mutated_locked))

            for locked in locked_orders:
                self.assertEqual(locked.order_intent.decision_date, decision_date)
                self.assertEqual(locked.reference_price, Decimal("10.00"))
                self.assertEqual(locked.price_cap, Decimal("11.00"))
                self.assertEqual(locked.limit_check, "APPLIED")
                self.assertEqual(locked.adv_window_status, "ADV_FULL_WINDOW")
                expected_reservation = T1OpenExecutor(calendar, portal, execution_date).fee_schedule.calculate(
                    "buy",
                    execution_date,
                    11.0,
                    locked.locked_quantity,
                ).net_amount
                self.assertEqual(locked.reserved_cash, expected_reservation)

            fills = T1OpenExecutor(calendar, portal, execution_date).execute_open_round(locked_orders, ledger)
            self.assertEqual([fill.status for fill in fills], ["FILLED", "FILLED"])
            self.assertEqual(ledger.positions["000001"].total_quantity, 4000)
            self.assertEqual(ledger.positions["000002"].total_quantity, 4000)

    def test_equal_weight_targets_are_hand_calculated_and_cap_aware(self) -> None:
        signal = _signal(
            {
                "000001": (AdjustedReturnStatus.OK, Decimal("0.30"), 1),
                "000002": (AdjustedReturnStatus.BLOCKED, None, None),
                "000003": (AdjustedReturnStatus.OK, Decimal("0.20"), 2),
            }
        )

        uncapped = build_equal_weight_targets(
            signal,
            top_n=3,
            max_single_name_weight=Decimal("1"),
        )
        capped = build_equal_weight_targets(
            signal,
            top_n=3,
            max_single_name_weight=Decimal("0.40"),
        )

        self.assertEqual([(target.security_id, target.weight) for target in uncapped], [
            ("000001", Decimal("0.5")),
            ("000003", Decimal("0.5")),
        ])
        self.assertEqual([(target.security_id, target.weight) for target in capped], [
            ("000001", Decimal("0.40")),
            ("000003", Decimal("0.40")),
        ])

    def test_blocked_current_holding_is_excluded_from_targets_but_not_frozen(self) -> None:
        trade_dates = _business_dates(date(2025, 1, 20), 254)
        decision_date = trade_dates[-2]
        daily_rows = _daily_rows_for_scores(
            trade_dates,
            decision_date,
            {
                "000001": Decimal("0.20"),
                "000002": Decimal("0.10"),
                "000003": Decimal("0.50"),
            },
        )
        fake_service = _FakeAdjustmentService(
            trade_dates,
            {
                "000001": (AdjustedReturnStatus.OK, Decimal("0.20"), None),
                "000002": (AdjustedReturnStatus.OK, Decimal("0.10"), None),
                "000003": (AdjustedReturnStatus.BLOCKED, None, "UNSUPPORTED_CA_TYPE:MERGER"),
            },
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            portal = PITDataPortal(_write_fixture(tmpdir, daily_rows=daily_rows))
            calendar = trading_calendar_from_dates(trade_dates)
            ledger = _ledger(calendar, initial_cash=Decimal("100000.00"))
            ledger.positions["000003"] = PositionState(
                "000003",
                [
                    PositionLot(
                        quantity=100,
                        cost_basis=Decimal("1000.00"),
                        trade_date=trade_dates[-4],
                        sellable_from=decision_date,
                        is_unlocked=True,
                    )
                ],
            )
            strategy = SignalDrivenMomentumStrategy(
                ("000001", "000002", "000003"),
                fake_service,  # type: ignore[arg-type]
                ledger,
                MomentumStrategyConfig(top_n=2, max_single_name_weight=Decimal("0.50")),
            )

            intents = strategy.on_bar(_ctx(portal, decision_date))

            self.assertNotIn("000003", {target.security_id for target in strategy.latest_target_weights})
            blocked_point = {point.security_id: point for point in strategy.latest_signal.points}["000003"]
            self.assertEqual(blocked_point.status, AdjustedReturnStatus.BLOCKED)
            sell = next(intent for intent in intents if intent.security_id == "000003")
            self.assertEqual(sell.side, "sell")
            self.assertEqual(sell.quantity, 100)
            self.assertEqual(sell.reason, "momentum_signal_sell_to_target")

    def test_strategy_module_only_emits_order_intents_and_does_not_touch_execution_internals(self) -> None:
        source = inspect.getsource(momentum_strategy)

        self.assertIn("OrderIntent", source)
        self.assertIn("MomentumOrderIntent", source)
        self.assertNotIn("LockedOrder", source)
        self.assertNotIn("FillLedgerEntry", source)
        self.assertNotIn("PortfolioLedgerEntry", source)
        self.assertNotIn("lock_order", source)
        self.assertNotIn("execute_open_round", source)
        self.assertNotIn("apply_execution_result", source)
        self.assertNotIn("reserve_cash_for_buy", source)
        self.assertNotIn("lock_for_sell", source)

    def test_momentum_order_intents_propagate_exploratory_taint_and_manifest(self) -> None:
        intent = MomentumOrderIntent(
            security_id="1",
            side="buy",
            quantity=100,
            decision_date=date(2026, 1, 5),
            reason="momentum_signal_buy_to_target",
            tag="momentum_rebalance",
            evidence_status=EVIDENCE_STATUS,
            signal_manifest_hash="abc123",
            target_weight=Decimal("0.25"),
        )

        self.assertEqual(intent.security_id, "000001")
        self.assertEqual(intent.evidence_status, "EXPLORATORY_TAINTED")
        self.assertEqual(intent.signal_manifest_hash, "abc123")
        self.assertEqual(intent.tag, "momentum_rebalance")


class _FakeAdjustmentService:
    def __init__(
        self,
        trade_dates: list[date],
        results: dict[str, tuple[AdjustedReturnStatus, Decimal | None, str | None]],
    ) -> None:
        self.trade_dates = trade_dates
        self.results = results

    def _daily_bars(self, security_id: str, derivation_asof: object) -> pd.DataFrame:
        del security_id, derivation_asof
        return pd.DataFrame({"trade_date_key": self.trade_dates, "snapshot_id": ["fake"] * len(self.trade_dates)})

    def cumulative_adjusted_return(
        self,
        security_id: str,
        asof_ts: object,
        lookback_trading_days: int,
        derivation_asof_ts: object,
    ) -> CumulativeAdjustedReturnResult:
        del lookback_trading_days, derivation_asof_ts
        status, adjusted_return, reason = self.results[str(security_id).zfill(6)]
        return CumulativeAdjustedReturnResult(
            str(security_id).zfill(6),
            pd.Timestamp(asof_ts).isoformat(),
            231,
            status,
            adjusted_return,
            EVIDENCE_STATUS,
            PriceBasis.PIT_DERIVED,
            pd.Timestamp(asof_ts).isoformat(),
            "fake-snapshot",
            tuple(),
            reason,
        )


def _signal(
    rows: dict[str, tuple[AdjustedReturnStatus, Decimal | None, int | None]],
) -> CrossSectionalMomentumSignal:
    points = tuple(
        CrossSectionalMomentumPoint(
            security_id,
            score,
            rank,
            status,
        )
        for security_id, (status, score, rank) in rows.items()
    )
    exclusions = tuple(
        CrossSectionalMomentumExclusion(security_id, status, status.value)
        for security_id, (status, _, _) in rows.items()
        if status != AdjustedReturnStatus.OK
    )
    counts = tuple(
        ExclusionReasonCount(status, sum(1 for item in exclusions if item.status == status))
        for status in (AdjustedReturnStatus.BLOCKED, AdjustedReturnStatus.NO_DATA)
        if any(item.status == status for item in exclusions)
    )
    return CrossSectionalMomentumSignal(
        asof_ts="2026-01-05T15:00:00+08:00",
        score_asof_ts="2025-12-05T15:00:00+08:00",
        derivation_asof_ts="2026-01-05T15:00:00+08:00",
        ranking_method="ordinal_descending_rank",
        lookback_trading_days=231,
        skip_recent_trading_days=21,
        points=points,
        excluded_securities=exclusions,
        exclusion_reason_counts=counts,
        universe_size_after_exclusion=sum(1 for status, _, _ in rows.values() if status == AdjustedReturnStatus.OK),
        evidence_status=EVIDENCE_STATUS,
        input_snapshot_id="signal-fixture",
        signal_manifest_hash="signal-hash-fixture",
    )


def _lock_intents(
    calendar,
    portal: PITDataPortal,
    ledger: PortfolioLedger,
    intents,
    execution_date: date,
) -> list[LockedOrder]:
    executor = T1OpenExecutor(calendar, portal, end_date=execution_date)
    locked_orders: list[LockedOrder] = []
    for intent in intents:
        locked = executor.lock_order(intent, available_cash=ledger.cash.available_cash)
        if not isinstance(locked, LockedOrder):
            raise AssertionError(f"expected LockedOrder, got {locked}")
        ledger.reserve_cash_for_buy(locked)
        locked_orders.append(locked)
    return locked_orders


def _locked_snapshot(orders: list[LockedOrder]) -> list[tuple[object, ...]]:
    return [
        (
            order.order_intent.security_id,
            order.order_intent.side,
            order.locked_quantity,
            order.original_quantity,
            order.reference_price,
            order.price_cap,
            order.price_floor,
            order.reserved_cash,
            order.ruleset_version,
            order.limit_check,
            order.capacity_reason,
            order.adv_window_status,
            order.limit_reference_status,
        )
        for order in orders
    ]


def _ctx(portal: PITDataPortal, trade_date: date) -> ClockContext:
    asof = pd.Timestamp(datetime.combine(trade_date, datetime.min.time()), tz=ASIA_SHANGHAI) + pd.Timedelta(hours=15)
    return ClockContext(trade_date, asof, AsofDataPortal(portal, trade_date, asof))


def _ledger(calendar, *, initial_cash: Decimal) -> PortfolioLedger:
    return PortfolioLedger(
        CashState(settled_cash=initial_cash, available_cash=initial_cash),
        calendar=calendar,
    )


def _write_fixture(
    tmpdir: str,
    *,
    daily_rows: list[dict[str, object]],
    suffix: str = "",
) -> dict[str, Path]:
    tmp = Path(tmpdir)
    daily_path = tmp / f"daily_bar_raw{suffix}.parquet"
    master_path = tmp / f"security_master{suffix}.parquet"
    ca_path = tmp / f"corporate_actions{suffix}.parquet"
    pd.DataFrame(daily_rows).to_parquet(daily_path, index=False)
    pd.DataFrame(_security_master_rows()).to_parquet(master_path, index=False)
    pd.DataFrame([], columns=_CA_COLUMNS).to_parquet(ca_path, index=False)
    return {"daily_bar_raw": daily_path, "security_master": master_path, "corporate_actions": ca_path}


def _daily_rows_for_scores(
    trade_dates: list[date],
    decision_date: date,
    scores: dict[str, Decimal],
    *,
    t1_open_override: Decimal | None = None,
) -> list[dict[str, object]]:
    decision_index = trade_dates.index(decision_date)
    anchor_index = decision_index - 21
    rows: list[dict[str, object]] = []
    for security_id, score in scores.items():
        anchor_close = Decimal("10") * (Decimal("1") + score)
        for index, trade_date in enumerate(trade_dates):
            close = Decimal("10")
            open_price = close
            if index == anchor_index:
                close = anchor_close
                open_price = close
            if index == decision_index + 1 and t1_open_override is not None:
                open_price = t1_open_override
                close = t1_open_override
            rows.append(_bar_row(security_id, trade_date, open_price, close))
    return rows


def _bar_row(
    security_id: str,
    trade_date: date,
    open_price: Decimal,
    close: Decimal,
) -> dict[str, object]:
    return {
        "security_id": security_id,
        "trade_date": trade_date.isoformat(),
        "open": str(open_price),
        "high": str(open_price),
        "low": str(open_price),
        "close": str(close),
        "volume": 9999999,
        "amount": "10000000.00",
        "trade_status": TradeStatus.NORMAL.value,
        "event_ts": f"{trade_date.isoformat()}T15:00:00+08:00",
        "available_at": f"{trade_date.isoformat()}T15:00:00+08:00",
        "price_basis": PriceBasis.RAW_UNADJUSTED.value,
        "snapshot_id": "daily-fixture",
    }


def _security_master_rows() -> list[dict[str, object]]:
    board = next(iter(LimitRuleTable().rules_by_board))
    return [
        {
            "security_id": security_id,
            "board": board,
            "list_date": "2020-01-01",
            "available_at": "2020-01-01T15:00:00+08:00",
            "snapshot_id": "master-fixture",
        }
        for security_id in ("000001", "000002", "000003")
    ]


def _business_dates(start: date, count: int) -> list[date]:
    dates: list[date] = []
    current = start
    while len(dates) < count:
        if current.weekday() < 5:
            dates.append(current)
        current += timedelta(days=1)
    return dates


_CA_COLUMNS = [
    "security_id",
    "ex_date",
    "action_type",
    "cash_dividend_per_share",
    "share_ratio",
    "event_ts",
    "available_at",
    "source_id",
    "snapshot_id",
]


if __name__ == "__main__":
    unittest.main()
