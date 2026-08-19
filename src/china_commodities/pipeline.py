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
from .collectors.ifind_http_adapter import (
    IFindHTTPClient,
    collect_futures_daily as collect_ifind_futures_daily,
    collect_futures_universe_daily as collect_ifind_futures_universe_daily,
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
    read_json,
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

    if result.core_futures_official_complete:
        futures_quality = "verified_official"
    elif result.scope_verified and result.primary_provider == "ifind":
        futures_quality = "verified_vendor_primary"
    else:
        futures_quality = "partial_or_fallback"

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
    metadata_carried = any(
        record.get("carried_forward") is True for record in result.contract_metadata
    )
    if metadata_carried:
        contract_quality = "partial_error_with_carry_forward"
    elif any(status.state == "error" for status in contract_statuses):
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
    warehouse_carried = any(
        record.get("carried_forward") is True for record in result.warehouse_records
    )
    if warehouse_carried:
        warehouse_quality = "partial_error_with_carry_forward"
    elif any(status.state == "error" for status in warehouse_statuses):
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
    ranking_carried = any(
        record.get("carried_forward") is True
        for record in result.member_ranking_summaries
    )
    if ranking_carried:
        ranking_quality = "reconciled_distribution_with_carry_forward"
    elif any(status.state == "error" for status in ranking_statuses):
        ranking_quality = "partial_error"
    elif rankings_reconciled:
        ranking_quality = "reconciled_published_distribution"
    elif result.member_ranking_summaries:
        ranking_quality = "invalid"
    else:
        ranking_quality = "unavailable"

    option_statuses = statuses("options")
    options_carried = any(
        record.get("carried_forward") is True for record in result.option_summaries
    )
    if not include_options:
        options_quality = "not_collected"
    elif options_carried:
        options_quality = "partial_error_with_carry_forward_product_aggregate_only"
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
        "basis": (
            "proxy_unmatched_with_carry_forward"
            if any(record.get("carried_forward") is True for record in result.basis_records)
            else "proxy_unmatched" if result.basis_records else "unavailable"
        ),
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
        "carried_forward_contract_meta_count": sum(
            record.get("carried_forward") is True
            for record in result.contract_metadata
        ),
        "carried_forward_warehouse_count": sum(
            record.get("carried_forward") is True
            for record in result.warehouse_records
        ),
        "carried_forward_basis_count": sum(
            record.get("carried_forward") is True
            for record in result.basis_records
        ),
        "carried_forward_member_ranking_count": sum(
            record.get("carried_forward") is True
            for record in result.member_ranking_summaries
        ),
        "carried_forward_option_summary_count": sum(
            record.get("carried_forward") is True
            for record in result.option_summaries
        ),
        "critical_module_errors": len(result.validation_errors),
        "full_market_ready": bool(result.verified),
        "excluded_exchange_count": len(result.excluded_exchanges),
        "coverage_penalty": bool(result.excluded_exchanges),
        "included_exchange_count": len(selected_exchanges),
    }


def _carry_forward_record(
    record: dict[str, Any],
    *,
    current_trade_date: str,
    current_state: str,
    reason: str,
) -> dict[str, Any]:
    carried = dict(record)
    source_trade_date = str(record.get("trade_date") or record.get("as_of_date") or "")
    carried.update(
        {
            "carried_forward": True,
            "carried_from_trade_date": source_trade_date or None,
            "current_trade_date": current_trade_date,
            "current_collection_state": current_state,
            "carry_forward_reason": reason,
            "is_stale": bool(source_trade_date and source_trade_date != current_trade_date),
        }
    )
    return carried


def _merge_previous_records(
    current: list[dict[str, Any]],
    previous: list[dict[str, Any]],
    *,
    key_fields: tuple[str, ...],
    scope_for: Callable[[dict[str, Any]], str],
    statuses: dict[str, ModuleStatus],
    current_trade_date: str,
    previous_trade_date: str | None,
    count_field: str | None = None,
) -> list[dict[str, Any]]:
    current_by_key = {
        tuple(record.get(field) for field in key_fields): dict(record)
        for record in current
    }
    for record in current_by_key.values():
        record.setdefault("carried_forward", False)
        record.setdefault("is_stale", False)

    same_trade_date = previous_trade_date == current_trade_date
    for prior in previous:
        key = tuple(prior.get(field) for field in key_fields)
        scope = scope_for(prior)
        status = statuses.get(scope)
        state = status.state if status is not None else "missing"
        existing = current_by_key.get(key)
        degraded_same_day = bool(
            same_trade_date
            and count_field
            and existing is not None
            and (prior.get(count_field) or 0) > (existing.get(count_field) or 0)
        )
        failed_current_module = status is None or status.state != "ok"
        missing_same_day = same_trade_date and existing is None
        if not (degraded_same_day or failed_current_module or missing_same_day):
            continue
        if existing is not None and not degraded_same_day:
            continue
        reason = (
            "same_trade_date_degraded_repeat"
            if same_trade_date
            else f"current_module_{state}"
        )
        current_by_key[key] = _carry_forward_record(
            prior,
            current_trade_date=current_trade_date,
            current_state=state,
            reason=reason,
        )
    return sorted(
        current_by_key.values(),
        key=lambda record: tuple(str(record.get(field) or "") for field in key_fields),
    )


def _merge_previous_auxiliary(
    result: PipelineResult,
    previous_snapshot: dict[str, Any],
    previous_contract_meta: dict[str, Any],
) -> None:
    previous_trade_date = previous_snapshot.get("trade_date")

    def status_map(dataset: str) -> dict[str, ModuleStatus]:
        return {
            status.scope: status
            for status in result.statuses
            if status.dataset == dataset
        }

    result.warehouse_records = _merge_previous_records(
        result.warehouse_records,
        list(previous_snapshot.get("warehouse_inventory", [])),
        key_fields=("exchange", "product"),
        scope_for=lambda record: str(record.get("exchange") or ""),
        statuses=status_map("warehouse"),
        current_trade_date=result.trade_date,
        previous_trade_date=previous_trade_date,
    )
    result.basis_records = _merge_previous_records(
        result.basis_records,
        list(previous_snapshot.get("proxy_basis", [])),
        key_fields=("product",),
        scope_for=lambda record: "100PPI",
        statuses=status_map("basis"),
        current_trade_date=result.trade_date,
        previous_trade_date=previous_trade_date,
    )
    result.member_ranking_summaries = _merge_previous_records(
        result.member_ranking_summaries,
        list(previous_snapshot.get("member_rankings", [])),
        key_fields=("exchange", "product", "contract", "ranking_scope"),
        scope_for=lambda record: str(record.get("exchange") or ""),
        statuses=status_map("member_rankings"),
        current_trade_date=result.trade_date,
        previous_trade_date=previous_trade_date,
    )
    result.option_summaries = _merge_previous_records(
        result.option_summaries,
        list(previous_snapshot.get("commodity_options", [])),
        key_fields=("exchange", "product", "source_symbol"),
        scope_for=lambda record: (
            f"{record.get('exchange')}:{record.get('product')}"
        ),
        statuses=status_map("options"),
        current_trade_date=result.trade_date,
        previous_trade_date=previous_trade_date,
        count_field="contract_count",
    )

    current_contracts = {
        (record["exchange"], record["contract"])
        for record in result.futures_records
    }
    current_metadata = {
        (record.get("exchange"), record.get("contract")): dict(record)
        for record in result.contract_metadata
    }
    contract_statuses = status_map("contract_info")
    previous_meta_date = previous_contract_meta.get("trade_date")
    same_meta_date = previous_meta_date == result.trade_date
    risk_fields = (
        "multiplier",
        "tick_size",
        "tick_value",
        "margin_rate_percent",
        "price_limit_percent",
        "list_date",
        "last_trading_day",
        "last_delivery_day",
    )
    for prior in previous_contract_meta.get("contracts", []):
        if not any(prior.get(field) is not None for field in risk_fields):
            continue
        key = (prior.get("exchange"), prior.get("contract"))
        if key not in current_contracts:
            continue
        status = contract_statuses.get(str(prior.get("exchange") or ""))
        state = status.state if status is not None else "missing"
        existing = current_metadata.get(key)
        if existing is not None:
            fields = [
                field
                for field in risk_fields
                if existing.get(field) is None and prior.get(field) is not None
            ]
            if not fields or not (same_meta_date or status is None or status.state != "ok"):
                continue
            for field in fields:
                existing[field] = prior[field]
            existing["carried_forward"] = True
            existing["carried_forward_fields"] = fields
            existing["carried_from_trade_date"] = previous_meta_date
            existing["is_stale"] = previous_meta_date != result.trade_date
            continue
        if not (same_meta_date or status is None or status.state != "ok"):
            continue
        carried = _carry_forward_record(
            dict(prior),
            current_trade_date=result.trade_date,
            current_state=state,
            reason=(
                "same_trade_date_degraded_repeat"
                if same_meta_date
                else f"current_module_{state}"
            ),
        )
        carried["as_of_date"] = prior.get("trade_date") or previous_meta_date
        carried["metadata_status"] = "carried_forward_previous_valid"
        carried["carried_forward_fields"] = list(risk_fields)
        current_metadata[key] = carried
    result.contract_metadata = sorted(
        current_metadata.values(),
        key=lambda record: (str(record.get("exchange")), str(record.get("contract"))),
    )


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
    provider: str = "akshare",
    ifind_dce_fallback: bool = False,
    ifind_http_client: IFindHTTPClient | None = None,
    now: datetime | None = None,
) -> PipelineResult:
    normalized_date = iso_date(trade_date)
    normalized_provider = str(provider).strip().lower()
    if normalized_provider not in {"akshare", "ifind"}:
        raise ValueError(f"unsupported data provider: {provider}")
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
        akshare_version=(
            akshare_version(ak_module)
            if normalized_provider == "akshare"
            else "not_used"
        ),
        primary_provider=normalized_provider,
        scope_id=_scope_id(selected_exchanges),
        included_exchanges=list(selected_exchanges),
        excluded_exchanges=excluded_exchanges,
    )
    base_target = Path(data_dir)
    publication_target = (
        base_target
        if selected_exchanges == COMMODITY_EXCHANGES
        else base_target / "scoped" / result.scope_id
    )
    previous_snapshot = (
        read_json(publication_target / "latest.json", default={}) if publish else {}
    )
    previous_contract_meta = (
        read_json(publication_target / "contract_meta.json", default={})
        if publish
        else {}
    )

    primary_ifind_client: IFindHTTPClient | None = None
    if normalized_provider == "ifind":
        primary_ifind_client = ifind_http_client or IFindHTTPClient()
    for exchange in selected_exchanges:
        if normalized_provider == "ifind":
            products = catalog.products_for_exchange(exchange)
            raw, status = _collect(
                dataset="futures",
                scope=exchange,
                trade_date=normalized_date,
                source_function="cmd_history_quotation",
                upstream_source="iFinD Quant API",
                is_proxy=False,
                call=lambda exchange=exchange, products=products: collect_ifind_futures_universe_daily(
                    normalized_date,
                    exchange,
                    products,
                    client=primary_ifind_client,
                ),
            )
        else:
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
        if (
            normalized_provider == "akshare"
            and exchange == "DCE"
            and status.state != "ok"
        ):
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
                    if ifind_dce_fallback:
                        contracts = sorted(
                            fallback_raw["symbol"]
                            .dropna()
                            .astype(str)
                            .str.upper()
                            .unique()
                            .tolist()
                        )
                        client = ifind_http_client or IFindHTTPClient()
                        ifind_raw, ifind_status = _collect(
                            dataset="futures",
                            scope="DCE",
                            trade_date=normalized_date,
                            source_function="cmd_history_quotation",
                            upstream_source="iFinD Quant API",
                            is_proxy=False,
                            is_fallback=True,
                            call=lambda: collect_ifind_futures_daily(
                                normalized_date,
                                "DCE",
                                contracts,
                                client=client,
                            ),
                        )
                        returned_contracts = (
                            set(
                                ifind_raw["symbol"]
                                .dropna()
                                .astype(str)
                                .str.upper()
                                .tolist()
                            )
                            if isinstance(ifind_raw, pd.DataFrame)
                            else set()
                        )
                        contract_coverage = (
                            len(returned_contracts.intersection(contracts))
                            / len(contracts)
                            if contracts
                            else 0.0
                        )
                        if ifind_status.state == "ok" and contract_coverage >= 0.95:
                            ifind_status.error = (
                                f"official DCE failed: {official_error}; "
                                "contract universe discovered from same-date Sina quotes; "
                                f"iFinD EOD contract coverage={contract_coverage:.1%}"
                            )
                            raw, status = ifind_raw, ifind_status
                        else:
                            ifind_error = ifind_status.error or (
                                f"contract coverage {contract_coverage:.1%} below 95%"
                            )
                            ifind_status.state = "error"
                            ifind_status.is_fresh = False
                            ifind_status.records = 0
                            ifind_status.error = (
                                f"official DCE failed: {official_error}; "
                                f"iFinD fallback failed: {ifind_error}; "
                                "Sina quote data retained only for contract discovery"
                            )
                            raw, status = None, ifind_status
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

    if normalized_provider == "ifind":
        for dataset in (
            "contract_info",
            "warehouse",
            "basis",
            "member_rankings",
            "options",
            "option_volatility",
        ):
            result.statuses.append(
                ModuleStatus(
                    dataset=dataset,
                    scope="full-market",
                    state="skipped",
                    trade_date=normalized_date,
                    source_function="not_yet_mapped",
                    records=0,
                    error=(
                        "iFinD report/indicator mapping and entitlement are not yet "
                        "verified for this module"
                    ),
                    upstream_source="iFinD Quant API",
                    is_fresh=False,
                    requested_trade_date=normalized_date,
                )
            )

    for exchange in (
        selected_exchanges if normalized_provider == "akshare" else ()
    ):
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
        exchange
        for exchange in WAREHOUSE_EXCHANGES
        if exchange in selected_exchanges and normalized_provider == "akshare"
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
    if normalized_provider == "akshare":
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
    for exchange in (
        selected_exchanges if normalized_provider == "akshare" else ()
    ):
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
        catalog,
        include_options and normalized_provider == "akshare",
        option_limit,
        selected_exchanges,
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
    if normalized_provider == "akshare":
        _merge_previous_auxiliary(
            result,
            previous_snapshot,
            previous_contract_meta,
        )
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
        and futures_statuses[exchange].upstream_source == exchange
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
        target = base_target
        if result.verified or selected_exchanges == COMMODITY_EXCHANGES:
            publish_status(result, target)
            publish_raw_options(result, target)
            if result.verified:
                publish_verified(result, target)
        else:
            scoped_target = publication_target
            publish_status(result, scoped_target)
            publish_raw_options(result, scoped_target)
            if result.scope_verified:
                publish_scope_verified(result, scoped_target)
    return result
