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
