"""Conservative derived calculations for the daily data foundation."""

from __future__ import annotations

from datetime import date
import math
from typing import Any, Mapping


_UNIT_FACTORS: dict[tuple[str, str], float] = {
    ("元/公斤", "元/吨"): 1000.0,
    ("美元/磅", "美元/公吨"): 2204.6226218488,
    ("公斤", "吨"): 0.001,
    ("吨", "公斤"): 1000.0,
}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def calculate_basis(
    spot: Mapping[str, Any] | None,
    futures: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Calculate the only supported basis convention: spot minus futures."""

    spot_record = spot or {}
    futures_record = futures or {}
    spot_value = _number(spot_record.get("value"))
    futures_value = _number(futures_record.get("settle"))
    if futures_value is None:
        futures_value = _number(futures_record.get("close"))
    configured_quality = str(
        spot_record.get("basis_quality") or "D"
    ).upper()
    quality = configured_quality if configured_quality in {"A", "B", "C", "D"} else "D"
    missing: list[str] = []
    if spot_value is None:
        missing.append("spot_value")
    if futures_value is None or futures_value <= 0:
        missing.append("futures_value")
    for field in ("region", "grade", "tax_included", "delivery_location"):
        if spot_record.get(field) is None:
            missing.append(field)
    if not futures_record.get("contract"):
        missing.append("mapped_contract")
    if missing and quality in {"A", "B"}:
        quality = "C" if spot_value is not None and futures_value else "D"

    basis_value = (
        spot_value - futures_value
        if spot_value is not None and futures_value is not None and futures_value > 0
        else None
    )
    is_fresh = spot_record.get("quality_state") == "fresh"
    return {
        "product": spot_record.get("product") or futures_record.get("product"),
        "exchange": spot_record.get("exchange") or futures_record.get("exchange"),
        "series_key": spot_record.get("series_key"),
        "formula": "spot - futures",
        "spot": spot_value,
        "futures": futures_value,
        "value": basis_value,
        "unit": spot_record.get("unit"),
        "quality_grade": quality,
        "eligible_for_physical_score": bool(
            basis_value is not None and quality in {"A", "B"} and is_fresh
        ),
        "region": spot_record.get("region"),
        "grade": spot_record.get("grade"),
        "tax_included": spot_record.get("tax_included"),
        "delivery_location": spot_record.get("delivery_location"),
        "mapped_contract": futures_record.get("contract"),
        "spot_observation_date": spot_record.get("observation_date"),
        "futures_source_date": futures_record.get("source_trade_date")
        or futures_record.get("trade_date"),
        "missing_fields": sorted(set(missing)),
        "missing_reason": (
            "missing required basis alignment fields: " + ", ".join(sorted(set(missing)))
            if basis_value is None or missing
            else None
        ),
    }


def convert_unit_value(value: Any, from_unit: str, to_unit: str) -> float:
    """Apply only an explicit, version-controlled physical unit conversion."""

    numeric = _number(value)
    if numeric is None:
        raise ValueError("unit conversion value must be finite")
    source = str(from_unit or "").strip()
    target = str(to_unit or "").strip()
    if not source or not target:
        raise ValueError("unit conversion requires source and target units")
    if source == target:
        return numeric
    factor = _UNIT_FACTORS.get((source, target))
    if factor is None:
        raise ValueError(f"unverified unit conversion: {source} -> {target}")
    return numeric * factor


def calculate_import_parity(
    definition: Mapping[str, Any],
    legs: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Evaluate a fully pinned landed-cost formula or return an explicit null.

    A verified definition supplies ``required_legs`` and a coefficient for every
    leg.  Contract legs must share a contract month, observations must be within
    ``max_time_gap_days``, and every leg must declare unit, currency and quality.
    This deliberately rejects approximate continuous-series substitutions.
    """

    parity_key = str(definition.get("parity_key") or "")
    if definition.get("status") != "verified":
        return {
            "parity_key": parity_key,
            "value": None,
            "quality_state": "unavailable",
            "missing_reason": definition.get("missing_reason")
            or "parity definition is not verified",
        }
    required = tuple(str(value) for value in definition.get("required_legs") or ())
    if not required:
        return {
            "parity_key": parity_key,
            "value": None,
            "quality_state": "invalid_definition",
            "missing_reason": "verified parity has no required_legs",
        }

    missing: list[str] = []
    observations: list[date] = []
    contract_months: set[str] = set()
    total = 0.0
    contract_legs = set(definition.get("contract_legs") or ())
    for name in required:
        leg = legs.get(name)
        if not isinstance(leg, Mapping):
            missing.append(name)
            continue
        value = _number(leg.get("value"))
        coefficient = _number(leg.get("coefficient"))
        if value is None or coefficient is None:
            missing.append(f"{name}.value_or_coefficient")
            continue
        for field in ("observation_date", "unit", "currency", "quality"):
            if leg.get(field) in (None, ""):
                missing.append(f"{name}.{field}")
        try:
            observations.append(date.fromisoformat(str(leg.get("observation_date"))))
        except ValueError:
            missing.append(f"{name}.observation_date")
        if name in contract_legs:
            month = str(leg.get("contract_month") or "")
            if not month:
                missing.append(f"{name}.contract_month")
            else:
                contract_months.add(month)
        total += value * coefficient

    if len(contract_months) > 1:
        missing.append("contract_month_mismatch")
    max_gap = definition.get("max_time_gap_days", 1)
    if not isinstance(max_gap, int) or isinstance(max_gap, bool) or max_gap < 0:
        missing.append("invalid_max_time_gap_days")
    elif observations and (max(observations) - min(observations)).days > max_gap:
        missing.append("observation_time_mismatch")
    if definition.get("quality_aligned") is not True:
        missing.append("quality_alignment")
    if definition.get("tax_treatment_verified") is not True:
        missing.append("tax_treatment")
    if definition.get("freight_treatment_verified") is not True:
        missing.append("freight_treatment")

    if missing:
        return {
            "parity_key": parity_key,
            "value": None,
            "quality_state": "incomplete",
            "missing_reason": "missing or misaligned parity inputs: "
            + ", ".join(sorted(set(missing))),
        }
    return {
        "parity_key": parity_key,
        "value": total,
        "unit": definition.get("output_unit"),
        "quality_state": "verified",
        "missing_reason": None,
        "contract_month": next(iter(contract_months), None),
        "observation_start": min(observations).isoformat() if observations else None,
        "observation_end": max(observations).isoformat() if observations else None,
    }


__all__ = ["calculate_basis", "calculate_import_parity", "convert_unit_value"]
