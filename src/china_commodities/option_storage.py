"""Validated publication and retention for end-of-day commodity option chains."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from pathlib import Path
import re
from typing import Any

from .option_quality import assess_option_snapshot_quality
from .storage import read_json, write_json_if_changed


DEFAULT_CHAIN_LIMIT = 20
DEFAULT_SUMMARY_LIMIT = 20
SNAPSHOT_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")


class OptionSnapshotValidationError(ValueError):
    """Raised before an incomplete or ambiguous option snapshot is promoted."""


def _selected_iv(record: dict[str, Any]) -> float | None:
    greeks = record.get("greeks") or {}
    selected = greeks.get("selected") or {}
    value = selected.get("iv_percent")
    return float(value) if isinstance(value, (int, float)) and value > 0 else None


def validate_option_snapshot(snapshot: dict[str, Any]) -> None:
    trade_date = str(snapshot.get("trade_date") or "")
    try:
        date.fromisoformat(trade_date)
    except ValueError as exc:
        raise OptionSnapshotValidationError("invalid option snapshot trade_date") from exc
    records = snapshot.get("records")
    if not isinstance(records, list) or not records:
        raise OptionSnapshotValidationError("option snapshot records must be non-empty")
    contracts: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise OptionSnapshotValidationError(f"option record {index} is not an object")
        if record.get("trade_date") != trade_date:
            raise OptionSnapshotValidationError(
                f"option record {index} does not match snapshot trade_date"
            )
        contract = str(record.get("contract") or "").upper()
        if not contract or not re.search(r"\d", contract):
            raise OptionSnapshotValidationError(
                f"option record {index} has no concrete contract"
            )
        if contract in contracts:
            raise OptionSnapshotValidationError(f"duplicate option contract: {contract}")
        contracts.add(contract)
        if not record.get("underlying_contract"):
            raise OptionSnapshotValidationError(
                f"option record {contract} has no underlying contract"
            )
        if str(record.get("option_type") or "").upper() not in {"C", "P"}:
            raise OptionSnapshotValidationError(
                f"option record {contract} has invalid option_type"
            )
        strike = record.get("strike")
        if not isinstance(strike, (int, float)) or strike <= 0:
            raise OptionSnapshotValidationError(
                f"option record {contract} has invalid strike"
            )
        provider = str(record.get("source_provider") or "").lower()
        if not provider.startswith("ifind"):
            raise OptionSnapshotValidationError(
                f"option record {contract} is not sourced from iFinD"
            )
        greeks = record.get("greeks")
        if not isinstance(greeks, dict) or greeks.get("quality") not in {
            "vendor_reported",
            "model_derived",
            "vendor_and_model",
            "unavailable",
        }:
            raise OptionSnapshotValidationError(
                f"option record {contract} has no explicit Greeks quality"
            )
    quality = assess_option_snapshot_quality(snapshot)
    if quality["full_chain_verified"] is not True:
        detail = "; ".join(quality["limitations"][:3])
        raise OptionSnapshotValidationError(
            f"option snapshot full-chain quality failed: {detail}"
        )


def build_option_summary(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str | None], list[dict[str, Any]]] = defaultdict(list)
    for record in snapshot["records"]:
        key = (
            str(record.get("exchange") or ""),
            str(record.get("product") or ""),
            str(record.get("underlying_contract") or ""),
            record.get("expiry_date"),
        )
        groups[key].append(record)

    output: list[dict[str, Any]] = []
    for (exchange, product, underlying, expiry), records in sorted(groups.items()):
        calls = [item for item in records if item.get("option_type") == "C"]
        puts = [item for item in records if item.get("option_type") == "P"]
        call_volume = sum(float(item.get("volume") or 0) for item in calls)
        put_volume = sum(float(item.get("volume") or 0) for item in puts)
        call_oi = sum(float(item.get("open_interest") or 0) for item in calls)
        put_oi = sum(float(item.get("open_interest") or 0) for item in puts)
        forward_values = [
            float(item["underlying_settle"])
            for item in records
            if isinstance(item.get("underlying_settle"), (int, float))
            and item["underlying_settle"] > 0
        ]
        forward = forward_values[0] if forward_values else None
        atm_records = (
            sorted(
                records,
                key=lambda item: abs(float(item["strike"]) - forward),
            )
            if forward is not None
            else []
        )
        atm_strike = float(atm_records[0]["strike"]) if atm_records else None
        atm_ivs = [
            _selected_iv(item)
            for item in records
            if atm_strike is not None and float(item["strike"]) == atm_strike
        ]
        atm_ivs = [value for value in atm_ivs if value is not None]
        output.append(
            {
                "exchange": exchange,
                "product": product,
                "underlying_contract": underlying,
                "expiry_date": expiry,
                "contract_count": len(records),
                "call_contract_count": len(calls),
                "put_contract_count": len(puts),
                "call_volume": call_volume,
                "put_volume": put_volume,
                "put_call_volume_ratio": put_volume / call_volume if call_volume else None,
                "call_open_interest": call_oi,
                "put_open_interest": put_oi,
                "put_call_open_interest_ratio": put_oi / call_oi if call_oi else None,
                "underlying_settle": forward,
                "atm_strike": atm_strike,
                "atm_iv_percent": sum(atm_ivs) / len(atm_ivs) if atm_ivs else None,
                "dealer_gamma_known": False,
            }
        )
    return output


def publish_option_eod(
    snapshot: dict[str, Any],
    data_dir: Path,
    *,
    chain_limit: int = DEFAULT_CHAIN_LIMIT,
    summary_limit: int = DEFAULT_SUMMARY_LIMIT,
) -> None:
    """Publish one verified EOD chain and enforce 20-day rolling retention."""
    if chain_limit < 1 or summary_limit < 1:
        raise ValueError("option retention limits must be positive")
    validate_option_snapshot(snapshot)
    trade_date = snapshot["trade_date"]
    quality = assess_option_snapshot_quality(snapshot)
    promoted_snapshot = dict(snapshot)
    promoted_snapshot["quality"] = quality
    summary = {
        "trade_date": trade_date,
        "generated_at": snapshot.get("generated_at"),
        "source_provider": snapshot.get("source_provider", "ifind_http"),
        "quality": quality,
        "series": build_option_summary(snapshot),
    }
    root = data_dir / "options"
    write_json_if_changed(root / "latest.json", promoted_snapshot)
    write_json_if_changed(
        root / "quality_latest.json",
        {
            "schema_version": 1,
            "trade_date": trade_date,
            "generated_at": snapshot.get("generated_at"),
            "quality": quality,
        },
    )
    write_json_if_changed(
        root / "snapshots" / f"{trade_date}.json", promoted_snapshot
    )

    snapshot_files = sorted(
        path
        for path in (root / "snapshots").glob("*.json")
        if SNAPSHOT_NAME.fullmatch(path.name)
    )
    for obsolete in snapshot_files[:-chain_limit]:
        obsolete.unlink()

    history_path = root / "history.json"
    current = read_json(history_path, default={"schema_version": 1, "records": []})
    records = [
        record
        for record in current.get("records", [])
        if record.get("trade_date") != trade_date
    ]
    records.append(summary)
    records.sort(key=lambda item: item["trade_date"])
    write_json_if_changed(
        history_path,
        {"schema_version": 1, "records": records[-summary_limit:]},
    )
