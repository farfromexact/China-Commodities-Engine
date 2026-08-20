"""Collect and publish iFinD commodity-option EOD snapshots."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from china_commodities.catalog import load_catalog
from china_commodities.collection_cache import verified_option_chain_available
from china_commodities.collectors.ifind_http_adapter import (
    IFindHTTPClient,
    IFindHTTPError,
)
from china_commodities.collectors.ifind_option_adapter import (
    ExchangeOptionUniverseConfig,
    IFindOptionDataError,
    IFindOptionReportConfig,
    collect_option_eod_from_exchange_universe,
    collect_option_eod_snapshot,
)
from china_commodities.option_storage import (
    build_option_summary,
    publish_option_attempt,
    publish_option_eod,
    validate_option_snapshot,
)
from china_commodities.option_quality import assess_option_snapshot_quality
from china_commodities.option_batch import (
    collect_option_market_snapshot,
    collect_option_market_snapshot_resuming,
)
from china_commodities.option_rules import load_option_rules, option_rule_for
from china_commodities.storage import read_json, write_json_if_changed


def load_reports(path: Path) -> list[IFindOptionReportConfig]:
    if not path.exists():
        raise IFindOptionDataError(
            f"verified iFinD option config not found: {path}"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    reports = payload.get("reports") if isinstance(payload, dict) else None
    if not isinstance(reports, list) or not reports:
        raise IFindOptionDataError("iFinD option config has no reports")
    if "REPLACE_WITH_" in json.dumps(reports, ensure_ascii=False):
        raise IFindOptionDataError(
            "iFinD option config still contains unverified placeholder fields"
        )
    return [IFindOptionReportConfig.from_dict(value) for value in reports]


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect iFinD commodity-option end-of-day chains."
    )
    parser.add_argument(
        "--date",
        help="Trade date in YYYY-MM-DD; defaults to the current Shanghai date.",
    )
    parser.add_argument(
        "--universe-source",
        choices=("exchange", "data-pool"),
        default="exchange",
        help=(
            "Contract-directory source. Exchange uses the published EOD directory; "
            "data-pool uses an account-specific SuperCommand report."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/ifind_options.json"),
        help="Verified local iFinD option report configuration for data-pool mode.",
    )
    parser.add_argument("--exchange", default="SHFE")
    parser.add_argument("--product", default="CU")
    parser.add_argument("--symbol", default="铜期权")
    parser.add_argument(
        "--all-products",
        action="store_true",
        help="Attempt every commodity-option product in the catalog.",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path("config/products.json"),
        help="Product catalog used by --all-products.",
    )
    parser.add_argument(
        "--option-rules",
        type=Path,
        default=Path("config/option_rules.json"),
        help="Versioned official exchange exercise-rule registry.",
    )
    parser.add_argument(
        "--minimum-product-coverage",
        type=float,
        default=0.75,
        help="Minimum successful product fraction required to promote all-market latest.",
    )
    parser.add_argument(
        "--request-interval-seconds",
        type=float,
        default=0.55,
        help="Minimum delay between iFinD API calls; default stays below 2 requests/second.",
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument(
        "--surface-shadow-days",
        type=int,
        default=5,
        help="Validated EOD dates required before surface_latest is promoted.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate collection and print counts without writing files.",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="request iFinD even when a verified same-date full chain exists",
    )
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    trade_date = arguments.date or datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    if (
        arguments.all_products
        and not arguments.force_refresh
        and not arguments.dry_run
        and verified_option_chain_available(arguments.data_dir, trade_date)
    ):
        status = read_json(
            arguments.data_dir / "options" / "last_run_status.json", default={}
        ) or {}
        print(
            json.dumps(
                {
                    "trade_date": trade_date,
                    "contracts": status.get("quote_contract_count"),
                    "skipped_existing": True,
                    "reason": "verified same-date iFinD option chain already exists",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    run_status: dict | None = None
    status_path = arguments.data_dir / "options" / "last_run_status.json"
    try:
        client = IFindHTTPClient(
            minimum_request_interval_seconds=arguments.request_interval_seconds
        )
        if arguments.universe_source == "data-pool":
            if arguments.all_products:
                raise IFindOptionDataError(
                    "--all-products currently requires --universe-source exchange"
                )
            reports = load_reports(arguments.config)
            snapshot = collect_option_eod_snapshot(
                trade_date,
                client=client,
                reports=reports,
            )
        elif arguments.all_products:
            catalog = load_catalog(arguments.catalog)
            rules = load_option_rules(arguments.option_rules)
            existing_snapshot = None
            if not arguments.force_refresh:
                candidate = read_json(
                    arguments.data_dir / "options" / "latest.json",
                    default={},
                ) or {}
                if candidate.get("trade_date") == trade_date:
                    existing_snapshot = candidate
            if existing_snapshot is not None:
                snapshot, run_status = collect_option_market_snapshot_resuming(
                    trade_date,
                    client=client,
                    option_products=catalog.options,
                    existing_snapshot=existing_snapshot,
                    minimum_product_coverage=arguments.minimum_product_coverage,
                    option_rules=rules,
                )
            else:
                snapshot, run_status = collect_option_market_snapshot(
                    trade_date,
                    client=client,
                    option_products=catalog.options,
                    minimum_product_coverage=arguments.minimum_product_coverage,
                    option_rules=rules,
                )
            if snapshot is None or not run_status["coverage"]["publish_eligible"]:
                if not arguments.dry_run:
                    if snapshot is not None:
                        publish_option_attempt(
                            snapshot,
                            arguments.data_dir,
                            surface_shadow_days=arguments.surface_shadow_days,
                        )
                        run_status["attempt_published"] = True
                        run_status["attempt_path"] = (
                            "data/options/attempt_latest.json.gz"
                        )
                        run_status["attempt_record_count"] = len(
                            snapshot["records"]
                        )
                    else:
                        run_status["attempt_published"] = False
                    run_status["published"] = False
                    write_json_if_changed(status_path, run_status)
                coverage = run_status["coverage"]
                print(
                    "Option all-market collection retained the previous latest: "
                    f"successful {coverage['successful_product_count']} of "
                    f"{coverage['expected_product_count']} products; minimum coverage "
                    f"is {coverage['minimum_product_coverage']:.0%}.",
                    file=sys.stderr,
                )
                return 2
        else:
            rules = load_option_rules(arguments.option_rules)
            rule = option_rule_for(
                arguments.exchange,
                arguments.product,
                rules=rules,
            )
            snapshot = collect_option_eod_from_exchange_universe(
                trade_date,
                client=client,
                universes=[
                    ExchangeOptionUniverseConfig(
                        exchange=arguments.exchange,
                        product=arguments.product,
                        symbol=arguments.symbol,
                        exercise_style=rule["exercise_style"],
                    )
                ],
            )
            for record in snapshot["records"]:
                record["exercise_style_rule_source_url"] = rule["source_url"]
                record["exercise_style_rules_as_of_date"] = rule["rules_as_of_date"]
        validate_option_snapshot(snapshot)
        quality = assess_option_snapshot_quality(snapshot)
        summaries = build_option_summary(snapshot)
        if not arguments.dry_run:
            publish_option_eod(
                snapshot,
                arguments.data_dir,
                surface_shadow_days=arguments.surface_shadow_days,
            )
            if run_status is not None:
                run_status["quality_status"] = quality["status"]
                run_status["published"] = True
                run_status["data_fresh"] = True
                write_json_if_changed(status_path, run_status)
    except (
        IFindHTTPError,
        IFindOptionDataError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        if not arguments.dry_run and run_status is not None:
            run_status["published"] = False
            run_status["data_fresh"] = False
            run_status["global_error"] = f"{type(exc).__name__}: {str(exc)[:400]}"
            write_json_if_changed(status_path, run_status)
        print(f"Option EOD collection stopped safely: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "trade_date": snapshot["trade_date"],
                "contracts": len(snapshot["records"]),
                "series": len(summaries),
                "quality_status": quality["status"],
                "expected_products": (
                    run_status["coverage"]["expected_product_count"]
                    if run_status is not None
                    else 1
                ),
                "successful_products": (
                    run_status["coverage"]["successful_product_count"]
                    if run_status is not None
                    else 1
                ),
                "product_scope_complete": (
                    run_status["coverage"]["scope_complete"]
                    if run_status is not None
                    else None
                ),
                "reused_products": (
                    run_status["coverage"].get("reused_product_count", 0)
                    if run_status is not None
                    else 0
                ),
                "requested_products": (
                    run_status["coverage"].get("requested_product_count")
                    if run_status is not None
                    else 1
                ),
                "surface_ready": quality["surface_ready"],
                "positioning_ready": quality["positioning_ready"],
                "execution_ready": quality["execution_ready"],
                "published": not arguments.dry_run,
                "chain_retention_trading_days": 20,
                "summary_retention_trading_days": 20,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
