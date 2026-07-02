from __future__ import annotations

from pathlib import Path

import pandas as pd


RAW_PATH = Path("data/raw/hs300_daily_10y.parquet")
QUALITY_DIR = Path("data/quality")
DETAIL_PATH = QUALITY_DIR / "hs300_10y_quality.csv"
REPORT_PATH = QUALITY_DIR / "hs300_10y_report.md"

DATE_COL = "日期"
CODE_COL = "股票代码"
OPEN_COL = "开盘"
CLOSE_COL = "收盘"
HIGH_COL = "最高"
LOW_COL = "最低"
VOLUME_COL = "成交量"
PCT_CHANGE_COL = "涨跌幅"
KEY_FIELDS = [OPEN_COL, CLOSE_COL, HIGH_COL, LOW_COL, VOLUME_COL]
REQUIRED_COLUMNS = [DATE_COL, CODE_COL, *KEY_FIELDS, PCT_CHANGE_COL]

ABS_DAILY_PCT_LIMIT = 21.0
CLOSE_TO_PREVIOUS_LIMIT = 0.50
FULL_HISTORY_YEARS = 10.0
MID_HISTORY_YEARS = 5.0
SHORT_HISTORY_YEARS = 2.0

FLAG_COLUMNS = [
    "收盘价<=0天数",
    "成交量=0天数",
    "单日涨跌幅绝对值>21%天数",
    "收盘价较前日变动>50%天数",
    "重复日期条数",
    "关键字段NaN条数",
]
SEVERITY_WEIGHTS = {
    "收盘价<=0天数": 1000,
    "关键字段NaN条数": 1000,
    "重复日期条数": 500,
    "收盘价较前日变动>50%天数": 100,
    "单日涨跌幅绝对值>21%天数": 50,
    "成交量=0天数": 1,
}


def read_raw_data() -> pd.DataFrame:
    if not RAW_PATH.exists():
        raise FileNotFoundError(f"Raw data not found: {RAW_PATH}")

    data = pd.read_parquet(RAW_PATH)
    missing_columns = [column for column in REQUIRED_COLUMNS if column not in data.columns]
    if missing_columns:
        raise ValueError(f"Raw data missing required columns: {missing_columns}")

    data = data.copy()
    data[DATE_COL] = pd.to_datetime(data[DATE_COL], errors="coerce")
    data[CODE_COL] = data[CODE_COL].astype(str).str.extract(r"(\d{6})", expand=False)

    numeric_columns = [*KEY_FIELDS, PCT_CHANGE_COL]
    for column in numeric_columns:
        data[column] = pd.to_numeric(data[column], errors="coerce")

    return data


def count_close_jumps(group: pd.DataFrame) -> int:
    previous_close = group[CLOSE_COL].shift(1)
    relative_change = (group[CLOSE_COL] / previous_close - 1).abs()
    return int(relative_change.gt(CLOSE_TO_PREVIOUS_LIMIT).sum())


def flag_items(record: dict[str, object]) -> list[str]:
    return [column for column in FLAG_COLUMNS if int(record[column]) > 0]


def severity_score(record: dict[str, object]) -> int:
    return sum(int(record[column]) * weight for column, weight in SEVERITY_WEIGHTS.items())


def build_quality_detail(data: pd.DataFrame) -> pd.DataFrame:
    records: list[dict[str, object]] = []

    for code, group in data.groupby(CODE_COL, dropna=False):
        group = group.sort_values(DATE_COL).reset_index(drop=True)
        valid_dates = group[DATE_COL].dropna()
        start_date = valid_dates.min()
        end_date = valid_dates.max()

        if pd.isna(start_date) or pd.isna(end_date):
            coverage_years = float("nan")
            start_text = ""
            end_text = ""
        else:
            coverage_years = (end_date - start_date).days / 365
            start_text = start_date.strftime("%Y-%m-%d")
            end_text = end_date.strftime("%Y-%m-%d")

        record: dict[str, object] = {
            "股票代码": code,
            "起始日期": start_text,
            "结束日期": end_text,
            "行数": len(group),
            "覆盖年数": round(coverage_years, 2),
            "收盘价<=0天数": int(group[CLOSE_COL].le(0).sum()),
            "成交量=0天数": int(group[VOLUME_COL].eq(0).sum()),
            "单日涨跌幅绝对值>21%天数": int(
                group[PCT_CHANGE_COL].abs().gt(ABS_DAILY_PCT_LIMIT).sum()
            ),
            "收盘价较前日变动>50%天数": count_close_jumps(group),
            "重复日期条数": int(group.duplicated(subset=[DATE_COL], keep=False).sum()),
            "关键字段NaN条数": int(group[KEY_FIELDS].isna().any(axis=1).sum()),
        }
        items = flag_items(record)
        record["任意红旗"] = bool(items)
        record["红旗项"] = "; ".join(items)
        record["红旗分数"] = severity_score(record)
        records.append(record)

    detail = pd.DataFrame(records)
    return detail.sort_values("股票代码").reset_index(drop=True)


def build_summary(detail: pd.DataFrame, total_rows: int) -> dict[str, int]:
    coverage = detail["覆盖年数"]
    return {
        "总股票数": int(len(detail)),
        "总行数": int(total_rows),
        "满10年股票数": int(coverage.ge(FULL_HISTORY_YEARS).sum()),
        "5-10年股票数": int(
            (coverage.ge(MID_HISTORY_YEARS) & coverage.lt(FULL_HISTORY_YEARS)).sum()
        ),
        "不足5年股票数": int(coverage.lt(MID_HISTORY_YEARS).sum()),
        "不足2年股票数": int(coverage.lt(SHORT_HISTORY_YEARS).sum()),
        "任意红旗股票数": int(detail["任意红旗"].sum()),
    }


def top_problem_stocks(detail: pd.DataFrame, limit: int = 15) -> pd.DataFrame:
    sort_columns = [
        "红旗分数",
        "关键字段NaN条数",
        "收盘价<=0天数",
        "重复日期条数",
        "收盘价较前日变动>50%天数",
        "单日涨跌幅绝对值>21%天数",
        "成交量=0天数",
    ]
    return (
        detail[detail["红旗分数"].gt(0)]
        .sort_values(sort_columns, ascending=False)
        .head(limit)
        .reset_index(drop=True)
    )


def frame_to_markdown(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "无"
    return frame.to_markdown(index=False)


def build_report(
    detail: pd.DataFrame,
    summary: dict[str, int],
    top_15: pd.DataFrame,
) -> str:
    red_flags = detail[detail["任意红旗"]].sort_values(
        ["红旗分数", "股票代码"], ascending=[False, True]
    )
    summary_frame = pd.DataFrame(
        [{"指标": key, "数值": value} for key, value in summary.items()]
    )

    top_columns = [
        "股票代码",
        "起始日期",
        "结束日期",
        "行数",
        "覆盖年数",
        "红旗分数",
        "红旗项",
        *FLAG_COLUMNS,
    ]

    lines = [
        "# 沪深300十年日线数据质检报告",
        "",
        f"- 原始数据: `{RAW_PATH.as_posix()}`",
        f"- 逐只明细: `{DETAIL_PATH.as_posix()}`",
        f"- 报告输出: `{REPORT_PATH.as_posix()}`",
        "",
        "## 全局摘要",
        "",
        frame_to_markdown(summary_frame),
        "",
        "## 判定阈值说明",
        "",
        "- 覆盖年数 = (结束日期 - 起始日期).days / 365。",
        f"- 满10年股票: 覆盖年数 >= {FULL_HISTORY_YEARS:.1f}。",
        f"- 5-10年股票: {MID_HISTORY_YEARS:.1f} <= 覆盖年数 < {FULL_HISTORY_YEARS:.1f}。",
        f"- 不足5年股票: 覆盖年数 < {MID_HISTORY_YEARS:.1f}。",
        f"- 不足2年股票: 覆盖年数 < {SHORT_HISTORY_YEARS:.1f}; 该计数包含在不足5年内。",
        "- 收盘价<=0天数: `收盘 <= 0` 的行数。",
        "- 成交量=0天数: `成交量 == 0` 的行数，作为停牌嫌疑红旗。",
        f"- 单日涨跌幅绝对值>21%天数: `abs(涨跌幅) > {ABS_DAILY_PCT_LIMIT:.1f}` 的行数。",
        f"- 收盘价较前日变动>50%天数: `abs(收盘 / 前一交易日收盘 - 1) > {CLOSE_TO_PREVIOUS_LIMIT:.0%}` 的行数。",
        "- 重复日期条数: 同一股票内日期重复的全部相关行数。",
        "- 关键字段NaN条数: 开盘/收盘/最高/最低/成交量任一字段为 NaN 的行数。",
        "- 任意红旗: 以上六类红旗任一计数大于 0。",
        "- 红旗分数 = 收盘价<=0*1000 + 关键字段NaN*1000 + 重复日期*500 + 收盘价较前日变动>50%*100 + 单日涨跌幅绝对值>21%*50 + 成交量=0*1。",
        "",
        "## Top 15 问题股票",
        "",
        frame_to_markdown(top_15[top_columns]),
        "",
        "## 红旗清单",
        "",
        frame_to_markdown(red_flags[top_columns]),
        "",
    ]
    return "\n".join(lines)


def print_summary(summary: dict[str, int], top_15: pd.DataFrame) -> None:
    print("global_summary")
    for key, value in summary.items():
        print(f"{key}={value}")

    if top_15.empty:
        print("top_problem_stocks=[]")
        return

    print("top_problem_stocks=" + ",".join(top_15["股票代码"].astype(str).tolist()))


def main() -> None:
    data = read_raw_data()
    detail = build_quality_detail(data)
    summary = build_summary(detail, total_rows=len(data))
    top_15 = top_problem_stocks(detail)

    QUALITY_DIR.mkdir(parents=True, exist_ok=True)
    detail.to_csv(DETAIL_PATH, index=False, encoding="utf-8-sig")
    REPORT_PATH.write_text(build_report(detail, summary, top_15), encoding="utf-8")

    print_summary(summary, top_15)
    print(f"detail_saved={DETAIL_PATH}")
    print(f"report_saved={REPORT_PATH}")


if __name__ == "__main__":
    main()
