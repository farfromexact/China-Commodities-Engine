"""Atomic, idempotent Parquet history for normalized market records.

Only normalized fields are retained here.  Vendor response bodies and access
credentials never enter the historical artifacts.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import json
import os
from pathlib import Path
import time
from typing import Any

import pandas as pd

from .models import PipelineResult


def _scalar(value: Any) -> Any:
    """Return a Parquet-safe scalar while preserving explicit provenance."""

    if isinstance(value, Mapping) or isinstance(value, (list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if hasattr(value, "item"):
        return value.item()
    return value


def _normalized_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {str(key): _scalar(value) for key, value in row.items()}
        for row in rows
    ]


def _replace_with_retry(temporary: Path, destination: Path) -> None:
    for attempt in range(5):
        try:
            os.replace(temporary, destination)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.2 * (attempt + 1))


def append_parquet_history(
    path: str | Path,
    rows: Iterable[Mapping[str, Any]],
    *,
    key_fields: Sequence[str],
    sort_fields: Sequence[str] | None = None,
) -> int:
    """Append normalized rows with last-write-wins de-duplication.

    Returns the number of rows in the complete persisted history.  Empty
    appends are no-ops.  Keys must be complete so same-day retries cannot
    silently create ambiguous records.
    """

    destination = Path(path)
    incoming_rows = _normalized_rows(rows)
    if not incoming_rows:
        if destination.exists():
            return int(len(pd.read_parquet(destination)))
        return 0
    keys = tuple(str(field) for field in key_fields)
    if not keys:
        raise ValueError("Parquet history requires at least one key field")
    for index, row in enumerate(incoming_rows):
        missing = [field for field in keys if row.get(field) in (None, "")]
        if missing:
            raise ValueError(
                f"Parquet history row {index} has incomplete key fields: "
                + ", ".join(missing)
            )

    incoming = pd.DataFrame(incoming_rows)
    if destination.exists():
        existing = pd.read_parquet(destination)
        combined = pd.concat([existing, incoming], ignore_index=True, sort=False)
    else:
        combined = incoming
    combined = combined.drop_duplicates(subset=list(keys), keep="last")
    order = list(sort_fields or keys)
    combined = combined.sort_values(order, kind="stable", na_position="last")
    combined = combined.reset_index(drop=True)

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    combined.to_parquet(temporary, index=False, engine="pyarrow")
    _replace_with_retry(temporary, destination)
    return int(len(combined))


def append_futures_history(result: PipelineResult, data_dir: str | Path) -> int:
    """Append one verified pipeline result to the long futures history."""

    if not result.scope_verified:
        raise ValueError("refusing to append an unverified futures result")
    rows = []
    for record in result.futures_records:
        row = dict(record)
        row.setdefault("trade_date", result.trade_date)
        row["requested_date"] = result.trade_date
        row["source_date"] = record.get("source_trade_date") or record.get("trade_date")
        row["observation_date"] = row["source_date"]
        row["vendor"] = result.primary_provider
        row["timezone"] = "Asia/Shanghai"
        row["frequency"] = "EOD"
        row["quality_state"] = (
            "fresh" if record.get("source_date_match") is True else "invalid_source_date"
        )
        rows.append(row)
    return append_parquet_history(
        Path(data_dir) / "history" / "futures.parquet",
        rows,
        key_fields=("trade_date", "exchange", "contract"),
        sort_fields=("trade_date", "exchange", "contract"),
    )


def append_option_history(snapshot: Mapping[str, Any], data_dir: str | Path) -> int:
    """Append a normalized EOD option chain without persisting raw responses."""

    trade_date = str(snapshot.get("trade_date") or "")
    rows: list[dict[str, Any]] = []
    for record in snapshot.get("records") or []:
        if not isinstance(record, Mapping):
            continue
        greeks = record.get("greeks")
        greek_map = greeks if isinstance(greeks, Mapping) else {}
        selected = greek_map.get("selected")
        selected_map = selected if isinstance(selected, Mapping) else {}
        rows.append(
            {
                "trade_date": trade_date,
                "requested_date": trade_date,
                "source_date": record.get("source_trade_date"),
                "observation_date": record.get("source_trade_date"),
                "timezone": "Asia/Shanghai",
                "vendor": record.get("source_provider") or snapshot.get("source_provider"),
                "original_source": "iFinD Quant API",
                "source_endpoint": record.get("source_endpoint") or "real_time_quotation",
                "frequency": "EOD",
                "quality_state": (
                    "fresh" if record.get("source_date_match") is True else "invalid_source_date"
                ),
                "missing_reason": None,
                "exchange": record.get("exchange"),
                "product": record.get("product"),
                "contract": record.get("contract"),
                "underlying_contract": record.get("underlying_contract"),
                "expiry_date": record.get("expiry_date"),
                "exercise_style": record.get("exercise_style"),
                "option_type": record.get("option_type"),
                "strike": record.get("strike"),
                "open": record.get("open"),
                "high": record.get("high"),
                "low": record.get("low"),
                "close": record.get("close"),
                "settle": record.get("settle"),
                "bid": record.get("bid"),
                "ask": record.get("ask"),
                "volume": record.get("volume"),
                "open_interest": record.get("open_interest"),
                "underlying_settle": record.get("underlying_settle"),
                "source_date_match": record.get("source_date_match"),
                "greeks_quality": greek_map.get("quality"),
                "greeks_source": greek_map.get("selected_source"),
                "iv_percent": selected_map.get("iv_percent", record.get("iv_percent")),
                "delta": selected_map.get("delta"),
                "gamma": selected_map.get("gamma"),
                "vega": selected_map.get("vega"),
                "theta": selected_map.get("theta"),
                "rho": selected_map.get("rho"),
            }
        )
    return append_parquet_history(
        Path(data_dir) / "options" / "history.parquet",
        rows,
        key_fields=("trade_date", "exchange", "contract"),
        sort_fields=("trade_date", "exchange", "product", "contract"),
    )


__all__ = [
    "append_futures_history",
    "append_option_history",
    "append_parquet_history",
]
