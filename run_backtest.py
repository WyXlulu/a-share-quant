from __future__ import annotations

from src.engine.backtest_runner import run_default_backtest, write_backtest_outputs


def main() -> int:
    result = run_default_backtest(deterministic_check=True)
    write_backtest_outputs(result)
    summary = result.summary
    print("backtest completed")
    print(f"trading_days={summary.trading_days}")
    print(f"filled_count={summary.filled_count}")
    print(f"rejected_count={summary.rejected_count}")
    print(f"suspended_count={summary.suspended_count}")
    print(f"UNPROCESSED_CA_count={summary.unprocessed_ca_count}")
    print(f"total_fees={summary.total_fees}")
    print(f"final_nav={summary.final_nav}")
    print("outputs=data/backtest_output/nav_daily.csv,data/backtest_output/run_manifest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
