"""Build a compact, provenance-first input contract for downstream reports.

The collection pipeline deliberately keeps its canonical artifacts separated by
module.  Report writers should not have to know which file contains the latest
market state, option surface, or foundation status, nor should they infer a
module timestamp from the futures run.  This module joins those already
published artifacts without making any vendor request.

The output is intentionally a *report input* rather than a recommendation.  It
contains derived market features and explicit quality gates, while preserving
``null`` for unavailable fundamentals, basis, parity, or execution fields.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import math
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .storage import read_json, write_json_if_changed


REPORT_SCHEMA_VERSION = 2
REPORT_INPUT_NAME = "report_input_latest.json"

# This is the approved Physical scope, not the full futures universe.  Keep the
# order stable so report diffs remain easy to review.
CORE_PRODUCTS: tuple[str, ...] = (
    "I",
    "JM",
    "J",
    "RB",
    "HC",
    "FG",
    "SA",
    "SC",
    "FU",
    "LU",
    "MA",
    "TA",
    "PX",
    "CU",
    "AL",
    "LC",
    "SI",
    "M",
    "Y",
    "P",
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _read(root: Path, relative: str, default: Any = None) -> Any:
    return read_json(root / relative, default=default)


def _trade_date(payload: Mapping[str, Any], fallback: str | None = None) -> str | None:
    for field in ("trading_date", "trade_date", "requested_date", "run_date"):
        value = payload.get(field)
        if value:
            return str(value)
    return fallback


def _merge_options_status(
    root: Path, futures_status: Mapping[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Reflect the separately published option chain in the root status.

    The futures CLI intentionally runs with ``--skip-options`` and the option
    collector publishes under ``data/options``.  Without this reconciliation,
    the root status keeps the CLI's placeholder ``not_collected`` value even
    after a same-date option chain has been promoted.

    Only a same-date, published option manifest is merged.  Quality gates for
    surface, positioning, and execution remain independent fields.
    """

    merged = dict(futures_status)
    option_status = _mapping(_read(root, "options/last_run_status.json", {}))
    if not option_status or "published" not in option_status:
        return merged, False
    option_latest = _mapping(_read(root, "options/latest.json", {}))
    option_quality_payload = _mapping(_read(root, "options/quality_latest.json", {}))
    option_quality = _mapping(option_quality_payload.get("quality"))
    requested_date = _trade_date(merged)
    option_date = (
        _trade_date(option_status)
        or _trade_date(option_quality)
        or _trade_date(option_latest)
    )
    if not requested_date or option_date != requested_date:
        return merged, False

    coverage = _mapping(option_status.get("coverage"))
    latest_matches = _trade_date(option_latest) == requested_date
    published = (
        option_status.get("published") is True
        and option_status.get("data_fresh") is True
        and latest_matches
        and coverage.get("publish_eligible") is True
    )
    full_scope = bool(
        coverage.get("scope_complete") is True
        and option_quality.get("full_product_scope_verified") is True
    )
    full_chain = bool(
        option_quality.get("full_chain_verified") is True and full_scope
    )
    quality_status = str(option_quality.get("status") or "").strip()
    if not published:
        chain_quality = "partial_error" if option_status.get("global_error") else "not_collected"
        module_state = "error" if option_status.get("global_error") else "empty"
        module_error = option_status.get("global_error")
    elif full_chain:
        chain_quality = "verified_vendor_full_chain"
        module_state = "ok"
        module_error = None
    elif quality_status == "partial_chain" or not full_scope:
        chain_quality = "partial_chain"
        module_state = "ok"
        module_error = None
    else:
        chain_quality = "available_vendor_chain"
        module_state = "ok"
        module_error = None

    module_records = option_status.get("quote_contract_count")
    if not isinstance(module_records, int) or isinstance(module_records, bool):
        module_records = option_quality.get("record_count")
    if not isinstance(module_records, int) or isinstance(module_records, bool):
        module_records = option_latest.get("record_count", 0)
    if not isinstance(module_records, int) or isinstance(module_records, bool):
        module_records = 0

    source_date_match = option_quality.get("source_date_match_pct") == 1.0
    module = {
        "dataset": "options",
        "scope": "full-market",
        "state": module_state,
        "trade_date": requested_date,
        "source_function": "collect_ifind_options.py",
        "records": module_records,
        "error": module_error,
        "upstream_source": "iFinD Quant API",
        "is_fresh": published,
        "is_proxy": False,
        "is_fallback": False,
        "requested_trade_date": requested_date,
        "source_trade_date": requested_date if source_date_match else None,
        "source_date_match": source_date_match,
        "fetched_at": option_status.get("generated_at"),
        "source_endpoint": "collect_ifind_options.py",
        "raw_payload_sha256": None,
        "schema_signature": None,
    }

    modules = [dict(item) for item in _list(merged.get("modules")) if isinstance(item, Mapping)]
    replaced = False
    for index, item in enumerate(modules):
        if item.get("dataset") == "options" and item.get("scope") == "full-market":
            modules[index] = module
            replaced = True
            break
    if not replaced:
        modules.append(module)
    merged["modules"] = modules

    module_quality = dict(_mapping(merged.get("module_quality")))
    module_quality["options_chain"] = chain_quality
    module_quality["options_surface"] = (
        "surface_ready" if option_quality.get("surface_ready") is True else "not_ready"
    )
    merged["module_quality"] = module_quality

    quality_metrics = dict(_mapping(merged.get("quality_metrics")))
    quality_metrics.update(
        {
            "options_chain_data_fresh": published,
            "options_full_product_scope_verified": full_scope,
            "options_full_chain_verified": full_chain,
            "options_surface_ready": option_quality.get("surface_ready") is True,
            "options_positioning_ready": option_quality.get("positioning_ready") is True,
            "options_execution_ready": option_quality.get("execution_ready") is True,
        }
    )
    merged["quality_metrics"] = quality_metrics
    return merged, True


def reconcile_main_status(data_dir: str | Path = "data") -> bool:
    """Persist same-date independent option status into root artifacts."""

    root = Path(data_dir)
    path = root / "last_run_status.json"
    current = _mapping(_read(root, "last_run_status.json", {}))
    if not current:
        return False
    merged, applicable = _merge_options_status(root, current)
    if not applicable:
        return False
    changed = write_json_if_changed(path, merged)
    option_module = next(
        (
            dict(item)
            for item in _list(merged.get("modules"))
            if isinstance(item, Mapping)
            and item.get("dataset") == "options"
            and item.get("scope") == "full-market"
        ),
        None,
    )
    if option_module is None:
        return changed

    requested_date = _trade_date(merged)
    for relative, module_key in (
        ("latest.json", "source"),
        ("radar_latest.json", "session_freshness"),
    ):
        artifact_path = root / relative
        artifact = _mapping(_read(root, relative, {}))
        if not artifact or _trade_date(artifact) != requested_date:
            continue
        artifact = dict(artifact)
        artifact["module_quality"] = dict(_mapping(merged.get("module_quality")))
        artifact["quality_metrics"] = dict(_mapping(merged.get("quality_metrics")))
        if module_key == "source":
            source = dict(_mapping(artifact.get("source")))
            modules = [
                dict(item)
                for item in _list(source.get("modules"))
                if isinstance(item, Mapping)
            ]
            replaced = False
            for index, item in enumerate(modules):
                if (
                    item.get("dataset") == "options"
                    and item.get("scope") == "full-market"
                ):
                    modules[index] = option_module
                    replaced = True
                    break
            if not replaced:
                modules.append(option_module)
            source["modules"] = modules
            artifact["source"] = source
        else:
            freshness = [
                dict(item)
                for item in _list(artifact.get(module_key))
                if isinstance(item, Mapping)
            ]
            replaced = False
            for index, item in enumerate(freshness):
                if (
                    item.get("dataset") == "options"
                    and item.get("scope") == "full-market"
                ):
                    freshness[index] = option_module
                    replaced = True
                    break
            if not replaced:
                freshness.append(option_module)
            artifact[module_key] = freshness
        changed = write_json_if_changed(artifact_path, artifact) or changed
    return changed


def _metric_value(product: Mapping[str, Any], horizon: str) -> float | None:
    value = _mapping(product.get("settlement_return_pct")).get(horizon)
    value = _mapping(value).get("value")
    return value if isinstance(value, (int, float)) else None


def _compact_product(product: Mapping[str, Any]) -> dict[str, Any]:
    curve = _mapping(product.get("curve"))
    quality = _mapping(product.get("quality"))
    return {
        "product_key": (
            f"{str(product.get('exchange') or '').upper()}:"
            f"{str(product.get('product') or '').upper()}"
        ),
        "exchange": product.get("exchange"),
        "product": product.get("product"),
        "product_name": product.get("product_name"),
        "sector": product.get("sector"),
        "current_contract": product.get("current_contract"),
        "current_contract_month": product.get("current_contract_month"),
        "main_contract_roll_flag": bool(product.get("main_contract_roll_flag")),
        "settlement_return_pct": {
            horizon: _metric_value(product, horizon)
            for horizon in ("1D", "3D", "5D", "20D")
        },
        "realized_vol_20d_annualized_pct": product.get(
            "realized_vol_20d_annualized_pct"
        ),
        "realized_vol_20d_observations": product.get(
            "realized_vol_20d_observations"
        ),
        "volume_zscore": product.get("volume_zscore"),
        "oi_level_zscore": product.get("oi_level_zscore"),
        "delta_OI_1D": product.get("delta_OI_1D"),
        "delta_OI_pct_1D": product.get("delta_OI_pct_1D"),
        "oi_change_zscore": product.get("oi_change_zscore"),
        "volume_oi": product.get("volume_oi"),
        "attribution_clue": product.get("attribution_clue"),
        "curve": {
            "nearest_liquid_contract": curve.get("nearest_liquid_contract"),
            "next_liquid_contract": curve.get("next_liquid_contract"),
            "current": curve.get("current"),
            "zscore": curve.get("zscore"),
            "observations": curve.get("observations"),
            "pair_roll_flag": bool(curve.get("pair_roll_flag")),
        },
        "state_vector": {
            key: _mapping(value).get("score")
            for key, value in _mapping(product.get("state_vector")).items()
        },
        "quality": {
            "history_complete": quality.get("history_complete"),
            "same_contract_observations": quality.get("same_contract_observations"),
            "missing_metrics": list(quality.get("missing_metrics") or []),
            "warnings": list(quality.get("warnings") or []),
        },
    }


def _compact_surface(surface: Mapping[str, Any]) -> dict[str, Any]:
    """Remove option strike points while retaining every reportable gate."""

    return {
        key: surface.get(key)
        for key in (
            "exchange",
            "product",
            "underlying_contract",
            "expiry_date",
            "trade_date",
            "requested_date",
            "source_date",
            "observation_date",
            "timezone",
            "frequency",
            "quality_state",
            "missing_reason",
            "contract_count",
            "call_contract_count",
            "put_contract_count",
            "source_date_match_pct",
            "iv_coverage",
            "open_interest_coverage",
            "bid_ask_coverage",
            "underlying_settle",
            "atm_strike",
            "atm_iv_percent",
            "risk_reversal_25d_iv_points",
            "butterfly_25d_iv_points",
            "surface_ready",
            "positioning_ready",
            "execution_ready",
            "limitations",
        )
    }


def _option_product_summary(surfaces: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for surface in surfaces:
        exchange = str(surface.get("exchange") or "").upper()
        product = str(surface.get("product") or "").upper()
        if exchange and product:
            grouped[f"{exchange}:{product}"].append(surface)

    output: list[dict[str, Any]] = []
    for product_key, items in sorted(grouped.items()):
        def _coverage(field: str) -> list[float]:
            return [
                float(item[field])
                for item in items
                if isinstance(item.get(field), (int, float))
            ]

        iv = _coverage("iv_coverage")
        oi = _coverage("open_interest_coverage")
        bid_ask = _coverage("bid_ask_coverage")
        exchange, product = product_key.split(":", 1)
        output.append(
            {
                "product_key": product_key,
                "exchange": exchange,
                "product": product,
                "series_count": len(items),
                "surface_ready_count": sum(
                    item.get("surface_ready") is True for item in items
                ),
                "positioning_ready_count": sum(
                    item.get("positioning_ready") is True for item in items
                ),
                "execution_ready_count": sum(
                    item.get("execution_ready") is True for item in items
                ),
                "iv_coverage_min": min(iv) if iv else None,
                "open_interest_coverage_min": min(oi) if oi else None,
                "bid_ask_coverage_min": min(bid_ask) if bid_ask else None,
                "underlying_contracts": sorted(
                    {
                        str(item.get("underlying_contract"))
                        for item in items
                        if item.get("underlying_contract")
                    }
                ),
                "expiry_dates": sorted(
                    {
                        str(item.get("expiry_date"))
                        for item in items
                        if item.get("expiry_date")
                    }
                ),
            }
        )
    return output


def _foundation_view(snapshot: Mapping[str, Any], status: Mapping[str, Any]) -> dict[str, Any]:
    series = [
        {
            key: item.get(key)
            for key in (
                "series_key",
                "domain",
                "product",
                "exchange",
                "target",
                "metric",
                "requested_date",
                "source_date",
                "observation_date",
                "indicator_id",
                "report_id",
                "source_endpoint",
                "permission_status",
                "current_permission_status",
                "value",
                "unit",
                "frequency",
                "vendor",
                "original_source",
                "usage",
                "quality_state",
                "missing_reason",
                "is_stale",
                "carried_forward",
                "region",
                "grade",
                "delivery_location",
                "tax_included",
                "basis_quality",
            )
        }
        for item in _list(snapshot.get("series"))
        if isinstance(item, Mapping)
    ]
    return {
        "requested_date": snapshot.get("requested_date") or status.get("requested_date"),
        "generated_at": snapshot.get("generated_at") or status.get("generated_at"),
        "data_fresh": status.get("data_fresh"),
        "validation_passed": status.get("validation_passed"),
        "published": status.get("published"),
        "coverage": snapshot.get("coverage") or status.get("coverage"),
        "coverage_matrix": [
            {
                key: item.get(key)
                for key in (
                    "key",
                    "exchange",
                    "requested_date",
                    "mapping_status",
                    "permission_status",
                    "current_permission_status",
                    "quality_state",
                    "series_keys",
                    "missing_reason",
                )
            }
            for item in _list(snapshot.get("coverage_matrix"))
            if isinstance(item, Mapping)
        ],
        "series": series,
        "basis": [
            {key: item.get(key) for key in item.keys() if key != "points"}
            for item in _list(snapshot.get("basis"))
            if isinstance(item, Mapping)
        ],
        "import_parities": list(snapshot.get("import_parities") or []),
        "fundamental_score": snapshot.get("fundamental_score"),
        "convexity_score": snapshot.get("convexity_score"),
        "provider_consensus_ready": snapshot.get("provider_consensus_ready"),
        "provider_consensus_missing_reason": snapshot.get(
            "provider_consensus_missing_reason"
        ),
    }


def _module_freshness(
    name: str,
    snapshot: Mapping[str, Any],
    status: Mapping[str, Any],
    *,
    source_dates: list[str] | None = None,
) -> dict[str, Any]:
    dates = sorted({str(value) for value in (source_dates or []) if value})
    return {
        "module": name,
        "requested_date": _trade_date(snapshot) or _trade_date(status),
        "source_dates": dates,
        "source_date_min": dates[0] if dates else None,
        "source_date_max": dates[-1] if dates else None,
        "generated_at": snapshot.get("generated_at") or status.get("generated_at"),
        "data_fresh": status.get("data_fresh"),
        "validation_passed": status.get("validation_passed"),
        "published": status.get("published"),
    }


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _percent_change(value: Any, reference: Any) -> float | None:
    numerator = _finite_number(value)
    denominator = _finite_number(reference)
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return (numerator / denominator - 1.0) * 100.0


def _first_finite(*values: Any) -> float | None:
    for value in values:
        numeric = _finite_number(value)
        if numeric is not None:
            return numeric
    return None


def _night_contract_summary(item: Mapping[str, Any]) -> dict[str, Any]:
    """Return one report-safe contract row, never the full raw session feed."""

    previous_day_close = _first_finite(
        item.get("previous_day_close"), item.get("prior_eod_close")
    )
    previous_day_settlement = _first_finite(
        item.get("previous_day_settlement"),
        item.get("prior_eod_settlement"),
        item.get("pre_settlement"),
    )
    night_close = _finite_number(item.get("night_close"))
    source = _mapping(item.get("source"))
    return {
        "contract": item.get("contract"),
        "night_open": item.get("open"),
        "night_high": item.get("high"),
        "night_low": item.get("low"),
        "night_close": night_close,
        "previous_day_close": previous_day_close,
        "previous_day_settlement": previous_day_settlement,
        "return_vs_close_pct": item.get("return_vs_close_pct")
        if _finite_number(item.get("return_vs_close_pct")) is not None
        else _percent_change(night_close, previous_day_close),
        "return_vs_settlement_pct": item.get("return_vs_settlement_pct")
        if _finite_number(item.get("return_vs_settlement_pct")) is not None
        else _percent_change(night_close, previous_day_settlement),
        "volume": item.get("volume"),
        "open_interest": item.get("open_interest"),
        "delta_open_interest": item.get("delta_open_interest"),
        "session_start": item.get("session_start")
        or item.get("session_window_start"),
        "session_end": item.get("session_end") or item.get("session_window_end"),
        "source_timestamp": item.get("source_timestamp"),
        "source": {
            "provider": source.get("provider") or item.get("source_provider"),
            "endpoint": source.get("endpoint") or item.get("source_endpoint"),
            "code": source.get("code") or item.get("source_code"),
        },
        "quality_state": item.get("quality_state"),
    }


def _night_session_view(
    snapshot: Mapping[str, Any], status: Mapping[str, Any]
) -> dict[str, Any]:
    """Build a compact product index over the canonical night-session file."""

    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for item in _list(snapshot.get("records")):
        if not isinstance(item, Mapping) or item.get("record_state") != "night_session":
            continue
        exchange = str(item.get("exchange") or "").upper()
        product = str(item.get("product") or "").upper()
        if exchange and product:
            grouped[(exchange, product)].append(item)

    def rank(item: Mapping[str, Any]) -> tuple[float, float, str]:
        volume = _finite_number(item.get("volume"))
        open_interest = _finite_number(item.get("open_interest"))
        return (
            -(volume if volume is not None else -1.0),
            -(open_interest if open_interest is not None else -1.0),
            str(item.get("contract") or ""),
        )

    products: dict[str, dict[str, Any]] = {}
    for (exchange, product), contracts in sorted(grouped.items()):
        representative = sorted(contracts, key=rank)[0]
        product_key = f"{exchange}:{product}"
        products[product_key] = {
            "product_key": product_key,
            "exchange": exchange,
            "product": product,
            "fresh_contract_count": len(contracts),
            "selection_basis": "highest_night_volume_then_open_interest",
            "representative_contract": _night_contract_summary(representative),
        }

    session_start_date = (
        snapshot.get("session_start_date")
        or snapshot.get("night_session_date")
        or status.get("session_start_date")
        or status.get("night_session_date")
    )
    session_end_date = (
        snapshot.get("session_end_date")
        or status.get("session_end_date")
        or snapshot.get("trading_date")
        or status.get("trading_date")
    )
    return {
        "source_path": "data/night_session/latest.json",
        "summary_only": True,
        "trading_date": snapshot.get("trading_date") or status.get("trading_date"),
        "night_session_date": session_start_date,
        "session_start_date": session_start_date,
        "session_end_date": session_end_date,
        "generated_at": snapshot.get("generated_at") or status.get("generated_at"),
        "data_fresh": status.get("data_fresh"),
        "validation_passed": status.get("validation_passed"),
        "published": status.get("published"),
        "coverage": snapshot.get("coverage") or status.get("coverage"),
        "coverage_complete": status.get("coverage_complete"),
        "coverage_warnings": list(status.get("coverage_warnings") or []),
        "session_start": snapshot.get("session_start")
        or snapshot.get("session_window_start"),
        "session_end": snapshot.get("session_end")
        or snapshot.get("session_window_end"),
        "record_count": sum(len(items) for items in grouped.values()),
        "product_count": len(products),
        "products": products,
    }


def build_report_input(data_dir: str | Path = "data") -> dict[str, Any]:
    """Join current local artifacts into a stable, report-facing JSON object."""

    root = Path(data_dir)
    futures = _mapping(_read(root, "latest.json", {}))
    futures_status, _ = _merge_options_status(
        root, _mapping(_read(root, "last_run_status.json", {}))
    )
    market_state = _mapping(_read(root, "market_state_latest.json", {}))
    physical = _mapping(_read(root, "physical/latest.json", {}))
    physical_status = _mapping(_read(root, "physical/last_run_status.json", {}))
    external = _mapping(_read(root, "external/latest.json", {}))
    external_status = _mapping(_read(root, "external/last_run_status.json", {}))
    night_session = _mapping(_read(root, "night_session/latest.json", {}))
    night_session_status = _mapping(
        _read(root, "night_session/last_run_status.json", {})
    )
    options_latest = _mapping(_read(root, "options/latest.json", {}))
    options_status = _mapping(_read(root, "options/last_run_status.json", {}))
    options_quality_payload = _mapping(_read(root, "options/quality_latest.json", {}))
    options_quality = _mapping(options_quality_payload.get("quality"))
    options_surface = _mapping(_read(root, "options/surface_latest.json", {}))
    contract_meta = _mapping(_read(root, "contract_meta.json", {}))

    products = [
        _compact_product(item)
        for item in _list(market_state.get("products"))
        if isinstance(item, Mapping)
    ]
    product_by_code = {
        str(item.get("product") or "").upper(): item for item in products
    }
    surfaces = [
        _compact_surface(item)
        for item in _list(options_surface.get("surfaces"))
        if isinstance(item, Mapping)
    ]

    physical_view = _foundation_view(physical, physical_status)
    external_view = _foundation_view(external, external_status)
    night_session_view = _night_session_view(night_session, night_session_status)
    physical_source_dates = [
        str(item.get("source_date"))
        for item in _list(physical.get("series"))
        if isinstance(item, Mapping) and item.get("source_date")
    ]
    external_source_dates = [
        str(item.get("source_date"))
        for item in _list(external.get("series"))
        if isinstance(item, Mapping) and item.get("source_date")
    ]

    # Keep the report's canonical date tied to the daily EOD layer.  The night
    # snapshot belongs to the following trading day, but it is an overlay on
    # the prior completed EOD rather than a replacement for it.
    requested_date = (
        _trade_date(futures)
        or _trade_date(market_state)
        or _trade_date(options_surface)
        or _trade_date(physical)
        or _trade_date(external)
        or _trade_date(night_session)
        or _trade_date(night_session_status)
    )
    generated_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()

    futures_source_dates = [
        str(item.get("source_trade_date") or item.get("trade_date"))
        for item in _list(futures.get("futures_contracts"))
        if isinstance(item, Mapping)
        and (item.get("source_trade_date") or item.get("trade_date"))
    ]
    market_state_window = _mapping(market_state.get("history_window"))

    limitations: list[str] = []
    if not physical_view.get("data_fresh"):
        limitations.append("physical_module_not_fresh")
    if not external_view.get("data_fresh"):
        limitations.append("external_module_not_fresh")
    if not night_session_view.get("data_fresh"):
        limitations.append("night_session_module_not_fresh")
    if night_session_view.get("coverage_complete") is not True:
        limitations.append("night_session_coverage_incomplete")
    if not _list(futures.get("proxy_basis")):
        limitations.append("basis_not_available_in_futures_snapshot")
    if not _list(futures.get("member_rankings")):
        limitations.append("member_rankings_not_available")
    if options_quality.get("positioning_ready") is not True:
        limitations.append("options_positioning_not_ready")
    if options_quality.get("execution_ready") is not True:
        limitations.append("options_execution_not_ready")
    if contract_meta.get("quality_state") != "complete":
        limitations.append("contract_metadata_partial")

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": generated_at,
        "requested_date": requested_date,
        "timezone": "Asia/Shanghai",
        "frequency": (
            "EOD+night_session"
            if night_session_view.get("published")
            else "EOD"
        ),
        "intraday": bool(night_session_view.get("published")),
        "source_paths": {
            "futures": "data/latest.json",
            "market_state": "data/market_state_latest.json",
            "physical": "data/physical/latest.json",
            "external": "data/external/latest.json",
            "night_session": "data/night_session/latest.json",
            "options_chain_index": "data/options/latest.json",
            "options_quality": "data/options/quality_latest.json",
            "options_surface": "data/options/surface_latest.json",
            "contract_meta": "data/contract_meta.json",
        },
        "module_freshness": [
            _module_freshness(
                "futures", futures, futures_status, source_dates=futures_source_dates
            ),
            _module_freshness(
                "market_state",
                market_state,
                futures_status,
                source_dates=[
                    str(value)
                    for value in (
                        market_state_window.get("first_date"),
                        market_state_window.get("last_date"),
                    )
                    if value
                ],
            ),
            _module_freshness(
                "physical", physical, physical_status, source_dates=physical_source_dates
            ),
            _module_freshness(
                "external", external, external_status, source_dates=external_source_dates
            ),
            _module_freshness(
                "night_session",
                night_session,
                night_session_status,
                source_dates=[
                    str(night_session.get("night_session_date"))
                    if night_session.get("night_session_date")
                    else ""
                ],
            ),
            _module_freshness(
                "options",
                options_surface,
                options_status,
                source_dates=[
                    str(options_surface.get("trade_date"))
                    if options_surface.get("trade_date")
                    else ""
                ],
            ),
        ],
        "futures": {
            "trade_date": futures.get("trade_date"),
            "generated_at": futures.get("generated_at"),
            "verified": futures.get("verified"),
            "scope_verified": futures.get("scope_verified"),
            "full_market_ready": futures.get("verified") is True,
            "provider": _mapping(futures.get("source")).get("provider"),
            "quality_metrics": futures.get("quality_metrics") or {},
            "contract_count": len(_list(futures.get("futures_contracts"))),
        },
        "products": products,
        "core_products": [
            product_by_code.get(product)
            for product in CORE_PRODUCTS
            if product_by_code.get(product) is not None
        ],
        "physical": physical_view,
        "external": external_view,
        "night_session": night_session_view,
        "options": {
            "trade_date": options_surface.get("trade_date") or options_latest.get("trade_date"),
            "generated_at": options_surface.get("generated_at") or options_latest.get("generated_at"),
            "record_count": options_quality.get("record_count") or options_latest.get("record_count"),
            "product_coverage": options_quality.get("product_coverage")
            or _mapping(options_latest.get("coverage")).get("product_coverage"),
            "series_count": options_surface.get("series_count"),
            "surface_ready_count": options_surface.get("surface_ready_count"),
            "positioning_ready_count": options_surface.get("positioning_ready_count"),
            "execution_ready_count": options_surface.get("execution_ready_count"),
            "surface_ready": options_quality.get("surface_ready"),
            "positioning_ready": options_quality.get("positioning_ready"),
            "execution_ready": options_quality.get("execution_ready"),
            "iv_coverage": options_quality.get("iv_coverage"),
            "open_interest_coverage": options_quality.get("open_interest_coverage"),
            "bid_ask_coverage": options_quality.get("bid_ask_coverage"),
            "model_greeks_coverage": options_quality.get("model_greeks_coverage"),
            "limitations": list(options_quality.get("limitations") or []),
            "coverage": options_latest.get("coverage") or {},
            "product_statuses": list(options_latest.get("product_statuses") or []),
            "product_summaries": _option_product_summary(surfaces),
            "series": surfaces,
        },
        "contract_metadata": {
            key: contract_meta.get(key)
            for key in (
                "trade_date",
                "generated_at",
                "quality_state",
                "contract_match_coverage",
                "effective_contract_match_coverage",
                "multiplier_coverage",
                "tick_size_coverage",
                "tick_value_coverage",
                "night_session_coverage",
                "delivery_unit_coverage",
                "margin_rate_coverage",
                "price_limit_coverage",
                "last_trading_day_coverage",
                "warning",
            )
        },
        "derived_inputs": {
            "basis": list(physical.get("basis") or []),
            "import_parities": list(physical.get("import_parities") or [])
            + list(external.get("import_parities") or []),
            "warehouse_inventory": list(futures.get("warehouse_inventory") or []),
            "member_rankings": list(futures.get("member_rankings") or []),
        },
        "quality": {
            "market_state_history_window": market_state.get("history_window"),
            "market_state_quality": market_state.get("quality") or {},
            "futures_status": futures_status,
            "physical_status": physical_status,
            "external_status": external_status,
            "night_session_status": night_session_status,
            "options_status": options_status,
            "options_quality": options_quality,
            "limitations": limitations,
        },
    }


def publish_report_input(
    data_dir: str | Path = "data",
    *,
    output_path: str | Path | None = None,
) -> Path:
    """Write the joined report input and return its path."""

    root = Path(data_dir)
    reconcile_main_status(root)
    destination = Path(output_path) if output_path else root / REPORT_INPUT_NAME
    write_json_if_changed(destination, build_report_input(root))
    return destination


__all__ = [
    "CORE_PRODUCTS",
    "REPORT_INPUT_NAME",
    "REPORT_SCHEMA_VERSION",
    "build_report_input",
    "publish_report_input",
    "reconcile_main_status",
]
