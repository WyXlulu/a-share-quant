from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterator

import akshare as ak
import pandas as pd
import requests


DEFAULT_REQUEST_TIMEOUT_SECONDS = 30
PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)
DIRECT_SESSION = requests.Session()
DIRECT_SESSION.trust_env = False


@dataclass(frozen=True)
class ConstituentsResult:
    symbols: list[str]
    source_id: str
    errors: list[str]


@dataclass(frozen=True)
class DailyBarsResult:
    frame: pd.DataFrame | None
    source_id: str | None = None
    failure_reason: str | None = None


@dataclass(frozen=True)
class CorporateActionsResult:
    frame: pd.DataFrame
    source_id: str
    errors: list[str]
    coverage_gaps: list[str]


@dataclass(frozen=True)
class AdapterFrameResult:
    frame: pd.DataFrame
    source_id: str
    errors: list[str]


def akshare_revision_id() -> str:
    return f"akshare-{ak.__version__}"


def _date_to_yyyymmdd(day: date) -> str:
    return day.strftime("%Y%m%d")


def _extract_six_digit_codes(frame: pd.DataFrame, candidate_columns: list[str]) -> list[str]:
    columns = [column for column in candidate_columns if column in frame.columns]
    if not columns:
        columns = list(frame.columns)

    best_codes: list[str] = []
    for column in columns:
        codes = (
            frame[column]
            .astype(str)
            .str.extract(r"(\d{6})", expand=False)
            .dropna()
            .drop_duplicates()
            .sort_values()
            .tolist()
        )
        if len(codes) > len(best_codes):
            best_codes = codes
    return best_codes


@contextmanager
def direct_data_requests() -> Iterator[None]:
    saved_env = {key: os.environ.get(key) for key in PROXY_ENV_KEYS}
    original_get = requests.get
    original_post = requests.post
    original_session_request = requests.sessions.Session.request

    def _request(
        session: requests.Session,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> requests.Response:
        previous_trust_env = session.trust_env
        session.trust_env = False
        kwargs.setdefault("timeout", DEFAULT_REQUEST_TIMEOUT_SECONDS)
        kwargs["proxies"] = {}
        try:
            return original_session_request(session, method, url, **kwargs)
        finally:
            session.trust_env = previous_trust_env

    def _get(url: str, params: Any = None, **kwargs: Any) -> requests.Response:
        return DIRECT_SESSION.request("GET", url, params=params, **kwargs)

    def _post(url: str, data: Any = None, json: Any = None, **kwargs: Any) -> requests.Response:
        return DIRECT_SESSION.request("POST", url, data=data, json=json, **kwargs)

    try:
        for key in PROXY_ENV_KEYS:
            os.environ.pop(key, None)
        DIRECT_SESSION.trust_env = False
        requests.sessions.Session.request = _request
        requests.get = _get
        requests.post = _post
        yield
    finally:
        requests.get = original_get
        requests.post = original_post
        requests.sessions.Session.request = original_session_request
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def fetch_hs300_constituents(index_symbol: str = "000300") -> ConstituentsResult:
    errors: list[str] = []
    sources = [
        (
            "akshare.index_stock_cons_csindex",
            lambda: ak.index_stock_cons_csindex(symbol=index_symbol),
            ["成分券代码"],
        ),
        (
            "akshare.index_stock_cons_sina",
            lambda: ak.index_stock_cons_sina(symbol=index_symbol),
            ["品种代码", "代码", "成分券代码"],
        ),
        (
            "akshare.index_stock_cons",
            lambda: ak.index_stock_cons(symbol=index_symbol),
            ["品种代码", "代码", "成分券代码"],
        ),
    ]

    with direct_data_requests():
        for source_id, loader, candidate_columns in sources:
            try:
                frame = loader()
                symbols = _extract_six_digit_codes(frame, candidate_columns)
                if symbols:
                    return ConstituentsResult(symbols=symbols, source_id=source_id, errors=errors)
                errors.append(f"{source_id}: no six-digit symbols found")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{source_id}: {type(exc).__name__}: {exc}")

    raise RuntimeError("Unable to fetch HS300 constituents: " + " | ".join(errors))


def _sina_symbol(symbol: str) -> str:
    prefix = "sh" if symbol.startswith("6") else "sz"
    return f"{prefix}{symbol}"


def fetch_stock_daily_raw(symbol: str, start: date, end: date) -> DailyBarsResult:
    errors: list[str] = []
    eastmoney_source_id = "akshare.stock_zh_a_hist"
    with direct_data_requests():
        try:
            frame = ak.stock_zh_a_hist(
                symbol=symbol,
                period="daily",
                start_date=_date_to_yyyymmdd(start),
                end_date=_date_to_yyyymmdd(end),
                adjust="",
                timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS,
            )
            if frame.empty:
                errors.append(f"{eastmoney_source_id}: empty result")
            else:
                return DailyBarsResult(
                    frame=normalize_vendor_daily_frame(symbol, frame),
                    source_id=eastmoney_source_id,
                )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{eastmoney_source_id}: {type(exc).__name__}: {exc}")

        sina_source_id = "akshare.stock_zh_a_daily_sina"
        try:
            frame = ak.stock_zh_a_daily(
                symbol=_sina_symbol(symbol),
                start_date=_date_to_yyyymmdd(start),
                end_date=_date_to_yyyymmdd(end),
                adjust="",
            )
            if frame.empty:
                errors.append(f"{sina_source_id}: empty result")
            else:
                return DailyBarsResult(
                    frame=normalize_sina_daily_frame(symbol, frame),
                    source_id=sina_source_id,
                )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{sina_source_id}: {type(exc).__name__}: {exc}")

    return DailyBarsResult(frame=None, failure_reason=" | ".join(errors))


def fetch_stock_corporate_actions(symbol: str, start: date, end: date) -> CorporateActionsResult:
    errors: list[str] = []
    coverage_gaps: list[str] = []
    frames: list[pd.DataFrame] = []
    observed_sources: list[str] = []
    provider_observed = False

    dividend_sources = [
        (
            "akshare.stock_fhps_detail_em",
            lambda: ak.stock_fhps_detail_em(symbol=symbol),
            _normalize_em_corporate_actions,
        ),
        (
            "akshare.stock_dividend_cninfo",
            lambda: ak.stock_dividend_cninfo(symbol=symbol),
            _normalize_cninfo_corporate_actions,
        ),
        (
            "akshare.stock_fhps_detail_ths",
            lambda: ak.stock_fhps_detail_ths(symbol=symbol),
            _normalize_ths_corporate_actions,
        ),
        (
            "akshare.stock_history_dividend_detail.dividend",
            lambda: ak.stock_history_dividend_detail(symbol=symbol, indicator="分红"),
            _normalize_sina_dividend_corporate_actions,
        ),
    ]

    with direct_data_requests():
        dividend_frame = None
        for source_id, loader, normalizer in dividend_sources:
            try:
                source_frame = loader()
                normalized = normalizer(symbol, source_frame, source_id)
                provider_observed = True
                normalized = _filter_corporate_actions_by_date(normalized, start, end)
                if normalized.empty:
                    errors.append(f"{source_id}: no dividend rows in requested date range")
                    continue
                dividend_frame = normalized
                observed_sources.append(source_id)
                break
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{source_id}: {type(exc).__name__}: {exc}")

        if dividend_frame is not None:
            frames.append(dividend_frame)

        rights_source_id = "akshare.stock_history_dividend_detail.rights"
        try:
            rights_raw = ak.stock_history_dividend_detail(symbol=symbol, indicator="配股")
            rights_frame = _normalize_sina_rights_corporate_actions(symbol, rights_raw, rights_source_id)
            provider_observed = True
            rights_frame = _filter_corporate_actions_by_date(rights_frame, start, end)
            if not rights_frame.empty:
                frames.append(rights_frame)
                observed_sources.append(rights_source_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{rights_source_id}: {type(exc).__name__}: {exc}")
            coverage_gaps.append("RIGHTS_ISSUE: Sina rights provider failed for this symbol")

    if frames:
        combined = pd.concat(frames, ignore_index=True)
        combined = combined.drop_duplicates(
            subset=[
                "security_id",
                "ex_date",
                "action_type",
                "cash_dividend_per_share",
                "share_ratio",
            ],
            keep="last",
        ).sort_values(["security_id", "ex_date", "action_type"])
    else:
        combined = _empty_corporate_actions_frame()

    return CorporateActionsResult(
        frame=combined.reset_index(drop=True),
        source_id=";".join(dict.fromkeys(observed_sources))
        if observed_sources
        else ("NO_ACTIONS_OBSERVED" if provider_observed else "UNAVAILABLE"),
        errors=errors,
        coverage_gaps=list(dict.fromkeys(coverage_gaps)),
    )


def _empty_corporate_actions_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "security_id",
            "ex_date",
            "action_type",
            "cash_dividend_per_share",
            "share_ratio",
            "record_date",
            "announcement_date",
            "action_description",
            "source_id",
        ]
    )


def _filter_corporate_actions_by_date(frame: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    if frame.empty:
        return frame
    ex_dates = pd.to_datetime(frame["ex_date"], errors="coerce").dt.date
    return frame.loc[ex_dates.between(start, end)].copy()


def _normalize_em_corporate_actions(
    symbol: str, frame: pd.DataFrame, source_id: str
) -> pd.DataFrame:
    if frame.empty:
        return _empty_corporate_actions_frame()

    cash_per_10 = _numeric_column(frame, "现金分红-现金分红比例")
    share_per_10 = _numeric_column(frame, "送转股份-送股比例").fillna(0) + _numeric_column(
        frame, "送转股份-转股比例"
    ).fillna(0)
    if share_per_10.isna().all():
        share_per_10 = _numeric_column(frame, "送转股份-送转总比例")
    return _build_normalized_corporate_actions(
        symbol=symbol,
        ex_date=frame["除权除息日"],
        cash_per_10=cash_per_10,
        share_per_10=share_per_10,
        source_id=source_id,
        record_date=frame.get("股权登记日"),
        announcement_date=frame.get("最新公告日期"),
        description=frame.get("现金分红-现金分红比例描述"),
    )


def _normalize_cninfo_corporate_actions(
    symbol: str, frame: pd.DataFrame, source_id: str
) -> pd.DataFrame:
    if frame.empty:
        return _empty_corporate_actions_frame()

    share_per_10 = _numeric_column(frame, "送股比例").fillna(0) + _numeric_column(
        frame, "转增比例"
    ).fillna(0)
    return _build_normalized_corporate_actions(
        symbol=symbol,
        ex_date=frame["除权日"],
        cash_per_10=_numeric_column(frame, "派息比例"),
        share_per_10=share_per_10,
        source_id=source_id,
        record_date=frame.get("股权登记日"),
        announcement_date=frame.get("实施方案公告日期"),
        description=frame.get("实施方案分红说明"),
    )


def _normalize_ths_corporate_actions(
    symbol: str, frame: pd.DataFrame, source_id: str
) -> pd.DataFrame:
    if frame.empty:
        return _empty_corporate_actions_frame()

    description = frame.get("分红方案说明", pd.Series([""] * len(frame), index=frame.index))
    sent_per_10 = description.astype(str).str.extract(r"送\s*([0-9.]+)", expand=False)
    transfer_per_10 = description.astype(str).str.extract(r"转(?:增)?\s*([0-9.]+)", expand=False)
    cash_per_10 = description.astype(str).str.extract(r"派\s*([0-9.]+)\s*元", expand=False)
    return _build_normalized_corporate_actions(
        symbol=symbol,
        ex_date=frame["A股除权除息日"],
        cash_per_10=pd.to_numeric(cash_per_10, errors="coerce"),
        share_per_10=(
            pd.to_numeric(sent_per_10, errors="coerce").fillna(0)
            + pd.to_numeric(transfer_per_10, errors="coerce").fillna(0)
        ),
        source_id=source_id,
        record_date=frame.get("A股股权登记日"),
        announcement_date=frame.get("实施公告日"),
        description=description,
    )


def _normalize_sina_dividend_corporate_actions(
    symbol: str, frame: pd.DataFrame, source_id: str
) -> pd.DataFrame:
    if frame.empty:
        return _empty_corporate_actions_frame()

    return _build_normalized_corporate_actions(
        symbol=symbol,
        ex_date=frame["除权除息日"],
        cash_per_10=_numeric_column(frame, "派息"),
        share_per_10=_numeric_column(frame, "送股").fillna(0) + _numeric_column(
            frame, "转增"
        ).fillna(0),
        source_id=source_id,
        record_date=frame.get("股权登记日"),
        announcement_date=frame.get("公告日期"),
        description=frame.get("进度"),
    )


def _normalize_sina_rights_corporate_actions(
    symbol: str, frame: pd.DataFrame, source_id: str
) -> pd.DataFrame:
    if frame.empty:
        return _empty_corporate_actions_frame()

    normalized = _build_normalized_corporate_actions(
        symbol=symbol,
        ex_date=frame["除权日"],
        cash_per_10=pd.Series([0.0] * len(frame), index=frame.index),
        share_per_10=_numeric_column(frame, "配股方案"),
        source_id=source_id,
        record_date=frame.get("股权登记日"),
        announcement_date=frame.get("公告日期"),
        description=None,
        forced_action_type="RIGHTS_ISSUE",
    )
    if "配股价格" in frame.columns:
        normalized["rights_price_per_share"] = pd.to_numeric(frame["配股价格"], errors="coerce")
    return normalized


def _numeric_column(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        return pd.Series([pd.NA] * len(frame), index=frame.index, dtype="Float64")
    return pd.to_numeric(frame[column], errors="coerce")


def _build_normalized_corporate_actions(
    *,
    symbol: str,
    ex_date: pd.Series,
    cash_per_10: pd.Series,
    share_per_10: pd.Series,
    source_id: str,
    record_date: pd.Series | None,
    announcement_date: pd.Series | None,
    description: pd.Series | None,
    forced_action_type: str | None = None,
) -> pd.DataFrame:
    normalized = pd.DataFrame(
        {
            "security_id": str(symbol).zfill(6),
            "ex_date": pd.to_datetime(ex_date, errors="coerce").dt.date,
            "cash_dividend_per_share": (pd.to_numeric(cash_per_10, errors="coerce") / 10).fillna(0),
            "share_ratio": (pd.to_numeric(share_per_10, errors="coerce") / 10).fillna(0),
            "source_id": source_id,
        }
    )
    normalized["record_date"] = (
        pd.to_datetime(record_date, errors="coerce").dt.date if record_date is not None else pd.NaT
    )
    normalized["announcement_date"] = (
        pd.to_datetime(announcement_date, errors="coerce").dt.date
        if announcement_date is not None
        else pd.NaT
    )
    normalized["action_description"] = (
        description.astype(str) if description is not None else pd.Series([""] * len(normalized))
    )
    normalized["action_type"] = normalized.apply(
        lambda row: forced_action_type
        or _classify_corporate_action(row["cash_dividend_per_share"], row["share_ratio"]),
        axis=1,
    )
    normalized = normalized.dropna(subset=["ex_date"])
    has_effect = normalized["cash_dividend_per_share"].gt(0) | normalized["share_ratio"].gt(0)
    normalized = normalized.loc[has_effect | normalized["action_type"].eq("RIGHTS_ISSUE")].copy()
    return normalized[
        [
            "security_id",
            "ex_date",
            "action_type",
            "cash_dividend_per_share",
            "share_ratio",
            "record_date",
            "announcement_date",
            "action_description",
            "source_id",
        ]
    ]


def _classify_corporate_action(cash_dividend_per_share: object, share_ratio: object) -> str:
    cash = float(cash_dividend_per_share or 0)
    shares = float(share_ratio or 0)
    if shares > 0:
        return "STOCK_DIVIDEND"
    if cash > 0:
        return "CASH_DIVIDEND"
    return "OTHER"


def normalize_vendor_daily_frame(symbol: str | None, frame: pd.DataFrame) -> pd.DataFrame:
    column_map = {
        "日期": "trade_date",
        "开盘": "open",
        "最高": "high",
        "最低": "low",
        "收盘": "close",
        "成交量": "volume",
        "成交额": "amount",
    }
    missing = [column for column in column_map if column not in frame.columns]
    if missing:
        raise ValueError(f"{symbol} missing source columns: {missing}")

    normalized = frame[list(column_map)].rename(columns=column_map).copy()
    if symbol is None:
        codes = _extract_six_digit_codes(frame, ["股票代码"])
        if len(codes) == 1:
            normalized["security_id"] = codes[0]
        elif "股票代码" in frame.columns:
            normalized["security_id"] = (
                frame["股票代码"].astype(str).str.extract(r"(\d{6})", expand=False).str.zfill(6)
            )
        else:
            raise ValueError("symbol is required when vendor frame has no security code column")
    else:
        normalized["security_id"] = symbol
    normalized["trade_date"] = pd.to_datetime(normalized["trade_date"], errors="raise").dt.date

    numeric_columns = ["open", "high", "low", "close", "volume", "amount"]
    for column in numeric_columns:
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")

    return normalized.drop_duplicates(subset=["security_id", "trade_date"], keep="last")


def normalize_sina_daily_frame(symbol: str, frame: pd.DataFrame) -> pd.DataFrame:
    required_columns = ["date", "open", "high", "low", "close", "volume", "amount"]
    missing = [column for column in required_columns if column not in frame.columns]
    if missing:
        raise ValueError(f"{symbol} missing sina columns: {missing}")

    normalized = frame[required_columns].rename(columns={"date": "trade_date"}).copy()
    normalized["security_id"] = symbol
    normalized["trade_date"] = pd.to_datetime(normalized["trade_date"], errors="raise").dt.date

    numeric_columns = ["open", "high", "low", "close", "volume", "amount"]
    for column in numeric_columns:
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")

    return normalized.drop_duplicates(subset=["security_id", "trade_date"], keep="last")


def fetch_exchange_trade_dates() -> list[date]:
    with direct_data_requests():
        raw_calendar = ak.tool_trade_date_hist_sina()
    if "trade_date" not in raw_calendar.columns:
        raise ValueError(f"akshare calendar missing trade_date: {list(raw_calendar.columns)}")
    return pd.to_datetime(raw_calendar["trade_date"], errors="raise").dt.date.tolist()


def fetch_current_a_share_names() -> AdapterFrameResult:
    with direct_data_requests():
        frame = ak.stock_info_a_code_name()

    normalized = frame.rename(columns={"code": "security_id", "name": "name"}).copy()
    normalized["security_id"] = (
        normalized["security_id"].astype(str).str.extract(r"(\d{6})", expand=False).str.zfill(6)
    )
    normalized = normalized[["security_id", "name"]].dropna(subset=["security_id"])
    return AdapterFrameResult(
        frame=normalized.drop_duplicates(subset=["security_id"], keep="last"),
        source_id="akshare.stock_info_a_code_name",
        errors=[],
    )


def _normalize_exchange_listing_frame(
    frame: pd.DataFrame,
    source_id: str,
    code_column: str,
    name_column: str,
    list_date_column: str,
    board: str | None = None,
    board_column: str | None = None,
) -> pd.DataFrame:
    normalized = pd.DataFrame(
        {
            "security_id": frame[code_column]
            .astype(str)
            .str.extract(r"(\d{6})", expand=False)
            .str.zfill(6),
            "exchange_name": frame[name_column].astype(str),
            "list_date": pd.to_datetime(frame[list_date_column], errors="coerce").dt.date,
            "list_date_source_id": source_id,
        }
    )
    if board_column and board_column in frame.columns:
        normalized["exchange_board"] = frame[board_column].astype(str)
    else:
        normalized["exchange_board"] = board
    return normalized.dropna(subset=["security_id"]).drop_duplicates(
        subset=["security_id"], keep="last"
    )


def fetch_exchange_listing_info() -> AdapterFrameResult:
    errors: list[str] = []
    frames: list[pd.DataFrame] = []

    with direct_data_requests():
        sources = [
            (
                "akshare.stock_info_sh_name_code.main_board_a",
                lambda: ak.stock_info_sh_name_code(symbol="主板A股"),
                "证券代码",
                "证券简称",
                "上市日期",
                "主板",
                None,
            ),
            (
                "akshare.stock_info_sh_name_code.star_market",
                lambda: ak.stock_info_sh_name_code(symbol="科创板"),
                "证券代码",
                "证券简称",
                "上市日期",
                "科创板",
                None,
            ),
            (
                "akshare.stock_info_sz_name_code.a_share",
                lambda: ak.stock_info_sz_name_code(symbol="A股列表"),
                "A股代码",
                "A股简称",
                "A股上市日期",
                None,
                "板块",
            ),
            (
                "akshare.stock_info_bj_name_code",
                ak.stock_info_bj_name_code,
                "证券代码",
                "证券简称",
                "上市日期",
                "北交所",
                None,
            ),
        ]

        for source_id, loader, code_col, name_col, list_col, board, board_col in sources:
            try:
                source_frame = loader()
                frames.append(
                    _normalize_exchange_listing_frame(
                        source_frame,
                        source_id=source_id,
                        code_column=code_col,
                        name_column=name_col,
                        list_date_column=list_col,
                        board=board,
                        board_column=board_col,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{source_id}: {type(exc).__name__}: {exc}")

    if frames:
        combined = pd.concat(frames, ignore_index=True).drop_duplicates(
            subset=["security_id"], keep="last"
        )
    else:
        combined = pd.DataFrame(
            columns=[
                "security_id",
                "exchange_name",
                "list_date",
                "list_date_source_id",
                "exchange_board",
            ]
        )

    return AdapterFrameResult(
        frame=combined,
        source_id="akshare.exchange_listing_info",
        errors=errors,
    )


def fetch_current_st_symbols() -> AdapterFrameResult:
    errors: list[str] = []
    sources = [
        (
            "akshare.stock_zh_a_st_em",
            ak.stock_zh_a_st_em,
            ["代码", "股票代码", "证券代码"],
            [],
            "direct_st_list",
        ),
        (
            "akshare.stock_zh_a_spot_em.name_scan",
            ak.stock_zh_a_spot_em,
            ["代码", "股票代码", "证券代码"],
            ["名称", "股票简称", "证券简称"],
            "name_scan",
        ),
        (
            "akshare.stock_info_a_code_name.name_scan",
            ak.stock_info_a_code_name,
            ["code"],
            ["name"],
            "name_scan",
        ),
    ]

    with direct_data_requests():
        for source_id, loader, code_columns, name_columns, mode in sources:
            try:
                frame = loader()
                if mode == "direct_st_list":
                    symbols = _extract_six_digit_codes(frame, code_columns)
                    if symbols:
                        return AdapterFrameResult(
                            frame=pd.DataFrame({"security_id": symbols}),
                            source_id=source_id,
                            errors=errors,
                        )
                    errors.append(f"{source_id}: no ST symbols parsed")
                    continue

                normalized = _normalize_code_name_frame(frame, code_columns, name_columns)
                normalized["is_st"] = normalized["name"].map(_is_st_name)
                st_frame = normalized.loc[normalized["is_st"], ["security_id"]].copy()
                return AdapterFrameResult(frame=st_frame, source_id=source_id, errors=errors)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{source_id}: {type(exc).__name__}: {exc}")

    return AdapterFrameResult(
        frame=pd.DataFrame(columns=["security_id"]),
        source_id="UNAVAILABLE",
        errors=errors,
    )


def fetch_current_status_overrides() -> AdapterFrameResult:
    errors: list[str] = []
    frames: list[pd.DataFrame] = []
    successful_sources: list[str] = []

    with direct_data_requests():
        try:
            source_id = "akshare.stock_zh_a_stop_em"
            frame = ak.stock_zh_a_stop_em()
            symbols = _extract_six_digit_codes(frame, ["代码", "股票代码", "证券代码"])
            if symbols:
                frames.append(pd.DataFrame({"security_id": symbols, "status": "停牌"}))
            successful_sources.append(source_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{source_id}: {type(exc).__name__}: {exc}")

        try:
            source_id = "akshare.stock_info_a_code_name.delist_name_scan"
            frame = ak.stock_info_a_code_name()
            normalized = _normalize_code_name_frame(frame, ["code"], ["name"])
            delisted = normalized.loc[
                normalized["name"].map(_is_delisted_name), ["security_id"]
            ].copy()
            if not delisted.empty:
                delisted["status"] = "退市"
                frames.append(delisted)
            successful_sources.append(source_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{source_id}: {type(exc).__name__}: {exc}")

    if frames:
        combined = pd.concat(frames, ignore_index=True).drop_duplicates(
            subset=["security_id"], keep="last"
        )
    else:
        combined = pd.DataFrame(columns=["security_id", "status"])

    return AdapterFrameResult(
        frame=combined,
        source_id=";".join(successful_sources) if successful_sources else "UNAVAILABLE",
        errors=errors,
    )


def fetch_current_suspended_symbols() -> AdapterFrameResult:
    status_overrides = fetch_current_status_overrides()
    suspended = status_overrides.frame.loc[
        status_overrides.frame["status"].eq("停牌"), ["security_id"]
    ].copy()
    return AdapterFrameResult(
        frame=suspended,
        source_id=status_overrides.source_id,
        errors=status_overrides.errors,
    )


def _normalize_code_name_frame(
    frame: pd.DataFrame,
    code_columns: list[str],
    name_columns: list[str],
) -> pd.DataFrame:
    code_column = next((column for column in code_columns if column in frame.columns), None)
    name_column = next((column for column in name_columns if column in frame.columns), None)
    if code_column is None or name_column is None:
        raise ValueError(
            f"missing code/name columns; columns={list(frame.columns)}, "
            f"code_candidates={code_columns}, name_candidates={name_columns}"
        )

    normalized = pd.DataFrame(
        {
            "security_id": frame[code_column]
            .astype(str)
            .str.extract(r"(\d{6})", expand=False)
            .str.zfill(6),
            "name": frame[name_column].astype(str),
        }
    )
    return normalized.dropna(subset=["security_id"]).drop_duplicates(
        subset=["security_id"], keep="last"
    )


def _is_st_name(name: object) -> bool:
    if pd.isna(name):
        return False
    normalized = str(name).upper().replace(" ", "")
    return normalized.startswith("ST") or normalized.startswith("*ST")


def _is_delisted_name(name: object) -> bool:
    if pd.isna(name):
        return False
    normalized = str(name).upper().replace(" ", "")
    return "退" in normalized
