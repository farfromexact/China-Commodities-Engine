"""Failure-isolated daily collection, feature generation and publication."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd

from .catalog import OptionProduct, ProductCatalog, load_catalog
from .collectors.akshare_adapter import (
    COMMODITY_EXCHANGES,
    akshare_version,
    collect_basis_daily,
    collect_contract_info,
    collect_dce_realtime_fallback,
    collect_futures_daily,
    collect_member_rankings,
    collect_option_daily,
    collect_option_volatility_daily,
    collect_warehouse_receipt,
)
from .features import (
    build_curve_features,
    enrich_and_score_curves,
    select_candidates,
    summarize_options,
)
from .models import ModuleStatus, PipelineResult
from .normalize import (
    iso_date,
    normalize_basis,
    normalize_contract_info,
    normalize_futures,
    normalize_member_rankings,
    normalize_options,
    normalize_option_series_volatility,
    normalize_warehouse,
)
from .quality import validate_run
from .storage import (
    publish_raw_options,
    publish_scope_verified,
    publish_status,
    publish_verified,
)


WAREHOUSE_EXCHANGES = ("SHFE", "DCE", "CZCE", "GFEX")


def _normalize_exchanges(exchanges: Sequence[str] | None) -> tuple[str, ...]:
    if exchanges is None:
        return COMMODITY_EXCHANGES
    requested = {str(exchange).upper() for exchange in exchanges}
    invalid = sorted(requested.difference(COMMODITY_EXCHANGES))
    if invalid:
        raise ValueError(f"unsupported exchanges: {', '.join(invalid)}")
    selected = tuple(
        exchange for exchange in COMMODITY_EXCHANGES if exchange in requested
    )
    if not selected:
        raise ValueError("at least one commodity exchange must be selected")
    return selected


def _scope_id(exchanges: tuple[str, ...]) -> str:
    excluded = [
        exchange.lower()
        for exchange in COMMODITY_EXCHANGES
        if exchange not in exchanges
    ]
    return "full-market" if not excluded else "ex-" + "-".join(excluded)


def _payload_count(payload: Any) -> int:
    if isinstance(payload, pd.DataFrame):
        return int(len(payload))
    if isinstance(payload, dict):
        return sum(_payload_count(value) for value in payload.values())
    if isinstance(payload, (list, tuple)):
        return len(payload)
    return 0


def _audit_payload(value: Any) -> Any:
    if isinstance(value, pd.DataFrame):
        clean = value.astype(object).where(pd.notna(value), None)
        return {
            "columns": [str(column) for column in value.columns],
            "records": clean.to_dict(orient="records"),
        }
    if isinstance(value, dict):
        return {
            str(key): _audit_payload(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_audit_payload(item) for item in value]
    if hasattr(value, "item"):
        return value.item()
    return value


def _payload_audit(payload: Any) -> tuple[str, str]:
    normalized = _audit_payload(payload)
    raw = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    raw_hash = hashlib.sha256(raw).hexdigest()

    def schema(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: schema(item) for key, item in value.items()}
        if isinstance(value, list):
            return schema(value[0]) if value else []
        return type(value).__name__

    schema_raw = json.dumps(
        schema(normalized), ensure_ascii=False, sort_keys=True, default=str
    ).encode("utf-8")
    return raw_hash, hashlib.sha256(schema_raw).hexdigest()


def _confirm_futures_source_date(
    status: ModuleStatus, records: list[dict[str, Any]]
) -> None:
    source_dates = sorted(
        {
            str(record["source_trade_date"])
            for record in records
            if record.get("source_trade_date")
        }
    )
    status.source_trade_date = source_dates[0] if len(source_dates) == 1 else None
    status.source_date_match = bool(
        source_dates == [status.requested_trade_date or status.trade_date]
        and all(record.get("source_date_match") is True for record in records)
    )
    status.is_fresh = bool(
        status.state == "ok" and status.records > 0 and status.source_date_match
    )


def _collect(
    *,
    dataset: str,
    scope: str,
    trade_date: str,
    source_function: str,
    upstream_source: str,
    is_proxy: bool,
    is_fallback: bool = False,
    call: Callable[[], Any],
) -> tuple[Any | None, ModuleStatus]:
    try:
        payload = call()
        records = _payload_count(payload)
        state = "ok" if records > 0 else "empty"
        raw_hash, schema_signature = _payload_audit(payload)
        return payload, ModuleStatus(
            dataset=dataset,
            scope=scope,
            state=state,
            trade_date=trade_date,
            source_function=source_function,
            records=records,
            upstream_source=upstream_source,
            is_fresh=False,
            is_proxy=is_proxy,
            is_fallback=is_fallback,
            requested_trade_date=trade_date,
            fetched_at=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
            source_endpoint=source_function,
            raw_payload_sha256=raw_hash,
            schema_signature=schema_signature,
        )
    except Exception as exc:  # data-source failures must remain scoped
        return None, ModuleStatus(
            dataset=dataset,
            scope=scope,
            state="error",
            trade_date=trade_date,
            source_function=source_function,
            records=0,
            error=f"{type(exc).__name__}: {exc}",
            upstream_source=upstream_source,
            is_fresh=False,
            is_proxy=is_proxy,
            is_fallback=is_fallback,
            requested_trade_date=trade_date,
            fetched_at=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
            source_endpoint=source_function,
        )


def _options_to_collect(
    catalog: ProductCatalog,
    include_options: bool,
    option_limit: int | None,
    exchanges: tuple[str, ...],
) -> tuple[OptionProduct, ...]:
    if not include_options:
        return ()
    selected = tuple(
        option for option in catalog.options if option.exchange in exchanges
    )
    return selected if option_limit is None else selected[:option_limit]


def _build_module_quality(
    result: PipelineResult,
    selected_exchanges: tuple[str, ...],
    include_options: bool,
) -> dict[str, str]:
    def statuses(dataset: str) -> list[ModuleStatus]:
        return [status for status in result.statuses if status.dataset == dataset]

    futures_quality = (
        "verified_official"
        if result.core_futures_official_complete
        else "partial_or_fallback"
    )

    metadata_by_contract = {
        (record.get("exchange"), record.get("contract")): record
        for record in result.contract_metadata
    }
    required_meta = (
        "multiplier",
        "tick_size",
        "tick_value",
        "margin_rate_percent",
        "price_limit_percent",
        "last_trading_day",
    )
    metadata_complete = bool(result.futures_records) and all(
        all(metadata_by_contract.get((record["exchange"], record["contract"]), {}).get(field) is not None for field in required_meta)
        for record in result.futures_records
    )
    contract_statuses = statuses("contract_info")
    if any(status.state == "error" for status in contract_statuses):
        contract_quality = "partial_error"
    elif metadata_complete:
        contract_quality = "complete"
    elif result.contract_metadata:
        contract_quality = "partial"
    else:
        contract_quality = "unavailable"

    expected_warehouse = {
        exchange for exchange in selected_exchanges if exchange in WAREHOUSE_EXCHANGES
    }
    warehouse_statuses = [
        status
        for status in statuses("warehouse")
        if status.scope in expected_warehouse
    ]
    if any(status.state == "error" for status in warehouse_statuses):
        warehouse_quality = "partial_error"
    elif warehouse_statuses and all(status.is_fresh for status in warehouse_statuses):
        warehouse_quality = "verified_official"
    elif result.warehouse_records:
        warehouse_quality = "available_source_date_unverified"
    else:
        warehouse_quality = "unavailable"

    ranking_statuses = statuses("member_rankings")
    rankings_reconciled = bool(result.member_ranking_summaries) and all(
        record.get("ranking_reconciled") is True
        for record in result.member_ranking_summaries
    )
    if any(status.state == "error" for status in ranking_statuses):
        ranking_quality = "partial_error"
    elif rankings_reconciled:
        ranking_quality = "reconciled_published_distribution"
    elif result.member_ranking_summaries:
        ranking_quality = "invalid"
    else:
        ranking_quality = "unavailable"

    option_statuses = statuses("options")
    if not include_options:
        options_quality = "not_collected"
    elif any(status.state == "error" for status in option_statuses):
        options_quality = "partial_error_product_aggregate_only"
    elif result.option_records:
        options_quality = "available_product_aggregate_only"
    else:
        options_quality = "unavailable"

    return {
        "futures": futures_quality,
        "contract_meta": contract_quality,
        "warehouse": warehouse_quality,
        "basis": "proxy_unmatched" if result.basis_records else "unavailable",
        "member_rankings": ranking_quality,
        "options_chain": options_quality,
        "options_surface": "not_ready",
    }


def _build_quality_metrics(
    result: PipelineResult,
    catalog: ProductCatalog,
    selected_exchanges: tuple[str, ...],
) -> dict[str, Any]:
    futures_statuses = [
        status for status in result.statuses if status.dataset == "futures"
    ]
    source_matches = sum(status.source_date_match is True for status in futures_statuses)
    unknown_products = sorted(
        {
            str(record.get("product", ""))
            for record in result.futures_records
            if record.get("product")
            and catalog.sector_for(str(record["product"])) == "未分类"
        }
    )
    contract_keys = [
        (record.get("exchange"), record.get("contract"))
        for record in result.futures_records
    ]
    duplicate_contract_count = len(contract_keys) - len(set(contract_keys))

    invalid_ohlc_count = 0
    ohlc_placeholder_count = 0
    negative_volume_or_oi_count = 0
    for record in result.futures_records:
        if record.get("ohlc_quality") == "exchange_zero_placeholder_normalized_to_null":
            ohlc_placeholder_count += 1
        open_price = pd.to_numeric(record.get("open"), errors="coerce")
        high = pd.to_numeric(record.get("high"), errors="coerce")
        low = pd.to_numeric(record.get("low"), errors="coerce")
        close = pd.to_numeric(record.get("close"), errors="coerce")
        prices = [value for value in (open_price, close) if pd.notna(value)]
        if pd.notna(high) and pd.notna(low) and high < low:
            invalid_ohlc_count += 1
        elif prices and (
            (pd.notna(high) and high < max(prices))
            or (pd.notna(low) and low > min(prices))
        ):
            invalid_ohlc_count += 1
        volume = pd.to_numeric(record.get("volume"), errors="coerce")
        open_interest = pd.to_numeric(record.get("open_interest"), errors="coerce")
        if (pd.notna(volume) and volume < 0) or (
            pd.notna(open_interest) and open_interest < 0
        ):
            negative_volume_or_oi_count += 1

    ranking_reconciled = bool(result.member_ranking_summaries) and all(
        record.get("ranking_reconciled") is True
        for record in result.member_ranking_summaries
    )
    return {
        "source_date_match_pct": (
            round(source_matches / len(futures_statuses) * 100.0, 2)
            if futures_statuses
            else 0.0
        ),
        "unknown_product_count": len(unknown_products),
        "unknown_products": unknown_products,
        "duplicate_contract_count": duplicate_contract_count,
        "invalid_ohlc_count": invalid_ohlc_count,
        "ohlc_placeholder_count": ohlc_placeholder_count,
        "negative_volume_or_oi_count": negative_volume_or_oi_count,
        "member_rankings_reconciled": ranking_reconciled,
        "critical_module_errors": len(result.validation_errors),
        "full_market_ready": bool(result.verified),
        "excluded_exchange_count": len(result.excluded_exchanges),
        "coverage_penalty": bool(result.excluded_exchanges),
        "included_exchange_count": len(selected_exchanges),
    }


def run_pipeline(
    trade_date: str,
    *,
    data_dir: str | Path = "data",
    catalog_path: str | Path | None = None,
    include_options: bool = True,
    option_limit: int | None = None,
    exchanges: Sequence[str] | None = None,
    publish: bool = True,
    ak_module: Any | None = None,
    now: datetime | None = None,
) -> PipelineResult:
    normalized_date = iso_date(trade_date)
    selected_exchanges = _normalize_exchanges(exchanges)
    excluded_exchanges = [
        exchange
        for exchange in COMMODITY_EXCHANGES
        if exchange not in selected_exchanges
    ]
    generated = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    catalog = load_catalog(catalog_path)
    result = PipelineResult(
        trade_date=normalized_date,
        generated_at=generated.isoformat(),
        akshare_version=akshare_version(ak_module),
        scope_id=_scope_id(selected_exchanges),
        included_exchanges=list(selected_exchanges),
        excluded_exchanges=excluded_exchanges,
    )

    for exchange in selected_exchanges:
        raw, status = _collect(
            dataset="futures",
            scope=exchange,
            trade_date=normalized_date,
            source_function="get_futures_daily",
            upstream_source=exchange,
            is_proxy=False,
            call=lambda exchange=exchange: collect_futures_daily(
                normalized_date, exchange, ak_module
            ),
        )
        if exchange == "DCE" and status.state != "ok":
            official_error = status.error or f"official state={status.state}"
            fallback_raw, fallback_status = _collect(
                dataset="futures",
                scope="DCE",
                trade_date=normalized_date,
                source_function="futures_zh_realtime",
                upstream_source="Sina",
                is_proxy=False,
                is_fallback=True,
                call=lambda: collect_dce_realtime_fallback(normalized_date, ak_module),
            )
            if fallback_status.state == "ok":
                coverage = float(fallback_raw.attrs.get("product_coverage", 0.0))
                if coverage < 0.75:
                    fallback_status.state = "error"
                    fallback_status.is_fresh = False
                    fallback_status.records = 0
                    fallback_status.error = (
                        f"official DCE failed: {official_error}; "
                        f"Sina fallback product coverage {coverage:.1%} below 75%"
                    )
                    raw, status = None, fallback_status
                else:
                    failed_products = fallback_raw.attrs.get("failed_products", [])
                    fallback_status.error = f"official DCE failed: {official_error}"
                    if failed_products:
                        fallback_status.error += "; fallback product errors: " + ", ".join(
                            failed_products
                        )
                    raw, status = fallback_raw, fallback_status
        result.statuses.append(status)
        if raw is not None and status.state == "ok":
            try:
                records = normalize_futures(raw, exchange, normalized_date)
                result.futures_records.extend(records)
                status.records = len(records)
                if not records:
                    status.state = "empty"
                    status.is_fresh = False
                else:
                    _confirm_futures_source_date(status, records)
            except Exception as exc:
                status.state = "error"
                status.is_fresh = False
                status.records = 0
                status.error = f"normalization {type(exc).__name__}: {exc}"

    for exchange in selected_exchanges:
        function_name = {
            "SHFE": "futures_contract_info_shfe",
            "INE": "futures_contract_info_ine",
            "DCE": "futures_contract_info_dce",
            "CZCE": "futures_contract_info_czce",
            "GFEX": "futures_contract_info_gfex",
        }[exchange]
        raw_meta, meta_status = _collect(
            dataset="contract_info",
            scope=exchange,
            trade_date=normalized_date,
            source_function=function_name,
            upstream_source=exchange,
            is_proxy=False,
            call=lambda exchange=exchange: collect_contract_info(
                normalized_date, exchange, ak_module
            ),
        )
        result.statuses.append(meta_status)
        if raw_meta is not None and meta_status.state == "ok":
            try:
                records = normalize_contract_info(raw_meta, exchange, normalized_date)
                result.contract_metadata.extend(records)
                meta_status.records = len(records)
                if not records:
                    meta_status.state = "empty"
                    meta_status.is_fresh = False
            except Exception as exc:
                meta_status.state = "error"
                meta_status.is_fresh = False
                meta_status.records = 0
                meta_status.error = f"normalization {type(exc).__name__}: {exc}"

    for exchange in (
        exchange for exchange in WAREHOUSE_EXCHANGES if exchange in selected_exchanges
    ):
        function_name = {
            "SHFE": "futures_shfe_warehouse_receipt",
            "DCE": "futures_warehouse_receipt_dce",
            "CZCE": "futures_warehouse_receipt_czce",
            "GFEX": "futures_gfex_warehouse_receipt",
        }[exchange]
        raw, status = _collect(
            dataset="warehouse",
            scope=exchange,
            trade_date=normalized_date,
            source_function=function_name,
            upstream_source=exchange,
            is_proxy=False,
            call=lambda exchange=exchange: collect_warehouse_receipt(
                normalized_date, exchange, ak_module
            ),
        )
        result.statuses.append(status)
        if raw is not None and status.state == "ok":
            try:
                records = normalize_warehouse(raw, exchange, normalized_date, catalog)
                result.warehouse_records.extend(records)
                status.records = len(records)
                if not records:
                    status.state = "empty"
                    status.is_fresh = False
            except Exception as exc:
                status.state = "error"
                status.is_fresh = False
                status.records = 0
                status.error = f"normalization {type(exc).__name__}: {exc}"

    products = sorted({record["product"] for record in result.futures_records})
    raw_basis, basis_status = _collect(
        dataset="basis",
        scope="100PPI",
        trade_date=normalized_date,
        source_function="futures_spot_price",
        upstream_source="100ppi",
        is_proxy=True,
        call=lambda: collect_basis_daily(normalized_date, products, ak_module),
    )
    result.statuses.append(basis_status)
    if raw_basis is not None and basis_status.state == "ok":
        try:
            result.basis_records = normalize_basis(raw_basis, normalized_date)
            basis_status.records = len(result.basis_records)
        except Exception as exc:
            basis_status.state = "error"
            basis_status.is_fresh = False
            basis_status.records = 0
            basis_status.error = f"normalization {type(exc).__name__}: {exc}"

    products_by_exchange: dict[str, list[str]] = {}
    for record in result.futures_records:
        products_by_exchange.setdefault(record["exchange"], []).append(record["product"])
    for exchange in selected_exchanges:
        products_for_exchange = sorted(set(products_by_exchange.get(exchange, [])))
        if not products_for_exchange:
            products_for_exchange = sorted(
                {
                    option.product
                    for option in catalog.options
                    if option.exchange == exchange
                }
            )
        function_name = {
            "SHFE": "get_shfe_rank_table",
            "INE": "get_shfe_rank_table",
            "DCE": "get_dce_rank_table",
            "CZCE": "get_rank_table_czce",
            "GFEX": "futures_gfex_position_rank",
        }[exchange]
        raw_rankings, ranking_status = _collect(
            dataset="member_rankings",
            scope=exchange,
            trade_date=normalized_date,
            source_function=function_name,
            upstream_source=exchange,
            is_proxy=False,
            call=lambda exchange=exchange, products_for_exchange=products_for_exchange: collect_member_rankings(
                normalized_date,
                exchange,
                products_for_exchange or None,
                ak_module,
            ),
        )
        result.statuses.append(ranking_status)
        if raw_rankings is not None and ranking_status.state == "ok":
            try:
                records = normalize_member_rankings(
                    raw_rankings, exchange, normalized_date
                )
                result.member_ranking_summaries.extend(records)
                ranking_status.records = len(records)
                if not records:
                    ranking_status.state = "empty"
                    ranking_status.is_fresh = False
            except Exception as exc:
                ranking_status.state = "error"
                ranking_status.is_fresh = False
                ranking_status.records = 0
                ranking_status.error = f"normalization {type(exc).__name__}: {exc}"

    for option in _options_to_collect(
        catalog, include_options, option_limit, selected_exchanges
    ):
        source_function = {
            "DCE": "option_hist_dce",
            "CZCE": "option_hist_czce",
            "SHFE": "option_hist_shfe",
            "INE": "option_hist_shfe",
            "GFEX": "option_hist_gfex",
        }[option.exchange]
        raw, status = _collect(
            dataset="options",
            scope=f"{option.exchange}:{option.product}",
            trade_date=normalized_date,
            source_function=source_function,
            upstream_source=option.exchange,
            is_proxy=False,
            call=lambda option=option: collect_option_daily(
                normalized_date, option.exchange, option.symbol, ak_module
            ),
        )
        result.statuses.append(status)
        if raw is not None and status.state == "ok":
            try:
                records = normalize_options(
                    raw,
                    option.exchange,
                    option.product,
                    option.symbol,
                    normalized_date,
                )
                result.option_records.extend(records)
                status.records = len(records)
                if not records:
                    status.state = "empty"
                    status.is_fresh = False
            except Exception as exc:
                status.state = "error"
                status.is_fresh = False
                status.records = 0
                status.error = f"normalization {type(exc).__name__}: {exc}"

            if status.state == "ok" and option.exchange in {"SHFE", "INE"}:
                raw_vol, vol_status = _collect(
                    dataset="option_volatility",
                    scope=f"{option.exchange}:{option.product}",
                    trade_date=normalized_date,
                    source_function="option_vol_shfe",
                    upstream_source=option.exchange,
                    is_proxy=False,
                    call=lambda option=option: collect_option_volatility_daily(
                        normalized_date,
                        option.exchange,
                        option.symbol,
                        ak_module,
                    ),
                )
                result.statuses.append(vol_status)
                if raw_vol is not None and vol_status.state == "ok":
                    try:
                        series_iv = normalize_option_series_volatility(raw_vol)
                        for record in records:
                            if record.get("iv_percent") is None:
                                record["iv_percent"] = series_iv.get(
                                    record.get("underlying_contract")
                                )
                                if record["iv_percent"] is not None:
                                    record["iv_source"] = "exchange_series_average"
                        vol_status.records = len(series_iv)
                        if not series_iv:
                            vol_status.state = "empty"
                            vol_status.is_fresh = False
                    except Exception as exc:
                        vol_status.state = "error"
                        vol_status.is_fresh = False
                        vol_status.records = 0
                        vol_status.error = (
                            f"normalization {type(exc).__name__}: {exc}"
                        )

    curves = build_curve_features(result.futures_records, catalog)
    result.option_summaries = summarize_options(result.option_records)
    result.curves = enrich_and_score_curves(
        curves,
        result.warehouse_records,
        result.basis_records,
        result.option_summaries,
        result.member_ranking_summaries,
    )
    result.candidates = select_candidates(result.curves)
    result.validation_errors = validate_run(
        normalized_date,
        result.statuses,
        result.futures_records,
        expected_exchanges=selected_exchanges,
    )
    unknown_products = sorted(
        {
            record["product"]
            for record in result.futures_records
            if catalog.sector_for(record["product"]) == "未分类"
        }
    )
    if unknown_products:
        result.validation_errors.append(
            "unknown catalog products: " + ", ".join(unknown_products)
        )
        result.validation_errors = sorted(set(result.validation_errors))
    result.scope_verified = not result.validation_errors
    result.verified = (
        result.scope_verified and selected_exchanges == COMMODITY_EXCHANGES
    )
    futures_statuses = {
        status.scope: status
        for status in result.statuses
        if status.dataset == "futures"
    }
    result.core_futures_official_complete = result.scope_verified and all(
        not futures_statuses[exchange].is_fallback
        for exchange in selected_exchanges
    )
    result.module_quality = _build_module_quality(
        result, selected_exchanges, include_options
    )
    result.scope_official_complete = bool(
        result.core_futures_official_complete
        and result.module_quality["contract_meta"] == "complete"
        and result.module_quality["warehouse"] == "verified_official"
        and result.module_quality["member_rankings"]
        == "reconciled_published_distribution"
        and result.module_quality["options_chain"]
        == "verified_official_contract_chain"
    )
    result.official_complete = result.verified and result.scope_official_complete
    result.quality_metrics = _build_quality_metrics(
        result, catalog, selected_exchanges
    )

    if publish:
        target = Path(data_dir)
        if result.verified or selected_exchanges == COMMODITY_EXCHANGES:
            publish_status(result, target)
            publish_raw_options(result, target)
            if result.verified:
                publish_verified(result, target)
        else:
            scoped_target = target / "scoped" / result.scope_id
            publish_status(result, scoped_target)
            publish_raw_options(result, scoped_target)
            if result.scope_verified:
                publish_scope_verified(result, scoped_target)
    return result
