"""AKShare-primary Physical and External end-of-day foundation.

This module intentionally keeps the public-source layer separate from the
iFinD pinned-ID collector in :mod:`china_commodities.foundation`.  Physical
records use AKShare's 100ppi spot/basis table; External records use AKShare's
Sina foreign-futures history.  Both provenance and coverage gaps remain
explicit so public-source data is never presented as an exchange settlement
or a fully aligned import-parity leg.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import pandas as pd

from .collectors.akshare_adapter import (
    akshare_version,
    collect_basis_daily,
    collect_foreign_futures_history,
)
from .derivations import calculate_basis, calculate_import_parity
from .history_storage import append_parquet_history
from .normalize import iso_date
from .promotion import update_shadow_state
from .source_registry import (
    CORE_PHYSICAL_PRODUCTS,
    EXTERNAL_TARGETS,
    load_source_registry,
)
from .storage import read_json, write_json_if_changed


FOUNDATION_DOMAINS = ("physical", "external")
_TIMEZONE = "Asia/Shanghai"
_EXTERNAL_HISTORY_OBSERVATION_LIMIT = 252


_PHYSICAL_EXCHANGES: dict[str, str] = {
    "I": "DCE",
    "JM": "DCE",
    "J": "DCE",
    "RB": "SHFE",
    "HC": "SHFE",
    "FG": "CZCE",
    "SA": "CZCE",
    "SC": "INE",
    "FU": "SHFE",
    "LU": "INE",
    "MA": "CZCE",
    "TA": "CZCE",
    "PX": "CZCE",
    "CU": "SHFE",
    "AL": "SHFE",
    "LC": "GFEX",
    "SI": "GFEX",
    "M": "DCE",
    "Y": "DCE",
    "P": "DCE",
}


@dataclass(frozen=True)
class _ExternalSpec:
    target: str
    symbol: str
    name: str
    unit: str
    contract_kind: str
    max_lag_days: int = 3


# ``futures_foreign_hist`` is AKShare's documented daily-history route backed
# by Sina Finance.  These are public market context series, not exact import
# parity legs, and therefore keep ``usage=context_only`` below.
_EXTERNAL_SPECS: tuple[_ExternalSpec, ...] = (
    _ExternalSpec("WTI", "CL", "NYMEX原油", "USD/bbl", "public_continuous"),
    _ExternalSpec("BRENT", "OIL", "布伦特原油", "USD/bbl", "public_continuous"),
    _ExternalSpec("LME_CU", "CAD", "LME铜3个月", "USD/t", "three_month"),
    _ExternalSpec("LME_AL", "AHD", "LME铝3个月", "USD/t", "three_month"),
    _ExternalSpec("LME_ZN", "ZSD", "LME锌3个月", "USD/t", "three_month"),
    _ExternalSpec("LME_NI", "NID", "LME镍3个月", "USD/t", "three_month"),
    _ExternalSpec("COMEX_AU", "GC", "COMEX黄金", "USD/troy_oz", "public_continuous"),
    _ExternalSpec("COMEX_AG", "SI", "COMEX白银", "USD/troy_oz", "public_continuous"),
    _ExternalSpec("SGX_IRON_ORE", "FEF", "新加坡铁矿石", "USD/t", "public_continuous"),
    _ExternalSpec("CBOT_SOYBEAN", "S", "CBOT黄豆", "USC/bu", "public_continuous"),
    _ExternalSpec("CBOT_SOYMEAL", "SM", "CBOT黄豆粉", "USD/short_ton", "public_continuous"),
    _ExternalSpec("CBOT_SOYBEAN_OIL", "BO", "CBOT黄豆油", "USC/lb", "public_continuous"),
    _ExternalSpec("CBOT_CORN", "C", "CBOT玉米", "USC/bu", "public_continuous"),
    _ExternalSpec("CBOT_WHEAT", "W", "CBOT小麦", "USC/bu", "public_continuous"),
    _ExternalSpec("BMD_PALM", "FCPO", "马棕油", "MYR/t", "public_continuous"),
    _ExternalSpec("ICE_SUGAR", "RS", "美国原糖", "USC/lb", "public_continuous"),
    _ExternalSpec("ICE_COTTON", "CT", "NYBOT棉花", "USC/lb", "public_continuous"),
)

_EXTERNAL_BY_TARGET = {spec.target: spec for spec in _EXTERNAL_SPECS}
_EXTERNAL_UNAVAILABLE: dict[str, str] = {
    "DUBAI_OMAN": "No exact Dubai/Oman public daily contract is configured in AKShare.",
    "SINGAPORE_HSFO": "No exact Singapore HSFO public daily contract is configured in AKShare.",
    "SINGAPORE_VLSFO": "No exact Singapore VLSFO public daily contract is configured in AKShare.",
    "USDCNH": "AKShare's public USD/CNY quote is not an exact USD/CNH replacement.",
    "DXY": "No stable, exact public Dollar Index daily route is configured in AKShare.",
}

if tuple(_PHYSICAL_EXCHANGES) != CORE_PHYSICAL_PRODUCTS:
    raise RuntimeError("AKShare Physical product order must match the approved scope")
if set(_EXTERNAL_BY_TARGET).union(_EXTERNAL_UNAVAILABLE) != set(EXTERNAL_TARGETS):
    raise RuntimeError("AKShare External target coverage must match the approved scope")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _date_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return pd.Timestamp(value).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _is_stale(requested_date: str, source_date: str, max_lag_days: int) -> bool:
    return (date.fromisoformat(requested_date) - date.fromisoformat(source_date)).days > max_lag_days


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
    *,
    domain: str,
    requested_date: str,
    series_key: str,
    indicator_id: str,
    source_endpoint: str,
    name: str,
    unit: str,
    frequency: str,
    original_source: str,
    usage: str,
    max_lag_days: int,
    product: str | None = None,
    exchange: str | None = None,
    target: str | None = None,
    metric: str | None = None,
    contract_kind: str | None = None,
) -> dict[str, Any]:
    return {
        "series_key": series_key,
        "domain": domain,
        "product": product,
        "exchange": exchange,
        "target": target,
        "metric": metric,
        "requested_date": requested_date,
        "source_date": None,
        "observation_date": None,
        "timezone": _TIMEZONE,
        "indicator_id": indicator_id,
        "report_id": None,
        "source_endpoint": source_endpoint,
        "permission_status": "public",
        "current_permission_status": None,
        "name": name,
        "value": None,
        "unit": unit,
        "frequency": frequency,
        "vendor": "AKShare",
        "source_provider": "akshare",
        "original_source": original_source,
        "usage": usage,
        "contract_kind": contract_kind,
        "max_lag_days": max_lag_days,
        "quality_state": "unavailable",
        "missing_reason": None,
        "current_collection_state": None,
        "carried_forward": False,
        "carried_from_observation_date": None,
        "is_stale": None,
        "is_fallback": False,
        "normalized_value_sha256": None,
    }


def _history_record(record: Mapping[str, Any], source_date: str, value: float) -> dict[str, Any]:
    history = dict(record)
    history.update(
        {
            "source_date": source_date,
            "observation_date": source_date,
            "value": value,
            "quality_state": "historical_observation",
            "missing_reason": None,
            "current_collection_state": "ok",
            "current_permission_status": "available",
            "carried_forward": False,
            "carried_from_observation_date": None,
            "is_stale": _is_stale(
                str(history["requested_date"]),
                source_date,
                int(history["max_lag_days"]),
            ),
        }
    )
    history["normalized_value_sha256"] = _record_hash(history)
    return history


def _previous_by_key(target_root: Path) -> dict[str, dict[str, Any]]:
    previous: dict[str, dict[str, Any]] = {}
    for payload in (
        _mapping(read_json(target_root / "latest.json", default={})),
        _mapping(read_json(target_root / "attempt_latest.json", default={})),
    ):
        for item in payload.get("series") or []:
            if not isinstance(item, Mapping) or not item.get("series_key"):
                continue
            record = dict(item)
            key = str(record["series_key"])
            current = previous.get(key)
            if current is None or (
                str(record.get("observation_date") or ""),
                str(record.get("requested_date") or ""),
            ) > (
                str(current.get("observation_date") or ""),
                str(current.get("requested_date") or ""),
            ):
                previous[key] = record
    return previous


def _carry_previous(
    base: dict[str, Any],
    previous: Mapping[str, Any] | None,
    *,
    reason: str,
    state: str,
) -> dict[str, Any]:
    if not previous or previous.get("value") is None or not previous.get("observation_date"):
        base.update(
            {
                "quality_state": state,
                "missing_reason": reason,
                "current_collection_state": state,
                "current_permission_status": state,
                "is_stale": None,
            }
        )
        return base
    carried = dict(previous)
    carried.update(
        {
            "requested_date": base["requested_date"],
            "quality_state": "carried_forward",
            "missing_reason": reason,
            "current_collection_state": state,
            "current_permission_status": state,
            "carried_forward": True,
            "carried_from_observation_date": previous.get("observation_date"),
            "is_stale": _is_stale(
                str(base["requested_date"]),
                str(previous["observation_date"]),
                int(base["max_lag_days"]),
            ),
        }
    )
    carried["normalized_value_sha256"] = _record_hash(carried)
    return carried


def _physical_base(product: str, requested_date: str) -> dict[str, Any]:
    return _base_record(
        domain="physical",
        requested_date=requested_date,
        series_key=f"physical.{product}.akshare_100ppi_spot",
        indicator_id=f"AKSHARE:100PPI:{product}",
        source_endpoint="futures_spot_price",
        name=f"100ppi现货价:{product}",
        unit="CNY/t",
        frequency="D",
        original_source="100ppi via AKShare",
        usage="physical_evidence",
        max_lag_days=0,
        product=product,
        exchange=_PHYSICAL_EXCHANGES[product],
        metric="spot",
    )


def _external_base(spec: _ExternalSpec, requested_date: str) -> dict[str, Any]:
    return _base_record(
        domain="external",
        requested_date=requested_date,
        series_key=f"external.{spec.target}.akshare_{spec.symbol.lower()}_close",
        indicator_id=f"AKSHARE:SINA:{spec.symbol}",
        source_endpoint="futures_foreign_hist",
        name=spec.name,
        unit=spec.unit,
        frequency="D",
        original_source="Sina Finance foreign futures via AKShare",
        usage="context_only",
        max_lag_days=spec.max_lag_days,
        target=spec.target,
        metric="close",
        contract_kind=spec.contract_kind,
    )


def _target_matrix(
    targets: tuple[str, ...],
    *,
    records: Mapping[str, Mapping[str, Any]],
    unavailable_reasons: Mapping[str, str],
    configured_targets: set[str],
    requested_date: str,
    physical: bool,
) -> list[dict[str, Any]]:
    matrix: list[dict[str, Any]] = []
    for key in targets:
        record = records.get(key)
        if record is not None:
            quality = str(record.get("quality_state") or "collection_error")
            reason = record.get("missing_reason")
            current_permission = record.get("current_permission_status")
            series_keys = [record.get("series_key")]
            mapping_status = "configured"
            permission_status = "public"
        else:
            quality = "unavailable"
            reason = unavailable_reasons.get(
                key,
                "AKShare returned no usable observation for this configured target.",
            )
            current_permission = "unavailable"
            series_keys = []
            mapping_status = "configured" if key in configured_targets else "unavailable"
            permission_status = "public" if key in configured_targets else "not_available"
        matrix.append(
            {
                "key": key,
                "exchange": _PHYSICAL_EXCHANGES.get(key) if physical else None,
                "requested_date": requested_date,
                "mapping_status": mapping_status,
                "permission_status": permission_status,
                "current_permission_status": current_permission,
                "quality_state": quality,
                "series_keys": [value for value in series_keys if value],
                "missing_reason": reason,
            }
        )
    return matrix


def _coverage(
    matrix: list[dict[str, Any]],
    *,
    configured_target_count: int,
    status_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "target_count": len(matrix),
        "configured_mapping_count": configured_target_count,
        "unavailable_mapping_count": len(matrix) - configured_target_count,
        "fresh_target_count": sum(item["quality_state"] == "fresh" for item in matrix),
        "stale_target_count": sum(item["quality_state"] == "stale" for item in matrix),
        "carried_forward_target_count": sum(
            item["quality_state"] == "carried_forward" for item in matrix
        ),
        "unavailable_target_count": sum(
            item["quality_state"] == "unavailable" for item in matrix
        ),
        "failed_target_count": sum(
            item["quality_state"]
            not in {"fresh", "stale", "carried_forward", "unavailable"}
            for item in matrix
        ),
        "series_count": len(status_rows),
        "cache_hit_count": 0,
        "request_count": sum(bool(item.get("request_made")) for item in status_rows),
    }


def _validate_payload(
    records: list[dict[str, Any]],
    matrix: list[dict[str, Any]],
    requested_date: str,
) -> list[str]:
    errors: list[str] = []
    accepted_target_states = {"fresh", "stale", "unavailable", "carried_forward"}
    for target in matrix:
        state = target.get("quality_state")
        key = target.get("key")
        if state not in accepted_target_states:
            errors.append(f"{key}: invalid target quality state {state}")
        if state == "carried_forward":
            errors.append(f"{key}: current collection is carried forward")
    configured_observations = sum(
        target.get("mapping_status") == "configured"
        and target.get("quality_state") in {"fresh", "stale"}
        for target in matrix
    )
    if any(target.get("mapping_status") == "configured" for target in matrix) and not (
        configured_observations
    ):
        errors.append("no configured AKShare target returned a usable observation")
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


def _main_futures(latest: Mapping[str, Any], product: str) -> Mapping[str, Any] | None:
    for curve in latest.get("commodity_curves") or []:
        if str(curve.get("product") or "").upper() == product:
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
    records: list[dict[str, Any]], data_dir: Path, registry_path: str | Path | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    latest = _mapping(read_json(data_dir / "latest.json", default={}))
    basis = [
        calculate_basis(record, _main_futures(latest, str(record.get("product"))))
        for record in records
        if record.get("metric") == "spot"
    ]
    registry = load_source_registry(registry_path)
    parities = [calculate_import_parity(definition, {}) for definition in registry.parities]
    official_warehouse = [
        record
        for record in latest.get("warehouse_inventory") or []
        if str(record.get("product") or "").upper() in set(CORE_PHYSICAL_PRODUCTS)
    ]
    return basis, parities, official_warehouse


def _publish(
    *,
    target_root: Path,
    payload: dict[str, Any],
    status: dict[str, Any],
    history_rows: list[dict[str, Any]],
    requested_date: str,
    publish: bool,
    shadow_days: int,
) -> None:
    if not publish:
        status["published"] = False
        return
    if history_rows:
        append_parquet_history(
            target_root / "history.parquet",
            history_rows,
            key_fields=("series_key", "observation_date"),
            sort_fields=("series_key", "observation_date"),
        )
    previous_shadow = _mapping(read_json(target_root / "shadow_state.json", default={}))
    shadow_state = update_shadow_state(
        previous_shadow,
        requested_date=requested_date,
        validation_passed=bool(status["validation_passed"]),
        required_pass_days=shadow_days,
    )
    payload["promotion"] = shadow_state
    write_json_if_changed(target_root / "attempt_latest.json", payload)
    write_json_if_changed(target_root / "shadow_state.json", shadow_state)
    published = bool(shadow_state["promotion_allowed"])
    if published:
        write_json_if_changed(target_root / "latest.json", payload)
    status.update(
        {
            "published": published,
            "shadow_state": shadow_state,
            "previous_valid_snapshot_retained": bool(
                _mapping(read_json(target_root / "latest.json", default={})) and not published
            ),
        }
    )
    write_json_if_changed(target_root / "last_run_status.json", status)


def _collect_physical(
    requested_date: str,
    *,
    target_root: Path,
    ak_module: Any | None,
) -> tuple[
    dict[str, dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, str]
]:
    records: dict[str, dict[str, Any]] = {}
    statuses: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    unavailable: dict[str, str] = {}
    try:
        frame = collect_basis_daily(
            requested_date, products=CORE_PHYSICAL_PRODUCTS, ak_module=ak_module
        )
        if not isinstance(frame, pd.DataFrame):
            raise TypeError("AKShare futures_spot_price did not return a DataFrame")
        if "symbol" not in frame.columns or "spot_price" not in frame.columns:
            raise ValueError("AKShare futures_spot_price response is missing symbol/spot_price")
        for _, row in frame.iterrows():
            product = str(row.get("symbol") or "").upper()
            if product not in _PHYSICAL_EXCHANGES:
                continue
            source_date = _date_text(row.get("date"))
            value = _number(row.get("spot_price"))
            if source_date is None or value is None or source_date > requested_date:
                continue
            record = _physical_base(product, requested_date)
            stale = _is_stale(requested_date, source_date, 0)
            record.update(
                {
                    "source_date": source_date,
                    "observation_date": source_date,
                    "value": value,
                    "quality_state": "stale" if stale else "fresh",
                    "missing_reason": (
                        "AKShare 100ppi source date differs from the requested EOD"
                        if stale
                        else None
                    ),
                    "current_collection_state": "ok",
                    "current_permission_status": "available",
                    "is_stale": stale,
                    "basis_quality": "C",
                    "near_contract": str(row.get("near_contract") or "") or None,
                    "dominant_contract": str(row.get("dominant_contract") or "") or None,
                    "near_contract_price": _number(row.get("near_contract_price")),
                    "dominant_contract_price": _number(row.get("dominant_contract_price")),
                }
            )
            record["normalized_value_sha256"] = _record_hash(record)
            records[product] = record
            statuses.append(
                {
                    "series_key": record["series_key"],
                    "indicator_id": record["indicator_id"],
                    "state": "ok",
                    "quality_state": record["quality_state"],
                    "source_date": source_date,
                    "carried_forward": False,
                    "detail": record["missing_reason"],
                    "cache_hit": False,
                    "request_made": True,
                    "query_start_date": requested_date,
                    "query_end_date": requested_date,
                }
            )
            history.append(_history_record(record, source_date, value))
        for product in CORE_PHYSICAL_PRODUCTS:
            if product not in records:
                unavailable[product] = (
                    "AKShare futures_spot_price returned no usable spot row for this product."
                )
    except Exception as exc:
        detail = f"{type(exc).__name__}: {str(exc)[:400]}"
        previous = _previous_by_key(target_root)
        for product in CORE_PHYSICAL_PRODUCTS:
            base = _physical_base(product, requested_date)
            record = _carry_previous(
                base,
                previous.get(base["series_key"]),
                reason=detail,
                state="collection_error",
            )
            records[product] = record
            statuses.append(
                {
                    "series_key": base["series_key"],
                    "indicator_id": base["indicator_id"],
                    "state": "collection_error",
                    "quality_state": record["quality_state"],
                    "source_date": record.get("source_date"),
                    "carried_forward": record.get("carried_forward", False),
                    "detail": detail,
                    "cache_hit": False,
                    "request_made": True,
                    "query_start_date": requested_date,
                    "query_end_date": requested_date,
                }
            )
    return records, statuses, history, unavailable


def _collect_external(
    requested_date: str,
    *,
    target_root: Path,
    ak_module: Any | None,
    request_interval_seconds: float,
) -> tuple[
    dict[str, dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, str]
]:
    records: dict[str, dict[str, Any]] = {}
    statuses: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    unavailable = dict(_EXTERNAL_UNAVAILABLE)
    previous = _previous_by_key(target_root)
    for index, spec in enumerate(_EXTERNAL_SPECS):
        if index and request_interval_seconds > 0:
            time.sleep(request_interval_seconds)
        base = _external_base(spec, requested_date)
        try:
            frame = collect_foreign_futures_history(spec.symbol, ak_module=ak_module)
            if not isinstance(frame, pd.DataFrame):
                raise TypeError("AKShare futures_foreign_hist did not return a DataFrame")
            if "date" not in frame.columns or "close" not in frame.columns:
                raise ValueError("AKShare futures_foreign_hist response is missing date/close")
            candidates: list[tuple[str, float]] = []
            for _, row in frame.iterrows():
                source_date = _date_text(row.get("date"))
                value = _number(row.get("close"))
                if source_date is None or value is None or source_date > requested_date:
                    continue
                candidates.append((source_date, value))
            if not candidates:
                unavailable[spec.target] = (
                    "AKShare futures_foreign_hist returned no usable daily close for this target."
                )
                statuses.append(
                    {
                        "series_key": base["series_key"],
                        "indicator_id": base["indicator_id"],
                        "state": "unavailable",
                        "quality_state": "unavailable",
                        "source_date": None,
                        "carried_forward": False,
                        "detail": unavailable[spec.target],
                        "cache_hit": False,
                        "request_made": True,
                        "query_start_date": None,
                        "query_end_date": requested_date,
                    }
                )
                continue
            candidates.sort(key=lambda item: item[0])
            source_date, value = candidates[-1]
            stale = _is_stale(requested_date, source_date, spec.max_lag_days)
            base.update(
                {
                    "source_date": source_date,
                    "observation_date": source_date,
                    "value": value,
                    "quality_state": "stale" if stale else "fresh",
                    "missing_reason": (
                        f"observation exceeds allowed lag of {spec.max_lag_days} day(s)"
                        if stale
                        else None
                    ),
                    "current_collection_state": "ok",
                    "current_permission_status": "available",
                    "is_stale": stale,
                }
            )
            base["normalized_value_sha256"] = _record_hash(base)
            records[spec.target] = base
            statuses.append(
                {
                    "series_key": base["series_key"],
                    "indicator_id": base["indicator_id"],
                    "state": "ok",
                    "quality_state": base["quality_state"],
                    "source_date": source_date,
                    "carried_forward": False,
                    "detail": base["missing_reason"],
                    "cache_hit": False,
                    "request_made": True,
                    "query_start_date": candidates[0][0],
                    "query_end_date": requested_date,
                }
            )
            for historical_date, historical_value in candidates[-_EXTERNAL_HISTORY_OBSERVATION_LIMIT:]:
                history.append(_history_record(base, historical_date, historical_value))
        except Exception as exc:
            detail = f"{type(exc).__name__}: {str(exc)[:400]}"
            record = _carry_previous(
                base,
                previous.get(base["series_key"]),
                reason=detail,
                state="collection_error",
            )
            records[spec.target] = record
            statuses.append(
                {
                    "series_key": base["series_key"],
                    "indicator_id": base["indicator_id"],
                    "state": "collection_error",
                    "quality_state": record["quality_state"],
                    "source_date": record.get("source_date"),
                    "carried_forward": record.get("carried_forward", False),
                    "detail": detail,
                    "cache_hit": False,
                    "request_made": True,
                    "query_start_date": None,
                    "query_end_date": requested_date,
                }
            )
    return records, statuses, history, unavailable


def collect_akshare_foundation_domain(
    domain: str,
    requested_date: str,
    *,
    data_dir: str | Path = "data",
    registry_path: str | Path | None = None,
    ak_module: Any | None = None,
    publish: bool = True,
    shadow_days: int = 5,
    request_interval_seconds: float = 0.25,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Collect and optionally publish one AKShare-backed foundation domain."""

    if domain not in FOUNDATION_DOMAINS:
        raise ValueError(f"unsupported foundation domain: {domain}")
    normalized_date = iso_date(requested_date)
    if shadow_days < 1:
        raise ValueError("shadow_days must be positive")
    if request_interval_seconds < 0:
        raise ValueError("request_interval_seconds must not be negative")
    target_root = Path(data_dir) / domain
    generated = now or datetime.now(ZoneInfo(_TIMEZONE))
    if domain == "physical":
        by_target, statuses, history, unavailable = _collect_physical(
            normalized_date, target_root=target_root, ak_module=ak_module
        )
        targets = CORE_PHYSICAL_PRODUCTS
        configured_count = len(CORE_PHYSICAL_PRODUCTS)
        matrix = _target_matrix(
            targets,
            records=by_target,
            unavailable_reasons=unavailable,
            configured_targets=set(CORE_PHYSICAL_PRODUCTS),
            requested_date=normalized_date,
            physical=True,
        )
    else:
        by_target, statuses, history, unavailable = _collect_external(
            normalized_date,
            target_root=target_root,
            ak_module=ak_module,
            request_interval_seconds=request_interval_seconds,
        )
        targets = EXTERNAL_TARGETS
        configured_count = len(_EXTERNAL_SPECS)
        matrix = _target_matrix(
            targets,
            records=by_target,
            unavailable_reasons=unavailable,
            configured_targets=set(_EXTERNAL_BY_TARGET),
            requested_date=normalized_date,
            physical=False,
        )
    records = list(by_target.values())
    coverage = _coverage(
        matrix, configured_target_count=configured_count, status_rows=statuses
    )
    validation_errors = _validate_payload(records, matrix, normalized_date)
    data_fresh = bool(records) and all(
        record.get("quality_state") == "fresh" for record in records
    )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "domain": domain,
        "provider": "akshare",
        "requested_date": normalized_date,
        "generated_at": generated.isoformat(),
        "timezone": _TIMEZONE,
        "vendor": "AKShare",
        "akshare_version": akshare_version(ak_module),
        "source_policy": "akshare_public_primary",
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
            "providers": ["AKShare"],
            "independent_second_source_required": True,
            "missing_reason": "an independent second provider is not configured",
        },
    }
    if domain == "physical":
        basis, parities, official_warehouse = _physical_derivations(
            records, Path(data_dir), registry_path
        )
        payload.update(
            {
                "basis": basis,
                "import_parities": parities,
                "official_warehouse": official_warehouse,
            }
        )
    status: dict[str, Any] = {
        "schema_version": 1,
        "domain": domain,
        "provider": "akshare",
        "requested_date": normalized_date,
        "generated_at": generated.isoformat(),
        "data_fresh": data_fresh,
        "coverage": coverage,
        "series": statuses,
        "validation_passed": not validation_errors,
        "validation_errors": validation_errors,
    }
    _publish(
        target_root=target_root,
        payload=payload,
        status=status,
        history_rows=history,
        requested_date=normalized_date,
        publish=publish,
        shadow_days=shadow_days,
    )
    return {"payload": payload, "status": status}


def run_akshare_foundation(
    requested_date: str,
    *,
    scope: str = "all",
    data_dir: str | Path = "data",
    registry_path: str | Path | None = None,
    ak_module: Any | None = None,
    publish: bool = True,
    shadow_days: int = 5,
    request_interval_seconds: float = 0.25,
) -> dict[str, Any]:
    domains = FOUNDATION_DOMAINS if scope == "all" else (scope,)
    if any(domain not in FOUNDATION_DOMAINS for domain in domains):
        raise ValueError("foundation scope must be physical, external, or all")
    return {
        domain: collect_akshare_foundation_domain(
            domain,
            requested_date,
            data_dir=data_dir,
            registry_path=registry_path,
            ak_module=ak_module,
            publish=publish,
            shadow_days=shadow_days,
            request_interval_seconds=request_interval_seconds,
        )
        for domain in domains
    }


def akshare_foundation_audit() -> dict[str, Any]:
    """Return the static public-source matrix without making any request."""

    return {
        "provider": "akshare",
        "physical": {
            "target_count": len(CORE_PHYSICAL_PRODUCTS),
            "configured_target_count": len(CORE_PHYSICAL_PRODUCTS),
            "source_endpoint": "futures_spot_price",
            "original_source": "100ppi via AKShare",
            "targets": list(CORE_PHYSICAL_PRODUCTS),
        },
        "external": {
            "target_count": len(EXTERNAL_TARGETS),
            "configured_target_count": len(_EXTERNAL_SPECS),
            "source_endpoint": "futures_foreign_hist",
            "original_source": "Sina Finance foreign futures via AKShare",
            "configured_targets": [spec.target for spec in _EXTERNAL_SPECS],
            "unavailable_targets": dict(_EXTERNAL_UNAVAILABLE),
        },
    }


__all__ = [
    "FOUNDATION_DOMAINS",
    "akshare_foundation_audit",
    "collect_akshare_foundation_domain",
    "run_akshare_foundation",
]
