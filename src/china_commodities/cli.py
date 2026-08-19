"""Command-line entry point for collection and validation."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .backfill import run_ifind_backfill
from .collectors.akshare_adapter import COMMODITY_EXCHANGES
from .pipeline import run_pipeline
from .quality import validate_snapshot
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

    backfill = subparsers.add_parser(
        "backfill", help="backfill verified common trading days through iFinD"
    )
    backfill.add_argument("--end-date", default=_today_shanghai())
    backfill.add_argument("--days", type=int, default=60)
    backfill.add_argument("--data-dir", default="data")
    backfill.add_argument("--catalog", default=None)
    backfill.add_argument("--history-limit", type=int, default=252)
    backfill.add_argument("--snapshot-limit", type=int, default=60)
    backfill.add_argument("--calendar-days", type=int, default=None)

    validate = subparsers.add_parser("validate", help="validate latest promoted snapshot")
    validate.add_argument("--data-dir", default="data")
    validate.add_argument("--scope", default=None)
    return parser


def _run(args: argparse.Namespace) -> int:
    excluded_exchanges = tuple(dict.fromkeys(args.exclude_exchange))
    included_exchanges = tuple(
        exchange for exchange in COMMODITY_EXCHANGES
        if exchange not in excluded_exchanges
    )
    if not included_exchanges:
        raise ValueError("at least one exchange must remain included")

    result = run_pipeline(
        args.date,
        data_dir=args.data_dir,
        catalog_path=args.catalog,
        include_options=not args.skip_options,
        option_limit=args.option_limit,
        publish=not args.dry_run,
        exchanges=included_exchanges,
        provider=getattr(args, "provider", "akshare"),
        ifind_dce_fallback=args.ifind_dce_fallback,
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


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        if set(args.exclude_exchange) == set(COMMODITY_EXCHANGES):
            parser.error("at least one exchange must remain included")
        return _run(args)
    if args.command == "backfill":
        return _backfill(args)
    return _validate(args)


if __name__ == "__main__":
    raise SystemExit(main())
