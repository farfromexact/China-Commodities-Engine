"""Collect an auditable end-of-night futures snapshot.

The daily futures artifact is deliberately EOD-only.  This module is a
separate, small session layer: it asks iFinD for the latest quote before the
day session opens and accepts a row only when the vendor timestamp falls in
the completed overnight window.  It therefore never relabels a daytime quote
as night-session data and never modifies ``data/latest.json``.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
import math
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

from .collectors.ifind_adapter import contract_to_ifind_code
from .collectors.ifind_http_adapter import IFindHTTPClient, IFindHTTPError
from .history_storage import append_parquet_history
from .storage import read_json, write_json_if_changed


NIGHT_SESSION_FIELDS: tuple[str, ...] = (
    "open",
    "high",
    "low",
    "latest",
    "settlement",
    "preSettlement",
    "volume",
    "amount",
    "openInterest",
)
NIGHT_SESSION_BATCH_SIZE = 100
NIGHT_SESSION_HISTORY_DAYS = 252
NIGHT_SESSION_TIMEZONE = "Asia/Shanghai"
_NIGHT_WINDOW_START = time(20, 0)
_NIGHT_WINDOW_END = time(3, 45)
_RESOLVED_STATES = frozenset({"night_session", "outside_night_window"})


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: Any) -> float | None:
    converted = pd.to_numeric(value, errors="coerce")
    if pd.isna(converted):
        return None
    result = float(converted)
    return result if math.isfinite(result) else None


def _timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    if not isinstance(parsed, pd.Timestamp):
        return None
    zone = ZoneInfo(NIGHT_SESSION_TIMEZONE)
    if parsed.tzinfo is None:
        parsed = parsed.tz_localize(zone)
    else:
        parsed = parsed.tz_convert(zone)
    return parsed.to_pydatetime()


def _night_window(trading_date: str) -> tuple[str, datetime, datetime]:
    target = date.fromisoformat(trading_date)
    session_date = target - timedelta(days=1)
    zone = ZoneInfo(NIGHT_SESSION_TIMEZONE)
    start = datetime.combine(session_date, _NIGHT_WINDOW_START, tzinfo=zone)
    end = datetime.combine(target, _NIGHT_WINDOW_END, tzinfo=zone)
    return session_date.isoformat(), start, end


def _is_night_timestamp(
    timestamp: datetime | None, start: datetime, end: datetime
) -> bool:
    return timestamp is not None and start <= timestamp <= end


def _ifind_code(record: Mapping[str, Any]) -> str | None:
    contract = str(record.get("contract") or "").upper().strip()
    exchange = str(record.get("exchange") or "").upper().strip()
    if not contract or not exchange:
        return None
    try:
        return contract_to_ifind_code(contract, exchange)
    except ValueError:
        return None


def _universe_from_daily_snapshot(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    universe: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in snapshot.get("futures_contracts") or []:
        if not isinstance(raw, Mapping):
            continue
        exchange = str(raw.get("exchange") or "").upper().strip()
        contract = str(raw.get("contract") or "").upper().strip()
        code = _ifind_code(raw)
        key = (exchange, contract)
        if not code or key in seen:
            continue
        seen.add(key)
        universe.append(
            {
                "exchange": exchange,
                "product": str(raw.get("product") or "").upper().strip(),
                "contract": contract,
                "source_code": code,
                "prior_eod_trade_date": str(raw.get("trade_date") or snapshot.get("trade_date") or ""),
                "prior_eod_settlement": _number(raw.get("settle")),
                "prior_eod_close": _number(raw.get("close")),
            }
        )
    return sorted(
        universe,
        key=lambda item: (item["exchange"], item["product"], item["contract"]),
    )


def _quote_frames(
    codes: Sequence[str], *, client: IFindHTTPClient, batch_size: int
) -> tuple[pd.DataFrame, dict[str, str]]:
    """Fetch quotes in isolated batches so one entitlement failure is local."""

    failures: dict[str, str] = {}
    frames: list[pd.DataFrame] = []

    def query(batch: list[str]) -> None:
        try:
            frame = client.realtime_quotes(batch, NIGHT_SESSION_FIELDS)
        except IFindHTTPError as exc:
            if len(batch) == 1:
                failures[batch[0].upper()] = str(exc)[:500]
                return
            midpoint = len(batch) // 2
            query(batch[:midpoint])
            query(batch[midpoint:])
            return
        if not frame.empty:
            frames.append(frame)

    for offset in range(0, len(codes), batch_size):
        query(list(codes[offset : offset + batch_size]))
    if not frames:
        return pd.DataFrame(), failures
    return pd.concat(frames, ignore_index=True), failures


def _quote_map(frame: pd.DataFrame) -> dict[str, Mapping[str, Any]]:
    if frame.empty or "thscode" not in frame.columns:
        return {}
    output: dict[str, Mapping[str, Any]] = {}
    for row in frame.to_dict(orient="records"):
        code = str(row.get("thscode") or "").upper().strip()
        if code:
            output[code] = row
    return output


def _cache_records(
    data_dir: Path, trading_date: str
) -> dict[tuple[str, str], dict[str, Any]]:
    selected: dict[tuple[str, str], dict[str, Any]] = {}
    for relative in (
        "night_session/latest.json",
        "night_session/attempt_latest.json",
    ):
        payload = _mapping(read_json(data_dir / relative, default={}))
        if str(payload.get("trading_date") or "") != trading_date:
            continue
        for raw in payload.get("records") or []:
            if not isinstance(raw, Mapping):
                continue
            key = (
                str(raw.get("exchange") or "").upper(),
                str(raw.get("contract") or "").upper(),
            )
            if not all(key):
                continue
            current = selected.get(key)
            candidate = dict(raw)
            if current is None or candidate.get("record_state") in _RESOLVED_STATES:
                selected[key] = candidate
    return selected


def _record_from_quote(
    context: Mapping[str, Any],
    quote: Mapping[str, Any] | None,
    *,
    trading_date: str,
    session_date: str,
    window_start: datetime,
    window_end: datetime,
    error: str | None = None,
) -> dict[str, Any]:
    base = {
        "trading_date": trading_date,
        "night_session_date": session_date,
        "timezone": NIGHT_SESSION_TIMEZONE,
        "frequency": "night_session_snapshot",
        "session_window_start": window_start.isoformat(),
        "session_window_end": window_end.isoformat(),
        "exchange": context["exchange"],
        "product": context["product"],
        "contract": context["contract"],
        "source_code": context["source_code"],
        "source_provider": "ifind_http",
        "source_endpoint": "real_time_quotation",
        "prior_eod_trade_date": context.get("prior_eod_trade_date") or None,
        "prior_eod_settlement": context.get("prior_eod_settlement"),
        "prior_eod_close": context.get("prior_eod_close"),
        "source_timestamp": None,
        "open": None,
        "high": None,
        "low": None,
        "night_close": None,
        "settlement": None,
        "pre_settlement": None,
        "volume": None,
        "turnover": None,
        "open_interest": None,
        "night_return_pct": None,
        "record_state": None,
        "quality_state": None,
        "missing_reason": None,
        "cache_hit": False,
    }
    if error is not None:
        base.update(
            {
                "record_state": "query_error",
                "quality_state": "unavailable",
                "missing_reason": error,
            }
        )
        return base
    if quote is None:
        base.update(
            {
                "record_state": "missing_quote",
                "quality_state": "unavailable",
                "missing_reason": "iFinD returned no realtime row for the concrete contract",
            }
        )
        return base

    source_timestamp = _timestamp(quote.get("time"))
    base.update(
        {
            "source_timestamp": source_timestamp.isoformat()
            if source_timestamp is not None
            else None,
            "open": _number(quote.get("open")),
            "high": _number(quote.get("high")),
            "low": _number(quote.get("low")),
            "night_close": _number(quote.get("latest")),
            "settlement": _number(quote.get("settlement")),
            "pre_settlement": _number(quote.get("preSettlement")),
            "volume": _number(quote.get("volume")),
            "turnover": _number(quote.get("amount")),
            "open_interest": _number(quote.get("openInterest")),
        }
    )
    if source_timestamp is None:
        base.update(
            {
                "record_state": "missing_timestamp",
                "quality_state": "invalid",
                "missing_reason": "iFinD realtime quote did not include a parseable timestamp",
            }
        )
        return base
    if not _is_night_timestamp(source_timestamp, window_start, window_end):
        base.update(
            {
                "record_state": "outside_night_window",
                "quality_state": "not_applicable",
                "missing_reason": "latest iFinD quote timestamp is outside the completed night-session window",
            }
        )
        return base
    if base["night_close"] is None or base["night_close"] <= 0:
        base.update(
            {
                "record_state": "missing_price",
                "quality_state": "invalid",
                "missing_reason": "night-session quote has no positive latest price",
            }
        )
        return base
    denominator = base["pre_settlement"] or base["prior_eod_settlement"]
    if denominator is not None and denominator > 0:
        base["night_return_pct"] = (base["night_close"] / denominator - 1.0) * 100.0
    base.update({"record_state": "night_session", "quality_state": "fresh"})
    return base


def _coverage(records: Sequence[Mapping[str, Any]], request_count: int) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for record in records:
        state = str(record.get("record_state") or "unknown")
        counts[state] = counts.get(state, 0) + 1
    night_count = counts.get("night_session", 0)
    unresolved = sum(
        counts.get(state, 0)
        for state in ("query_error", "missing_quote", "missing_timestamp", "missing_price")
    )
    return {
        "selected_contract_count": len(records),
        "request_contract_count": request_count,
        "cache_hit_count": sum(bool(record.get("cache_hit")) for record in records),
        "night_session_contract_count": night_count,
        "outside_night_window_count": counts.get("outside_night_window", 0),
        "missing_timestamp_count": counts.get("missing_timestamp", 0),
        "missing_price_count": counts.get("missing_price", 0),
        "missing_quote_count": counts.get("missing_quote", 0),
        "query_error_count": counts.get("query_error", 0),
        "unresolved_contract_count": unresolved,
        "night_session_coverage_pct": round(
            night_count / len(records) * 100.0, 4
        )
        if records
        else 0.0,
    }


def collect_night_session(
    trading_date: str,
    *,
    data_dir: str | Path = "data",
    client: IFindHTTPClient | None = None,
    publish: bool = True,
    force_refresh: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Capture the completed night session preceding ``trading_date``.

    A live quote is accepted only when its own vendor timestamp is within the
    overnight window.  Rows outside that window are explicitly retained as
    ``not_applicable`` rather than silently counted as fresh night data.
    """

    target = date.fromisoformat(trading_date).isoformat()
    session_date, window_start, window_end = _night_window(target)
    root = Path(data_dir)
    night_root = root / "night_session"
    prior_latest = _mapping(read_json(night_root / "latest.json", default={}))
    daily_snapshot = _mapping(read_json(root / "latest.json", default={}))
    universe = _universe_from_daily_snapshot(daily_snapshot)
    generated_at = (now or datetime.now(ZoneInfo(NIGHT_SESSION_TIMEZONE))).isoformat()

    if not universe:
        status = {
            "schema_version": 1,
            "trading_date": target,
            "night_session_date": session_date,
            "generated_at": generated_at,
            "data_fresh": False,
            "validation_passed": False,
            "published": False,
            "coverage": _coverage([], 0),
            "global_error": "no concrete futures universe is available from data/latest.json",
            "previous_valid_snapshot_retained": bool(prior_latest),
        }
        snapshot = {
            "schema_version": 1,
            "trading_date": target,
            "night_session_date": session_date,
            "generated_at": generated_at,
            "timezone": NIGHT_SESSION_TIMEZONE,
            "frequency": "night_session_snapshot",
            "records": [],
            "coverage": status["coverage"],
        }
        if publish:
            write_json_if_changed(night_root / "attempt_latest.json", snapshot)
            write_json_if_changed(night_root / "last_run_status.json", status)
        return {"snapshot": snapshot, "status": status}

    cached = _cache_records(root, target) if not force_refresh else {}
    request_contexts = [
        context
        for context in universe
        if (context["exchange"], context["contract"]) not in cached
        or cached[(context["exchange"], context["contract"])].get("record_state")
        not in _RESOLVED_STATES
    ]
    quote_frame = pd.DataFrame()
    failures: dict[str, str] = {}
    if request_contexts:
        quote_frame, failures = _quote_frames(
            [str(context["source_code"]) for context in request_contexts],
            client=client or IFindHTTPClient(minimum_request_interval_seconds=0.25),
            batch_size=NIGHT_SESSION_BATCH_SIZE,
        )
    quotes = _quote_map(quote_frame)
    records: list[dict[str, Any]] = []
    for context in universe:
        key = (context["exchange"], context["contract"])
        prior = cached.get(key)
        if prior is not None and prior.get("record_state") in _RESOLVED_STATES:
            record = dict(prior)
            record["cache_hit"] = True
            records.append(record)
            continue
        code = str(context["source_code"]).upper()
        records.append(
            _record_from_quote(
                context,
                quotes.get(code),
                trading_date=target,
                session_date=session_date,
                window_start=window_start,
                window_end=window_end,
                error=failures.get(code),
            )
        )

    coverage = _coverage(records, len(request_contexts))
    data_fresh = bool(
        coverage["night_session_contract_count"]
        and coverage["unresolved_contract_count"] == 0
    )
    validation_errors: list[str] = []
    if not coverage["night_session_contract_count"]:
        validation_errors.append("no concrete contract has a quote in the completed night-session window")
    if coverage["unresolved_contract_count"]:
        validation_errors.append(
            f"{coverage['unresolved_contract_count']} concrete contracts are unresolved"
        )
    snapshot = {
        "schema_version": 1,
        "trading_date": target,
        "night_session_date": session_date,
        "generated_at": generated_at,
        "timezone": NIGHT_SESSION_TIMEZONE,
        "frequency": "night_session_snapshot",
        "intraday_used": True,
        "session_window_start": window_start.isoformat(),
        "session_window_end": window_end.isoformat(),
        "reference_eod_trade_date": daily_snapshot.get("trade_date"),
        "source": {"provider": "ifind_http", "endpoint": "real_time_quotation"},
        "coverage": coverage,
        "records": records,
        "limitations": {
            "is_separate_from_daily_eod": True,
            "is_not_minute_or_tick_history": True,
            "requires_vendor_timestamp_in_completed_session_window": True,
        },
    }
    published = bool(coverage["night_session_contract_count"])
    status = {
        "schema_version": 1,
        "trading_date": target,
        "night_session_date": session_date,
        "generated_at": generated_at,
        "data_fresh": data_fresh,
        "validation_passed": not validation_errors,
        "published": published,
        "coverage": coverage,
        "validation_errors": validation_errors,
        "previous_valid_snapshot_retained": bool(prior_latest and not published),
    }
    if publish:
        write_json_if_changed(night_root / "attempt_latest.json", snapshot)
        if published:
            write_json_if_changed(night_root / "latest.json", snapshot)
            fresh_records = [
                record for record in records if record.get("record_state") == "night_session"
            ]
            append_parquet_history(
                night_root / "history.parquet",
                fresh_records,
                key_fields=("trading_date", "exchange", "contract"),
                sort_fields=("trading_date", "exchange", "product", "contract"),
                retention_field="trading_date",
                retention_days=NIGHT_SESSION_HISTORY_DAYS,
            )
        write_json_if_changed(night_root / "last_run_status.json", status)
    return {"snapshot": snapshot, "status": status}


__all__ = [
    "NIGHT_SESSION_BATCH_SIZE",
    "NIGHT_SESSION_FIELDS",
    "NIGHT_SESSION_HISTORY_DAYS",
    "collect_night_session",
]
