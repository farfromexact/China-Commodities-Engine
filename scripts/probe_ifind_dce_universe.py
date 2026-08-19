"""Measure iFinD DCE EOD coverage using a current public contract universe."""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from getpass import getpass
import json
import os
import re

import pandas as pd

from china_commodities.collectors.akshare_adapter import collect_dce_realtime_fallback
from china_commodities.collectors.ifind_adapter import IFIND_FUTURES_FIELDS, contract_to_ifind_code
from china_commodities.collectors.ifind_http_adapter import IFindHTTPClient


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        default=(date.today() - timedelta(days=1)).isoformat(),
        help="completed EOD date to request from iFinD",
    )
    parser.add_argument(
        "--universe-date",
        default=date.today().isoformat(),
        help="current trade date used only to discover concrete DCE contracts",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    universe = collect_dce_realtime_fallback(args.universe_date)
    contracts = sorted(universe["symbol"].dropna().astype(str).str.upper().unique().tolist())
    codes = [contract_to_ifind_code(contract, "DCE") for contract in contracts]
    refresh_token = os.environ.get("IFIND_REFRESH_TOKEN") or getpass(
        "iFinD refresh token: "
    )
    client = IFindHTTPClient(refresh_token=refresh_token)
    frame = client.history_quotes(codes, IFIND_FUTURES_FIELDS, args.date)
    returned_codes = set(frame.get("thscode", pd.Series(dtype=str)).dropna().astype(str).str.upper())
    requested_codes = set(codes)
    source_dates: list[str] = []
    if "time" in frame.columns:
        parsed_dates = pd.to_datetime(frame["time"], errors="coerce").dropna()
        source_dates = sorted(parsed_dates.dt.date.astype(str).unique().tolist())

    def product(code: str) -> str:
        match = re.match(r"[A-Z]+", code)
        return match.group(0) if match else ""

    metrics = {
        "history_date": args.date,
        "universe_date": args.universe_date,
        "requested_contracts": len(requested_codes),
        "returned_contracts": len(returned_codes),
        "contract_coverage_pct": round(
            len(requested_codes.intersection(returned_codes)) / len(requested_codes) * 100.0,
            2,
        )
        if requested_codes
        else 0.0,
        "requested_products": len({product(code) for code in requested_codes}),
        "returned_products": len({product(code) for code in returned_codes}),
        "source_dates": source_dates,
        "missing_contract_count": len(requested_codes - returned_codes),
        "missing_contract_examples": sorted(requested_codes - returned_codes)[:20],
        "unexpected_contract_count": len(returned_codes - requested_codes),
        "field_non_null_pct": {
            field: round(frame[field].notna().mean() * 100.0, 2)
            if field in frame.columns and len(frame)
            else 0.0
            for field in IFIND_FUTURES_FIELDS
        },
    }
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0 if metrics["contract_coverage_pct"] >= 95.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
