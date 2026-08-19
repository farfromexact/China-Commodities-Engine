"""Expiry-isolated EOD commodity-option surface construction."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
import math
from typing import Any, Mapping


IV_THRESHOLD = 0.80
OPEN_INTEREST_THRESHOLD = 0.90
BID_ASK_THRESHOLD = 0.80


def _number(value: Any, *, positive: bool = False) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric):
        return None
    if positive and numeric <= 0:
        return None
    return numeric


def _selected(record: Mapping[str, Any], field: str) -> float | None:
    if field == "iv_percent":
        direct = _number(record.get(field), positive=True)
        if direct is not None:
            return direct
    greeks = record.get("greeks")
    if isinstance(greeks, Mapping):
        selected = greeks.get("selected")
        if isinstance(selected, Mapping):
            return _number(selected.get(field), positive=(field == "iv_percent"))
    return None


def _valid_date(value: Any) -> str | None:
    try:
        text = str(value)
        parsed = date.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return text if parsed.isoformat() == text else None


def _source_matches(record: Mapping[str, Any], trade_date: str) -> bool:
    return bool(
        record.get("source_date_match") is True
        and record.get("trade_date") == trade_date
        and record.get("source_trade_date") == trade_date
    )


def _bid_ask(record: Mapping[str, Any]) -> bool:
    bid = _number(record.get("bid"))
    ask = _number(record.get("ask"), positive=True)
    return bid is not None and ask is not None and ask >= bid


def _ratio(count: int, total: int) -> float:
    return count / total if total else 0.0


def _leg(record: Mapping[str, Any]) -> dict[str, Any]:
    greeks = record.get("greeks")
    greek_map = greeks if isinstance(greeks, Mapping) else {}
    return {
        "contract": record.get("contract"),
        "settle": record.get("settle"),
        "close": record.get("close"),
        "bid": record.get("bid"),
        "ask": record.get("ask"),
        "volume": record.get("volume"),
        "open_interest": record.get("open_interest"),
        "iv_percent": _selected(record, "iv_percent"),
        "delta": _selected(record, "delta"),
        "gamma": _selected(record, "gamma"),
        "vega": _selected(record, "vega"),
        "theta": _selected(record, "theta"),
        "greeks_quality": greek_map.get("quality"),
        "greeks_source": greek_map.get("selected_source"),
    }


def _nearest_delta_iv(
    records: list[Mapping[str, Any]], option_type: str, target: float
) -> float | None:
    candidates = []
    for record in records:
        if str(record.get("option_type") or "").upper() != option_type:
            continue
        delta = _selected(record, "delta")
        iv = _selected(record, "iv_percent")
        if delta is not None and iv is not None:
            candidates.append((abs(delta - target), iv))
    return min(candidates, default=(0.0, None), key=lambda value: value[0])[1]


def _build_one_surface(
    key: tuple[str, str, str, str],
    records: list[Mapping[str, Any]],
    trade_date: str,
) -> dict[str, Any]:
    exchange, product, underlying, expiry_raw = key
    expiry = _valid_date(expiry_raw)
    count = len(records)
    calls = [record for record in records if str(record.get("option_type")).upper() == "C"]
    puts = [record for record in records if str(record.get("option_type")).upper() == "P"]
    source_coverage = _ratio(
        sum(_source_matches(record, trade_date) for record in records), count
    )
    iv_coverage = _ratio(
        sum(_selected(record, "iv_percent") is not None for record in records), count
    )
    oi_coverage = _ratio(
        sum(_number(record.get("open_interest")) is not None for record in records),
        count,
    )
    bid_ask_coverage = _ratio(sum(_bid_ask(record) for record in records), count)
    forward_values = [
        value
        for value in (
            _number(record.get("underlying_settle"), positive=True)
            for record in records
        )
        if value is not None
    ]
    underlying_settle = forward_values[0] if forward_values else None
    metadata_sources = sorted(
        {
            str(record.get("universe_source"))
            for record in records
            if record.get("universe_source")
        }
    )

    point_map: dict[float, dict[str, Any]] = {}
    duplicate_strike_sides: list[str] = []
    for record in records:
        strike = _number(record.get("strike"), positive=True)
        option_type = str(record.get("option_type") or "").upper()
        if strike is None or option_type not in {"C", "P"}:
            continue
        point = point_map.setdefault(strike, {"strike": strike, "call": None, "put": None})
        side = "call" if option_type == "C" else "put"
        if point[side] is not None:
            duplicate_strike_sides.append(f"{strike:g}:{option_type}")
        point[side] = _leg(record)
    points = [point_map[strike] for strike in sorted(point_map)]

    atm_strike = None
    atm_iv = None
    if underlying_settle is not None and points:
        atm_point = min(points, key=lambda point: abs(point["strike"] - underlying_settle))
        atm_strike = atm_point["strike"]
        atm_values = [
            side.get("iv_percent")
            for side in (atm_point.get("call"), atm_point.get("put"))
            if isinstance(side, Mapping) and side.get("iv_percent") is not None
        ]
        if atm_values:
            atm_iv = sum(atm_values) / len(atm_values)
    call_25 = _nearest_delta_iv(records, "C", 0.25)
    put_25 = _nearest_delta_iv(records, "P", -0.25)
    rr25 = call_25 - put_25 if call_25 is not None and put_25 is not None else None
    bf25 = (
        (call_25 + put_25) / 2 - atm_iv
        if call_25 is not None and put_25 is not None and atm_iv is not None
        else None
    )

    limitations: list[str] = []
    if not exchange or not product or not underlying or expiry is None:
        limitations.append("exchange/product/underlying/expiry key is incomplete")
    if source_coverage < 1.0:
        limitations.append(f"source_date_match_pct={source_coverage:.4f} < 1.0000")
    if iv_coverage < IV_THRESHOLD:
        limitations.append(f"iv_coverage={iv_coverage:.4f} < {IV_THRESHOLD:.4f}")
    if not calls or not puts:
        limitations.append("both call and put records are required")
    if underlying_settle is None:
        limitations.append("underlying settlement is missing")
    if duplicate_strike_sides:
        limitations.append("duplicate strike/side records: " + ", ".join(duplicate_strike_sides[:5]))
    surface_ready = not limitations
    positioning_ready = bool(surface_ready and oi_coverage >= OPEN_INTEREST_THRESHOLD)
    execution_ready = bool(surface_ready and bid_ask_coverage >= BID_ASK_THRESHOLD)
    if oi_coverage < OPEN_INTEREST_THRESHOLD:
        limitations.append(
            f"positioning_ready blocked: open_interest_coverage={oi_coverage:.4f} "
            f"< {OPEN_INTEREST_THRESHOLD:.4f}"
        )
    if bid_ask_coverage < BID_ASK_THRESHOLD:
        limitations.append(
            f"execution_ready blocked: bid_ask_coverage={bid_ask_coverage:.4f} "
            f"< {BID_ASK_THRESHOLD:.4f}"
        )
    return {
        "exchange": exchange,
        "product": product,
        "underlying_contract": underlying,
        "expiry_date": expiry,
        "trade_date": trade_date,
        "requested_date": trade_date,
        "source_date": trade_date if source_coverage == 1.0 else None,
        "observation_date": trade_date if source_coverage == 1.0 else None,
        "timezone": "Asia/Shanghai",
        "vendor": "iFinD",
        "original_source": "iFinD Quant API quotes",
        "metadata_sources": metadata_sources,
        "frequency": "EOD",
        "quality_state": "ready" if surface_ready else "not_ready",
        "missing_reason": "; ".join(limitations) if not surface_ready else None,
        "contract_count": count,
        "call_contract_count": len(calls),
        "put_contract_count": len(puts),
        "source_date_match_pct": source_coverage,
        "iv_coverage": iv_coverage,
        "open_interest_coverage": oi_coverage,
        "bid_ask_coverage": bid_ask_coverage,
        "underlying_settle": underlying_settle,
        "atm_strike": atm_strike,
        "atm_iv_percent": atm_iv,
        "risk_reversal_25d_iv_points": rr25,
        "butterfly_25d_iv_points": bf25,
        "surface_ready": surface_ready,
        "positioning_ready": positioning_ready,
        "execution_ready": execution_ready,
        "dealer_gamma_direction_known": False,
        "points": points,
        "limitations": limitations,
    }


def build_option_surface(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Build surfaces without ever mixing underlying contracts or expiries."""

    trade_date = str(snapshot.get("trade_date") or "")
    groups: dict[tuple[str, str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for record in snapshot.get("records") or []:
        if not isinstance(record, Mapping):
            continue
        key = (
            str(record.get("exchange") or "").upper(),
            str(record.get("product") or "").upper(),
            str(record.get("underlying_contract") or "").upper(),
            str(record.get("expiry_date") or ""),
        )
        groups[key].append(record)
    surfaces = [
        _build_one_surface(key, records, trade_date)
        for key, records in sorted(groups.items())
    ]
    ready_count = sum(surface["surface_ready"] for surface in surfaces)
    positioning_count = sum(surface["positioning_ready"] for surface in surfaces)
    execution_count = sum(surface["execution_ready"] for surface in surfaces)
    if ready_count == len(surfaces) and surfaces:
        status = "ready"
    elif ready_count:
        status = "partial_surface"
    else:
        status = "unavailable"
    return {
        "schema_version": 1,
        "trade_date": trade_date,
        "generated_at": snapshot.get("generated_at"),
        "source_provider": snapshot.get("source_provider"),
        "requested_date": trade_date,
        "timezone": "Asia/Shanghai",
        "frequency": "EOD",
        "collection_mode": "end_of_day",
        "intraday": False,
        "grouping_key": ["exchange", "product", "underlying_contract", "expiry_date"],
        "series_count": len(surfaces),
        "surface_ready_count": ready_count,
        "positioning_ready_count": positioning_count,
        "execution_ready_count": execution_count,
        "status": status,
        "promotion_eligible": ready_count > 0,
        "convexity_score": None,
        "surfaces": surfaces,
    }


__all__ = [
    "BID_ASK_THRESHOLD",
    "IV_THRESHOLD",
    "OPEN_INTEREST_THRESHOLD",
    "build_option_surface",
]
