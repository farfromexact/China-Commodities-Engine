"""Validated publication and retention for end-of-day commodity option chains."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
import hashlib
from pathlib import Path
from pathlib import PurePosixPath
import re
from typing import Any

from .history_storage import append_option_history
from .option_quality import assess_option_snapshot_quality
from .option_surface import build_option_surface
from .promotion import update_shadow_state
from .storage import read_json, write_json_gzip_if_changed, write_json_if_changed


DEFAULT_CHAIN_LIMIT = 20
DEFAULT_SUMMARY_LIMIT = 20
LATEST_INDEX_SCHEMA_VERSION = 2
LATEST_STORAGE_FORMAT = "product_shards_gzip"
SNAPSHOT_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}\.json(?:\.gz)?$")
SHARD_COMPONENT = re.compile(r"^[A-Z0-9][A-Z0-9_-]*$")
LATEST_STORAGE_KEYS = frozenset(
    {
        "snapshot_schema_version",
        "storage_format",
        "records_sharded",
        "record_count",
        "shard_count",
        "full_snapshot",
        "shards",
    }
)


class OptionSnapshotValidationError(ValueError):
    """Raised before an incomplete or ambiguous option snapshot is promoted."""


def _safe_shard_component(value: Any, label: str) -> str:
    component = str(value or "").upper()
    if SHARD_COMPONENT.fullmatch(component) is None:
        raise OptionSnapshotValidationError(
            f"option record has invalid {label} shard component: {value!r}"
        )
    return component


def _resolve_manifest_path(root: Path, relative_path: Any) -> Path:
    if not isinstance(relative_path, str) or not relative_path:
        raise OptionSnapshotValidationError("option latest manifest has invalid path")
    portable = PurePosixPath(relative_path)
    if portable.is_absolute() or any(part in {"", ".", ".."} for part in portable.parts):
        raise OptionSnapshotValidationError(
            f"option latest manifest path is not relative: {relative_path!r}"
        )
    root_resolved = root.resolve()
    candidate = root.joinpath(*portable.parts).resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise OptionSnapshotValidationError(
            f"option latest manifest path escapes options root: {relative_path!r}"
        )
    return candidate


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _cleanup_latest_shards(shard_root: Path, expected_paths: set[Path]) -> None:
    if not shard_root.exists():
        return
    expected = {path.resolve() for path in expected_paths}
    for path in shard_root.glob("*/*/*.json.gz"):
        if path.resolve() not in expected:
            path.unlink()
    for directory in sorted(
        [path for path in shard_root.glob("*/*") if path.is_dir()],
        key=lambda value: len(value.parts),
        reverse=True,
    ):
        try:
            directory.rmdir()
        except OSError:
            pass
    for directory in [path for path in shard_root.glob("*") if path.is_dir()]:
        try:
            directory.rmdir()
        except OSError:
            pass


def write_option_latest(
    snapshot: dict[str, Any],
    data_dir: Path,
    *,
    snapshot_retention_days: int = DEFAULT_CHAIN_LIMIT,
) -> dict[str, Any]:
    """Write a compact latest index plus deterministic product shards."""

    if snapshot_retention_days < 1:
        raise ValueError("option snapshot retention must be positive")
    validate_option_snapshot(snapshot)
    trade_date = str(snapshot["trade_date"])
    root = Path(data_dir) / "options"
    records = sorted(
        (dict(record) for record in snapshot["records"]),
        key=lambda record: (
            str(record.get("exchange") or ""),
            str(record.get("product") or ""),
            str(record.get("contract") or ""),
        ),
    )
    full_snapshot = dict(snapshot)
    full_snapshot["records"] = records
    snapshot_path = root / "snapshots" / f"{trade_date}.json.gz"
    write_json_gzip_if_changed(snapshot_path, full_snapshot)

    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        exchange = _safe_shard_component(record.get("exchange"), "exchange")
        product = _safe_shard_component(record.get("product"), "product")
        groups[(exchange, product)].append(record)

    shard_root = root / "latest_shards"
    expected_paths: set[Path] = set()
    shards: list[dict[str, Any]] = []
    for (exchange, product), group_records in sorted(groups.items()):
        relative_path = PurePosixPath(
            "latest_shards", trade_date, exchange, f"{product}.json.gz"
        )
        path = root.joinpath(*relative_path.parts)
        write_json_gzip_if_changed(
            path,
            {
                "schema_version": 1,
                "trade_date": trade_date,
                "generated_at": snapshot.get("generated_at"),
                "source_provider": snapshot.get("source_provider", "ifind_http"),
                "exchange": exchange,
                "product": product,
                "record_count": len(group_records),
                "records": group_records,
            },
        )
        expected_paths.add(path)
        shards.append(
            {
                "exchange": exchange,
                "product": product,
                "path": relative_path.as_posix(),
                "record_count": len(group_records),
                "sha256": _sha256(path),
            }
        )

    snapshot_schema_version = snapshot.get("schema_version", 1)
    metadata = {
        key: value
        for key, value in full_snapshot.items()
        if key not in {"schema_version", "records"}
    }
    manifest = {
        "schema_version": LATEST_INDEX_SCHEMA_VERSION,
        "snapshot_schema_version": snapshot_schema_version,
        **metadata,
        "storage_format": LATEST_STORAGE_FORMAT,
        "records_sharded": True,
        "record_count": len(records),
        "shard_count": len(shards),
        "full_snapshot": {
            "path": f"snapshots/{trade_date}.json.gz",
            "record_count": len(records),
            "sha256": _sha256(snapshot_path),
            "retention_trading_days": snapshot_retention_days,
        },
        "shards": shards,
    }
    write_json_if_changed(root / "latest.json", manifest)
    _cleanup_latest_shards(shard_root, expected_paths)
    return manifest


def read_option_latest(
    data_dir: str | Path,
    *,
    default: Any = None,
) -> dict[str, Any] | Any:
    """Read either the legacy monolith or the sharded latest-chain format."""

    root = Path(data_dir) / "options"
    manifest = read_json(root / "latest.json", default=default)
    if manifest is default:
        return default
    if not isinstance(manifest, dict):
        raise OptionSnapshotValidationError("option latest payload is not an object")
    if isinstance(manifest.get("records"), list):
        return manifest
    if manifest.get("storage_format") != LATEST_STORAGE_FORMAT:
        raise OptionSnapshotValidationError(
            "option latest payload has neither records nor a supported shard index"
        )
    trade_date = str(manifest.get("trade_date") or "")
    shards = manifest.get("shards")
    if not isinstance(shards, list) or not shards:
        raise OptionSnapshotValidationError("option latest shard index is empty")

    records: list[dict[str, Any]] = []
    contracts: set[str] = set()
    for descriptor in shards:
        if not isinstance(descriptor, dict):
            raise OptionSnapshotValidationError("option latest shard descriptor is invalid")
        exchange = _safe_shard_component(descriptor.get("exchange"), "exchange")
        product = _safe_shard_component(descriptor.get("product"), "product")
        path = _resolve_manifest_path(root, descriptor.get("path"))
        if not path.is_file():
            raise OptionSnapshotValidationError(f"option latest shard is missing: {path}")
        expected_hash = descriptor.get("sha256")
        if expected_hash and _sha256(path) != expected_hash:
            raise OptionSnapshotValidationError(
                f"option latest shard hash mismatch: {descriptor.get('path')}"
            )
        shard = read_json(path, default={})
        if not isinstance(shard, dict):
            raise OptionSnapshotValidationError("option latest shard is not an object")
        shard_records = shard.get("records")
        if (
            shard.get("trade_date") != trade_date
            or shard.get("exchange") != exchange
            or shard.get("product") != product
            or not isinstance(shard_records, list)
            or len(shard_records) != int(descriptor.get("record_count") or -1)
        ):
            raise OptionSnapshotValidationError(
                f"option latest shard metadata mismatch: {descriptor.get('path')}"
            )
        for record in shard_records:
            if not isinstance(record, dict):
                raise OptionSnapshotValidationError("option latest shard record is invalid")
            if (
                str(record.get("exchange") or "").upper() != exchange
                or str(record.get("product") or "").upper() != product
            ):
                raise OptionSnapshotValidationError(
                    f"option latest shard record is misclassified: {record.get('contract')}"
                )
            contract = str(record.get("contract") or "").upper()
            if contract in contracts:
                raise OptionSnapshotValidationError(
                    f"duplicate option contract across latest shards: {contract}"
                )
            contracts.add(contract)
            records.append(record)
    if len(records) != int(manifest.get("record_count") or -1):
        raise OptionSnapshotValidationError("option latest index record count mismatch")

    snapshot = {
        key: value
        for key, value in manifest.items()
        if key not in LATEST_STORAGE_KEYS and key != "schema_version"
    }
    snapshot["schema_version"] = manifest.get("snapshot_schema_version", 1)
    snapshot["records"] = records
    return snapshot


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
    surface_shadow_days: int = 5,
) -> None:
    """Publish one verified EOD chain with bounded latest and history storage."""
    if chain_limit < 1 or summary_limit < 1:
        raise ValueError("option retention limits must be positive")
    validate_option_snapshot(snapshot)
    trade_date = snapshot["trade_date"]
    quality = assess_option_snapshot_quality(snapshot)
    surface = build_option_surface(snapshot)
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
    had_previous_surface = (root / "surface_latest.json").exists()
    append_option_history(snapshot, data_dir)
    write_option_latest(
        promoted_snapshot,
        data_dir,
        snapshot_retention_days=chain_limit,
    )
    write_json_if_changed(
        root / "quality_latest.json",
        {
            "schema_version": 1,
            "trade_date": trade_date,
            "generated_at": snapshot.get("generated_at"),
            "quality": quality,
        },
    )
    previous_shadow = read_json(root / "surface_shadow_state.json", default={}) or {}
    surface_shadow = update_shadow_state(
        previous_shadow,
        requested_date=trade_date,
        validation_passed=bool(surface["promotion_eligible"]),
        required_pass_days=surface_shadow_days,
    )
    write_json_gzip_if_changed(root / "surface_attempt_latest.json.gz", surface)
    write_json_if_changed(root / "surface_shadow_state.json", surface_shadow)
    surface_published = bool(surface_shadow["promotion_allowed"])
    if surface_published:
        write_json_if_changed(root / "surface_latest.json", surface)
    write_json_if_changed(
        root / "surface_last_run_status.json",
        {
            "schema_version": 1,
            "trade_date": trade_date,
            "generated_at": snapshot.get("generated_at"),
            "status": surface["status"],
            "series_count": surface["series_count"],
            "surface_ready_count": surface["surface_ready_count"],
            "positioning_ready_count": surface["positioning_ready_count"],
            "execution_ready_count": surface["execution_ready_count"],
            "published": surface_published,
            "previous_valid_surface_retained": bool(
                had_previous_surface and not surface_published
            ),
            "shadow_state": surface_shadow,
        },
    )
    snapshot_root = root / "snapshots"
    legacy_snapshot = snapshot_root / f"{trade_date}.json"
    if legacy_snapshot.exists():
        legacy_snapshot.unlink()

    snapshot_files = sorted(
        path
        for path in snapshot_root.iterdir()
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


def publish_option_attempt(
    snapshot: dict[str, Any],
    data_dir: Path,
    *,
    surface_shadow_days: int = 5,
) -> None:
    """Persist a validated partial attempt without promoting global latest."""

    validate_option_snapshot(snapshot)
    quality = assess_option_snapshot_quality(snapshot)
    surface = build_option_surface(snapshot)
    attempt = dict(snapshot)
    attempt["quality"] = quality
    attempt["attempt_only"] = True
    attempt["promotion_eligible"] = bool(
        (snapshot.get("coverage") or {}).get("publish_eligible")
    )
    write_json_gzip_if_changed(
        data_dir / "options" / "attempt_latest.json.gz",
        attempt,
    )
    root = data_dir / "options"
    had_previous_surface = (root / "surface_latest.json").exists()
    write_json_gzip_if_changed(root / "surface_attempt_latest.json.gz", surface)
    previous_shadow = read_json(root / "surface_shadow_state.json", default={}) or {}
    surface_shadow = update_shadow_state(
        previous_shadow,
        requested_date=str(snapshot.get("trade_date")),
        validation_passed=bool(surface["promotion_eligible"]),
        required_pass_days=surface_shadow_days,
    )
    write_json_if_changed(root / "surface_shadow_state.json", surface_shadow)
    write_json_if_changed(
        root / "surface_last_run_status.json",
        {
            "schema_version": 1,
            "trade_date": snapshot.get("trade_date"),
            "generated_at": snapshot.get("generated_at"),
            "status": surface["status"],
            "series_count": surface["series_count"],
            "surface_ready_count": surface["surface_ready_count"],
            "positioning_ready_count": surface["positioning_ready_count"],
            "execution_ready_count": surface["execution_ready_count"],
            "published": False,
            "attempt_only": True,
            "previous_valid_surface_retained": had_previous_surface,
            "shadow_state": surface_shadow,
        },
    )
