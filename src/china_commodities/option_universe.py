"""Fallback commodity-option contract directory from the OpenCTP instrument list."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date
import re
from typing import Any

import requests

from .catalog import OptionProduct
from .collectors.ifind_option_adapter import IFindOptionDataError


OPENCTP_OPTION_DIRECTORY_URL = "http://dict.openctp.cn/instruments?types=option"


def _iso_date(value: Any) -> str | None:
    try:
        return date.fromisoformat(str(value).strip()[:10]).isoformat()
    except (TypeError, ValueError):
        return None


def _product_from_underlying(value: Any) -> str | None:
    match = re.match(r"([A-Za-z]+)", str(value or "").strip())
    return match.group(1).upper() if match else None


def _option_type(value: Any, contract: str) -> str | None:
    if str(value).strip() == "1":
        return "C"
    if str(value).strip() == "2":
        return "P"
    match = re.search(r"[-]?([CP])[-]?\d+(?:\.\d+)?$", contract.upper())
    return match.group(1) if match else None


def normalize_openctp_option_directory(
    rows: Sequence[dict[str, Any]],
    *,
    trade_date: str,
    option_products: Sequence[OptionProduct],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Normalize active catalog contracts without treating metadata as quotes."""

    requested_date = date.fromisoformat(trade_date).isoformat()
    target_keys = {
        (product.exchange.upper(), product.product.upper())
        for product in option_products
    }
    output: dict[tuple[str, str], list[dict[str, Any]]] = {
        key: [] for key in target_keys
    }
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        exchange = str(row.get("ExchangeID") or "").strip().upper()
        underlying = str(row.get("UnderlyingInstrID") or "").strip().upper()
        product = _product_from_underlying(underlying)
        if product is None or (exchange, product) not in target_keys:
            continue
        open_date = _iso_date(row.get("OpenDate"))
        expiry_date = _iso_date(row.get("ExpireDate"))
        if (
            open_date is None
            or expiry_date is None
            or open_date > requested_date
            or expiry_date < requested_date
        ):
            continue
        contract = str(row.get("InstrumentID") or "").strip().upper()
        option_type = _option_type(row.get("OptionsType"), contract)
        try:
            strike = float(row.get("StrikePrice"))
        except (TypeError, ValueError):
            strike = 0.0
        if not contract or not underlying or option_type not in {"C", "P"} or strike <= 0:
            continue
        duplicate_key = (exchange, contract)
        if duplicate_key in seen:
            raise IFindOptionDataError(
                f"OpenCTP option directory returned duplicate contract: {exchange}:{contract}"
            )
        seen.add(duplicate_key)
        output[(exchange, product)].append(
            {
                "trade_date": requested_date,
                "exchange": exchange,
                "product": product,
                "contract": contract,
                "underlying_contract": underlying,
                "expiry_date": expiry_date,
                "option_type": option_type,
                "strike": strike,
                "exercise_style": "unknown",
                "universe_source": "openctp_contract_directory",
                "universe_source_provider": "openctp",
                "universe_source_date": None,
            }
        )
    return {
        key: sorted(records, key=lambda item: item["contract"])
        for key, records in output.items()
        if records
    }


def collect_openctp_option_directories(
    trade_date: str,
    option_products: Sequence[OptionProduct],
    *,
    transport: Callable[..., Any] = requests.get,
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Download the current OpenCTP instrument dictionary once per batch."""

    response = transport(OPENCTP_OPTION_DIRECTORY_URL, timeout=60)
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise IFindOptionDataError("OpenCTP option directory returned no records")
    return normalize_openctp_option_directory(
        rows,
        trade_date=trade_date,
        option_products=option_products,
    )


__all__ = [
    "OPENCTP_OPTION_DIRECTORY_URL",
    "collect_openctp_option_directories",
    "normalize_openctp_option_directory",
]
