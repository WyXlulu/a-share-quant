# Golden Slice Pipeline Report

> This result is limited to honest pipeline behavior inside the frozen golden slice. It does not establish strategy effectiveness in the full market.
> BACKTEST_VALIDATED status may be granted only by a later audit of all ten conditions in BACKTEST_DESIGN section 12.3. This run does not self-certify.

## Status

- experiment_id: `golden_slice_momentum_ic_20260728_94e025b6`
- evidence_status: `EXPLORATORY_TAINTED`
- audit_status: `PENDING_AUDIT`
- validation_scope: `GOLDEN_SLICE_PIPELINE`
- validation_scope_manifest_hash: `94e025b6a0b259c56751c6d3f3953c4a804aeea1c85b3b73dac9f9b2f468d4ae`

## Statistical Limitations

**The cross-section contains only 12 securities. Daily RankIC therefore has a very small cross-sectional sample and high noise. RankIC is not evidence of strategy effectiveness. A mean near zero or a confidence interval crossing zero does not invalidate the pipeline-honesty objective.**

The moving-block bootstrap uses block length 21. It addresses overlapping-label dependence but remains statistically fragile with this small universe and the observed coverage.

## Governance And Snapshot

- frozen manifest gate: PASS
- snapshot_id: `golden_slice_2026-07-28_ADJUSTMENT_ONLY`
- L1 rows: 15516
- CA rows: 76
- snapshot purpose: ADJUSTMENT_ONLY; prohibited for 4b execution
- source_snapshot_id retained: YES
- scheme X path: 76/76 used disclosure_time_known=False and real TradingCalendar
- full pipeline portal: CachedPITDataPortal
- standard/cached 20-trading-day equivalence: PASS
- equivalence sample: 600276 2020-05-11..2020-06-05
- cached pipeline elapsed seconds: 4146.727554900004
- timing comparison caveat: the standard run did not complete, and cached elapsed is wall time including any host suspension; no speedup ratio is inferred

Scheme X samples:

| security_id | disclosure_date | ex_date | derived_available_at |
|---|---|---|---|
| 000333 | 2019-05-23 | 2019-05-30 | 2019-05-24T09:30:00+08:00 |
| 600276 | 2020-05-19 | 2020-05-25 | 2020-05-20T09:30:00+08:00 |
| 601939 | 2023-07-08 | 2023-07-14 | 2023-07-10T09:30:00+08:00 |

### Standard Portal Runtime Diagnostic

The initial standard PITDataPortal run remained healthy but was operator-interrupted before result generation. Its snapshot was reused unchanged for the cached run.

- accumulated CPU seconds: 6094.65625
- wall-clock seconds: 6164.9168309
- result artifacts produced before interruption: none beyond the two snapshot parquet files

## Combined Cash And Stock-Dividend Check

The three 600276 events use action_type=STOCK_DIVIDEND but retain both cash and share-ratio pricing inputs. This is a documented ADJUSTMENT_ONLY schema debt.

- ex_date: 2020-05-25
- previous close: 95.88
- cash deduction: 0.23
- denominator: 1.2
- hand-calculated reference price: 79.70833333333333333333333333
- service reference price: 79.70833333333333333333333333

## Negative Diagnostics

- total daily adjustment points: 15516
- status counts: `{"NO_DATA": 46, "OK": 15470}`
- BLOCKED reasons: `{}`
- NO_DATA reasons: `{"MISSING_CLOSE_OR_PREVIOUS_CLOSE": 46}`
- label status counts: `{"NOT_TRADABLE_ENTRY": 11, "NOT_TRADABLE_EXIT": 11, "NO_DATA": 31, "OK": 11587}`
- immature labels excluded by IC: 0
- missing labels in IC: 31
- mean coverage: 0.9973367697594501718213058430
- minimum coverage: 0.9166666666666666666666666667
- zero-coverage days: 0
- CI available: True

## Signal And RankIC

- signal days: 970
- average scorable securities/day: 11.19484536082474226804123711
- RankIC mean: 0.1093038233244418811429120707
- ICIR: 0.24903428484025386
- CI method: `moving_block_bootstrap_non_iid(block_length=21,iterations=500)`
- CI bounds: `['0.005179871674717035541777809915', '0.1976459279552063057217696397']`
- quantile monotonicity: `NOT_MONOTONIC`

| quantile | mean future return | sample count |
|---:|---:|---:|
| 1 | 0.008025674220778487939817183308 | 2811 |
| 2 | 0.01384747741143839734903297161 | 1940 |
| 3 | -0.001224717707053803271525953574 | 2197 |
| 4 | -0.004863669203119851236766145656 | 1940 |
| 5 | -0.002794308751097897955072961484 | 1940 |

## Akshare L2 Bidirectional Difference

Comparison key is `(security_id, ex_date)` only. Amount, action_type, and share_ratio are reported but do not define presence.

- akshare only: 0
- verified only: 2

### Akshare only

None.

### Verified only

| security_id | ex_date | action_type | cash | share_ratio | source |
|---|---|---|---:|---:|---|
| 600519 | 2022-12-27 | CASH_DIVIDEND | 21.91 | 0.0 | 600519_2022-12-21_1.pdf |
| 600519 | 2023-12-20 | CASH_DIVIDEND | 19.106 | 0.0 | 600519_2023-12-14_1.pdf |
