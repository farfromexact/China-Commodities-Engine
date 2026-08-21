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
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .storage import read_json, write_json_if_changed


REPORT_SCHEMA_VERSION = 1
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
    for field in ("trade_date", "requested_date", "run_date"):
        value = payload.get(field)
        if value:
            return str(value)
    return fallback


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


def build_report_input(data_dir: str | Path = "data") -> dict[str, Any]:
    """Join current local artifacts into a stable, report-facing JSON object."""

    root = Path(data_dir)
    futures = _mapping(_read(root, "latest.json", {}))
    futures_status = _mapping(_read(root, "last_run_status.json", {}))
    market_state = _mapping(_read(root, "market_state_latest.json", {}))
    physical = _mapping(_read(root, "physical/latest.json", {}))
    physical_status = _mapping(_read(root, "physical/last_run_status.json", {}))
    external = _mapping(_read(root, "external/latest.json", {}))
    external_status = _mapping(_read(root, "external/last_run_status.json", {}))
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

    requested_date = (
        _trade_date(futures)
        or _trade_date(market_state)
        or _trade_date(options_surface)
        or _trade_date(physical)
        or _trade_date(external)
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
        "frequency": "EOD",
        "intraday": False,
        "source_paths": {
            "futures": "data/latest.json",
            "market_state": "data/market_state_latest.json",
            "physical": "data/physical/latest.json",
            "external": "data/external/latest.json",
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
    destination = Path(output_path) if output_path else root / REPORT_INPUT_NAME
    write_json_if_changed(destination, build_report_input(root))
    return destination


__all__ = [
    "CORE_PRODUCTS",
    "REPORT_INPUT_NAME",
    "REPORT_SCHEMA_VERSION",
    "build_report_input",
    "publish_report_input",
]
