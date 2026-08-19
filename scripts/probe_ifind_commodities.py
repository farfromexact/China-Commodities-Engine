"""Run a narrow, read-only iFinD commodity-futures entitlement probe.

Credentials are accepted only from process environment variables or hidden
interactive prompts.  The probe prints response metadata and field coverage;
it does not persist credentials or raw vendor payloads.
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from getpass import getpass
import json
import os
from typing import Any

import pandas as pd

from china_commodities.collectors.ifind_http_adapter import (
    IFindHTTPClient,
    IFindHTTPError,
)


DEFAULT_CODES = (
    "I2609.DCE",
    "M2609.DCE",
    "RB2610.SHF",
    "CU2609.SHF",
    "SC2609.INE",
    "SR609.CZC",
    "LC2609.GFE",
)
DEFAULT_FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "settlement",
    "preSettlement",
    "volume",
    "amount",
    "openInterest",
)


def _response_error(response: Any) -> tuple[Any, str | None]:
    if isinstance(response, dict):
        code = response.get("errorcode", response.get("errcode"))
        message = response.get("errmsg", response.get("message"))
        return code, None if message is None else str(message)
    code = getattr(response, "errorcode", getattr(response, "errcode", None))
    message = getattr(response, "errmsg", getattr(response, "message", None))
    return code, None if message is None else str(message)


def _response_frame(response: Any) -> pd.DataFrame:
    data = response.get("data") if isinstance(response, dict) else getattr(response, "data", None)
    if isinstance(data, pd.DataFrame):
        return data.copy()
    if isinstance(data, list):
        return pd.DataFrame(data)
    return pd.DataFrame()


def _safe_message(message: str | None) -> str | None:
    if not message:
        return None
    return message[:300]


def _probe_code(ifind: Any, code: str, fields: tuple[str, ...], trade_date: str) -> dict[str, Any]:
    response = ifind.THS_HQ(
        code,
        ";".join(fields),
        "",
        trade_date,
        trade_date,
    )
    error_code, error_message = _response_error(response)
    frame = _response_frame(response)
    source_dates: list[str] = []
    if "time" in frame.columns:
        parsed = pd.to_datetime(frame["time"], errors="coerce").dropna()
        source_dates = sorted(parsed.dt.date.astype(str).unique().tolist())
    non_null = {
        field: int(frame[field].notna().sum()) if field in frame.columns else 0
        for field in fields
    }
    returned_codes = (
        sorted(frame["thscode"].dropna().astype(str).unique().tolist())
        if "thscode" in frame.columns
        else []
    )
    return {
        "code": code,
        "error_code": error_code,
        "error_message": _safe_message(error_message),
        "rows": int(len(frame)),
        "columns": [str(column) for column in frame.columns],
        "source_dates": source_dates,
        "returned_codes": returned_codes,
        "non_null_fields": non_null,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider",
        choices=("http", "sdk"),
        default="http",
        help="iFinD access route; HTTP reads a refresh token, SDK reads account credentials",
    )
    parser.add_argument(
        "--mode",
        choices=("history", "realtime"),
        default="history",
        help="HTTP quotation mode; SDK currently uses historical THS_HQ",
    )
    parser.add_argument(
        "--date",
        default=(date.today() - timedelta(days=1)).isoformat(),
        help="completed trade date to probe (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--codes",
        nargs="+",
        default=list(DEFAULT_CODES),
        help="iFinD commodity contract codes",
    )
    parser.add_argument(
        "--fields",
        nargs="+",
        default=list(DEFAULT_FIELDS),
        help="THS_HQ fields to request",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="send all HTTP codes in one request to test batch behavior",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.provider == "http":
        refresh_token = os.environ.get("IFIND_REFRESH_TOKEN") or getpass(
            "iFinD refresh token: "
        )
        client = IFindHTTPClient(refresh_token=refresh_token)
        if args.batch:
            requested_codes = [str(code).upper() for code in args.codes]
            try:
                frame = client.history_quotes(requested_codes, args.fields, args.date)
                returned_codes = sorted(
                    frame.get("thscode", pd.Series(dtype=str))
                    .dropna()
                    .astype(str)
                    .str.upper()
                    .unique()
                    .tolist()
                )
                payload = {
                    "provider": "ifind_http",
                    "mode": "history_batch",
                    "access_token_ok": bool(client.access_token),
                    "trade_date": args.date,
                    "requested_codes": requested_codes,
                    "returned_codes": returned_codes,
                    "rows": int(len(frame)),
                    "error": None,
                }
            except IFindHTTPError as exc:
                payload = {
                    "provider": "ifind_http",
                    "mode": "history_batch",
                    "access_token_ok": bool(client.access_token),
                    "trade_date": args.date,
                    "requested_codes": requested_codes,
                    "returned_codes": [],
                    "rows": 0,
                    "error": _safe_message(str(exc)),
                }
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
            return 0 if payload["error"] is None and payload["rows"] > 0 else 1
        results: list[dict[str, Any]] = []
        for code in args.codes:
            try:
                frame = (
                    client.history_quotes([str(code).upper()], args.fields, args.date)
                    if args.mode == "history"
                    else client.realtime_quotes([str(code).upper()], args.fields)
                )
                source_dates: list[str] = []
                if "time" in frame.columns:
                    parsed = pd.to_datetime(frame["time"], errors="coerce").dropna()
                    source_dates = sorted(parsed.dt.date.astype(str).unique().tolist())
                results.append(
                    {
                        "code": str(code).upper(),
                        "error": None,
                        "rows": int(len(frame)),
                        "columns": [str(column) for column in frame.columns],
                        "source_dates": source_dates,
                        "non_null_fields": {
                            field: int(frame[field].notna().sum())
                            if field in frame.columns
                            else 0
                            for field in args.fields
                        },
                    }
                )
            except IFindHTTPError as exc:
                results.append(
                    {
                        "code": str(code).upper(),
                        "error": _safe_message(str(exc)),
                        "rows": 0,
                        "columns": [],
                        "source_dates": [],
                        "non_null_fields": {field: 0 for field in args.fields},
                    }
                )
        print(
            json.dumps(
                {
                    "provider": "ifind_http",
                    "mode": args.mode,
                    "access_token_ok": bool(client.access_token),
                    "trade_date": args.date,
                    "requested_fields": list(args.fields),
                    "successful_queries": sum(
                        result["error"] is None and result["rows"] > 0
                        for result in results
                    ),
                    "queries": results,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        successful_queries = sum(
            result["error"] is None and result["rows"] > 0 for result in results
        )
        return 0 if client.access_token and successful_queries > 0 else 1

    username = os.environ.get("IFIND_USERNAME") or getpass("iFinD username: ")
    password = os.environ.get("IFIND_PASSWORD") or getpass("iFinD password: ")

    import iFinDPy as ifind

    login_result = ifind.THS_iFinDLogin(username, password)
    if login_result != 0:
        get_error = getattr(ifind, "THS_GetErrorInfo", None)
        detail = get_error(login_result) if callable(get_error) else None
        print(
            json.dumps(
                {
                    "login_ok": False,
                    "login_code": login_result,
                    "login_error": _safe_message(None if detail is None else str(detail)),
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 1

    try:
        results = [
            _probe_code(ifind, str(code).upper(), tuple(args.fields), args.date)
            for code in args.codes
        ]
        print(
            json.dumps(
                {
                    "login_ok": True,
                    "trade_date": args.date,
                    "requested_fields": list(args.fields),
                    "successful_queries": sum(
                        result["error_code"] in (0, "0", None) and result["rows"] > 0
                        for result in results
                    ),
                    "queries": results,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
    finally:
        ifind.THS_iFinDLogout()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
