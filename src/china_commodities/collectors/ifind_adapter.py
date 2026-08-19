"""Read-only iFinD SDK adapter for commodity-futures shadow collection.

This module is intentionally independent from the AKShare adapter.  It does
not publish data or choose a canonical provider; callers must reconcile and
promote observations through the normal pipeline quality gates.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
import importlib
import os
import re
from types import TracebackType
from typing import Any

import pandas as pd


IFIND_EXCHANGE_SUFFIX: dict[str, str] = {
    "SHFE": "SHF",
    "INE": "INE",
    "DCE": "DCE",
    "CZCE": "CZC",
    "GFEX": "GFE",
}

IFIND_FUTURES_FIELDS: tuple[str, ...] = (
    "open",
    "high",
    "low",
    "close",
    "settlement",
    "preSettlement",
    "volume",
    "amount",
    "openInterest",
)


class IFindSDKError(RuntimeError):
    """Raised for SDK authentication, entitlement, or response errors."""


def _safe_error_message(value: Any) -> str:
    return str(value or "unknown iFinD error")[:300]


def contract_to_ifind_code(contract: str, exchange: str) -> str:
    """Convert a concrete exchange contract to an iFinD code."""

    normalized_exchange = str(exchange).upper()
    suffix = IFIND_EXCHANGE_SUFFIX.get(normalized_exchange)
    if suffix is None:
        raise ValueError(f"unsupported iFinD commodity exchange: {exchange}")
    symbol = str(contract).strip().upper()
    if not re.fullmatch(r"[A-Z]+\d{3,4}", symbol):
        raise ValueError(f"not a concrete commodity futures contract: {contract}")
    return f"{symbol}.{suffix}"


def _response_error(response: Any) -> tuple[Any, str | None]:
    if isinstance(response, dict):
        code = response.get("errorcode", response.get("errcode"))
        message = response.get("errmsg", response.get("message"))
        return code, None if message is None else str(message)
    code = getattr(response, "errorcode", getattr(response, "errcode", None))
    message = getattr(response, "errmsg", getattr(response, "message", None))
    return code, None if message is None else str(message)


def _response_frame(response: Any) -> pd.DataFrame:
    data = response.get("data") if isinstance(response, dict) else getattr(response, "data", None)
    if isinstance(data, pd.DataFrame):
        return data.copy()
    if isinstance(data, list):
        return pd.DataFrame(data)
    return pd.DataFrame()


def _chunks(values: Sequence[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield list(values[start : start + size])


@dataclass
class IFindSDKSession:
    """A short-lived authenticated SDK session with guaranteed logout."""

    username: str | None = None
    password: str | None = None
    ifind_module: Any | None = None
    logged_in: bool = False

    def __enter__(self) -> "IFindSDKSession":
        username = self.username or os.environ.get("IFIND_USERNAME")
        password = self.password or os.environ.get("IFIND_PASSWORD")
        if not username or not password:
            raise IFindSDKError(
                "iFinD credentials are required in process environment variables "
                "IFIND_USERNAME and IFIND_PASSWORD"
            )
        if self.ifind_module is None:
            self.ifind_module = importlib.import_module("iFinDPy")
        result = self.ifind_module.THS_iFinDLogin(username, password)
        if result != 0:
            get_error = getattr(self.ifind_module, "THS_GetErrorInfo", None)
            detail = get_error(result) if callable(get_error) else result
            raise IFindSDKError(
                f"iFinD SDK login failed with code {result}: {_safe_error_message(detail)}"
            )
        self.logged_in = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.logged_in and self.ifind_module is not None:
            self.ifind_module.THS_iFinDLogout()
        self.logged_in = False

    def history_quotes(
        self,
        codes: Sequence[str],
        fields: Sequence[str],
        trade_date: str,
        *,
        batch_size: int = 50,
    ) -> pd.DataFrame:
        if not self.logged_in or self.ifind_module is None:
            raise IFindSDKError("iFinD SDK session is not logged in")
        if batch_size < 1:
            raise ValueError("batch_size must be positive")

        frames: list[pd.DataFrame] = []
        for batch in _chunks(list(codes), batch_size):
            response = self.ifind_module.THS_HQ(
                ",".join(batch),
                ";".join(fields),
                "",
                trade_date,
                trade_date,
            )
            error_code, error_message = _response_error(response)
            if error_code not in (0, "0", None):
                raise IFindSDKError(
                    "iFinD THS_HQ failed with code "
                    f"{error_code}: {_safe_error_message(error_message)}"
                )
            frame = _response_frame(response)
            if not frame.empty:
                frames.append(frame)
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def collect_futures_daily(
    trade_date: str,
    exchange: str,
    contracts: Sequence[str],
    *,
    session: IFindSDKSession,
    fields: Sequence[str] = IFIND_FUTURES_FIELDS,
    batch_size: int = 50,
) -> pd.DataFrame:
    """Collect iFinD EOD fields and map them to the existing raw schema."""

    normalized_exchange = str(exchange).upper()
    codes = [contract_to_ifind_code(contract, normalized_exchange) for contract in contracts]
    if not codes:
        return pd.DataFrame()
    raw = session.history_quotes(codes, fields, trade_date, batch_size=batch_size)
    if raw.empty:
        return raw
    required = {"thscode", "time"}
    missing = sorted(required.difference(raw.columns))
    if missing:
        raise IFindSDKError(
            f"iFinD futures response missing columns: {', '.join(missing)}"
        )

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
    output["source_provider"] = "ifind_sdk"
    output["source_endpoint"] = "THS_HQ"
    output["source_code"] = raw["thscode"].astype(str)
    return output


__all__ = [
    "IFIND_EXCHANGE_SUFFIX",
    "IFIND_FUTURES_FIELDS",
    "IFindSDKError",
    "IFindSDKSession",
    "collect_futures_daily",
    "contract_to_ifind_code",
]
