"""Run a no-write, full-market iFinD commodity-futures canary."""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from getpass import getpass
import json
import os

from china_commodities.collectors.ifind_http_adapter import IFindHTTPClient
from china_commodities.pipeline import run_pipeline


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        default=(date.today() - timedelta(days=1)).isoformat(),
        help="completed EOD date to request from iFinD",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    refresh_token = os.environ.get("IFIND_REFRESH_TOKEN") or getpass(
        "iFinD refresh token: "
    )
    result = run_pipeline(
        args.date,
        provider="ifind",
        include_options=False,
        publish=False,
        ifind_http_client=IFindHTTPClient(refresh_token=refresh_token),
    )
    contracts_by_exchange = {
        exchange: sum(
            record["exchange"] == exchange for record in result.futures_records
        )
        for exchange in result.included_exchanges
    }
    print(
        json.dumps(
            {
                "trade_date": result.trade_date,
                "primary_provider": result.primary_provider,
                "verified": result.verified,
                "contracts": len(result.futures_records),
                "contracts_by_exchange": contracts_by_exchange,
                "module_quality": result.module_quality,
                "validation_errors": result.validation_errors,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if result.verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
