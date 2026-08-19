"""Deterministic historical market-state features for commodity futures.

This module deliberately operates on already published snapshot payloads.  It
does not collect data, infer a continuous contract, or make a trade
recommendation.  The current main contract is locked from the latest curve
snapshot and every historical observation is looked up by the exact
``exchange + contract`` pair.
"""

from __future__ import annotations

import json
import math
import re
from datetime import date
from math import fsum, sqrt
from typing import Any, Iterable


REQUESTED_HISTORY_DAYS = 20
SCHEMA_VERSION = 1
MIN_ZSCORE_OBSERVATIONS = 5
MIN_REALIZED_VOL_OBSERVATIONS = 5
STATE_SCORE_RULE = (
    "standardized_value >= 2 => 2; >= 1 => 1; "
    "between -1 and 1 => 0; <= -2 => -2; <= -1 => -1; "
    "null when the underlying metric is unavailable"
)


def _finite_number(value: Any) -> float | None:
    """Return a finite float, without allowing NaN/Inf into the output."""

    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first_number(record: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _finite_number(record.get(key))
        if value is not None:
            return value
    return None


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _parse_trade_date(value: Any, *, position: int) -> date:
    text = _text(value)
    if text is None:
        raise ValueError(f"eligible snapshot at position {position} has no trade_date")
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            f"eligible snapshot at position {position} has invalid trade_date: {text}"
        ) from exc
    if parsed.isoformat() != text:
        raise ValueError(
            f"eligible snapshot at position {position} must use ISO trade_date: {text}"
        )
    return parsed


def _is_verified(payload: dict[str, Any]) -> bool:
    """Use strict JSON booleans so a string such as ``"false"`` is not trusted."""

    return payload.get("verified") is True or payload.get("scope_verified") is True


def _collection_records(value: Any) -> list[dict[str, Any]]:
    """Accept the repository's list form and a simple mapping form for tests."""

    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        if any(key in value for key in ("exchange", "product", "contract")):
            return [value]
        records: list[dict[str, Any]] = []
        for key, item in value.items():
            if isinstance(item, dict):
                record = dict(item)
                if not record.get("product"):
                    record["product"] = key
                records.append(record)
            elif isinstance(item, list):
                records.extend(item for item in item if isinstance(item, dict))
        return records
    return []


def _normalised_key(exchange: Any, value: Any) -> tuple[str, str] | None:
    left = _text(exchange)
    right = _text(value)
    if left is None or right is None:
        return None
    return left.upper(), right.upper()


def _curve_key(curve: dict[str, Any]) -> tuple[str, str] | None:
    return _normalised_key(curve.get("exchange"), curve.get("product"))


def _contract_from_component(value: Any) -> str | None:
    if isinstance(value, dict):
        return _text(value.get("contract"))
    return _text(value)


def _curve_map(payload: dict[str, Any]) -> tuple[dict[tuple[str, str], dict[str, Any]], bool]:
    """Index curves deterministically and report duplicate product curves."""

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for curve in _collection_records(payload.get("commodity_curves")):
        key = _curve_key(curve)
        if key is not None:
            grouped.setdefault(key, []).append(curve)
    output: dict[tuple[str, str], dict[str, Any]] = {}
    duplicate = False
    for key, records in grouped.items():
        if len(records) > 1:
            duplicate = True
        output[key] = min(records, key=_stable_json)
    return output, duplicate


def _futures_map(payload: dict[str, Any]) -> tuple[dict[tuple[str, str], dict[str, Any]], bool]:
    """Index exact exchange/contract records without selecting a daily main."""

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in _collection_records(payload.get("futures_contracts")):
        key = _normalised_key(record.get("exchange"), record.get("contract"))
        if key is not None:
            grouped.setdefault(key, []).append(record)
    output: dict[tuple[str, str], dict[str, Any]] = {}
    duplicate = False
    for key, records in grouped.items():
        if len(records) > 1:
            duplicate = True
        output[key] = min(records, key=_stable_json)
    return output, duplicate


def _contract_month(contract: str | None, trade_date: str) -> str | None:
    """Resolve common YYMM and CZCE YMM suffixes to the first of the month."""

    if not contract:
        return None
    match = re.match(r"^[A-Za-z]+(\d{3,4})", contract)
    if not match:
        return None
    digits = match.group(1)
    if len(digits) == 4:
        year = 2000 + int(digits[:2])
        month = int(digits[2:])
    else:
        year_digit = int(digits[0])
        month = int(digits[1:])
        current_year = date.fromisoformat(trade_date).year
        decade = current_year - current_year % 10
        candidates = [decade - 10 + year_digit, decade + year_digit, decade + 10 + year_digit]
        year = min(candidates, key=lambda candidate: abs(candidate - current_year))
    if not 1 <= month <= 12:
        return None
    return date(year, month, 1).isoformat()


def _sample_std(values: Iterable[float]) -> float | None:
    numbers = [value for value in values if _finite_number(value) is not None]
    if len(numbers) < 2:
        return None
    mean = fsum(numbers) / len(numbers)
    variance = fsum((value - mean) ** 2 for value in numbers) / (len(numbers) - 1)
    standard_deviation = sqrt(variance)
    return standard_deviation if math.isfinite(standard_deviation) else None


def _zscore(current: float | None, values: list[float], minimum: int = 5) -> tuple[float | None, int]:
    valid = [value for value in values if _finite_number(value) is not None]
    observations = len(valid)
    if current is None or observations < minimum:
        return None, observations
    standard_deviation = _sample_std(valid)
    if standard_deviation is None or standard_deviation == 0:
        return None, observations
    mean = fsum(valid) / observations
    result = (current - mean) / standard_deviation
    return (result if math.isfinite(result) else None), observations


def _score(standardized_value: float | None) -> int | None:
    if standardized_value is None or not math.isfinite(standardized_value):
        return None
    if standardized_value >= 2:
        return 2
    if standardized_value >= 1:
        return 1
    if standardized_value <= -2:
        return -2
    if standardized_value <= -1:
        return -1
    return 0


def _compound_return(values: list[float]) -> float | None:
    if not values:
        return None
    factor = 1.0
    for value in values:
        if not math.isfinite(value):
            return None
        factor *= 1.0 + value / 100.0
    result = (factor - 1.0) * 100.0
    return result if math.isfinite(result) else None


def _component_month(main_contract: dict[str, Any] | None, contract: str | None, trade_date: str) -> str | None:
    supplied = _text((main_contract or {}).get("contract_month"))
    if supplied is not None:
        return supplied
    return _contract_month(contract, trade_date)


def _snapshot_contract_is_fresh(record: dict[str, Any], trade_date: str) -> bool:
    record_trade_date = _text(record.get("trade_date"))
    source_trade_date = _text(record.get("source_trade_date"))
    if record_trade_date is not None and record_trade_date != trade_date:
        return False
    if source_trade_date is not None and source_trade_date != trade_date:
        return False
    if record.get("source_date_match") is False:
        return False
    return True


def _make_history_observations(
    snapshots: list[tuple[str, dict[str, Any], dict[tuple[str, str], dict[str, Any]]]],
    exchange: str,
    contract: str | None,
) -> tuple[list[dict[str, Any]], int]:
    if contract is None:
        return [], len(snapshots)
    key = (exchange.upper(), contract.upper())
    observations: list[dict[str, Any]] = []
    previous_settlement: float | None = None
    skipped_stale = 0
    for trade_date, _payload, futures in snapshots:
        record = futures.get(key)
        if record is None or not _snapshot_contract_is_fresh(record, trade_date):
            if record is not None:
                skipped_stale += 1
            previous_settlement = None
            continue
        settlement = _first_number(record, "settle", "settlement")
        pre_settlement = _first_number(record, "pre_settle", "pre_settlement")
        source_return = _first_number(
            record, "settle_return_pct", "settlement_return_pct"
        )
        daily_return = source_return
        if daily_return is None and settlement is not None and pre_settlement not in (None, 0):
            daily_return = (settlement / pre_settlement - 1.0) * 100.0
        if daily_return is None and settlement is not None and previous_settlement not in (None, 0):
            daily_return = (settlement / previous_settlement - 1.0) * 100.0
        if daily_return is not None and not math.isfinite(daily_return):
            daily_return = None
        volume = _first_number(record, "volume", "vol")
        open_interest = _first_number(record, "open_interest", "oi")
        observations.append(
            {
                "trade_date": trade_date,
                "contract": contract,
                "settlement": settlement,
                "settle": settlement,
                "pre_settlement": pre_settlement,
                "source_settle_return_pct": source_return,
                "settle_return_pct": daily_return,
                "volume": volume,
                "open_interest": open_interest,
            }
        )
        if settlement is not None:
            previous_settlement = settlement
    return observations, skipped_stale


def _daily_returns(observations: list[dict[str, Any]]) -> list[float]:
    return [
        value
        for observation in observations
        if (value := _finite_number(observation.get("settle_return_pct"))) is not None
    ]


def _trailing_daily_returns(
    observations: list[dict[str, Any]], snapshot_dates: list[str], latest_trade_date: str
) -> list[float]:
    if not observations or not snapshot_dates or snapshot_dates[-1] != latest_trade_date:
        return []
    by_date = {str(observation.get("trade_date")): observation for observation in observations}
    values: list[float] = []
    for trade_date in reversed(snapshot_dates):
        observation = by_date.get(trade_date)
        if observation is None:
            break
        value = _finite_number(observation.get("settle_return_pct"))
        if value is None:
            break
        values.append(value)
    values.reverse()
    return values


def _oi_differences(
    observations: list[dict[str, Any]], snapshot_dates: list[str], latest_trade_date: str
) -> tuple[list[float], float | None, float | None]:
    by_date = {
        str(observation.get("trade_date")): value
        for observation in observations
        if (value := _finite_number(observation.get("open_interest"))) is not None
    }
    differences = [
        by_date[current_date] - by_date[previous_date]
        for previous_date, current_date in zip(snapshot_dates, snapshot_dates[1:])
        if previous_date in by_date and current_date in by_date
    ]
    if len(snapshot_dates) < 2:
        return differences, None, None
    previous_date, current_date = snapshot_dates[-2:]
    if current_date != latest_trade_date:
        return differences, None, None
    if previous_date not in by_date or current_date not in by_date:
        return differences, None, None
    previous_level = by_date[previous_date]
    current_level = by_date[current_date]
    delta = current_level - previous_level
    delta_pct = None if previous_level == 0 else delta / previous_level * 100.0
    return differences, delta, delta_pct


def _curve_pair(curve: dict[str, Any] | None) -> tuple[str, str] | None:
    if not curve:
        return None
    near = _contract_from_component(curve.get("nearest_liquid_contract"))
    deferred = _contract_from_component(curve.get("next_liquid_contract"))
    if near is None or deferred is None:
        return None
    return near.upper(), deferred.upper()


def _curve_value(curve: dict[str, Any] | None) -> float | None:
    if not curve:
        return None
    near_next = curve.get("near_next_curve")
    if not isinstance(near_next, dict):
        return None
    return _finite_number(near_next.get("near_minus_deferred_pct"))


def _product_missing_metrics(
    observations: list[dict[str, Any]],
    daily_returns: list[float],
    trailing_returns: list[float],
    volume_zscore: float | None,
    oi_level_zscore: float | None,
    delta_oi: float | None,
    oi_change_zscore: float | None,
    curve_pair: tuple[str, str] | None,
    curve_observations: int,
    curve_zscore: float | None,
    available_days: int,
) -> list[str]:
    missing: set[str] = set()
    if len(observations) < available_days:
        missing.add("same_contract_history")
    for horizon in (1, 3, 5, 20):
        if len(trailing_returns) < horizon:
            missing.add(f"settlement_return_pct_{horizon}D")
    if len(daily_returns) < MIN_REALIZED_VOL_OBSERVATIONS:
        missing.add("realized_vol_20d_annualized_pct")
    if volume_zscore is None:
        missing.add("volume_zscore")
    if oi_level_zscore is None:
        missing.add("oi_level_zscore")
    if delta_oi is None:
        missing.add("delta_OI_1D")
        missing.add("delta_OI_pct_1D")
    if oi_change_zscore is None:
        missing.add("oi_change_zscore")
    if curve_pair is None:
        missing.add("near_next_curve_pair")
    if curve_observations < MIN_ZSCORE_OBSERVATIONS or curve_zscore is None:
        missing.add("curve_zscore")
    return sorted(missing)


def _state_item(
    raw_value: float | None,
    standardized_value: float | None,
    rule: str = STATE_SCORE_RULE,
) -> dict[str, Any]:
    return {
        "score": _score(standardized_value),
        "raw_value": raw_value,
        "standardized_value": standardized_value,
        "rule": rule,
    }


def _build_state_vector(
    *,
    trailing_returns: list[float],
    daily_returns: list[float],
    returns: dict[str, dict[str, Any]],
    delta_oi_pct: float | None,
    oi_change_zscore: float | None,
    volume_oi: float | None,
    volume_zscore: float | None,
    curve_current: float | None,
    curve_zscore: float | None,
) -> dict[str, Any]:
    daily_std = _sample_std(daily_returns)
    momentum_raw: float | None = None
    momentum_standardized: float | None = None
    momentum_rule = STATE_SCORE_RULE
    five_day = returns["5D"]["value"]
    one_day = returns["1D"]["value"]
    if five_day is not None and daily_std not in (None, 0):
        momentum_raw = five_day
        momentum_standardized = five_day / (daily_std * sqrt(5.0))
        momentum_rule = (
            "raw_value is 5D compounded settlement return percent; "
            "standardized_value = raw_value / (sample daily settlement return std * sqrt(5)); "
            + STATE_SCORE_RULE
        )
    elif one_day is not None and daily_std not in (None, 0):
        momentum_raw = one_day
        momentum_standardized = one_day / daily_std
        momentum_rule = (
            "5D return unavailable; raw_value is 1D settlement return percent; "
            "standardized_value = raw_value / sample daily settlement return std; "
            + STATE_SCORE_RULE
        )
    return {
        "price_momentum": _state_item(momentum_raw, momentum_standardized, momentum_rule),
        "oi_impulse": _state_item(
            delta_oi_pct,
            oi_change_zscore,
            "raw_value is current 1D OI change percent; standardized_value is OI-change z-score; "
            + STATE_SCORE_RULE,
        ),
        "curve_pressure": _state_item(
            curve_current,
            curve_zscore,
            "raw_value is near-minus-deferred curve percent; positive means backwardation; "
            + STATE_SCORE_RULE,
        ),
        "activity": _state_item(
            volume_oi,
            volume_zscore,
            "raw_value is current volume/OI; standardized_value is volume level z-score; "
            + STATE_SCORE_RULE,
        ),
        "fundamental_score": None,
        "convexity_score": None,
        "trade_recommendation": "unavailable",
    }


def _attribution_clue(latest_return: float | None, delta_oi: float | None) -> str:
    if latest_return is None or delta_oi is None:
        return "mixed_or_flat"
    if latest_return > 0 and delta_oi > 0:
        return "price_up_oi_up"
    if latest_return > 0 and delta_oi < 0:
        return "price_up_oi_down"
    if latest_return < 0 and delta_oi > 0:
        return "price_down_oi_up"
    if latest_return < 0 and delta_oi < 0:
        return "price_down_oi_down"
    return "mixed_or_flat"


def _product_record(
    *,
    latest_date: str,
    latest_curve: dict[str, Any],
    previous_curve: dict[str, Any] | None,
    futures_snapshots: list[
        tuple[str, dict[str, Any], dict[tuple[str, str], dict[str, Any]]]
    ],
    curve_snapshots: list[
        tuple[str, dict[str, Any], dict[tuple[str, str], dict[str, Any]]]
    ],
    available_days: int,
) -> tuple[dict[str, Any], set[str]]:
    product_key = _curve_key(latest_curve)
    exchange = _text(latest_curve.get("exchange")) or ""
    product = _text(latest_curve.get("product")) or ""
    main = latest_curve.get("main_contract")
    main = main if isinstance(main, dict) else {}
    current_contract = _contract_from_component(main)
    current_month = _component_month(main, current_contract, latest_date)

    prior_main_contract: str | None = None
    if previous_curve is not None:
        previous_main = previous_curve.get("main_contract")
        if isinstance(previous_main, dict):
            prior_main_contract = _contract_from_component(previous_main)
    main_roll_flag = (
        None
        if prior_main_contract is None or current_contract is None
        else current_contract.upper() != prior_main_contract.upper()
    )

    observations, skipped_stale = _make_history_observations(
        futures_snapshots, exchange, current_contract
    )
    snapshot_dates = [trade_date for trade_date, _payload, _records in futures_snapshots]
    daily_returns = _daily_returns(observations)
    trailing_returns = _trailing_daily_returns(
        observations, snapshot_dates, latest_date
    )
    return_metrics: dict[str, dict[str, Any]] = {}
    for label, horizon in (("1D", 1), ("3D", 3), ("5D", 5), ("20D", 20)):
        usable = trailing_returns[-horizon:]
        return_metrics[label] = {
            "value": _compound_return(usable) if len(usable) >= horizon else None,
            "observations": len(usable),
            "required_observations": horizon,
        }

    realized_values = trailing_returns[-REQUESTED_HISTORY_DAYS:]
    realized_vol: float | None = None
    if len(realized_values) >= MIN_REALIZED_VOL_OBSERVATIONS:
        standard_deviation = _sample_std(realized_values)
        if standard_deviation is not None:
            realized_vol = standard_deviation * sqrt(252.0)
            if not math.isfinite(realized_vol):
                realized_vol = None

    volumes = [
        value
        for observation in observations
        if (value := _finite_number(observation.get("volume"))) is not None
    ]
    open_interests = [
        value
        for observation in observations
        if (value := _finite_number(observation.get("open_interest"))) is not None
    ]
    current_observation = (
        observations[-1]
        if observations and observations[-1].get("trade_date") == latest_date
        else None
    )
    current_volume = (
        _finite_number(current_observation.get("volume"))
        if current_observation is not None
        else None
    )
    current_oi = (
        _finite_number(current_observation.get("open_interest"))
        if current_observation is not None
        else None
    )
    volume_zscore, volume_zscore_observations = _zscore(current_volume, volumes)
    oi_level_zscore, oi_level_zscore_observations = _zscore(current_oi, open_interests)
    oi_differences, delta_oi, delta_oi_pct = _oi_differences(
        observations, snapshot_dates, latest_date
    )
    oi_change_zscore, oi_change_zscore_observations = _zscore(delta_oi, oi_differences)
    volume_oi = None
    if current_volume is not None and current_oi not in (None, 0):
        volume_oi = current_volume / current_oi
        if not math.isfinite(volume_oi):
            volume_oi = None

    attribution = _attribution_clue(
        trailing_returns[-1] if trailing_returns else None,
        delta_oi,
    )

    latest_pair = _curve_pair(latest_curve)
    latest_curve_current = _curve_value(latest_curve)
    pair_values: list[float] = []
    if product_key is not None and latest_pair is not None:
        for _trade_date, _payload, curves in curve_snapshots:
            curve = curves.get(product_key)
            if _curve_pair(curve) == latest_pair:
                value = _curve_value(curve)
                if value is not None:
                    pair_values.append(value)
    curve_zscore, curve_observations = _zscore(
        latest_curve_current, pair_values, MIN_ZSCORE_OBSERVATIONS
    )
    prior_pair = _curve_pair(previous_curve)
    pair_roll_flag = (
        None
        if latest_pair is None or prior_pair is None
        else latest_pair != prior_pair
    )
    curve_record = {
        "nearest_liquid_contract": _contract_from_component(
            latest_curve.get("nearest_liquid_contract")
        ),
        "next_liquid_contract": _contract_from_component(
            latest_curve.get("next_liquid_contract")
        ),
        "current": latest_curve_current,
        "observations": curve_observations,
        "zscore": curve_zscore,
        "zscore_observations": curve_observations,
        "pair_roll_flag": pair_roll_flag,
    }

    missing_metrics = _product_missing_metrics(
        observations,
        daily_returns,
        trailing_returns,
        volume_zscore,
        oi_level_zscore,
        delta_oi,
        oi_change_zscore,
        latest_pair,
        curve_observations,
        curve_zscore,
        available_days,
    )
    product_warnings: set[str] = set()
    if skipped_stale:
        product_warnings.add("stale_exact_contract_record_skipped")
    if main_roll_flag is True:
        product_warnings.add("main_contract_roll_current_contract_history_only")
    if len(observations) < available_days:
        product_warnings.add("current_contract_missing_on_some_verified_days")
    if latest_pair is None:
        product_warnings.add("nearest_next_liquid_pair_unavailable")
    if pair_roll_flag is True:
        product_warnings.add("nearest_next_liquid_pair_roll_detected")
    if current_contract is None:
        product_warnings.add("latest_main_contract_unavailable")

    state_vector = _build_state_vector(
        trailing_returns=trailing_returns,
        daily_returns=trailing_returns,
        returns=return_metrics,
        delta_oi_pct=delta_oi_pct,
        oi_change_zscore=oi_change_zscore,
        volume_oi=volume_oi,
        volume_zscore=volume_zscore,
        curve_current=latest_curve_current,
        curve_zscore=curve_zscore,
    )
    product_record = {
        "exchange": exchange,
        "product": product,
        "product_name": _text(latest_curve.get("product_name")),
        "sector": _text(latest_curve.get("sector")),
        "current_contract": current_contract,
        "current_contract_month": current_month,
        "prior_main_contract": prior_main_contract,
        "main_contract_roll_flag": main_roll_flag,
        "history_observations": observations,
        "settlement_return_pct": return_metrics,
        "realized_vol_20d_annualized_pct": realized_vol,
        "realized_vol_20d_observations": len(realized_values),
        "volume_zscore": volume_zscore,
        "volume_zscore_observations": volume_zscore_observations,
        "oi_level_zscore": oi_level_zscore,
        "oi_level_zscore_observations": oi_level_zscore_observations,
        "delta_OI_1D": delta_oi,
        "delta_OI_pct_1D": delta_oi_pct,
        "oi_change_zscore": oi_change_zscore,
        "oi_change_zscore_observations": oi_change_zscore_observations,
        "volume_oi": volume_oi,
        "attribution_clue": attribution,
        "curve": curve_record,
        "state_vector": state_vector,
        "quality": {
            "history_complete": len(observations) == available_days,
            "same_contract_observations": len(observations),
            "missing_metrics": missing_metrics,
            "warnings": sorted(product_warnings),
        },
    }
    return product_record, product_warnings


def _json_safe(value: Any) -> Any:
    """Defensive final pass for values assembled from external JSON payloads."""

    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def build_market_state(snapshot_payloads: list[dict]) -> dict:
    """Build a current market-state vector from verified daily snapshots.

    Eligible snapshots with malformed or duplicate dates raise ``ValueError``
    because silently choosing between them would make historical features
    non-reproducible.  Unverified snapshots are ignored and reported in
    ``quality.warnings``.  Input order is not trusted; eligible snapshots are
    sorted by their ISO trade date and capped to the latest 20 days.
    """

    if not isinstance(snapshot_payloads, list):
        raise TypeError("snapshot_payloads must be a list of dictionaries")

    eligible: list[tuple[date, str, dict[str, Any]]] = []
    ignored_unverified = 0
    for position, payload in enumerate(snapshot_payloads):
        if not isinstance(payload, dict):
            raise TypeError(f"snapshot at position {position} must be a dictionary")
        if not _is_verified(payload):
            ignored_unverified += 1
            continue
        parsed = _parse_trade_date(payload.get("trade_date"), position=position)
        eligible.append((parsed, parsed.isoformat(), payload))

    if not eligible:
        raise ValueError("no verified or scope_verified snapshots were supplied")

    eligible.sort(key=lambda item: item[0])
    seen_dates: set[date] = set()
    for parsed, trade_date, _payload in eligible:
        if parsed in seen_dates:
            raise ValueError(f"duplicate eligible snapshot trade_date: {trade_date}")
        seen_dates.add(parsed)

    warnings: set[str] = set()
    if ignored_unverified:
        warnings.add("unverified_snapshots_ignored")
    if len(eligible) > REQUESTED_HISTORY_DAYS:
        eligible = eligible[-REQUESTED_HISTORY_DAYS:]
        warnings.add("input_capped_to_latest_20_verified_days")

    calculation_snapshots: list[
        tuple[str, dict[str, Any], dict[tuple[str, str], dict[str, Any]]]
    ] = []
    futures_snapshots: list[
        tuple[str, dict[str, Any], dict[tuple[str, str], dict[str, Any]]]
    ] = []
    duplicate_curve = False
    duplicate_futures = False
    for _parsed, trade_date, payload in eligible:
        curves, curve_duplicate = _curve_map(payload)
        futures, futures_duplicate = _futures_map(payload)
        duplicate_curve = duplicate_curve or curve_duplicate
        duplicate_futures = duplicate_futures or futures_duplicate
        calculation_snapshots.append((trade_date, payload, curves))
        futures_snapshots.append((trade_date, payload, futures))

    latest_date = calculation_snapshots[-1][0]
    latest_curves = calculation_snapshots[-1][2]
    previous_curves = calculation_snapshots[-2][2] if len(calculation_snapshots) > 1 else {}
    products: list[dict[str, Any]] = []
    missing_metrics: set[str] = set()
    product_warnings: set[str] = set()
    for product_key in sorted(latest_curves):
        latest_curve = latest_curves[product_key]
        previous_curve = previous_curves.get(product_key)
        product, product_warning = _product_record(
            latest_date=latest_date,
            latest_curve=latest_curve,
            previous_curve=previous_curve,
            futures_snapshots=futures_snapshots,
            curve_snapshots=calculation_snapshots,
            available_days=len(calculation_snapshots),
        )
        products.append(product)
        missing_metrics.update(product["quality"]["missing_metrics"])
        product_warnings.update(product_warning)

    latest_payload = calculation_snapshots[-1][1]
    if not _collection_records(latest_payload.get("commodity_options")):
        missing_metrics.add("commodity_options")
    if not _collection_records(latest_payload.get("warehouse_inventory")):
        missing_metrics.add("warehouse_inventory")
    if not _collection_records(latest_payload.get("proxy_basis")):
        missing_metrics.add("basis")
    if not _collection_records(latest_payload.get("member_rankings")):
        missing_metrics.add("member_rankings")
    if duplicate_curve:
        warnings.add("duplicate_curve_product_resolved_deterministically")
    if duplicate_futures:
        warnings.add("duplicate_exact_contract_resolved_deterministically")
    warnings.update(product_warnings)
    if len(calculation_snapshots) < REQUESTED_HISTORY_DAYS:
        warnings.add("history_window_shorter_than_requested_20_days")

    dates = [trade_date for trade_date, _payload, _curves in calculation_snapshots]
    output = {
        "schema_version": SCHEMA_VERSION,
        "trade_date": latest_date,
        "history_window": {
            "requested": REQUESTED_HISTORY_DAYS,
            "available_trading_days": len(dates),
            "first_date": dates[0],
            "last_date": dates[-1],
        },
        "methodology": {
            "version": "historical_features_v1",
            "snapshot_selection": (
                "Only snapshots with verified=true or scope_verified=true are used; "
                "eligible dates must be unique ISO dates, are sorted ascending, and "
                "the latest 20 are retained. Malformed or duplicate eligible dates raise ValueError."
            ),
            "contract_continuity": (
                "For each latest exchange/product, lock latest.main_contract.contract. "
                "Historical futures rows are matched only by the same exchange and exact contract; "
                "daily main contracts are never spliced."
            ),
            "settlement_returns": (
                "Use the source settle_return_pct when finite; otherwise use settle/pre_settle, "
                "then same-contract settlement differences. Returns are compounded in percent "
                "over the trailing valid same-contract daily returns."
            ),
            "realized_volatility": (
                "Use up to the latest 20 same-contract daily settlement returns, sample standard "
                "deviation (ddof=1), annualized by sqrt(252); fewer than 5 returns gives null."
            ),
            "z_scores": (
                "Level and change z-scores use sample standard deviation (ddof=1), include the "
                "latest valid observation, and require at least 5 observations; zero variance gives null."
            ),
            "oi_attribution": (
                "attribution_clue is only a price/OI quadrant clue, never a fact about new longs, "
                "short covering, new shorts, or long liquidation."
            ),
            "curve": (
                "Lock the latest nearest_liquid_contract + next_liquid_contract pair; curve history "
                "uses only the same pair's near_next_curve.near_minus_deferred_pct."
            ),
            "market_only_state_vector": (
                "Scores cover price momentum, OI impulse, curve pressure, and activity only. "
                "Fundamental score, convexity score, and trade recommendation remain unavailable."
            ),
            "state_score_thresholds": STATE_SCORE_RULE,
        },
        "products": products,
        "quality": {
            "exact_contract_only": True,
            "main_series_spliced": False,
            "history_complete": len(dates) == REQUESTED_HISTORY_DAYS,
            "missing_metrics": sorted(missing_metrics),
            "warnings": sorted(warnings),
        },
    }
    return _json_safe(output)
