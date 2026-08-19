"""Read-only iFinD Quant API adapter for Chinese commodity futures."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
import json
import os
import re
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


def _is_transient_transport_error(exc: IFindHTTPError) -> bool:
    message = str(exc)
    return bool(
        "iFinD transport failed:" in message
        or "iFinD response was not valid JSON" in message
        or re.search(r"iFinD HTTP (?:429|5\d\d):", message)
    )


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


def _edb_frame(
    response: dict[str, Any], indicator_ids: Sequence[str]
) -> pd.DataFrame:
    """Normalize common Quant API EDB response shapes to a narrow contract."""

    requested = tuple(dict.fromkeys(str(value).strip() for value in indicator_ids))
    normalized: list[dict[str, Any]] = []

    # The live ``edb_service`` response is column-oriented, but unlike quote
    # endpoints the columns live directly on each table instead of under a
    # nested ``table`` mapping.  A single indicator ID is commonly returned
    # once while its time/value arrays contain the full observation history.
    tables = response.get("tables") or []
    if isinstance(tables, Mapping):
        tables = [tables]
    if isinstance(tables, list):
        for table in tables:
            if not isinstance(table, Mapping):
                continue
            if "time" not in table or "value" not in table:
                continue
            raw_times = table.get("time")
            raw_values = table.get("value")
            raw_ids = (
                table.get("id")
                or table.get("indicator_id")
                or table.get("index_id")
                or []
            )
            times = raw_times if isinstance(raw_times, list) else [raw_times]
            values = raw_values if isinstance(raw_values, list) else [raw_values]
            ids = raw_ids if isinstance(raw_ids, list) else [raw_ids]
            row_count = max(len(times), len(values))
            for index in range(row_count):
                indicator_id = ""
                if len(ids) == 1:
                    indicator_id = str(ids[0] or "").strip()
                elif index < len(ids):
                    indicator_id = str(ids[index] or "").strip()
                if not indicator_id and len(requested) == 1:
                    indicator_id = requested[0]
                if indicator_id not in requested:
                    continue
                normalized.append(
                    {
                        "indicator_id": indicator_id,
                        "observation_date": (
                            times[index] if index < len(times) else None
                        ),
                        "value": values[index] if index < len(values) else None,
                    }
                )

    frame = _tables_frame(response)
    if not frame.empty:
        excluded = {"thscode", "time", "date", "indicator_id"}
        for _, row in frame.iterrows():
            row_indicator = str(
                row.get("indicator_id") or row.get("thscode") or ""
            ).strip()
            observation = row.get("time", row.get("date"))
            candidate_ids = (
                [row_indicator]
                if row_indicator in requested
                else [value for value in requested if value in frame.columns]
            )
            if not candidate_ids and len(requested) == 1:
                candidate_ids = [requested[0]]
            for indicator_id in candidate_ids:
                value = row.get(indicator_id)
                if value is None or (isinstance(value, float) and pd.isna(value)):
                    value = row.get("value")
                if value is None or (isinstance(value, float) and pd.isna(value)):
                    candidates = [
                        row[column]
                        for column in frame.columns
                        if column not in excluded
                        and column not in requested
                        and pd.notna(row[column])
                    ]
                    value = candidates[0] if len(candidates) == 1 else None
                normalized.append(
                    {
                        "indicator_id": indicator_id,
                        "observation_date": observation,
                        "value": value,
                    }
                )

    if not normalized:
        rows = response.get("data") or []
        if isinstance(rows, dict):
            rows = rows.get("records") or rows.get("data") or []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                indicator_id = str(
                    row.get("indicator_id")
                    or row.get("index_id")
                    or row.get("id")
                    or (requested[0] if len(requested) == 1 else "")
                ).strip()
                if indicator_id not in requested:
                    continue
                normalized.append(
                    {
                        "indicator_id": indicator_id,
                        "observation_date": row.get("observation_date")
                        or row.get("time")
                        or row.get("date"),
                        "value": row.get("value"),
                    }
                )

    output = pd.DataFrame(
        normalized,
        columns=("indicator_id", "observation_date", "value"),
    )
    if output.empty:
        return output
    output["observation_date"] = pd.to_datetime(
        output["observation_date"], errors="coerce"
    ).dt.strftime("%Y-%m-%d")
    output["value"] = pd.to_numeric(output["value"], errors="coerce")
    output = output.dropna(subset=["observation_date", "value"])
    output = output[output["indicator_id"].isin(requested)]
    return (
        output.drop_duplicates(
            subset=["indicator_id", "observation_date"], keep="last"
        )
        .sort_values(["indicator_id", "observation_date"])
        .reset_index(drop=True)
    )


@dataclass
class IFindHTTPClient:
    """Short-lived Quant API client; tokens remain in memory only."""

    refresh_token: str | None = None
    access_token: str | None = None
    base_url: str = DEFAULT_BASE_URL
    timeout: int = 45
    minimum_request_interval_seconds: float = 0.0
    max_transport_attempts: int = 3
    retry_backoff_seconds: float = 1.0
    transport: Callable[
        [str, dict[str, str], dict[str, Any] | None, int], dict[str, Any]
    ] = _default_transport
    _last_request_started_at: float = field(default=0.0, init=False, repr=False)

    def _wait_for_rate_limit(self) -> None:
        interval = max(0.0, float(self.minimum_request_interval_seconds))
        if interval <= 0:
            return
        remaining = interval - (time.monotonic() - self._last_request_started_at)
        if remaining > 0:
            time.sleep(remaining)
        self._last_request_started_at = time.monotonic()

    def get_access_token(self) -> str:
        if self.access_token:
            return self.access_token
        environment_access_token = os.environ.get("IFIND_ACCESS_TOKEN")
        if environment_access_token:
            self.access_token = environment_access_token
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
        access_token = self.get_access_token()
        attempts = max(1, int(self.max_transport_attempts))
        for attempt in range(attempts):
            self._wait_for_rate_limit()
            try:
                response = self.transport(
                    f"{self.base_url}/{endpoint}",
                    {"access_token": access_token, "ifindlang": "cn"},
                    payload,
                    self.timeout,
                )
            except IFindHTTPError as exc:
                if not _is_transient_transport_error(exc) or attempt + 1 >= attempts:
                    raise
                time.sleep(max(0.0, self.retry_backoff_seconds) * (2**attempt))
                continue
            _raise_api_error(endpoint, response)
            return response
        raise AssertionError("unreachable iFinD retry state")

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

    def edb_series(
        self,
        indicator_ids: Sequence[str],
        *,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        """Fetch pinned EDB indicator IDs; production callers never search by text."""

        indicators = tuple(
            dict.fromkeys(str(value).strip() for value in indicator_ids if value)
        )
        if not indicators:
            raise ValueError("at least one EDB indicator ID is required")
        response = self.request(
            "edb_service",
            {
                "indicators": ",".join(indicators),
                "startdate": start_date,
                "enddate": end_date,
            },
        )
        return _edb_frame(response, indicators)

    def basic_data(
        self,
        codes: Sequence[str],
        indicator_params: Sequence[Mapping[str, Any]],
    ) -> pd.DataFrame:
        """Fetch verified security metadata indicators through basic_data_service."""

        normalized_codes = tuple(
            dict.fromkeys(str(value).strip() for value in codes if value)
        )
        normalized_indicators = [dict(value) for value in indicator_params]
        if not normalized_codes or not normalized_indicators:
            raise ValueError("basic_data requires codes and indicator parameters")
        for item in normalized_indicators:
            if not str(item.get("indicator") or "").strip():
                raise ValueError("basic_data indicator name is required")
        response = self.request(
            "basic_data_service",
            {
                "codes": ",".join(normalized_codes),
                "indipara": normalized_indicators,
            },
        )
        return _tables_frame(response)

    def data_pool(
        self,
        report_name: str,
        *,
        function_parameters: Mapping[str, Any],
        output_parameters: Any,
    ) -> pd.DataFrame:
        """Fetch a pinned, account-verified iFinD report without guessing IDs."""

        name = str(report_name or "").strip()
        if not name:
            raise ValueError("data_pool report_name is required")
        if not isinstance(function_parameters, Mapping):
            raise TypeError("data_pool function_parameters must be a mapping")
        if output_parameters in (None, "", {}):
            raise ValueError("data_pool output_parameters are required")
        response = self.request(
            "data_pool",
            {
                "reportname": name,
                "functionpara": dict(function_parameters),
                "outputpara": output_parameters,
            },
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
