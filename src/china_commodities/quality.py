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
) -> list[str]:
    errors: list[str] = []
    futures_status = {
        status.scope: status
        for status in statuses
        if status.dataset == "futures"
    }
    for exchange in MANDATORY_FUTURES_EXCHANGES:
        status = futures_status.get(exchange)
        if status is None:
            errors.append(f"missing futures status for {exchange}")
        elif status.state != "ok" or not status.is_fresh or status.records <= 0:
            errors.append(
                f"{exchange} futures not fresh: state={status.state}, records={status.records}"
            )

    counts = Counter(record.get("exchange") for record in futures_records)
    for exchange in MANDATORY_FUTURES_EXCHANGES:
        if counts.get(exchange, 0) <= 0:
            errors.append(f"no normalized futures contracts for {exchange}")

    keys: set[tuple[str, str]] = set()
    for record in futures_records:
        exchange = str(record.get("exchange", ""))
        contract = str(record.get("contract", ""))
        if record.get("trade_date") != trade_date:
            errors.append(f"trade date mismatch for {exchange}:{contract}")
        if not re.search(r"\d", contract):
            errors.append(f"non-concrete contract rejected: {exchange}:{contract}")
        key = (exchange, contract)
        if key in keys:
            errors.append(f"duplicate contract: {exchange}:{contract}")
        keys.add(key)

    liquid_by_exchange = Counter(
        record["exchange"]
        for record in futures_records
        if (record.get("volume") or 0) > 0 and (record.get("open_interest") or 0) > 0
    )
    for exchange in MANDATORY_FUTURES_EXCHANGES:
        if liquid_by_exchange.get(exchange, 0) <= 0:
            errors.append(f"no liquid concrete contracts for {exchange}")
    return sorted(set(errors))


def validate_snapshot(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if payload.get("schema_version") != 1:
        errors.append("unsupported or missing schema_version")
    if not payload.get("verified"):
        errors.append("snapshot is not verified")
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
