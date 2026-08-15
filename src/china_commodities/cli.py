"""Command-line entry point for collection and validation."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

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

    validate = subparsers.add_parser("validate", help="validate latest promoted snapshot")
    validate.add_argument("--data-dir", default="data")
    return parser


def _run(args: argparse.Namespace) -> int:
    result = run_pipeline(
        args.date,
        data_dir=args.data_dir,
        catalog_path=args.catalog,
        include_options=not args.skip_options,
        option_limit=args.option_limit,
        publish=not args.dry_run,
    )
    summary = {
        "trade_date": result.trade_date,
        "verified": result.verified,
        "official_complete": result.official_complete,
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
    payload = read_json(data_dir / "latest.json")
    if payload is None:
        status = read_json(data_dir / "last_run_status.json")
        if (
            isinstance(status, dict)
            and status.get("data_fresh") is False
            and status.get("validation_errors")
        ):
            print(
                "No verified latest snapshot was promoted; the failed/partial "
                "run is explicitly recorded in last_run_status.json."
            )
            return 0
        print("No verified data/latest.json or valid partial-run status exists.")
        return 1
    errors = validate_snapshot(payload)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(
        f"Validated {data_dir / 'latest.json'} for {payload['trade_date']} "
        f"with {len(payload['futures_contracts'])} futures contracts."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "run":
        return _run(args)
    return _validate(args)


if __name__ == "__main__":
    raise SystemExit(main())
