"""Failure-isolated daily collection, feature generation and publication."""

from __future__ import annotations

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
from .storage import publish_raw_options, publish_status, publish_verified


WAREHOUSE_EXCHANGES = ("SHFE", "DCE", "CZCE", "GFEX")


def _payload_count(payload: Any) -> int:
    if isinstance(payload, pd.DataFrame):
        return int(len(payload))
    if isinstance(payload, dict):
        return sum(_payload_count(value) for value in payload.values())
    if isinstance(payload, (list, tuple)):
        return len(payload)
    return 0


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
        return payload, ModuleStatus(
            dataset=dataset,
            scope=scope,
            state=state,
            trade_date=trade_date,
            source_function=source_function,
            records=records,
            upstream_source=upstream_source,
            is_fresh=state == "ok",
            is_proxy=is_proxy,
            is_fallback=is_fallback,
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
        )


def _options_to_collect(
    catalog: ProductCatalog,
    include_options: bool,
    option_limit: int | None,
) -> tuple[OptionProduct, ...]:
    if not include_options:
        return ()
    return catalog.options if option_limit is None else catalog.options[:option_limit]


def run_pipeline(
    trade_date: str,
    *,
    data_dir: str | Path = "data",
    catalog_path: str | Path | None = None,
    include_options: bool = True,
    option_limit: int | None = None,
    publish: bool = True,
    ak_module: Any | None = None,
    now: datetime | None = None,
) -> PipelineResult:
    normalized_date = iso_date(trade_date)
    generated = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    catalog = load_catalog(catalog_path)
    result = PipelineResult(
        trade_date=normalized_date,
        generated_at=generated.isoformat(),
        akshare_version=akshare_version(ak_module),
    )

    for exchange in COMMODITY_EXCHANGES:
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
            except Exception as exc:
                status.state = "error"
                status.is_fresh = False
                status.records = 0
                status.error = f"normalization {type(exc).__name__}: {exc}"

    for exchange in COMMODITY_EXCHANGES:
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

    for exchange in WAREHOUSE_EXCHANGES:
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

    products = sorted(catalog.product_to_sector)
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
    for exchange in COMMODITY_EXCHANGES:
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

    for option in _options_to_collect(catalog, include_options, option_limit):
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
        normalized_date, result.statuses, result.futures_records
    )
    result.verified = not result.validation_errors
    futures_statuses = {
        status.scope: status
        for status in result.statuses
        if status.dataset == "futures"
    }
    result.official_complete = result.verified and all(
        not futures_statuses[exchange].is_fallback
        for exchange in COMMODITY_EXCHANGES
    )

    if publish:
        target = Path(data_dir)
        publish_status(result, target)
        publish_raw_options(result, target)
        if result.verified:
            publish_verified(result, target)
    return result
