"""Atomic, idempotent Parquet history for normalized market records.

Only normalized fields are retained here.  Vendor response bodies and access
credentials never enter the historical artifacts.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date
import json
import os
from pathlib import Path
import re
import time
from typing import Any

import pandas as pd
import pyarrow.parquet as pq

from .models import PipelineResult


DEFAULT_OPTION_HISTORY_DAYS = 252
OPTION_PARTITION_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})\.parquet$")


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
    retention_field: str | None = None,
    retention_days: int | None = None,
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
    if (retention_field is None) != (retention_days is None):
        raise ValueError(
            "retention_field and retention_days must be supplied together"
        )
    if retention_days is not None and retention_days < 1:
        raise ValueError("retention_days must be positive")

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
    if retention_field is not None and retention_days is not None:
        field = str(retention_field)
        if field not in combined.columns:
            raise ValueError(f"Parquet history retention field is missing: {field}")
        retained = sorted(
            {
                str(value)
                for value in combined[field].dropna().tolist()
                if str(value)
            }
        )
        if len(retained) > retention_days:
            combined = combined[combined[field].astype(str).isin(retained[-retention_days:])]
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
    rows = _futures_history_rows(
        result.trade_date,
        result.primary_provider,
        result.futures_records,
    )
    return append_parquet_history(
        Path(data_dir) / "history" / "futures.parquet",
        rows,
        key_fields=("trade_date", "exchange", "contract"),
        sort_fields=("trade_date", "exchange", "contract"),
    )


def _futures_history_rows(
    trade_date: str,
    provider: str | None,
    records: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize futures rows from either a live result or a stored snapshot."""

    rows: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        row = dict(record)
        row.setdefault("trade_date", trade_date)
        row["requested_date"] = trade_date
        row["source_date"] = record.get("source_trade_date") or record.get(
            "source_date"
        ) or record.get("trade_date")
        row["observation_date"] = row["source_date"]
        row["vendor"] = provider
        row["timezone"] = "Asia/Shanghai"
        row["frequency"] = "EOD"
        row["quality_state"] = (
            "fresh" if record.get("source_date_match") is True else "invalid_source_date"
        )
        rows.append(row)
    return rows


def rebuild_futures_history_from_snapshots(
    data_dir: str | Path = "data",
    *,
    retention_days: int = 252,
) -> int:
    """Repair/extend futures Parquet from local snapshots without vendor calls.

    Existing Parquet rows are retained and same-date rows from verified local
    snapshots are upserted.  Only the most recent ``retention_days`` distinct
    trade dates are retained.  This is intentionally separate from collection
    so a report or a one-off repair can recover history after an older release
    was written with an incomplete Parquet file.
    """

    if retention_days < 1:
        raise ValueError("futures history retention must be positive")
    root = Path(data_dir)
    snapshot_root = root / "snapshots"
    rows: list[dict[str, Any]] = []
    seen_dates: set[str] = set()
    if snapshot_root.exists():
        for snapshot_path in sorted(snapshot_root.glob("*.json")):
            try:
                payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, Mapping):
                continue
            if payload.get("verified") is not True and payload.get("scope_verified") is not True:
                continue
            trade_date = str(payload.get("trade_date") or "")
            if not trade_date:
                continue
            date.fromisoformat(trade_date)
            if trade_date in seen_dates:
                raise ValueError(f"duplicate verified snapshot trade_date: {trade_date}")
            seen_dates.add(trade_date)
            source = payload.get("source")
            source_map = source if isinstance(source, Mapping) else {}
            provider = source_map.get("provider") or payload.get("primary_provider")
            rows.extend(
                _futures_history_rows(
                    trade_date,
                    str(provider) if provider else None,
                    payload.get("futures_contracts") or [],
                )
            )

    destination = root / "history" / "futures.parquet"
    append_parquet_history(
        destination,
        rows,
        key_fields=("trade_date", "exchange", "contract"),
        sort_fields=("trade_date", "exchange", "contract"),
    )
    if not destination.exists():
        return 0

    frame = pd.read_parquet(destination)
    if frame.empty or "trade_date" not in frame.columns:
        return int(len(frame))
    frame["trade_date"] = frame["trade_date"].astype(str)
    distinct_dates = sorted(set(frame["trade_date"].dropna().tolist()))
    if len(distinct_dates) > retention_days:
        keep_dates = set(distinct_dates[-retention_days:])
        frame = frame[frame["trade_date"].isin(keep_dates)].copy()
        frame = frame.sort_values(
            [field for field in ("trade_date", "exchange", "contract") if field in frame.columns],
            kind="stable",
            na_position="last",
        ).reset_index(drop=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        destination.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(temporary, index=False, engine="pyarrow")
        _replace_with_retry(temporary, destination)
    return int(len(frame))


def _option_history_rows(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
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
    return rows


def _option_partition_path(history_root: Path, trade_date: str) -> Path:
    parsed = date.fromisoformat(trade_date)
    return (
        history_root
        / f"year={parsed.year:04d}"
        / f"month={parsed.month:02d}"
        / f"{trade_date}.parquet"
    )


def _option_partition_files(history_root: Path) -> list[tuple[str, Path]]:
    output: list[tuple[str, Path]] = []
    if not history_root.exists():
        return output
    for path in history_root.glob("year=*/month=*/*.parquet"):
        match = OPTION_PARTITION_NAME.fullmatch(path.name)
        if match is None:
            continue
        trade_date = match.group(1)
        try:
            parsed = date.fromisoformat(trade_date)
        except ValueError:
            continue
        if path.parent.name != f"month={parsed.month:02d}":
            continue
        if path.parent.parent.name != f"year={parsed.year:04d}":
            continue
        output.append((trade_date, path))
    return sorted(output)


def _migrate_legacy_option_history(legacy_path: Path, history_root: Path) -> None:
    """Split the old monolithic option history before deleting it."""

    if not legacy_path.exists():
        return
    frame = pd.read_parquet(legacy_path)
    if not frame.empty and "trade_date" not in frame.columns:
        raise ValueError("legacy option history has no trade_date column")
    trade_dates: list[str] = []
    if not frame.empty:
        for raw_value in frame["trade_date"].dropna().unique().tolist():
            trade_date = str(raw_value)
            date.fromisoformat(trade_date)
            trade_dates.append(trade_date)
    for trade_date in sorted(trade_dates):
        rows = frame.loc[
            frame["trade_date"].astype(str).eq(trade_date)
        ].to_dict(orient="records")
        append_parquet_history(
            _option_partition_path(history_root, trade_date),
            rows,
            key_fields=("trade_date", "exchange", "contract"),
            sort_fields=("trade_date", "exchange", "product", "contract"),
        )
    legacy_path.unlink()


def _prune_option_partitions(history_root: Path, retention_days: int) -> None:
    partitions = _option_partition_files(history_root)
    retained_dates = sorted({trade_date for trade_date, _ in partitions})
    obsolete_dates = set(retained_dates[:-retention_days])
    removed_parents: set[Path] = set()
    for trade_date, path in partitions:
        if trade_date not in obsolete_dates:
            continue
        path.unlink()
        removed_parents.add(path.parent)
        removed_parents.add(path.parent.parent)
    for directory in sorted(
        removed_parents, key=lambda value: len(value.parts), reverse=True
    ):
        try:
            directory.rmdir()
        except OSError:
            pass


def append_option_history(
    snapshot: Mapping[str, Any],
    data_dir: str | Path,
    *,
    retention_days: int = DEFAULT_OPTION_HISTORY_DAYS,
) -> int:
    """Append one EOD option chain to rolling daily Parquet partitions.

    The most recent ``retention_days`` distinct trade dates are retained.
    Existing monolithic ``data/options/history.parquet`` data is migrated
    losslessly before that legacy file is removed.
    """

    if retention_days < 1:
        raise ValueError("option history retention must be positive")
    trade_date = str(snapshot.get("trade_date") or "")
    date.fromisoformat(trade_date)
    option_root = Path(data_dir) / "options"
    history_root = option_root / "history"
    _migrate_legacy_option_history(option_root / "history.parquet", history_root)
    rows = _option_history_rows(snapshot)
    if rows:
        append_parquet_history(
            _option_partition_path(history_root, trade_date),
            rows,
            key_fields=("trade_date", "exchange", "contract"),
            sort_fields=("trade_date", "exchange", "product", "contract"),
        )
    _prune_option_partitions(history_root, retention_days)
    return sum(
        int(pq.ParquetFile(path).metadata.num_rows)
        for _, path in _option_partition_files(history_root)
    )


__all__ = [
    "DEFAULT_OPTION_HISTORY_DAYS",
    "append_futures_history",
    "append_option_history",
    "append_parquet_history",
    "rebuild_futures_history_from_snapshots",
]
