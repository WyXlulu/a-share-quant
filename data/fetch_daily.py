from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import akshare as ak


SYMBOL = "600519"
OUTPUT_PATH = Path("data/raw/600519.parquet")


def one_year_ago(day: date) -> date:
    try:
        return day.replace(year=day.year - 1)
    except ValueError:
        return day - timedelta(days=365)


def main() -> None:
    end = date.today()
    start = one_year_ago(end)

    daily = ak.stock_zh_a_hist(
        symbol=SYMBOL,
        period="daily",
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
        adjust="hfq",
    )

    if daily.empty:
        raise RuntimeError(f"No daily data returned for {SYMBOL} from {start} to {end}")

    print(daily.head(5))

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    daily.to_parquet(OUTPUT_PATH, index=False)

    print(f"rows={len(daily)}")
    print(f"columns={list(daily.columns)}")
    print(f"saved={OUTPUT_PATH}")


if __name__ == "__main__":
    main()
