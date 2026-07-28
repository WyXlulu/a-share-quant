# Golden Slice Block 4b Execution Audit

> This run validates the execution boundary and corporate-action ledger on the
> manually verified CA ledger. L1 prices still come from `akshare_raw`; it is
> incorrect to describe all inputs as clean. This report contains no strategy
> performance conclusion and deliberately omits annualized return, Sharpe,
> drawdown, win rate, and NAV charts.
>
> BACKTEST_DESIGN §12.3 condition 6 is **not fully satisfied** because this
> repository has no real broker profile. The run uses the Phase 1 default
> `FeeSchedule` test tier. Official cash payment dates are also not validated:
> the existing handler pays on the first trading day after ex-date.
>
> Whether the project reaches `BACKTEST_VALIDATED` remains subject to the ten
> post-run audit conditions in §12.3. This run does not self-certify.

## Status

- evidence_status: `EXPLORATORY_TAINTED`
- audit_status: `PENDING_AUDIT`
- validation_scope: `GOLDEN_SLICE_PIPELINE`
- validation_scope_manifest_hash: `94e025b6a0b259c56751c6d3f3953c4a804aeea1c85b3b73dac9f9b2f468d4ae`
- frozen manifest gate: `PASS`

## Snapshot

- snapshot_id: `golden_slice_2026-07-28_EXECUTION`
- L1 rows: `15516`
- CA rows: `76`
- security_master rows: `12`
- cash fields: `22` different / `54` equal
- maximum split: `{"security_id": "000651", "ex_date": "2021-08-23", "cash_dividend_per_share": "3.0", "ex_right_cash_deduction_per_share": "2.784787", "absolute_difference": "0.215213"}`

The EXECUTION snapshot carries actual cash entitlement in
`cash_dividend_per_share` and the ex-right deduction in
`ex_right_cash_deduction_per_share`. It does not reuse the 4a
`ADJUSTMENT_ONLY` snapshot.

## Signal Input

- physically projected columns: `signal_asof_ts, score_asof_ts, security_id, signal_status, momentum_score, cross_sectional_rank`
- predictions SHA-256: `3e92a87a59d53095809137e2afd668394e44669c145d06e1e70098c1388b2158`
- 4a trusted baseline for this file hash: `False`
- ordered hash binding days: `970`

“Ordered hash binding” means the Nth real trading day is paired with the Nth
hash in 4a's feature manifest. It is **not** a hash recomputed from parquet
contents; Decimal-to-float loss and the lack of date keys prevent that stronger
claim without regenerating 4a.

## Execution Diagnostics

- orders: `122`
- locked orders: `91`
- fills/outcomes: `122`
- fill status counts: `{"FILLED": 91, "REJECTED": 31}`
- rejection reasons: `{"CASH_INSUFFICIENT": 31}`
- requested outcome categories: `{"filled": 91, "limit_up_or_down_rejected": 0, "suspended": 0, "cash_insufficient": 31, "capacity_rejected": 0, "no_open_price": 0}`
- capacity capped: `0`
- lot-size adjustments: `0`
- total fees under Phase 1 test tier: `9978.63`
- maximum accounting identity deviation: `0.00`

## Corporate Actions

- naturally triggered ledger observations: `27`
- event counts: `{"CA_DIVIDEND_ACCRUED": 13, "CA_DIVIDEND_PAID": 13, "CA_SHARES_ADJUSTED": 1}`
- UNPROCESSED_CA_count: `0`

| date | security | action_type | prior position | share delta | receivable cash delta | cost basis delta | sellable date |
|---|---|---:|---:|---:|---:|---:|---|
| 2020-05-25 | 600276 | CASH_DIVIDEND | 3700 | 0 | 851.00 | 0.00 | None |
| 2020-05-25 | 600276 | STOCK_DIVIDEND | 3700 | 740 | 0.00 | 0.00 | 2020-05-25 |
| 2020-05-26 | 600276 | CASH_DIVIDEND_PAYMENT | 4440 | 0 | 0.00 | 0.00 | None |
| 2020-06-22 | 000858 | CASH_DIVIDEND | 2400 | 0 | 5280.00 | 0.00 | None |
| 2020-06-23 | 000858 | CASH_DIVIDEND_PAYMENT | 2400 | 0 | 0.00 | 0.00 | None |
| 2020-06-24 | 600519 | CASH_DIVIDEND | 200 | 0 | 3405.00 | 0.00 | None |
| 2020-06-29 | 600519 | CASH_DIVIDEND_PAYMENT | 200 | 0 | 0.00 | 0.00 | None |
| 2021-06-02 | 000333 | CASH_DIVIDEND | 6400 | 0 | 10240.00 | 0.00 | None |
| 2021-06-03 | 000333 | CASH_DIVIDEND_PAYMENT | 0 | 0 | 0.00 | 0.00 | None |
| 2021-06-25 | 600519 | CASH_DIVIDEND | 200 | 0 | 3858.60 | 0.00 | None |
| 2021-06-28 | 600519 | CASH_DIVIDEND_PAYMENT | 200 | 0 | 0.00 | 0.00 | None |
| 2021-07-09 | 000858 | CASH_DIVIDEND | 1700 | 0 | 4386.00 | 0.00 | None |
| 2021-07-12 | 000858 | CASH_DIVIDEND_PAYMENT | 1700 | 0 | 0.00 | 0.00 | None |
| 2021-07-13 | 600036 | CASH_DIVIDEND | 9500 | 0 | 11903.50 | 0.00 | None |
| 2021-07-14 | 600036 | CASH_DIVIDEND_PAYMENT | 9500 | 0 | 0.00 | 0.00 | None |
| 2022-06-10 | 600028 | CASH_DIVIDEND | 100100 | 0 | 31031.00 | 0.00 | None |
| 2022-06-13 | 600028 | CASH_DIVIDEND_PAYMENT | 100100 | 0 | 0.00 | 0.00 | None |
| 2022-07-12 | 601398 | CASH_DIVIDEND | 91900 | 0 | 26954.27 | 0.00 | None |
| 2022-07-13 | 601398 | CASH_DIVIDEND_PAYMENT | 91900 | 0 | 0.00 | 0.00 | None |
| 2022-09-19 | 600028 | CASH_DIVIDEND | 104000 | 0 | 16640.00 | 0.00 | None |
| 2022-09-20 | 600028 | CASH_DIVIDEND_PAYMENT | 104000 | 0 | 0.00 | 0.00 | None |
| 2023-06-21 | 600028 | CASH_DIVIDEND | 84900 | 0 | 16555.50 | 0.00 | None |
| 2023-06-26 | 600028 | CASH_DIVIDEND_PAYMENT | 84900 | 0 | 0.00 | 0.00 | None |
| 2023-08-09 | 000651 | CASH_DIVIDEND | 14900 | 0 | 14900.00 | 0.00 | None |
| 2023-08-10 | 000651 | CASH_DIVIDEND_PAYMENT | 14900 | 0 | 0.00 | 0.00 | None |
| 2023-09-15 | 600028 | CASH_DIVIDEND | 92900 | 0 | 13470.50 | 0.00 | None |
| 2023-09-18 | 600028 | CASH_DIVIDEND_PAYMENT | 92900 | 0 | 0.00 | 0.00 | None |

The deterministic audit does not depend on the strategy happening to hold the
required names:

- cash audits: `[{"security_id": "000651", "ex_date": "2021-08-23", "prior_position": 1000, "actual_per_share": "3.0", "ex_right_deduction_per_share": "2.784787", "expected_receivable_cash_delta": "3000.00", "actual_receivable_cash_delta": "3000.00"}, {"security_id": "000651", "ex_date": "2022-04-08", "prior_position": 1000, "actual_per_share": "1.0", "ex_right_deduction_per_share": "0.936124", "expected_receivable_cash_delta": "1000.00", "actual_receivable_cash_delta": "1000.00"}, {"security_id": "000333", "ex_date": "2023-06-01", "prior_position": 1000, "actual_per_share": "2.5", "ex_right_deduction_per_share": "2.45", "expected_receivable_cash_delta": "2500.00", "actual_receivable_cash_delta": "2500.00"}]`
- stock audits: `[{"security_id": "600276", "ex_date": "2020-05-25", "prior_position": 1000, "share_ratio": "0.2", "expected_share_delta": 200, "actual_share_delta": 200, "total_cost_basis_before": "10000.00", "total_cost_basis_after": "10000.00", "cost_basis_delta": "0.00", "per_share_cost_before": "10.00", "per_share_cost_after": "8.333333333333333333333333333", "new_lot_sellable_from": "2020-05-25", "new_lot_is_unlocked": true}, {"security_id": "600276", "ex_date": "2021-06-10", "prior_position": 1000, "share_ratio": "0.2", "expected_share_delta": 200, "actual_share_delta": 200, "total_cost_basis_before": "10000.00", "total_cost_basis_after": "10000.00", "cost_basis_delta": "0.00", "per_share_cost_before": "10.00", "per_share_cost_after": "8.333333333333333333333333333", "new_lot_sellable_from": "2021-06-10", "new_lot_is_unlocked": true}]`

For stock dividends the asserted semantics are: total cost basis unchanged,
`cost_basis_delta=0`, per-share cost diluted, and the new lot has
`sellable_from=ex_date` with immediate unlock.

## Explicit Limitations

- No performance analysis is produced.
- Missing real broker profile means §12.3 condition 6 remains incomplete.
- Cash payment timing is the existing ex-date-plus-one-trading-day
  simplification, not an official payment-date validation.
- L1 remains `akshare_raw`; only the frozen CA ledger is manually verified from
  official PDFs.
