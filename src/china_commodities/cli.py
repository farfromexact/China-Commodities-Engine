"""Command-line entry point for collection and validation."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .backfill import run_ifind_backfill
from .collectors.akshare_adapter import COMMODITY_EXCHANGES
from .collection_cache import (
    verified_foundation_available,
    verified_futures_available,
    verified_night_session_available,
)
from .foundation import run_foundation
from .history_storage import rebuild_futures_history_from_snapshots
from .night_session import collect_night_session
from .pipeline import run_pipeline
from .quality import validate_snapshot
from .reporting import publish_report_input
from .source_registry import load_source_registry
from .storage import read_json


def _today_shanghai() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="China commodity daily data engine")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="collect and publish one daily run")
    run.add_argument("--date", default=_today_shanghai())
    run.add_argument("--data-dir", default="data")
    run.add_argument("--catalog", default=None)
    run.add_argument("--skip-options", action="store_true")
    run.add_argument("--option-limit", type=int, default=None)
    run.add_argument("--dry-run", action="store_true")
    run.add_argument(
        "--force-refresh",
        action="store_true",
        help="request the provider even when a verified same-date snapshot exists",
    )
    run.add_argument(
        "--provider",
        choices=("ifind", "akshare"),
        default="ifind",
        help="primary futures data provider; iFinD reads IFIND_REFRESH_TOKEN",
    )
    run.add_argument(
        "--ifind-dce-fallback",
        action="store_true",
        help=(
            "after an official DCE failure, use same-date public contract discovery "
            "and iFinD HTTP EOD history; reads IFIND_REFRESH_TOKEN"
        ),
    )
    run.add_argument(
        "--exclude-exchange",
        action="append",
        choices=COMMODITY_EXCHANGES,
        default=[],
        help="exclude an exchange; may be repeated",
    )
    run.add_argument(
        "--skip-official-auxiliary",
        action="store_true",
        help="skip official exchange contract/warehouse auxiliary collection",
    )

    backfill = subparsers.add_parser(
        "backfill", help="backfill verified common trading days through iFinD"
    )
    backfill.add_argument("--end-date", default=_today_shanghai())
    backfill.add_argument("--days", type=int, default=20)
    backfill.add_argument("--data-dir", default="data")
    backfill.add_argument("--catalog", default=None)
    backfill.add_argument("--history-limit", type=int, default=20)
    backfill.add_argument("--snapshot-limit", type=int, default=20)
    backfill.add_argument("--calendar-days", type=int, default=None)

    history = subparsers.add_parser(
        "history-rebuild",
        help="repair futures Parquet from local verified snapshots (no vendor request)",
    )
    history.add_argument("--data-dir", default="data")
    history.add_argument("--retention-days", type=int, default=252)

    report = subparsers.add_parser(
        "report-input",
        help="publish compact report input from existing local artifacts",
    )
    report.add_argument("--data-dir", default="data")
    report.add_argument("--output", default=None)
    report.add_argument(
        "--repair-futures-history",
        action="store_true",
        help="repair futures Parquet from local snapshots before joining the report input",
    )

    validate = subparsers.add_parser("validate", help="validate latest promoted snapshot")
    validate.add_argument("--data-dir", default="data")
    validate.add_argument("--scope", default=None)

    foundation = subparsers.add_parser(
        "foundation", help="collect pinned-ID Physical and External EOD series"
    )
    foundation.add_argument("--date", default=_today_shanghai())
    foundation.add_argument("--data-dir", default="data")
    foundation.add_argument("--registry", default=None)
    foundation.add_argument(
        "--scope", choices=("physical", "external", "all"), default="all"
    )
    foundation.add_argument(
        "--lookback-days",
        type=int,
        default=400,
        help="calendar-day EDB window; 400 days normally covers 252 trading observations",
    )
    foundation.add_argument("--shadow-days", type=int, default=5)
    foundation.add_argument(
        "--audit-only",
        action="store_true",
        help="validate the fixed source/permission matrix without using a token",
    )
    foundation.add_argument("--dry-run", action="store_true")
    foundation.add_argument(
        "--force-refresh",
        action="store_true",
        help="request iFinD even when a verified same-date module snapshot exists",
    )

    night_session = subparsers.add_parser(
        "night-session",
        help="capture the completed prior-night futures session as a separate snapshot",
    )
    night_session.add_argument(
        "--trade-date",
        default=_today_shanghai(),
        help="exchange trading date following the completed night session",
    )
    night_session.add_argument("--data-dir", default="data")
    night_session.add_argument("--dry-run", action="store_true")
    night_session.add_argument(
        "--force-refresh",
        action="store_true",
        help="request iFinD even when a complete same-session snapshot exists",
    )
    return parser


def _run(args: argparse.Namespace) -> int:
    excluded_exchanges = tuple(dict.fromkeys(args.exclude_exchange))
    included_exchanges = tuple(
        exchange for exchange in COMMODITY_EXCHANGES
        if exchange not in excluded_exchanges
    )
    if not included_exchanges:
        raise ValueError("at least one exchange must remain included")

    provider = getattr(args, "provider", "akshare")
    scope_id = (
        "full-market"
        if not excluded_exchanges
        else "ex-" + "-".join(exchange.lower() for exchange in excluded_exchanges)
    )
    target = (
        Path(args.data_dir)
        if scope_id == "full-market"
        else Path(args.data_dir) / "scoped" / scope_id
    )
    if (
        provider == "ifind"
        and not getattr(args, "force_refresh", False)
        and verified_futures_available(
            target,
            args.date,
            allow_scoped=scope_id != "full-market",
        )
    ):
        snapshot = read_json(target / "latest.json", default={}) or {}
        print(
            json.dumps(
                {
                    "trade_date": args.date,
                    "primary_provider": "ifind",
                    "included_exchanges": list(included_exchanges),
                    "excluded_exchanges": list(excluded_exchanges),
                    "futures_contracts": len(snapshot.get("futures_contracts") or []),
                    "skipped_existing": True,
                    "reason": "verified same-date iFinD futures snapshot already exists",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    result = run_pipeline(
        args.date,
        data_dir=args.data_dir,
        catalog_path=args.catalog,
        include_options=not args.skip_options,
        option_limit=args.option_limit,
        publish=not args.dry_run,
        exchanges=included_exchanges,
        provider=provider,
        ifind_dce_fallback=args.ifind_dce_fallback,
        include_official_auxiliary=not args.skip_official_auxiliary,
    )
    summary = {
        "trade_date": result.trade_date,
        "primary_provider": getattr(result, "primary_provider", None),
        "verified": result.verified,
        "official_complete": result.official_complete,
        "included_exchanges": result.included_exchanges,
        "excluded_exchanges": result.excluded_exchanges,
        "scope_verified": result.scope_verified,
        "futures_contracts": len(result.futures_records),
        "contract_metadata": len(result.contract_metadata),
        "warehouse_products": len(result.warehouse_records),
        "basis_products": len(result.basis_records),
        "option_contracts": len(result.option_records),
        "member_ranking_scopes": len(result.member_ranking_summaries),
        "candidates": len(result.candidates),
        "validation_errors": result.validation_errors,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _validate(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    scope = getattr(args, "scope", None)
    snapshot_path = data_dir / "scoped" / scope / "latest.json" if scope else data_dir / "latest.json"
    payload = read_json(snapshot_path)
    if payload is None:
        status_path = (
            data_dir / "scoped" / scope / "last_run_status.json"
            if scope
            else data_dir / "last_run_status.json"
        )
        status = read_json(status_path)
        if (
            isinstance(status, dict)
            and (
                status.get("scope_data_fresh") is False
                if scope
                else status.get("data_fresh") is False
            )
            and status.get("validation_errors")
        ):
            print(
                "No verified latest snapshot was promoted; the failed/partial "
                f"run is explicitly recorded in {status_path}."
            )
            return 0
        print("No verified data/latest.json or valid partial-run status exists.")
        return 1
    errors = validate_snapshot(payload, allow_scoped=bool(scope))
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(
        f"Validated {snapshot_path} for {payload['trade_date']} "
        f"with {len(payload['futures_contracts'])} futures contracts."
    )
    return 0


def _backfill(args: argparse.Namespace) -> int:
    summary = run_ifind_backfill(
        end_date=args.end_date,
        days=args.days,
        data_dir=args.data_dir,
        catalog_path=args.catalog,
        history_limit=args.history_limit,
        snapshot_limit=args.snapshot_limit,
        calendar_days=args.calendar_days,
    )
    print(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2))
    return 0


def _foundation(args: argparse.Namespace) -> int:
    if args.audit_only:
        audit = load_source_registry(args.registry).audit()
        print(json.dumps(audit, ensure_ascii=False, indent=2))
        return 0
    domains = ("physical", "external") if args.scope == "all" else (args.scope,)
    skipped = {
        domain
        for domain in domains
        if not args.force_refresh
        and not args.dry_run
        and verified_foundation_available(args.data_dir, domain, args.date)
    }
    pending = tuple(domain for domain in domains if domain not in skipped)
    results = {}
    if pending:
        pending_scope = "all" if len(pending) == 2 else pending[0]
        results = run_foundation(
            args.date,
            scope=pending_scope,
            data_dir=args.data_dir,
            registry_path=args.registry,
            lookback_days=args.lookback_days,
            publish=not args.dry_run,
            shadow_days=args.shadow_days,
        )
    summary = {
        domain: {
            "requested_date": result["payload"]["requested_date"],
            "coverage": result["payload"]["coverage"],
            "data_fresh": result["status"]["data_fresh"],
            "validation_passed": result["status"]["validation_passed"],
            "published": result["status"].get("published", False),
            "shadow_state": result["status"].get("shadow_state"),
        }
        for domain, result in results.items()
    }
    for domain in skipped:
        status = read_json(
            Path(args.data_dir) / domain / "last_run_status.json", default={}
        ) or {}
        summary[domain] = {
            "requested_date": args.date,
            "coverage": status.get("coverage"),
            "data_fresh": status.get("data_fresh"),
            "validation_passed": True,
            "published": True,
            "skipped_existing": True,
            "reason": "verified same-date module snapshot already exists",
        }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _history_rebuild(args: argparse.Namespace) -> int:
    rows = rebuild_futures_history_from_snapshots(
        args.data_dir,
        retention_days=args.retention_days,
    )
    print(
        json.dumps(
            {
                "data_dir": str(args.data_dir),
                "retention_days": args.retention_days,
                "futures_history_rows": rows,
                "vendor_request_made": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _night_session(args: argparse.Namespace) -> int:
    if (
        not args.force_refresh
        and not args.dry_run
        and verified_night_session_available(args.data_dir, args.trade_date)
    ):
        snapshot = read_json(
            Path(args.data_dir) / "night_session" / "latest.json", default={}
        ) or {}
        print(
            json.dumps(
                {
                    "trading_date": args.trade_date,
                    "night_session_date": snapshot.get("night_session_date"),
                    "skipped_existing": True,
                    "reason": "complete same-session night snapshot already exists",
                    "coverage": snapshot.get("coverage"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    result = collect_night_session(
        args.trade_date,
        data_dir=args.data_dir,
        publish=not args.dry_run,
        force_refresh=args.force_refresh,
    )
    status = result["status"]
    print(
        json.dumps(
            {
                "trading_date": status["trading_date"],
                "night_session_date": status["night_session_date"],
                "data_fresh": status["data_fresh"],
                "validation_passed": status["validation_passed"],
                "published": status["published"],
                "coverage": status["coverage"],
                "validation_errors": status["validation_errors"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    # A closed-market night can legitimately have no qualifying timestamp.  The
    # status artifact records that explicitly while downstream daily EOD work
    # continues; this command is therefore observational rather than a hard
    # workflow failure.
    return 0


def _report_input(args: argparse.Namespace) -> int:
    if args.repair_futures_history:
        rebuild_futures_history_from_snapshots(args.data_dir, retention_days=252)
    path = publish_report_input(args.data_dir, output_path=args.output)
    print(
        json.dumps(
            {
                "path": str(path),
                "vendor_request_made": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        if set(args.exclude_exchange) == set(COMMODITY_EXCHANGES):
            parser.error("at least one exchange must remain included")
        return _run(args)
    if args.command == "backfill":
        return _backfill(args)
    if args.command == "foundation":
        return _foundation(args)
    if args.command == "night-session":
        return _night_session(args)
    if args.command == "history-rebuild":
        return _history_rebuild(args)
    if args.command == "report-input":
        return _report_input(args)
    return _validate(args)


if __name__ == "__main__":
    raise SystemExit(main())
