"""Compare iFinD HTTP history with a promoted commodity snapshot in memory."""

from __future__ import annotations

import argparse
from getpass import getpass
import json
import math
import os
from pathlib import Path
from typing import Any

import pandas as pd

from china_commodities.collectors.ifind_http_adapter import (
    IFindHTTPClient,
    collect_futures_daily,
)
from china_commodities.normalize import normalize_futures


COMPARE_FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "settle",
    "pre_settle",
    "volume",
    "open_interest",
    "turnover",
)


def _number(value: Any) -> float | None:
    parsed = pd.to_numeric(value, errors="coerce")
    return None if pd.isna(parsed) else float(parsed)


def _field_metrics(pairs: list[tuple[Any, Any]]) -> dict[str, Any]:
    comparable = 0
    exact = 0
    max_abs_diff = 0.0
    max_relative_diff = 0.0
    for left, right in pairs:
        left_number = _number(left)
        right_number = _number(right)
        if left_number is None or right_number is None:
            continue
        comparable += 1
        difference = abs(left_number - right_number)
        max_abs_diff = max(max_abs_diff, difference)
        scale = max(abs(left_number), abs(right_number), 1.0)
        max_relative_diff = max(max_relative_diff, difference / scale)
        exact += math.isclose(left_number, right_number, rel_tol=1e-12, abs_tol=1e-12)
    return {
        "comparable": comparable,
        "exact_matches": exact,
        "exact_match_pct": round(exact / comparable * 100.0, 2) if comparable else None,
        "max_abs_diff": max_abs_diff if comparable else None,
        "max_relative_diff_pct": round(max_relative_diff * 100.0, 6)
        if comparable
        else None,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot",
        default="data/scoped/ex-dce/latest.json",
        help="promoted snapshot used as the comparison baseline",
    )
    parser.add_argument(
        "--contracts",
        nargs="*",
        default=[],
        help="optional concrete contracts; defaults to the most active contract per exchange",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    snapshot = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    records = list(snapshot.get("futures_contracts", []))
    if args.contracts:
        selected_names = {str(value).upper() for value in args.contracts}
        selected = [
            record for record in records if str(record.get("contract")).upper() in selected_names
        ]
    else:
        selected = []
        for exchange in snapshot.get("coverage_scope", {}).get("included_exchanges", []):
            candidates = [record for record in records if record.get("exchange") == exchange]
            candidates.sort(key=lambda record: _number(record.get("volume")) or -1, reverse=True)
            if candidates:
                selected.append(candidates[0])
    if not selected:
        raise SystemExit("no matching baseline contracts")

    refresh_token = os.environ.get("IFIND_REFRESH_TOKEN") or getpass(
        "iFinD refresh token: "
    )
    client = IFindHTTPClient(refresh_token=refresh_token)
    trade_date = str(snapshot["trade_date"])
    ifind_records: list[dict[str, Any]] = []
    for exchange in sorted({str(record["exchange"]) for record in selected}):
        contracts = [
            str(record["contract"])
            for record in selected
            if record.get("exchange") == exchange
        ]
        raw = collect_futures_daily(
            trade_date,
            exchange,
            contracts,
            client=client,
        )
        ifind_records.extend(normalize_futures(raw, exchange, trade_date))

    baseline = {
        (record.get("exchange"), record.get("contract")): record for record in selected
    }
    observed = {
        (record.get("exchange"), record.get("contract")): record
        for record in ifind_records
    }
    common_keys = sorted(set(baseline).intersection(observed))
    result = {
        "trade_date": trade_date,
        "baseline_source": str(args.snapshot),
        "candidate_source": "ifind_http/cmd_history_quotation",
        "requested_contracts": len(baseline),
        "returned_contracts": len(observed),
        "matched_contracts": len(common_keys),
        "missing_in_ifind": [f"{exchange}:{contract}" for exchange, contract in sorted(set(baseline) - set(observed))],
        "unexpected_in_ifind": [f"{exchange}:{contract}" for exchange, contract in sorted(set(observed) - set(baseline))],
        "fields": {
            field: _field_metrics(
                [(baseline[key].get(field), observed[key].get(field)) for key in common_keys]
            )
            for field in COMPARE_FIELDS
        },
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if len(common_keys) == len(baseline) else 1


if __name__ == "__main__":
    raise SystemExit(main())
