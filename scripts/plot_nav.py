"""Plot the Phase 1 ten-year dummy-strategy backtest NAV chart."""

from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "data" / "backtest_output"
INPUT_CSV = OUTPUT_DIR / "nav_daily.csv"
OUTPUT_PNG = OUTPUT_DIR / "nav_10y.png"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MPLCONFIGDIR", str(OUTPUT_DIR / ".matplotlib"))

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.font_manager as font_manager
import matplotlib.pyplot as plt
from matplotlib.ticker import StrMethodFormatter
import pandas as pd


INITIAL_NAV = 1_000_000


def _find_column(columns: list[str], candidates: tuple[str, ...], fallback_index: int) -> str:
    normalized = {column.strip().lower(): column for column in columns}
    for candidate in candidates:
        if candidate.strip().lower() in normalized:
            return normalized[candidate.strip().lower()]
    return columns[fallback_index]


def _read_nav(csv_path: Path) -> pd.DataFrame:
    if not csv_path.exists():
        raise FileNotFoundError(f"Missing input file: {csv_path}")

    raw = pd.read_csv(csv_path, encoding="utf-8-sig")
    if len(raw.columns) < 4:
        raise ValueError("nav_daily.csv must have at least four columns: date, nav, cash, market_value")

    columns = list(raw.columns)
    date_col = _find_column(columns, ("日期", "date", "trade_date"), 0)
    nav_col = _find_column(columns, ("净值", "nav", "net_asset_value"), 1)
    cash_col = _find_column(columns, ("现金", "cash"), 2)
    market_value_col = _find_column(columns, ("持仓市值", "market_value", "position_market_value"), 3)

    data = pd.DataFrame(
        {
            "date": pd.to_datetime(raw[date_col]),
            "nav": pd.to_numeric(raw[nav_col], errors="raise"),
            "cash": pd.to_numeric(raw[cash_col], errors="raise"),
            "market_value": pd.to_numeric(raw[market_value_col], errors="raise"),
        }
    )
    return data.sort_values("date").reset_index(drop=True)


def _pick_labels() -> tuple[str, dict[str, str]]:
    fonts = {font.name for font in font_manager.fontManager.ttflist}
    chinese_candidates = (
        "Microsoft YaHei",
        "SimHei",
        "Noto Sans CJK SC",
        "Source Han Sans SC",
        "Arial Unicode MS",
    )
    for font_name in chinese_candidates:
        if font_name in fonts:
            plt.rcParams["font.sans-serif"] = [font_name, "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False
            return font_name, {
                "title": "DummyRebalance哑策略·EXPLORATORY_TAINTED·当前HS300非时点成分·仅验证引擎非投资表现",
                "nav": "每日净值",
                "initial_nav": "起点净值 1,000,000",
                "value": "金额",
                "allocation": "现金与持仓市值",
                "cash": "现金",
                "market_value": "持仓市值",
                "year": "年份",
            }

    plt.rcParams["font.sans-serif"] = ["DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    return "DejaVu Sans", {
        "title": (
            "DummyRebalance dummy strategy | EXPLORATORY_TAINTED | "
            "current HS300 non-PIT universe | engine validation only, not investment performance"
        ),
        "nav": "Daily NAV",
        "initial_nav": "Initial NAV 1,000,000",
        "value": "Value",
        "allocation": "Cash and position market value",
        "cash": "Cash",
        "market_value": "Position market value",
        "year": "Year",
    }


def plot_nav(data: pd.DataFrame, output_png: Path) -> None:
    _, labels = _pick_labels()

    fig, (ax_nav, ax_stack) = plt.subplots(
        2,
        1,
        figsize=(14, 8),
        sharex=True,
        gridspec_kw={"height_ratios": [2.2, 1.0]},
    )

    ax_nav.plot(data["date"], data["nav"], color="#1f6f8b", linewidth=1.8, label=labels["nav"])
    ax_nav.axhline(
        INITIAL_NAV,
        color="#555555",
        linestyle="--",
        linewidth=1.0,
        alpha=0.8,
        label=labels["initial_nav"],
    )
    ax_nav.set_ylabel(labels["value"])
    ax_nav.yaxis.set_major_formatter(StrMethodFormatter("{x:,.0f}"))
    ax_nav.grid(True, color="#d9d9d9", linewidth=0.8, alpha=0.7)
    ax_nav.legend(loc="best", frameon=False)

    ax_stack.stackplot(
        data["date"],
        data["cash"],
        data["market_value"],
        labels=(labels["cash"], labels["market_value"]),
        colors=("#7fb069", "#d98f45"),
        alpha=0.9,
    )
    ax_stack.set_ylabel(labels["allocation"])
    ax_stack.set_xlabel(labels["year"])
    ax_stack.yaxis.set_major_formatter(StrMethodFormatter("{x:,.0f}"))
    ax_stack.grid(True, color="#d9d9d9", linewidth=0.8, alpha=0.7)
    ax_stack.legend(loc="upper left", frameon=False, ncol=2)

    start_year = int(data["date"].dt.year.min())
    end_year = int(data["date"].dt.year.max())
    ax_stack.set_xlim(pd.Timestamp(f"{start_year}-01-01"), pd.Timestamp(f"{end_year}-12-31"))
    ax_stack.xaxis.set_major_locator(mdates.YearLocator())
    ax_stack.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

    fig.suptitle(labels["title"], fontsize=14, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(output_png, dpi=180, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    data = _read_nav(INPUT_CSV)
    plot_nav(data, OUTPUT_PNG)
    print(OUTPUT_PNG)


if __name__ == "__main__":
    main()
