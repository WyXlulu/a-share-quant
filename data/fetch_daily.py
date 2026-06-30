from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from random import uniform
from time import sleep

import akshare as ak
import pandas as pd


INDEX_SYMBOL = "000300"
OUTPUT_PATH = Path("data/raw/hs300_daily.parquet")
REQUEST_DELAY_SECONDS = (0.2, 0.5)


def one_year_ago(day: date) -> date:
    try:
        return day.replace(year=day.year - 1)
    except ValueError:
        return day - timedelta(days=365)


def get_hs300_symbols() -> list[str]:
    constituents = ak.index_stock_cons_csindex(symbol=INDEX_SYMBOL)
    if constituents.empty:
        raise RuntimeError(f"No constituents returned for index {INDEX_SYMBOL}")

    code_column = "成分券代码"
    if code_column not in constituents.columns:
        raise RuntimeError(
            f"Constituent response missing {code_column}; columns={list(constituents.columns)}"
        )

    return (
        constituents[code_column]
        .astype(str)
        .str.extract(r"(\d{6})", expand=False)
        .dropna()
        .drop_duplicates()
        .tolist()
    )


def fetch_daily(symbol: str, start: date, end: date) -> pd.DataFrame:
    daily = ak.stock_zh_a_hist(
        symbol=symbol,
        period="daily",
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
        adjust="hfq",
    )

    if daily.empty:
        return daily

    daily = daily.copy()
    daily["股票代码"] = symbol
    return daily


def main() -> None:
    end = date.today()
    start = one_year_ago(end)
    symbols = get_hs300_symbols()

    frames: list[pd.DataFrame] = []
    failures: list[dict[str, str]] = []

    print(f"fetching index={INDEX_SYMBOL}, symbols={len(symbols)}, start={start}, end={end}")

    for index, symbol in enumerate(symbols, start=1):
        try:
            daily = fetch_daily(symbol, start, end)
            if daily.empty:
                failures.append({"股票代码": symbol, "原因": "empty result"})
            else:
                frames.append(daily)
        except Exception as exc:  # noqa: BLE001
            failures.append({"股票代码": symbol, "原因": f"{type(exc).__name__}: {exc}"})

        if index % 25 == 0 or index == len(symbols):
            print(
                f"progress={index}/{len(symbols)}, "
                f"success={len(frames)}, failures={len(failures)}"
            )

        if index < len(symbols):
            sleep(uniform(*REQUEST_DELAY_SECONDS))

    if not frames:
        raise RuntimeError(f"No daily data returned for any {INDEX_SYMBOL} constituents")

    all_daily = pd.concat(frames, ignore_index=True)
    all_daily = all_daily.sort_values(["股票代码", "日期"]).reset_index(drop=True)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    all_daily.to_parquet(OUTPUT_PATH, index=False)

    print(all_daily.head(5))
    print(f"successful_symbols={len(frames)}")
    print(f"rows={len(all_daily)}")
    print(f"columns={list(all_daily.columns)}")

    if failures:
        print("failures:")
        for failure in failures:
            print(f"- {failure['股票代码']}: {failure['原因']}")
    else:
        print("failures=[]")

    print(f"saved={OUTPUT_PATH}")


if __name__ == "__main__":
    main()
