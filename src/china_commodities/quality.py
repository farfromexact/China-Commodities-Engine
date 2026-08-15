"""Quality gates for promoting a run to a verified daily snapshot."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from .models import ModuleStatus


MANDATORY_FUTURES_EXCHANGES = ("SHFE", "INE", "DCE", "CZCE", "GFEX")


def validate_run(
    trade_date: str,
    statuses: list[ModuleStatus],
    futures_records: list[dict[str, Any]],
    expected_exchanges: tuple[str, ...] = MANDATORY_FUTURES_EXCHANGES,
) -> list[str]:
    errors: list[str] = []
    futures_status = {
        status.scope: status
        for status in statuses
        if status.dataset == "futures"
    }
    for exchange in expected_exchanges:
        status = futures_status.get(exchange)
        if status is None:
            errors.append(f"missing futures status for {exchange}")
        elif status.state != "ok" or not status.is_fresh or status.records <= 0:
            errors.append(
                f"{exchange} futures not fresh: state={status.state}, records={status.records}"
            )
        elif status.source_date_match is not True:
            errors.append(
                f"{exchange} futures source date not verified: "
                f"source_trade_date={status.source_trade_date}"
            )

    counts = Counter(record.get("exchange") for record in futures_records)
    for exchange in expected_exchanges:
        if counts.get(exchange, 0) <= 0:
            errors.append(f"no normalized futures contracts for {exchange}")

    keys: set[tuple[str, str]] = set()
    for record in futures_records:
        exchange = str(record.get("exchange", ""))
        contract = str(record.get("contract", ""))
        if record.get("trade_date") != trade_date:
            errors.append(f"trade date mismatch for {exchange}:{contract}")
        if record.get("requested_trade_date") != trade_date:
            errors.append(f"requested trade date mismatch for {exchange}:{contract}")
        if record.get("source_trade_date") != trade_date:
            errors.append(f"source trade date mismatch for {exchange}:{contract}")
        if record.get("source_date_match") is not True:
            errors.append(f"source trade date not verified for {exchange}:{contract}")
        if not re.search(r"\d", contract):
            errors.append(f"non-concrete contract rejected: {exchange}:{contract}")
        key = (exchange, contract)
        if key in keys:
            errors.append(f"duplicate contract: {exchange}:{contract}")
        keys.add(key)

        numeric = {
            name: record.get(name)
            for name in ("open", "high", "low", "close", "volume", "open_interest")
        }
        try:
            high = float(numeric["high"]) if numeric["high"] is not None else None
            low = float(numeric["low"]) if numeric["low"] is not None else None
            open_price = float(numeric["open"]) if numeric["open"] is not None else None
            close = float(numeric["close"]) if numeric["close"] is not None else None
            volume = float(numeric["volume"]) if numeric["volume"] is not None else None
            open_interest = (
                float(numeric["open_interest"])
                if numeric["open_interest"] is not None
                else None
            )
        except (TypeError, ValueError):
            errors.append(f"non-numeric core field for {exchange}:{contract}")
            continue
        prices = [value for value in (open_price, close) if value is not None]
        if high is not None and low is not None and high < low:
            errors.append(f"invalid OHLC range for {exchange}:{contract}")
        elif prices and (
            (high is not None and high < max(prices))
            or (low is not None and low > min(prices))
        ):
            errors.append(f"invalid OHLC bounds for {exchange}:{contract}")
        if (volume is not None and volume < 0) or (
            open_interest is not None and open_interest < 0
        ):
            errors.append(f"negative volume or open interest for {exchange}:{contract}")

    liquid_by_exchange = Counter(
        record["exchange"]
        for record in futures_records
        if (record.get("volume") or 0) > 0 and (record.get("open_interest") or 0) > 0
    )
    for exchange in expected_exchanges:
        if liquid_by_exchange.get(exchange, 0) <= 0:
            errors.append(f"no liquid concrete contracts for {exchange}")
    return sorted(set(errors))


def validate_snapshot(
    payload: dict[str, Any], *, allow_scoped: bool = False
) -> list[str]:
    errors: list[str] = []
    if payload.get("schema_version") != 1:
        errors.append("unsupported or missing schema_version")
    scope_verified = bool(payload.get("scope_verified"))
    if not payload.get("verified") and not (allow_scoped and scope_verified):
        errors.append("snapshot is not verified")
    if allow_scoped and scope_verified:
        coverage = payload.get("coverage_scope")
        if not isinstance(coverage, dict) or not coverage.get("included_exchanges"):
            errors.append("scoped snapshot coverage metadata missing")
    trade_date = payload.get("trade_date")
    if not isinstance(trade_date, str):
        errors.append("snapshot trade_date missing")
    futures = payload.get("futures_contracts")
    if not isinstance(futures, list) or not futures:
        errors.append("snapshot has no futures_contracts")
    curves = payload.get("commodity_curves")
    if not isinstance(curves, list) or not curves:
        errors.append("snapshot has no commodity_curves")
    return errors
