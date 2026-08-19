"""All-market commodity-option collection with product-level failure isolation."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, datetime
import re
from typing import Any

from .catalog import OptionProduct
from .collectors.ifind_http_adapter import IFindHTTPError, IFindHTTPClient
from .collectors.ifind_option_adapter import (
    ExchangeOptionUniverseConfig,
    IFindOptionDataError,
    collect_option_eod_from_exchange_universe,
)
from .option_universe import collect_openctp_option_directories


OptionCollector = Callable[..., dict[str, Any]]
DirectoryLoader = Callable[..., dict[tuple[str, str], list[dict[str, Any]]]]


_IFIND_SECURITY_EXCHANGE_ALIASES = {
    "DCE": "DCE",
    "GFE": "GFEX",
    "GFEX": "GFEX",
    "INE": "INE",
    "SHF": "SHFE",
    "SHFE": "SHFE",
    "CZC": "CZCE",
    "CZCE": "CZCE",
}


def _product_key(product: OptionProduct) -> str:
    return f"{product.exchange.upper()}:{product.product.upper()}"


def _error_detail(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:400]}"


def _permission_error_exchange(exc: IFindHTTPError) -> str | None:
    """Return the exchange named by an explicit iFinD security denial."""

    match = re.search(
        r"permission denied (?:by|for)\s+([A-Z]+)\s+security",
        str(exc),
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    return _IFIND_SECURITY_EXCHANGE_ALIASES.get(match.group(1).upper())


def _status_entry(
    product: OptionProduct,
    status: str,
    *,
    contract_count: int = 0,
    source_trade_date: str | None = None,
    quote_coverage_complete: bool = False,
    universe_source: str | None = None,
    fallback_used: bool = False,
    detail: str | None = None,
) -> dict[str, Any]:
    return {
        "exchange": product.exchange.upper(),
        "product": product.product.upper(),
        "symbol": product.symbol,
        "status": status,
        "contract_count": contract_count,
        "source_trade_date": source_trade_date,
        "quote_coverage_complete": quote_coverage_complete,
        "universe_source": universe_source,
        "fallback_used": fallback_used,
        "detail": detail,
    }


def collect_option_market_snapshot(
    trade_date: str,
    *,
    client: IFindHTTPClient,
    option_products: Sequence[OptionProduct],
    minimum_product_coverage: float = 0.75,
    ak_module: Any | None = None,
    collect_one: OptionCollector = collect_option_eod_from_exchange_universe,
    fallback_directory_loader: DirectoryLoader | None = collect_openctp_option_directories,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Attempt every catalog product and return a promotable snapshot plus status.

    Exchange-directory and schema failures are isolated to one product.
    Explicit market-security denials are isolated to the named exchange;
    authentication, transport, quota and unknown iFinD HTTP errors remain
    global so they are not repeated for every remaining product.
    """

    requested_date = date.fromisoformat(trade_date).isoformat()
    if not 0 < minimum_product_coverage <= 1:
        raise ValueError("minimum_product_coverage must be in (0, 1]")
    products = sorted(
        option_products,
        key=lambda item: (item.exchange.upper(), item.product.upper()),
    )
    if not products:
        raise IFindOptionDataError("option product catalog is empty")
    keys = [_product_key(product) for product in products]
    if len(keys) != len(set(keys)):
        raise IFindOptionDataError("option product catalog contains duplicates")

    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    statuses: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    universe_contract_count = 0
    successful_products: list[OptionProduct] = []
    global_error: str | None = None
    exchange_errors: dict[str, str] = {}
    fallback_directories: dict[tuple[str, str], list[dict[str, Any]]] | None = None
    fallback_directory_error: str | None = None

    for index, product in enumerate(products):
        product_exchange = product.exchange.upper()
        if product_exchange in exchange_errors:
            statuses.append(
                _status_entry(
                    product,
                    "skipped_exchange_ifind_error",
                    detail=(
                        f"not attempted after an explicit {product_exchange} "
                        "iFinD security denial"
                    ),
                )
            )
            continue
        universe = ExchangeOptionUniverseConfig(
            exchange=product.exchange,
            product=product.product,
            symbol=product.symbol,
        )
        fallback_used = False
        primary_error: str | None = None
        try:
            product_snapshot = collect_one(
                requested_date,
                client=client,
                universes=[universe],
                ak_module=ak_module,
            )
        except IFindHTTPError as exc:
            detail = _error_detail(exc)
            denied_exchange = _permission_error_exchange(exc)
            statuses.append(_status_entry(product, "failed", detail=detail))
            if denied_exchange == product_exchange:
                exchange_errors[product_exchange] = detail
                continue
            global_error = detail
            for remaining in products[index + 1 :]:
                statuses.append(
                    _status_entry(
                        remaining,
                        "skipped_global_ifind_error",
                        detail="not attempted after a global iFinD HTTP failure",
                    )
                )
            break
        except Exception as exc:
            primary_error = _error_detail(exc)
            if fallback_directory_loader is None:
                statuses.append(
                    _status_entry(product, "failed", detail=primary_error)
                )
                continue
            if fallback_directories is None and fallback_directory_error is None:
                try:
                    fallback_directories = fallback_directory_loader(
                        requested_date,
                        products,
                    )
                except Exception as fallback_exc:
                    fallback_directory_error = _error_detail(fallback_exc)
            fallback_records = (
                fallback_directories.get(
                    (product.exchange.upper(), product.product.upper())
                )
                if fallback_directories is not None
                else None
            )
            if not fallback_records:
                detail = primary_error
                if fallback_directory_error:
                    detail += f"; fallback directory failed: {fallback_directory_error}"
                else:
                    detail += "; fallback directory has no active catalog contracts"
                statuses.append(_status_entry(product, "failed", detail=detail))
                continue
            try:
                product_snapshot = collect_one(
                    requested_date,
                    client=client,
                    universes=[universe],
                    ak_module=ak_module,
                    directory_records_by_product={
                        (
                            product.exchange.upper(),
                            product.product.upper(),
                        ): fallback_records
                    },
                )
                fallback_used = True
            except IFindHTTPError as fallback_exc:
                fallback_detail = _error_detail(fallback_exc)
                denied_exchange = _permission_error_exchange(fallback_exc)
                statuses.append(
                    _status_entry(
                        product,
                        "failed",
                        detail=(
                            f"{primary_error}; fallback quote failed: "
                            f"{fallback_detail}"
                        ),
                    )
                )
                if denied_exchange == product_exchange:
                    exchange_errors[product_exchange] = fallback_detail
                    continue
                global_error = fallback_detail
                for remaining in products[index + 1 :]:
                    statuses.append(
                        _status_entry(
                            remaining,
                            "skipped_global_ifind_error",
                            detail="not attempted after a global iFinD HTTP failure",
                        )
                    )
                break
            except Exception as fallback_exc:
                statuses.append(
                    _status_entry(
                        product,
                        "failed",
                        detail=(
                            f"{primary_error}; fallback collection failed: "
                            f"{_error_detail(fallback_exc)}"
                        ),
                    )
                )
                continue

        try:
            product_records = product_snapshot.get("records")
            if not isinstance(product_records, list) or not product_records:
                raise IFindOptionDataError(
                    f"option collector returned no records for {_product_key(product)}"
                )
            if product_snapshot.get("quote_coverage_complete") is not True:
                raise IFindOptionDataError(
                    f"option collector returned incomplete quotes for {_product_key(product)}"
                )
            product_universe_count = product_snapshot.get("universe_contract_count")
            product_quote_count = product_snapshot.get("quote_contract_count")
            if (
                not isinstance(product_universe_count, int)
                or isinstance(product_universe_count, bool)
                or product_universe_count != len(product_records)
            ):
                raise IFindOptionDataError(
                    f"option collector returned inconsistent counts for {_product_key(product)}"
                )
            if (
                not isinstance(product_quote_count, int)
                or isinstance(product_quote_count, bool)
                or product_quote_count != len(product_records)
            ):
                raise IFindOptionDataError(
                    f"option collector returned inconsistent quote counts for {_product_key(product)}"
                )
            if any(
                not isinstance(record, dict)
                or record.get("trade_date") != requested_date
                or str(record.get("exchange") or "").upper()
                != product.exchange.upper()
                or str(record.get("product") or "").upper()
                != product.product.upper()
                for record in product_records
            ):
                raise IFindOptionDataError(
                    f"option collector returned out-of-scope records for {_product_key(product)}"
                )
        except Exception as exc:
            statuses.append(
                _status_entry(
                    product,
                    "failed",
                    detail=(
                        f"{primary_error}; fallback validation failed: {_error_detail(exc)}"
                        if fallback_used and primary_error
                        else _error_detail(exc)
                    ),
                )
            )
            continue

        records.extend(product_records)
        universe_contract_count += product_universe_count
        successful_products.append(product)
        statuses.append(
            _status_entry(
                product,
                "success",
                contract_count=len(product_records),
                source_trade_date=requested_date,
                quote_coverage_complete=True,
                universe_source=product_snapshot.get("universe_source"),
                fallback_used=fallback_used,
                detail=(
                    f"primary directory failed; fallback used: {primary_error}"
                    if fallback_used and primary_error
                    else None
                ),
            )
        )

    contracts = [str(record.get("contract") or "").upper() for record in records]
    duplicate_contract_count = len(contracts) - len(set(contracts))
    if duplicate_contract_count:
        global_error = (
            f"combined option snapshot contains {duplicate_contract_count} duplicate contract(s)"
        )

    expected_count = len(products)
    successful_count = len(successful_products)
    failed_count = sum(item["status"] == "failed" for item in statuses)
    skipped_count = expected_count - successful_count - failed_count
    attempted_count = successful_count + failed_count
    product_coverage = successful_count / expected_count
    scope_complete = successful_count == expected_count
    publish_eligible = bool(
        records
        and duplicate_contract_count == 0
        and product_coverage >= minimum_product_coverage
    )
    universe_sources = sorted(
        {
            str(item["universe_source"])
            for item in statuses
            if item["status"] == "success" and item.get("universe_source")
        }
    )
    aggregate_universe_source = (
        universe_sources[0]
        if len(universe_sources) == 1
        else "mixed_contract_directories"
    )
    coverage = {
        "expected_product_count": expected_count,
        "attempted_product_count": attempted_count,
        "successful_product_count": successful_count,
        "failed_product_count": failed_count,
        "skipped_product_count": skipped_count,
        "product_coverage": product_coverage,
        "minimum_product_coverage": minimum_product_coverage,
        "scope_complete": scope_complete,
        "publish_eligible": publish_eligible,
        "successful_products": [
            _product_key(product) for product in successful_products
        ],
        "failed_products": [
            f"{item['exchange']}:{item['product']}"
            for item in statuses
            if item["status"] == "failed"
        ],
        "skipped_products": [
            f"{item['exchange']}:{item['product']}"
            for item in statuses
            if item["status"]
            in {"skipped_global_ifind_error", "skipped_exchange_ifind_error"}
        ],
    }
    status = {
        "schema_version": 1,
        "trade_date": requested_date,
        "generated_at": generated_at,
        "source_provider": "ifind_http",
        "universe_source": aggregate_universe_source,
        "universe_sources": universe_sources,
        "data_fresh": publish_eligible,
        "coverage": coverage,
        "universe_contract_count": universe_contract_count,
        "quote_contract_count": len(records),
        "duplicate_contract_count": duplicate_contract_count,
        "global_error": global_error,
        "exchange_errors": exchange_errors,
        "product_statuses": statuses,
    }
    if not records or duplicate_contract_count:
        return None, status

    snapshot = {
        "schema_version": 1,
        "trade_date": requested_date,
        "generated_at": generated_at,
        "source_provider": "ifind_http",
        "universe_source": aggregate_universe_source,
        "universe_sources": universe_sources,
        "universe_contract_count": universe_contract_count,
        "quote_contract_count": len(records),
        "quote_coverage_complete": len(records) == universe_contract_count,
        "collection_mode": "end_of_day_full_market",
        "intraday": False,
        "coverage": coverage,
        "product_statuses": statuses,
        "records": sorted(
            records,
            key=lambda item: (
                str(item.get("exchange") or ""),
                str(item.get("product") or ""),
                str(item.get("contract") or ""),
            ),
        ),
    }
    return snapshot, status


__all__ = ["collect_option_market_snapshot"]
