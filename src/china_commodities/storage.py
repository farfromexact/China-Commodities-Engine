"""Atomic JSON publication for verified and compact commodity artifacts."""

from __future__ import annotations

from collections.abc import Mapping
import gzip
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Any

from .features import contract_month
from .historical_features import build_market_state
from .history_storage import append_futures_history, rebuild_futures_history_from_snapshots
from .models import PipelineResult


DEFAULT_HISTORY_LIMIT = 20
DEFAULT_SNAPSHOT_LIMIT = 20
DEFAULT_NIGHT_SESSION_SNAPSHOT_LIMIT = 20
SNAPSHOT_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")


def _coverage_scope(result: PipelineResult) -> dict[str, Any]:
    return {
        "scope_id": result.scope_id,
        "included_exchanges": result.included_exchanges,
        "excluded_exchanges": result.excluded_exchanges,
        "is_full_market": not result.excluded_exchanges,
    }


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if hasattr(value, "item"):
        return json_safe(value.item())
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(json_safe(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    for attempt in range(5):
        try:
            os.replace(temporary, path)
            break
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.2 * (attempt + 1))


def write_json_gzip_atomic(path: Path, payload: Any) -> None:
    """Write deterministic gzip JSON so snapshots remain Git-friendly."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    content = (
        json.dumps(json_safe(payload), ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    temporary.write_bytes(gzip.compress(content, compresslevel=9, mtime=0))
    for attempt in range(5):
        try:
            os.replace(temporary, path)
            break
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.2 * (attempt + 1))


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    if path.suffix == ".gz":
        return json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
    return json.loads(path.read_text(encoding="utf-8"))


VOLATILE_AUDIT_KEYS = frozenset({"generated_at", "fetched_at"})


def _stable_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _stable_payload(item)
            for key, item in value.items()
            if key not in VOLATILE_AUDIT_KEYS
        }
    if isinstance(value, list):
        return [_stable_payload(item) for item in value]
    return value


def write_json_if_changed(path: Path, payload: Any) -> bool:
    """Avoid Git churn when only collection timestamps changed."""
    safe_payload = json_safe(payload)
    existing = read_json(path, default=None)
    if existing is not None and _stable_payload(existing) == _stable_payload(safe_payload):
        return False
    write_json_atomic(path, safe_payload)
    return True


def write_json_gzip_if_changed(path: Path, payload: Any) -> bool:
    """Write compressed JSON only when stable content changes."""

    safe_payload = json_safe(payload)
    existing = read_json(path, default=None)
    if existing is not None and _stable_payload(existing) == _stable_payload(safe_payload):
        return False
    write_json_gzip_atomic(path, safe_payload)
    return True


def _snapshot_payload(result: PipelineResult) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "trade_date": result.trade_date,
        "requested_date": result.trade_date,
        "generated_at": result.generated_at,
        "timezone": "Asia/Shanghai",
        "frequency": "EOD",
        "verified": result.verified,
        "official_complete": result.official_complete,
        "scope_verified": result.scope_verified,
        "core_futures_official_complete": result.core_futures_official_complete,
        "scope_official_complete": result.scope_official_complete,
        "module_quality": result.module_quality,
        "quality_metrics": result.quality_metrics,
        "coverage_scope": _coverage_scope(result),
        "source": {
            "provider": result.primary_provider,
            "akshare_version": result.akshare_version,
            "modules": [status.to_dict() for status in result.statuses],
        },
        "futures_contracts": result.futures_records,
        "commodity_curves": result.curves,
        "warehouse_inventory": result.warehouse_records,
        "proxy_basis": result.basis_records,
        "commodity_options": result.option_summaries,
        "member_rankings": result.member_ranking_summaries,
        "heatmap_candidates": result.candidates,
        "limitations": {
            "continuous_contracts_are_research_only": True,
            "basis_may_be_proxy": True,
            "warehouse_is_not_social_inventory": True,
            "dealer_gamma_direction_known": False,
            "seasonality_is_standalone_signal": False,
            "cross_sectional_activity_is_historical_anomaly": False,
            "product_option_summary_is_atm_iv": False,
            "member_rankings_are_participant_direction": False,
        },
    }


def _radar_payload(result: PipelineResult) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "trade_date": result.trade_date,
        "generated_at": result.generated_at,
        "verified": result.verified,
        "official_complete": result.official_complete,
        "scope_verified": result.scope_verified,
        "core_futures_official_complete": result.core_futures_official_complete,
        "scope_official_complete": result.scope_official_complete,
        "module_quality": result.module_quality,
        "quality_metrics": result.quality_metrics,
        "coverage_scope": _coverage_scope(result),
        "commodity_regime": None,
        "heatmap": result.candidates,
        "curve_basis": [
            {
                "exchange": curve["exchange"],
                "product": curve["product"],
                "product_name": curve["product_name"],
                "sector": curve["sector"],
                "main_contract": curve["main_contract"],
                "near_next_curve": curve["near_next_curve"],
                "basis": curve.get("basis"),
                "cross_sectional_activity_score": curve.get(
                    "cross_sectional_activity_score"
                ),
                "score_rank": curve.get("score_rank"),
                "evidence": curve.get("evidence"),
                "evidence_count": curve.get("evidence_count"),
            }
            for curve in result.curves
        ],
        "warehouse_inventory": result.warehouse_records,
        "commodity_options": result.option_summaries,
        "member_rankings": result.member_ranking_summaries,
        "session_freshness": [status.to_dict() for status in result.statuses],
        "no_trade_reason": "This engine identifies anomalies; it does not issue trades.",
    }


def _history_record(result: PipelineResult) -> dict[str, Any]:
    return {
        "trade_date": result.trade_date,
        "generated_at": result.generated_at,
        "coverage_scope": _coverage_scope(result),
        "products": [
            {
                "exchange": curve["exchange"],
                "product": curve["product"],
                "sector": curve["sector"],
                "main_contract": (curve.get("main_contract") or {}).get("contract"),
                "main_close": (curve.get("main_contract") or {}).get("close"),
                "main_close_return_pct": (curve.get("main_contract") or {}).get(
                    "close_return_pct"
                ),
                "main_volume": (curve.get("main_contract") or {}).get("volume"),
                "main_open_interest": (curve.get("main_contract") or {}).get(
                    "open_interest"
                ),
                "near_contract": (curve.get("nearest_liquid_contract") or {}).get(
                    "contract"
                ),
                "next_contract": (curve.get("next_liquid_contract") or {}).get(
                    "contract"
                ),
                "near_minus_next_pct": (curve.get("near_next_curve") or {}).get(
                    "near_minus_deferred_pct"
                ),
                "cross_sectional_activity_score": curve.get(
                    "cross_sectional_activity_score"
                ),
            }
            for curve in result.curves
        ],
    }


def _contract_meta(result: PipelineResult) -> dict[str, Any]:
    official = {
        (record["exchange"], record["contract"]): record
        for record in result.contract_metadata
    }
    contracts = []
    for record in result.futures_records:
        expiry = contract_month(record["contract"], result.trade_date)
        source = official.get((record["exchange"], record["contract"]), {})
        contracts.append({
            "exchange": record["exchange"],
            "product": record["product"],
            "contract": record["contract"],
            "requested_date": result.trade_date,
            "source_date": source.get("source_date"),
            "observation_date": source.get("source_date"),
            "timezone": "Asia/Shanghai",
            "frequency": "EOD",
            "vendor": source.get("metadata_vendor"),
            "original_source": source.get("original_source"),
            "quality_state": source.get("metadata_status", "observed_contract_only"),
            "missing_reason": (
                None if source else "official contract metadata unavailable"
            ),
            "contract_month": expiry.isoformat() if expiry else None,
            "multiplier": source.get("multiplier"),
            "tick_size": source.get("tick_size"),
            "tick_value": source.get("tick_value"),
            "night_session": source.get("night_session"),
            "delivery_unit": source.get("delivery_unit"),
            "delivery_grade": source.get("delivery_grade"),
            "delivery_location": source.get("delivery_location"),
            "margin_rate_percent": source.get("margin_rate_percent"),
            "price_limit_percent": source.get("price_limit_percent"),
            "list_date": source.get("list_date"),
            "last_trading_day": source.get("last_trading_day"),
            "last_delivery_day": source.get("last_delivery_day"),
            "metadata_status": source.get("metadata_status", "observed_contract_only"),
            "metadata_vendor": source.get("metadata_vendor"),
            "carried_forward": source.get("carried_forward", False),
            "carried_forward_fields": source.get("carried_forward_fields", []),
            "carried_from_trade_date": source.get("carried_from_trade_date"),
            "is_stale": source.get("is_stale", False),
        })
    def coverage(field: str, *, current_only: bool) -> float:
        def field_is_current(item: dict[str, Any]) -> bool:
            if item.get("source_date") != result.trade_date:
                return False
            if item.get("carried_forward") is not True:
                return True
            return field not in set(item.get("carried_forward_fields") or [])

        return (
            sum(
                item.get(field) is not None
                and (not current_only or field_is_current(item))
                for item in contracts
            )
            / len(contracts)
            if contracts
            else 0.0
        )

    matched = sum(
        item["metadata_status"] == "official_partial" for item in contracts
    )
    coverage_values = {
        "multiplier": coverage("multiplier", current_only=True),
        "tick_size": coverage("tick_size", current_only=True),
        "night_session": coverage("night_session", current_only=True),
        "delivery_unit": coverage("delivery_unit", current_only=True),
        "margin_rate_percent": coverage("margin_rate_percent", current_only=True),
        "price_limit_percent": coverage("price_limit_percent", current_only=True),
        "last_trading_day": coverage("last_trading_day", current_only=True),
    }
    static_gate = min(
        coverage_values[field]
        for field in ("multiplier", "tick_size", "night_session", "delivery_unit")
    )
    dynamic_gate = min(
        coverage_values[field]
        for field in ("margin_rate_percent", "price_limit_percent", "last_trading_day")
    )
    return {
        "schema_version": 1,
        "trade_date": result.trade_date,
        "generated_at": result.generated_at,
        "scope_verified": result.scope_verified,
        "requested_date": result.trade_date,
        "timezone": "Asia/Shanghai",
        "frequency": "EOD",
        "coverage_scope": _coverage_scope(result),
        "contracts": contracts,
        "contract_match_coverage": matched / len(contracts) if contracts else 0.0,
        "effective_contract_match_coverage": sum(
            item["metadata_status"]
            in {
                "official_partial",
                "official_partial_source_date_unverified",
                "carried_forward_previous_valid",
            }
            for item in contracts
        ) / len(contracts) if contracts else 0.0,
        "multiplier_coverage": coverage("multiplier", current_only=True),
        "tick_size_coverage": coverage("tick_size", current_only=True),
        "tick_value_coverage": coverage("tick_value", current_only=True),
        "night_session_coverage": coverage_values["night_session"],
        "delivery_unit_coverage": coverage_values["delivery_unit"],
        "delivery_grade_coverage": coverage("delivery_grade", current_only=True),
        "delivery_location_coverage": coverage("delivery_location", current_only=True),
        "margin_rate_coverage": coverage("margin_rate_percent", current_only=True),
        "price_limit_coverage": coverage("price_limit_percent", current_only=True),
        "last_trading_day_coverage": coverage("last_trading_day", current_only=True),
        "effective_multiplier_coverage": coverage("multiplier", current_only=False),
        "effective_tick_size_coverage": coverage("tick_size", current_only=False),
        "effective_tick_value_coverage": coverage("tick_value", current_only=False),
        "effective_night_session_coverage": coverage(
            "night_session", current_only=False
        ),
        "effective_delivery_unit_coverage": coverage(
            "delivery_unit", current_only=False
        ),
        "effective_margin_rate_coverage": coverage(
            "margin_rate_percent", current_only=False
        ),
        "effective_price_limit_coverage": coverage(
            "price_limit_percent", current_only=False
        ),
        "effective_last_trading_day_coverage": coverage(
            "last_trading_day", current_only=False
        ),
        "carried_forward_contract_count": sum(
            item.get("carried_forward") is True for item in contracts
        ),
        "coverage_targets": {
            "static_fields_minimum": 0.99,
            "dynamic_fields_minimum": 0.95,
        },
        "static_fields_min_coverage": static_gate,
        "dynamic_fields_min_coverage": dynamic_gate,
        "quality_state": (
            "complete" if static_gate >= 0.99 and dynamic_gate >= 0.95 else "partial"
        ),
        "warning": "Null trading parameters were not published by the verified exchange interface and must not be inferred.",
    }


def publish_night_session_derivatives(
    data_dir: str | Path,
    *,
    snapshot: Mapping[str, Any] | None = None,
    status: Mapping[str, Any] | None = None,
    history_limit: int = DEFAULT_NIGHT_SESSION_SNAPSHOT_LIMIT,
) -> dict[str, Any]:
    """Archive one verified night snapshot and remove deprecated EOD copies.

    ``data/night_session`` is the canonical night-session product.  Daily EOD
    artifacts intentionally remain free of intraday/session rows; report input
    builds its compact reference directly from the canonical source.
    """

    if history_limit < 1:
        raise ValueError("night-session snapshot limit must be positive")
    root = Path(data_dir)
    night_root = root / "night_session"
    source_snapshot = _mapping(
        snapshot
        if snapshot is not None
        else read_json(night_root / "latest.json", default={})
    )
    source_status = _mapping(
        status
        if status is not None
        else read_json(night_root / "last_run_status.json", default={})
    )
    trading_date = str(
        source_snapshot.get("trading_date") or source_status.get("trading_date") or ""
    )
    session_start_date = str(
        source_snapshot.get("session_start_date")
        or source_snapshot.get("night_session_date")
        or source_status.get("session_start_date")
        or source_status.get("night_session_date")
        or ""
    )
    valid_records = [
        item
        for item in source_snapshot.get("records") or []
        if isinstance(item, Mapping) and item.get("record_state") == "night_session"
    ]
    if (
        source_snapshot.get("frequency") != "night_session_snapshot"
        or source_status.get("data_fresh") is not True
        or source_status.get("validation_passed") is not True
        or source_status.get("published") is not True
        or not trading_date
        or not session_start_date
        or not valid_records
        or SNAPSHOT_NAME.fullmatch(f"{trading_date}.json") is None
    ):
        return {
            "published": False,
            "reason": "no verified timestamp-validated night-session snapshot",
            "updated_files": [],
        }
    updated_files: list[str] = []

    archive_path = night_root / f"{trading_date}.json"
    if write_json_if_changed(archive_path, source_snapshot):
        updated_files.append(archive_path.relative_to(root).as_posix())

    dated_snapshots = sorted(
        path
        for path in night_root.glob("*.json")
        if SNAPSHOT_NAME.fullmatch(path.name)
    )
    for obsolete in dated_snapshots[:-history_limit]:
        obsolete.unlink()
        updated_files.append(obsolete.relative_to(root).as_posix())

    legacy_fields = {
        "latest.json": ("night_session",),
        "market_state_latest.json": ("night_session",),
        "radar_latest.json": ("night_session",),
        "last_run_status.json": ("night_session",),
        "contract_meta.json": ("night_session_snapshot",),
        "radar_history.json": ("night_session_records",),
    }
    for relative, fields in legacy_fields.items():
        path = root / relative
        payload = _mapping(read_json(path, default={}))
        if not payload or not any(field in payload for field in fields):
            continue
        cleaned = dict(payload)
        for field in fields:
            cleaned.pop(field, None)
        if write_json_if_changed(path, cleaned):
            updated_files.append(relative)

    return {
        "published": True,
        "trading_date": trading_date,
        "night_session_date": session_start_date,
        "valid_contract_count": len(valid_records),
        "canonical_snapshot": archive_path.relative_to(root).as_posix(),
        "updated_files": updated_files,
    }


def publish_status(result: PipelineResult, data_dir: Path) -> None:
    write_json_if_changed(data_dir / "last_run_status.json", result.status_dict())


def publish_raw_options(result: PipelineResult, data_dir: Path) -> Path | None:
    if not result.option_records:
        return None
    path = data_dir / "raw" / result.trade_date / "commodity_options.json"
    write_json_atomic(
        path,
        {
            "schema_version": 1,
            "trade_date": result.trade_date,
            "generated_at": result.generated_at,
            "records": result.option_records,
        },
    )
    return path


def _publish_artifacts(
    result: PipelineResult,
    data_dir: Path,
    history_limit: int = DEFAULT_HISTORY_LIMIT,
    snapshot_limit: int = DEFAULT_SNAPSHOT_LIMIT,
) -> None:
    if history_limit < 1 or snapshot_limit < 1:
        raise ValueError("history and snapshot limits must be positive")
    snapshot = _snapshot_payload(result)
    radar_payload = _radar_payload(result)
    contract_meta_payload = _contract_meta(result)
    append_futures_history(result, data_dir)
    write_json_if_changed(data_dir / "latest.json", snapshot)
    write_json_if_changed(data_dir / "radar_latest.json", radar_payload)
    write_json_if_changed(data_dir / "contract_meta.json", contract_meta_payload)
    write_json_if_changed(
        data_dir / "snapshots" / f"{result.trade_date}.json", snapshot
    )
    # Reconcile the long history from retained local snapshots.  This is a
    # local repair/upsert only; it never makes an additional provider request.
    rebuild_futures_history_from_snapshots(data_dir, retention_days=252)
    snapshot_files = sorted(
        path
        for path in (data_dir / "snapshots").glob("*.json")
        if SNAPSHOT_NAME.fullmatch(path.name)
    )
    for obsolete in snapshot_files[:-snapshot_limit]:
        obsolete.unlink()

    retained_snapshot_files = sorted(
        path
        for path in (data_dir / "snapshots").glob("*.json")
        if SNAPSHOT_NAME.fullmatch(path.name)
    )
    retained_snapshots = [read_json(path) for path in retained_snapshot_files]
    market_state = build_market_state(
        [payload for payload in retained_snapshots if isinstance(payload, dict)]
    )
    write_json_if_changed(data_dir / "market_state_latest.json", market_state)

    history_path = data_dir / "radar_history.json"
    current = _mapping(
        read_json(history_path, default={"schema_version": 1, "records": []})
    )
    records = [
        record
        for record in current.get("records", [])
        if record.get("trade_date") != result.trade_date
    ]
    records.append(_history_record(result))
    records.sort(key=lambda record: record["trade_date"])
    history_payload = {
        "schema_version": current.get("schema_version", 1),
        "records": records[-history_limit:],
    }
    write_json_if_changed(history_path, history_payload)


def publish_verified(
    result: PipelineResult,
    data_dir: Path,
    history_limit: int = DEFAULT_HISTORY_LIMIT,
    snapshot_limit: int = DEFAULT_SNAPSHOT_LIMIT,
) -> None:
    if not result.verified:
        raise ValueError("refusing to publish an unverified commodity snapshot")
    _publish_artifacts(result, data_dir, history_limit, snapshot_limit)


def publish_scope_verified(
    result: PipelineResult,
    data_dir: Path,
    history_limit: int = DEFAULT_HISTORY_LIMIT,
    snapshot_limit: int = DEFAULT_SNAPSHOT_LIMIT,
) -> None:
    if not result.scope_verified:
        raise ValueError("refusing to publish an unverified scoped commodity snapshot")
    if result.verified:
        raise ValueError("full-market snapshots must use publish_verified")
    _publish_artifacts(result, data_dir, history_limit, snapshot_limit)
