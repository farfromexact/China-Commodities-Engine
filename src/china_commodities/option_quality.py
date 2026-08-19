"""Deterministic quality gates for commodity-option snapshots.

This module only assesses the evidence present in an option snapshot.  It does
not build an implied-volatility surface, infer skew, or infer the sign of
dealer Gamma from open interest.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
import math
from typing import Any, Mapping


_THRESHOLD = 0.8
_EXERCISE_STYLES = frozenset({"american", "european"})
_GREEK_FIELDS = ("iv_percent", "delta", "gamma", "vega", "theta", "rho")


def _finite_number(value: Any, *, positive: bool = False) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    numeric = float(value)
    if not math.isfinite(numeric):
        return False
    return numeric > 0 if positive else numeric >= 0


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    value = numerator / denominator
    return min(1.0, max(0.0, float(value)))


def _normalized_date(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) == 8 and text.isdigit():
        try:
            return datetime.strptime(text, "%Y%m%d").date().isoformat()
        except ValueError:
            return None
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return None
    return parsed.isoformat() if parsed.isoformat() == text else None


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _contract_key(record: Mapping[str, Any]) -> str:
    value = record.get("contract")
    if not isinstance(value, str):
        return ""
    return value.strip().upper()


def _record_source_date_matches(record: Mapping[str, Any], trade_date: Any) -> bool:
    if record.get("source_date_match") is not True:
        return False
    expected = _normalized_date(trade_date)
    if expected is None:
        return False
    for field in ("trade_date", "source_trade_date"):
        actual = record.get(field)
        if actual is None or (isinstance(actual, str) and not actual.strip()):
            continue
        if _normalized_date(actual) != expected:
            return False
    return True


def _greek_source_available(record: Mapping[str, Any], source: str) -> bool:
    greeks = record.get("greeks")
    if not isinstance(greeks, Mapping):
        return False

    source_values = greeks.get(source)
    if isinstance(source_values, Mapping) and any(
        _finite_number(source_values.get(field)) for field in _GREEK_FIELDS
    ):
        return True

    # Some older normalized records only retain the selected block together
    # with an explicit provenance/quality marker.  It is safe to recognize
    # those blocks without treating vendor output as model output.
    quality = _text(greeks.get("quality")).lower()
    selected_source = _text(greeks.get("selected_source")).lower()
    selected = greeks.get("selected")
    if not isinstance(selected, Mapping):
        return False
    if source == "vendor":
        allowed = {"vendor_reported", "vendor_and_model"}
    else:
        allowed = {"model_derived", "vendor_and_model"}
    return (
        quality in allowed
        and selected_source == source
        and any(_finite_number(selected.get(field)) for field in _GREEK_FIELDS)
    )


def _iv_available(record: Mapping[str, Any]) -> bool:
    candidates: list[Any] = [record.get("iv_percent")]
    greeks = record.get("greeks")
    if isinstance(greeks, Mapping):
        selected = greeks.get("selected")
        if isinstance(selected, Mapping):
            candidates.append(selected.get("iv_percent"))
    return any(_finite_number(value, positive=True) for value in candidates)


def _expiry(record: Mapping[str, Any]) -> str | None:
    return _normalized_date(record.get("expiry_date"))


def _exercise_style_available(record: Mapping[str, Any]) -> bool:
    return _text(record.get("exercise_style")).lower() in _EXERCISE_STYLES


def _bid_ask_available(record: Mapping[str, Any]) -> bool:
    bid = record.get("bid")
    ask = record.get("ask")
    return bool(
        _finite_number(bid)
        and _finite_number(ask, positive=True)
        and float(ask) >= float(bid)
    )


def _open_interest_available(record: Mapping[str, Any]) -> bool:
    return _finite_number(record.get("open_interest"))


def _series_stats(
    records: list[Mapping[str, Any]],
) -> tuple[int, float, int, int]:
    groups: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
    for record in records:
        expiry = _expiry(record) or _text(record.get("expiry_date"))
        key = (
            _text(record.get("exchange")).upper(),
            _text(record.get("product")).upper(),
            _text(record.get("underlying_contract")).upper(),
            expiry,
        )
        option_type = _text(record.get("option_type")).upper()
        groups[key].add(option_type)

    series_count = len(groups)
    balanced_count = sum(1 for types in groups.values() if {"C", "P"}.issubset(types))
    invalid_key_count = sum(
        1
        for exchange, product, underlying, expiry in groups
        if not exchange or not product or not underlying or not _normalized_date(expiry)
    )
    return (
        series_count,
        _ratio(balanced_count, series_count),
        balanced_count,
        invalid_key_count,
    )


def assess_option_snapshot_quality(snapshot: dict) -> dict:
    """Assess whether an iFinD option snapshot is usable for each purpose.

    Coverage values are fractions in the closed interval ``[0, 1]``.  A
    ``chain_only`` result means the chain is structurally verified but does
    not meet the evidence requirements for a surface.  No derived surface,
    skew, or dealer-Gamma direction is produced here.
    """

    raw_records = snapshot.get("records") if isinstance(snapshot, dict) else None
    records_value = raw_records if isinstance(raw_records, list) else []
    records: list[Mapping[str, Any]] = [
        record for record in records_value if isinstance(record, Mapping)
    ]
    record_count = len(records_value)
    unique_contracts = {
        _contract_key(record) for record in records if _contract_key(record)
    }
    unique_contract_count = len(unique_contracts)

    trade_date = snapshot.get("trade_date") if isinstance(snapshot, dict) else None
    source_date_match_count = sum(
        _record_source_date_matches(record, trade_date) for record in records
    )
    source_date_match_pct = _ratio(source_date_match_count, record_count)
    underlying_settle_count = sum(
        _finite_number(record.get("underlying_settle"), positive=True)
        for record in records
    )
    underlying_settle_coverage = _ratio(underlying_settle_count, record_count)
    expiry_coverage = _ratio(
        sum(_expiry(record) is not None for record in records), record_count
    )
    exercise_style_coverage = _ratio(
        sum(_exercise_style_available(record) for record in records), record_count
    )
    iv_coverage = _ratio(
        sum(_iv_available(record) for record in records), record_count
    )
    vendor_greeks_coverage = _ratio(
        sum(_greek_source_available(record, "vendor") for record in records),
        record_count,
    )
    model_greeks_coverage = _ratio(
        sum(_greek_source_available(record, "model") for record in records),
        record_count,
    )
    bid_ask_coverage = _ratio(
        sum(_bid_ask_available(record) for record in records), record_count
    )
    open_interest_coverage = _ratio(
        sum(_open_interest_available(record) for record in records), record_count
    )

    series_count, balanced_call_put_series_pct, _, invalid_series_key_count = _series_stats(
        records
    )

    coverage_value = snapshot.get("coverage")
    product_scope_declared = isinstance(coverage_value, Mapping)
    coverage = coverage_value if isinstance(coverage_value, Mapping) else {}
    expected_product_count = coverage.get("expected_product_count")
    successful_product_count = coverage.get("successful_product_count")
    valid_product_counts = all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in (expected_product_count, successful_product_count)
    )
    product_coverage = (
        _ratio(successful_product_count, expected_product_count)
        if valid_product_counts
        else 0.0
    )
    full_product_scope_verified = bool(
        not product_scope_declared
        or (
            valid_product_counts
            and expected_product_count > 0
            and successful_product_count == expected_product_count
            and coverage.get("scope_complete") is True
        )
    )

    universe_count = snapshot.get("universe_contract_count")
    quote_count = snapshot.get("quote_contract_count")
    valid_counts = all(
        isinstance(value, int) and not isinstance(value, bool)
        for value in (universe_count, quote_count)
    )
    counts_match = (
        valid_counts
        and universe_count == quote_count == record_count
    )
    provider_bad_count = sum(
        not _text(record.get("source_provider")).lower().startswith("ifind")
        for record in records
    )
    source_date_bad_count = record_count - source_date_match_count
    settle_bad_count = record_count - underlying_settle_count
    all_contracts_present = len(records) == record_count and all(
        _contract_key(record) for record in records
    )
    full_chain_verified = bool(
        record_count > 0
        and all_contracts_present
        and unique_contract_count == record_count
        and snapshot.get("quote_coverage_complete") is True
        and counts_match
        and provider_bad_count == 0
        and source_date_bad_count == 0
        and settle_bad_count == 0
    )

    surface_ready = bool(
        full_chain_verified
        and full_product_scope_verified
        and expiry_coverage >= 1.0
        and iv_coverage >= _THRESHOLD
        and invalid_series_key_count == 0
        and balanced_call_put_series_pct >= _THRESHOLD
    )
    execution_ready = bool(
        full_chain_verified
        and full_product_scope_verified
        and bid_ask_coverage >= _THRESHOLD
    )
    model_greeks_ready = bool(
        full_chain_verified
        and full_product_scope_verified
        and expiry_coverage >= _THRESHOLD
        and exercise_style_coverage >= _THRESHOLD
        and model_greeks_coverage >= _THRESHOLD
    )
    vendor_risk_available = bool(
        full_chain_verified
        and full_product_scope_verified
        and vendor_greeks_coverage >= _THRESHOLD
    )

    universe_source = _text(snapshot.get("universe_source")) or None
    all_inputs_ifind = bool(
        universe_source
        and universe_source.lower().startswith("ifind")
        and provider_bad_count == 0
    )

    limitations: list[str] = []
    if record_count == 0:
        limitations.append("records is empty")
    if not all_contracts_present:
        limitations.append("contract field is missing or records are not all objects")
    if unique_contract_count != record_count:
        limitations.append(
            f"contract uniqueness failed: unique_contract_count={unique_contract_count}, "
            f"record_count={record_count}"
        )
    if snapshot.get("quote_coverage_complete") is not True:
        limitations.append("quote_coverage_complete is not true")
    if not counts_match:
        limitations.append(
            "universe/quote/record counts are inconsistent: "
            f"universe={universe_count!r}, quote={quote_count!r}, records={record_count}"
        )
    if provider_bad_count:
        limitations.append(
            f"{provider_bad_count} record(s) have source_provider not starting with ifind"
        )
    if source_date_bad_count:
        limitations.append(
            f"{source_date_bad_count} record(s) fail source_date_match or have a stale trade date"
        )
    if settle_bad_count:
        limitations.append(
            f"{settle_bad_count} record(s) have missing or non-positive underlying_settle"
        )

    if expiry_coverage < 1.0:
        limitations.append(
            f"surface_ready blocked: expiry_coverage={expiry_coverage:.4f}; "
            "a real expiry_date is required for every record"
        )
    if iv_coverage < _THRESHOLD:
        limitations.append(
            f"surface_ready blocked: iv_coverage={iv_coverage:.4f} < {_THRESHOLD:.4f}"
        )
    if invalid_series_key_count:
        limitations.append(
            f"surface_ready blocked: {invalid_series_key_count} series group(s) "
            "have missing grouping keys or an invalid expiry_date"
        )
    if balanced_call_put_series_pct < _THRESHOLD:
        limitations.append(
            "surface_ready blocked: "
            f"balanced_call_put_series_pct={balanced_call_put_series_pct:.4f} "
            f"< {_THRESHOLD:.4f}"
        )
    if bid_ask_coverage < _THRESHOLD:
        limitations.append(
            f"execution_ready blocked: bid_ask_coverage={bid_ask_coverage:.4f} "
            f"< {_THRESHOLD:.4f}"
        )
    if expiry_coverage < _THRESHOLD:
        limitations.append(
            f"model_greeks_ready blocked: expiry_coverage={expiry_coverage:.4f} "
            f"< {_THRESHOLD:.4f}"
        )
    if exercise_style_coverage < _THRESHOLD:
        limitations.append(
            "model_greeks_ready blocked: "
            f"exercise_style_coverage={exercise_style_coverage:.4f} < {_THRESHOLD:.4f}"
        )
    if model_greeks_coverage < _THRESHOLD:
        limitations.append(
            "model_greeks_ready blocked: "
            f"model_greeks_coverage={model_greeks_coverage:.4f} < {_THRESHOLD:.4f}"
        )
    if not vendor_risk_available:
        limitations.append(
            f"vendor_risk_available blocked: vendor_greeks_coverage={vendor_greeks_coverage:.4f} "
            f"< {_THRESHOLD:.4f}"
        )
    if open_interest_coverage < 1.0:
        limitations.append(
            f"open_interest_coverage={open_interest_coverage:.4f}; some open interest is missing"
        )
    if universe_source and not all_inputs_ifind:
        limitations.append(
            f"contract universe source is {universe_source}; quotes are iFinD but not all inputs are iFinD"
        )
    if product_scope_declared and not full_product_scope_verified:
        limitations.append(
            "full-market scope is incomplete: "
            f"successful_products={successful_product_count!r}, "
            f"expected_products={expected_product_count!r}, "
            f"product_coverage={product_coverage:.4f}"
        )

    limitations.append(
        "vendor risk, when available, uses units_as_reported=true; no unit conversion is inferred"
    )
    limitations.append(
        "dealer_gamma_direction_known=false; dealer Gamma direction is not observed or inferred"
    )
    limitations.append(
        "IV surface and skew are not generated or inferred by this quality assessment"
    )

    if surface_ready:
        status = "surface_ready"
    elif full_chain_verified and product_scope_declared and not full_product_scope_verified:
        status = "partial_chain"
    elif full_chain_verified:
        status = "chain_only"
    else:
        status = "invalid"
    return {
        "trade_date": _normalized_date(trade_date),
        "record_count": record_count,
        "unique_contract_count": unique_contract_count,
        "source_date_match_pct": source_date_match_pct,
        "underlying_settle_coverage": underlying_settle_coverage,
        "expiry_coverage": expiry_coverage,
        "exercise_style_coverage": exercise_style_coverage,
        "iv_coverage": iv_coverage,
        "vendor_greeks_coverage": vendor_greeks_coverage,
        "model_greeks_coverage": model_greeks_coverage,
        "bid_ask_coverage": bid_ask_coverage,
        "open_interest_coverage": open_interest_coverage,
        "series_count": series_count,
        "balanced_call_put_series_pct": balanced_call_put_series_pct,
        "product_scope_declared": product_scope_declared,
        "expected_product_count": (
            expected_product_count if valid_product_counts else None
        ),
        "successful_product_count": (
            successful_product_count if valid_product_counts else None
        ),
        "product_coverage": product_coverage,
        "full_product_scope_verified": full_product_scope_verified,
        "universe_contract_count": (
            universe_count
            if isinstance(universe_count, int) and not isinstance(universe_count, bool)
            else None
        ),
        "quote_contract_count": (
            quote_count
            if isinstance(quote_count, int) and not isinstance(quote_count, bool)
            else None
        ),
        "quote_coverage_complete": snapshot.get("quote_coverage_complete") is True,
        "universe_source": universe_source,
        "all_inputs_ifind": all_inputs_ifind,
        "full_chain_verified": full_chain_verified,
        "surface_ready": surface_ready,
        "execution_ready": execution_ready,
        "model_greeks_ready": model_greeks_ready,
        "vendor_risk_available": vendor_risk_available,
        "vendor_risk_units_as_reported": True,
        "dealer_gamma_direction_known": False,
        "status": status,
        "limitations": limitations,
    }
