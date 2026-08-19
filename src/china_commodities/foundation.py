"""Pinned-ID Physical and External daily data foundation.

The collector is intentionally EOD-only.  It never performs semantic search,
never requests minute/session data, and never persists commercial response
bodies.  Each pinned series is isolated so one entitlement failure cannot
erase the previous valid observation for other series.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import pandas as pd

from .collectors.ifind_http_adapter import IFindHTTPClient
from .derivations import calculate_basis, calculate_import_parity
from .history_storage import append_parquet_history
from .normalize import iso_date
from .promotion import update_shadow_state
from .source_registry import (
    CoverageTarget,
    SeriesDefinition,
    SourceRegistry,
    load_source_registry,
)
from .storage import read_json, write_json_if_changed


FOUNDATION_DOMAINS = ("physical", "external")
EXTERNAL_HISTORY_OBSERVATION_LIMIT = 252


def _classification(exc: Exception) -> str:
    text = str(exc).lower()
    if any(value in text for value in ("permission", "entitlement", "权限", "-4302")):
        return "no_permission"
    if any(value in text for value in ("token", "access_token", "refresh_token", "auth")):
        return "authentication_error"
    if any(value in text for value in ("transport", "timeout", "timed out", "http 5")):
        return "transport_error"
    return "collection_error"


def _limited_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:400]}"


def _record_hash(record: Mapping[str, Any]) -> str:
    stable = {
        key: value
        for key, value in record.items()
        if key not in {"requested_date", "generated_at", "current_collection_state"}
    }
    encoded = json.dumps(
        stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _base_record(
    definition: SeriesDefinition,
    registry: SourceRegistry,
    requested_date: str,
) -> dict[str, Any]:
    record = {
        "series_key": definition.series_key,
        "domain": definition.domain,
        "product": definition.product,
        "exchange": definition.exchange,
        "target": definition.target,
        "metric": definition.metric,
        "requested_date": requested_date,
        "source_date": None,
        "observation_date": None,
        "timezone": registry.timezone,
        "indicator_id": definition.indicator_id,
        "report_id": definition.report_id,
        "source_endpoint": definition.source_endpoint,
        "permission_status": "verified",
        "mapping_verified_on": registry.mapping_verified_on,
        "current_permission_status": None,
        "name": definition.name,
        "value": None,
        "unit": definition.unit,
        "frequency": definition.frequency,
        "vendor": registry.vendor,
        "original_source": definition.original_source,
        "usage": definition.usage,
        "contract_kind": definition.contract_kind,
        "max_lag_days": definition.max_lag_days,
        "quality_state": "unavailable",
        "missing_reason": None,
        "current_collection_state": None,
        "carried_forward": False,
        "carried_from_observation_date": None,
        "is_stale": None,
        "normalized_value_sha256": None,
    }
    record.update(definition.metadata)
    return record


def _is_stale(requested: str, observed: str, max_lag_days: int) -> bool:
    return (date.fromisoformat(requested) - date.fromisoformat(observed)).days > max_lag_days


def _history_rows(
    frame: pd.DataFrame,
    definition: SeriesDefinition,
    registry: SourceRegistry,
    requested_date: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if frame.empty:
        return rows
    for item in frame.to_dict(orient="records"):
        observed = str(item.get("observation_date") or "")
        value = item.get("value")
        if not observed or value is None or observed > requested_date:
            continue
        record = _base_record(definition, registry, requested_date)
        record.update(
            {
                "source_date": observed,
                "observation_date": observed,
                "value": float(value),
                "quality_state": "historical_observation",
                "missing_reason": None,
                "current_collection_state": "ok",
                "current_permission_status": "available",
                "is_stale": _is_stale(
                    requested_date, observed, definition.max_lag_days
                ),
            }
        )
        record["normalized_value_sha256"] = _record_hash(record)
        rows.append(record)
    return rows


def _carry_previous(
    current: dict[str, Any],
    previous: Mapping[str, Any] | None,
    *,
    requested_date: str,
    reason: str,
    state: str,
) -> dict[str, Any]:
    if not previous or previous.get("value") is None or not previous.get("observation_date"):
        current.update(
            {
                "quality_state": state,
                "missing_reason": reason,
                "current_collection_state": state,
                "current_permission_status": state,
                "is_stale": None,
            }
        )
        return current
    carried = dict(previous)
    carried.update(
        {
            "requested_date": requested_date,
            "quality_state": "carried_forward",
            "missing_reason": reason,
            "current_collection_state": state,
            "current_permission_status": state,
            "carried_forward": True,
            "carried_from_observation_date": previous.get("observation_date"),
            "is_stale": _is_stale(
                requested_date,
                str(previous["observation_date"]),
                int(current.get("max_lag_days") or 0),
            ),
        }
    )
    carried["normalized_value_sha256"] = _record_hash(carried)
    return carried


def _collect_series(
    definition: SeriesDefinition,
    registry: SourceRegistry,
    requested_date: str,
    *,
    client: IFindHTTPClient,
    start_date: str,
    previous: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    base = _base_record(definition, registry, requested_date)
    try:
        frame = client.edb_series(
            [definition.indicator_id],
            start_date=start_date,
            end_date=requested_date,
        )
        if not isinstance(frame, pd.DataFrame):
            raise TypeError("iFinD EDB adapter did not return a DataFrame")
        candidates = frame.loc[
            (frame.get("indicator_id") == definition.indicator_id)
            & (frame.get("observation_date").astype(str) <= requested_date)
        ].copy() if not frame.empty else pd.DataFrame()
        history = _history_rows(frame, definition, registry, requested_date)
        if definition.domain == "external":
            history = history[-EXTERNAL_HISTORY_OBSERVATION_LIMIT:]
        if candidates.empty:
            record = _carry_previous(
                base,
                previous,
                requested_date=requested_date,
                reason="iFinD returned no usable observation for the pinned indicator ID",
                state="no_data",
            )
        else:
            candidates = candidates.sort_values("observation_date")
            latest = candidates.iloc[-1]
            observed = str(latest["observation_date"])
            value = float(latest["value"])
            if previous and str(previous.get("observation_date") or "") > observed:
                record = _carry_previous(
                    base,
                    previous,
                    requested_date=requested_date,
                    reason="current query returned an observation older than the retained snapshot",
                    state="regressed_source_date",
                )
            else:
                stale = _is_stale(requested_date, observed, definition.max_lag_days)
                record = base
                record.update(
                    {
                        "source_date": observed,
                        "observation_date": observed,
                        "value": value,
                        "quality_state": "stale" if stale else "fresh",
                        "missing_reason": (
                            f"observation exceeds allowed lag of {definition.max_lag_days} day(s)"
                            if stale
                            else None
                        ),
                        "current_collection_state": "ok",
                        "current_permission_status": "available",
                        "is_stale": stale,
                    }
                )
                record["normalized_value_sha256"] = _record_hash(record)
        status = {
            "series_key": definition.series_key,
            "indicator_id": definition.indicator_id,
            "state": record["current_collection_state"],
            "quality_state": record["quality_state"],
            "source_date": record.get("source_date"),
            "carried_forward": record.get("carried_forward", False),
            "detail": record.get("missing_reason"),
        }
        return record, status, history
    except Exception as exc:
        state = _classification(exc)
        detail = _limited_error(exc)
        record = _carry_previous(
            base,
            previous,
            requested_date=requested_date,
            reason=detail,
            state=state,
        )
        return (
            record,
            {
                "series_key": definition.series_key,
                "indicator_id": definition.indicator_id,
                "state": state,
                "quality_state": record["quality_state"],
                "source_date": record.get("source_date"),
                "carried_forward": record.get("carried_forward", False),
                "detail": detail,
            },
            [],
        )


def _target_matrix(
    targets: tuple[CoverageTarget, ...], records: list[dict[str, Any]], requested_date: str
) -> list[dict[str, Any]]:
    by_key: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        key = str(record.get("product") or record.get("target") or "")
        by_key.setdefault(key, []).append(record)
    matrix = []
    for target in targets:
        values = by_key.get(target.key, [])
        states = [str(value.get("quality_state")) for value in values]
        if target.mapping_status == "unavailable":
            quality = "unavailable"
            reason = target.missing_reason
        elif states and all(state == "fresh" for state in states):
            quality = "fresh"
            reason = None
        elif "carried_forward" in states:
            quality = "carried_forward"
            reason = next(
                (value.get("missing_reason") for value in values if value.get("missing_reason")),
                None,
            )
        elif "stale" in states:
            quality = "stale"
            reason = next(
                (value.get("missing_reason") for value in values if value.get("missing_reason")),
                None,
            )
        else:
            quality = states[0] if states else "no_data"
            reason = next(
                (value.get("missing_reason") for value in values if value.get("missing_reason")),
                target.missing_reason,
            )
        matrix.append(
            {
                "key": target.key,
                "exchange": target.exchange,
                "requested_date": requested_date,
                "mapping_status": target.mapping_status,
                "permission_status": target.permission_status,
                "current_permission_status": (
                    next(
                        (
                            value.get("current_permission_status")
                            for value in values
                            if value.get("current_permission_status")
                            not in (None, "available")
                        ),
                        "available" if values else target.permission_status,
                    )
                ),
                "quality_state": quality,
                "series_keys": [value.get("series_key") for value in values],
                "missing_reason": reason,
            }
        )
    return matrix


def _main_futures(latest: Mapping[str, Any], product: str) -> Mapping[str, Any] | None:
    for curve in latest.get("commodity_curves") or []:
        if str(curve.get("product") or "").upper() == product.upper():
            main = curve.get("main_contract")
            if isinstance(main, Mapping):
                output = dict(main)
                output.setdefault("product", curve.get("product"))
                output.setdefault("exchange", curve.get("exchange"))
                output.setdefault("trade_date", latest.get("trade_date"))
                output.setdefault("source_trade_date", latest.get("trade_date"))
                return output
    return None


def _physical_derivations(
    records: list[dict[str, Any]], data_dir: Path, registry: SourceRegistry
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    latest = read_json(data_dir / "latest.json", default={}) or {}
    basis = [
        calculate_basis(record, _main_futures(latest, str(record.get("product"))))
        for record in records
        if record.get("metric") == "spot"
    ]
    parities = [calculate_import_parity(definition, {}) for definition in registry.parities]
    official_warehouse = [
        record
        for record in latest.get("warehouse_inventory") or []
        if str(record.get("product") or "").upper()
        in {target.key for target in registry.physical_products}
    ]
    return basis, parities, official_warehouse


def _validate_foundation_payload(
    records: list[dict[str, Any]],
    matrix: list[dict[str, Any]],
    requested_date: str,
) -> list[str]:
    errors: list[str] = []
    accepted_target_states = {"fresh", "stale", "unavailable"}
    for target in matrix:
        if target.get("quality_state") not in accepted_target_states:
            errors.append(
                f"{target.get('key')}: invalid target quality state "
                f"{target.get('quality_state')}"
            )
    required_fields = (
        "requested_date",
        "source_date",
        "observation_date",
        "timezone",
        "indicator_id",
        "source_endpoint",
        "unit",
        "frequency",
        "vendor",
        "original_source",
        "quality_state",
    )
    for record in records:
        key = record.get("series_key")
        if record.get("carried_forward") is True:
            errors.append(f"{key}: current collection is carried forward")
        if record.get("current_collection_state") != "ok":
            errors.append(
                f"{key}: current collection state={record.get('current_collection_state')}"
            )
        if record.get("value") is None:
            errors.append(f"{key}: value is null")
        missing = [field for field in required_fields if record.get(field) in (None, "")]
        if missing:
            errors.append(f"{key}: missing fields {', '.join(missing)}")
        if str(record.get("observation_date") or "9999-12-31") > requested_date:
            errors.append(f"{key}: observation date is after requested date")
    return sorted(set(errors))


def collect_foundation_domain(
    domain: str,
    requested_date: str,
    *,
    data_dir: str | Path = "data",
    registry: SourceRegistry | None = None,
    registry_path: str | Path | None = None,
    client: IFindHTTPClient | None = None,
    lookback_days: int = 400,
    publish: bool = True,
    shadow_days: int = 5,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Collect and optionally publish one foundation domain."""

    if domain not in FOUNDATION_DOMAINS:
        raise ValueError(f"unsupported foundation domain: {domain}")
    normalized_date = iso_date(requested_date)
    if lookback_days < 1:
        raise ValueError("lookback_days must be positive")
    source_registry = registry or load_source_registry(registry_path)
    target_root = Path(data_dir) / domain
    promoted_payload = read_json(target_root / "latest.json", default={}) or {}
    previous_payload = promoted_payload or (
        read_json(target_root / "attempt_latest.json", default={}) or {}
    )
    previous_by_key = {
        str(record.get("series_key")): record
        for record in previous_payload.get("series") or []
        if isinstance(record, Mapping) and record.get("series_key")
    }
    targets = (
        source_registry.physical_products
        if domain == "physical"
        else source_registry.external_targets
    )
    definitions = source_registry.series(domain)
    http_client = client or IFindHTTPClient(
        minimum_request_interval_seconds=0.55
    )
    start_date = (
        date.fromisoformat(normalized_date) - timedelta(days=lookback_days)
    ).isoformat()
    records: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []
    history_rows: list[dict[str, Any]] = []
    for definition in definitions:
        record, status, historical = _collect_series(
            definition,
            source_registry,
            normalized_date,
            client=http_client,
            start_date=start_date,
            previous=previous_by_key.get(definition.series_key),
        )
        records.append(record)
        statuses.append(status)
        history_rows.extend(historical)

    matrix = _target_matrix(targets, records, normalized_date)
    generated = now or datetime.now(ZoneInfo(source_registry.timezone))
    coverage = {
        "target_count": len(targets),
        "verified_mapping_count": sum(
            target.mapping_status == "verified" for target in targets
        ),
        "unavailable_mapping_count": sum(
            target.mapping_status == "unavailable" for target in targets
        ),
        "fresh_target_count": sum(item["quality_state"] == "fresh" for item in matrix),
        "stale_target_count": sum(item["quality_state"] == "stale" for item in matrix),
        "carried_forward_target_count": sum(
            item["quality_state"] == "carried_forward" for item in matrix
        ),
        "failed_target_count": sum(
            item["quality_state"]
            not in {"fresh", "stale", "carried_forward", "unavailable"}
            for item in matrix
        ),
    }
    payload: dict[str, Any] = {
        "schema_version": 1,
        "domain": domain,
        "requested_date": normalized_date,
        "generated_at": generated.isoformat(),
        "timezone": source_registry.timezone,
        "vendor": source_registry.vendor,
        "intraday_used": False,
        "production_uses_natural_language_search": False,
        "coverage": coverage,
        "coverage_matrix": matrix,
        "series": records,
        "fundamental_score": None,
        "convexity_score": None,
        "provider_consensus_ready": False,
        "provider_consensus_missing_reason": "an independent second provider is not configured",
        "provider_consensus": {
            "ready": False,
            "providers": [source_registry.vendor],
            "independent_second_source_required": True,
            "missing_reason": "an independent second provider is not configured",
        },
    }
    if domain == "physical":
        basis, parities, official_warehouse = _physical_derivations(
            records, Path(data_dir), source_registry
        )
        payload.update(
            {
                "basis": basis,
                "import_parities": parities,
                "official_warehouse": official_warehouse,
            }
        )
    status_payload = {
        "schema_version": 1,
        "domain": domain,
        "requested_date": normalized_date,
        "generated_at": generated.isoformat(),
        "data_fresh": bool(
            definitions
            and all(record.get("quality_state") == "fresh" for record in records)
        ),
        "coverage": coverage,
        "series": statuses,
    }
    validation_errors = _validate_foundation_payload(
        records, matrix, normalized_date
    )
    validation_passed = not validation_errors
    status_payload["validation_passed"] = validation_passed
    status_payload["validation_errors"] = validation_errors
    if publish:
        if history_rows:
            append_parquet_history(
                target_root / "history.parquet",
                history_rows,
                key_fields=("series_key", "observation_date"),
                sort_fields=("series_key", "observation_date"),
            )
        previous_shadow = read_json(target_root / "shadow_state.json", default={}) or {}
        shadow_state = update_shadow_state(
            previous_shadow,
            requested_date=normalized_date,
            validation_passed=validation_passed,
            required_pass_days=shadow_days,
        )
        payload["promotion"] = shadow_state
        write_json_if_changed(target_root / "attempt_latest.json", payload)
        write_json_if_changed(target_root / "shadow_state.json", shadow_state)
        published = bool(shadow_state["promotion_allowed"])
        if published:
            write_json_if_changed(target_root / "latest.json", payload)
        status_payload.update(
            {
                "published": published,
                "shadow_state": shadow_state,
                "previous_valid_snapshot_retained": bool(
                    promoted_payload and not published
                ),
            }
        )
        write_json_if_changed(target_root / "last_run_status.json", status_payload)
    else:
        status_payload["published"] = False
    return {"payload": payload, "status": status_payload}


def run_foundation(
    requested_date: str,
    *,
    scope: str = "all",
    data_dir: str | Path = "data",
    registry_path: str | Path | None = None,
    client: IFindHTTPClient | None = None,
    lookback_days: int = 400,
    publish: bool = True,
    shadow_days: int = 5,
) -> dict[str, Any]:
    domains = FOUNDATION_DOMAINS if scope == "all" else (scope,)
    if any(domain not in FOUNDATION_DOMAINS for domain in domains):
        raise ValueError("foundation scope must be physical, external, or all")
    registry = load_source_registry(registry_path)
    http_client = client or IFindHTTPClient(
        minimum_request_interval_seconds=0.55
    )
    return {
        domain: collect_foundation_domain(
            domain,
            requested_date,
            data_dir=data_dir,
            registry=registry,
            client=http_client,
            lookback_days=lookback_days,
            publish=publish,
            shadow_days=shadow_days,
        )
        for domain in domains
    }


__all__ = [
    "FOUNDATION_DOMAINS",
    "collect_foundation_domain",
    "run_foundation",
]
