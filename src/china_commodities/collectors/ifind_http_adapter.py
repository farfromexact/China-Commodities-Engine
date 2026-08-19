"""Read-only iFinD Quant API adapter for Chinese commodity futures."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
import json
import os
import time
from typing import Any
import urllib.error
import urllib.request

import pandas as pd

from .ifind_adapter import IFIND_FUTURES_FIELDS, contract_to_ifind_code


DEFAULT_BASE_URL = "https://quantapi.51ifind.com/api/v1"
DEFAULT_HISTORY_BATCH_SIZE = 400
DEFAULT_RANGE_BATCH_SIZE = 20


class IFindHTTPError(RuntimeError):
    """Raised for Quant API authentication, entitlement, or schema errors."""


def _chunks(values: Sequence[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield list(values[start : start + size])


def generate_contract_candidates(
    trade_date: str,
    exchange: str,
    products: Sequence[str],
    *,
    history_years: int = 1,
    forward_years: int = 4,
) -> list[str]:
    """Generate a broad concrete-contract universe without another data vendor.

    Invalid or unlisted codes are harmless: iFinD returns empty market fields and
    ``collect_futures_daily`` removes those rows.  Zhengzhou uses its three-digit
    exchange contract convention; the other exchanges use four digits.
    """

    if history_years < 0 or forward_years < 0:
        raise ValueError("candidate year windows must be non-negative")
    normalized_exchange = str(exchange).upper()
    if normalized_exchange not in {"SHFE", "INE", "DCE", "CZCE", "GFEX"}:
        raise ValueError(f"unsupported iFinD commodity exchange: {exchange}")
    year = pd.Timestamp(trade_date).year
    contracts: list[str] = []
    for product in sorted({str(value).strip().upper() for value in products if value}):
        for contract_year in range(year - history_years, year + forward_years + 1):
            for month in range(1, 13):
                if normalized_exchange == "CZCE":
                    suffix = f"{contract_year % 10}{month:02d}"
                else:
                    suffix = f"{contract_year % 100:02d}{month:02d}"
                contracts.append(f"{product}{suffix}")
    return contracts


def _safe_message(value: Any) -> str:
    return str(value or "unknown iFinD HTTP error")[:500]


def _default_transport(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None,
    timeout: int,
) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Accept", "application/json")
    request.add_header("Connection", "close")
    request.add_header("Content-Type", "application/json")
    request.add_header("User-Agent", "china-commodities-engine-ifind/0.1")
    for key, value in headers.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        raise IFindHTTPError(
            f"iFinD HTTP {exc.code}: {_safe_message(text)}"
        ) from exc
    except Exception as exc:
        raise IFindHTTPError(
            f"iFinD transport failed: {type(exc).__name__}: {_safe_message(exc)}"
        ) from exc
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise IFindHTTPError("iFinD response was not valid JSON") from exc
    if not isinstance(result, dict):
        raise IFindHTTPError("iFinD response JSON was not an object")
    return result


def _raise_api_error(endpoint: str, response: dict[str, Any]) -> None:
    error_code = response.get("errorcode", response.get("errcode"))
    if error_code not in (0, "0", None):
        message = response.get("errmsg", response.get("message"))
        raise IFindHTTPError(
            f"iFinD {endpoint} failed with code {error_code}: {_safe_message(message)}"
        )


def _tables_frame(response: dict[str, Any]) -> pd.DataFrame:
    tables = response.get("tables") or response.get("data") or []
    if isinstance(tables, dict):
        tables = [tables]
    rows: list[dict[str, Any]] = []
    for table in tables:
        if not isinstance(table, dict):
            continue
        code = table.get("thscode") or table.get("code")
        times = table.get("time") or []
        if not isinstance(times, list):
            times = [times]
        data = table.get("table") or table.get("data") or {}
        if not isinstance(data, dict):
            continue
        list_lengths = [len(value) for value in data.values() if isinstance(value, list)]
        row_count = max(list_lengths + [len(times), 1])
        for index in range(row_count):
            row: dict[str, Any] = {"thscode": code}
            if times:
                row["time"] = times[index] if index < len(times) else times[-1]
            for field, value in data.items():
                if isinstance(value, list):
                    row[field] = value[index] if index < len(value) else None
                else:
                    row[field] = value
            rows.append(row)
    return pd.DataFrame(rows)


@dataclass
class IFindHTTPClient:
    """Short-lived Quant API client; tokens remain in memory only."""

    refresh_token: str | None = None
    access_token: str | None = None
    base_url: str = DEFAULT_BASE_URL
    timeout: int = 45
    transport: Callable[
        [str, dict[str, str], dict[str, Any] | None, int], dict[str, Any]
    ] = _default_transport

    def get_access_token(self) -> str:
        if self.access_token:
            return self.access_token
        refresh_token = self.refresh_token or os.environ.get("IFIND_REFRESH_TOKEN")
        if not refresh_token:
            raise IFindHTTPError(
                "iFinD refresh token is required in process environment variable "
                "IFIND_REFRESH_TOKEN"
            )
        response = self.transport(
            f"{self.base_url}/get_access_token",
            {"refresh_token": refresh_token},
            None,
            self.timeout,
        )
        _raise_api_error("get_access_token", response)
        token = (response.get("data") or {}).get("access_token")
        if not token:
            raise IFindHTTPError("iFinD get_access_token returned no access token")
        self.access_token = str(token)
        return self.access_token

    def request(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.transport(
            f"{self.base_url}/{endpoint}",
            {"access_token": self.get_access_token(), "ifindlang": "cn"},
            payload,
            self.timeout,
        )
        _raise_api_error(endpoint, response)
        return response

    def history_quotes(
        self,
        codes: Sequence[str],
        fields: Sequence[str],
        trade_date: str,
    ) -> pd.DataFrame:
        return self.history_quotes_range(
            codes,
            fields,
            start_date=trade_date,
            end_date=trade_date,
        )

    def history_quotes_range(
        self,
        codes: Sequence[str],
        fields: Sequence[str],
        *,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        response = self.request(
            "cmd_history_quotation",
            {
                "codes": ",".join(codes),
                "indicators": ",".join(fields),
                "startdate": start_date,
                "enddate": end_date,
                "functionpara": {"Fill": "Omit"},
            },
        )
        return _tables_frame(response)

    def realtime_quotes(
        self,
        codes: Sequence[str],
        fields: Sequence[str],
    ) -> pd.DataFrame:
        response = self.request(
            "real_time_quotation",
            {"codes": ",".join(codes), "indicators": ",".join(fields)},
        )
        return _tables_frame(response)


def collect_futures_history(
    start_date: str,
    end_date: str,
    exchange: str,
    contracts: Sequence[str],
    *,
    client: IFindHTTPClient,
    fields: Sequence[str] = IFIND_FUTURES_FIELDS,
    batch_size: int = DEFAULT_HISTORY_BATCH_SIZE,
    request_interval_seconds: float = 0.0,
) -> pd.DataFrame:
    """Collect a Quant API date range and map it to the existing raw schema."""

    codes = [contract_to_ifind_code(contract, exchange) for contract in contracts]
    if not codes:
        return pd.DataFrame()
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if request_interval_seconds < 0:
        raise ValueError("request interval cannot be negative")
    last_request_at = 0.0

    def query(batch: Sequence[str]) -> list[pd.DataFrame]:
        nonlocal last_request_at
        if request_interval_seconds and last_request_at:
            remaining = request_interval_seconds - (time.monotonic() - last_request_at)
            if remaining > 0:
                time.sleep(remaining)
        last_request_at = time.monotonic()
        try:
            if start_date == end_date:
                frame = client.history_quotes(batch, fields, start_date)
            else:
                frame = client.history_quotes_range(
                    batch,
                    fields,
                    start_date=start_date,
                    end_date=end_date,
                )
            return [frame]
        except IFindHTTPError as exc:
            if "code -4210" not in str(exc) or len(batch) <= 1:
                raise
            midpoint = len(batch) // 2
            return query(batch[:midpoint]) + query(batch[midpoint:])

    frames: list[pd.DataFrame] = []
    for batch in _chunks(codes, batch_size):
        frames.extend(query(batch))
    frames = [frame for frame in frames if not frame.empty]
    raw = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if raw.empty:
        return raw
    required = {"thscode", "time"}
    missing = sorted(required.difference(raw.columns))
    if missing:
        raise IFindHTTPError(
            f"iFinD history response missing columns: {', '.join(missing)}"
        )
    market_columns = [field for field in fields if field in raw.columns]
    if not market_columns:
        raise IFindHTTPError("iFinD history response contained no requested fields")
    raw = raw.loc[raw[market_columns].notna().any(axis=1)].copy()
    if raw.empty:
        return raw
    output = pd.DataFrame(index=raw.index)
    output["symbol"] = raw["thscode"].astype(str).str.split(".").str[0].str.upper()
    output["variety"] = output["symbol"].str.extract(r"^([A-Z]+)", expand=False)
    output["date"] = pd.to_datetime(raw["time"], errors="coerce").dt.strftime("%Y%m%d")
    field_map = {
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "settlement": "settle",
        "preSettlement": "pre_settle",
        "volume": "volume",
        "amount": "turnover",
        "openInterest": "open_interest",
    }
    for source, target in field_map.items():
        output[target] = raw[source] if source in raw.columns else None
    output["source_provider"] = "ifind_http"
    output["source_endpoint"] = "cmd_history_quotation"
    output["source_code"] = raw["thscode"].astype(str)
    return output


def collect_futures_daily(
    trade_date: str,
    exchange: str,
    contracts: Sequence[str],
    *,
    client: IFindHTTPClient,
    fields: Sequence[str] = IFIND_FUTURES_FIELDS,
    batch_size: int = DEFAULT_HISTORY_BATCH_SIZE,
    request_interval_seconds: float = 0.0,
) -> pd.DataFrame:
    """Collect one-day Quant API history and map to the existing raw schema."""

    return collect_futures_history(
        trade_date,
        trade_date,
        exchange,
        contracts,
        client=client,
        fields=fields,
        batch_size=batch_size,
        request_interval_seconds=request_interval_seconds,
    )


def collect_futures_universe_daily(
    trade_date: str,
    exchange: str,
    products: Sequence[str],
    *,
    client: IFindHTTPClient,
    fields: Sequence[str] = IFIND_FUTURES_FIELDS,
    batch_size: int = DEFAULT_HISTORY_BATCH_SIZE,
    request_interval_seconds: float = 0.0,
) -> pd.DataFrame:
    """Discover listed concrete contracts and collect their EOD fields via iFinD."""

    contracts = generate_contract_candidates(trade_date, exchange, products)
    output = collect_futures_daily(
        trade_date,
        exchange,
        contracts,
        client=client,
        fields=fields,
        batch_size=batch_size,
        request_interval_seconds=request_interval_seconds,
    )
    output.attrs["candidate_contracts"] = len(contracts)
    output.attrs["returned_contracts"] = len(output)
    return output


def collect_futures_universe_history(
    start_date: str,
    end_date: str,
    exchange: str,
    products: Sequence[str],
    *,
    client: IFindHTTPClient,
    fields: Sequence[str] = IFIND_FUTURES_FIELDS,
    batch_size: int = DEFAULT_RANGE_BATCH_SIZE,
    request_interval_seconds: float = 0.55,
) -> pd.DataFrame:
    """Collect a broad concrete-contract universe for a historical date range."""

    contracts = generate_contract_candidates(end_date, exchange, products)
    output = collect_futures_history(
        start_date,
        end_date,
        exchange,
        contracts,
        client=client,
        fields=fields,
        batch_size=batch_size,
        request_interval_seconds=request_interval_seconds,
    )
    output.attrs["candidate_contracts"] = len(contracts)
    output.attrs["returned_rows"] = len(output)
    return output


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_HISTORY_BATCH_SIZE",
    "DEFAULT_RANGE_BATCH_SIZE",
    "IFindHTTPClient",
    "IFindHTTPError",
    "collect_futures_daily",
    "collect_futures_history",
    "collect_futures_universe_daily",
    "collect_futures_universe_history",
    "generate_contract_candidates",
]
