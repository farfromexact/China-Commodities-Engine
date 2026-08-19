"""Range-efficient, quality-gated iFinD history backfill."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
import time
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from .catalog import load_catalog
from .collectors.akshare_adapter import COMMODITY_EXCHANGES
from .collectors.ifind_http_adapter import (
    IFindHTTPClient,
    IFindHTTPError,
    collect_futures_universe_history,
)
from .normalize import iso_date
from .pipeline import run_pipeline
from .storage import (
    DEFAULT_HISTORY_LIMIT,
    DEFAULT_SNAPSHOT_LIMIT,
    publish_status,
    publish_verified,
    read_json,
)


@dataclass(frozen=True)
class BackfillSummary:
    start_date: str
    end_date: str
    requested_days: int
    published_days: int
    published_dates: tuple[str, ...]
    contracts_by_date: dict[str, int]
    history_limit: int
    snapshot_limit: int
    collection_mode: str
    skipped_weekdays: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "start_date": self.start_date,
            "end_date": self.end_date,
            "requested_days": self.requested_days,
            "published_days": self.published_days,
            "published_dates": list(self.published_dates),
            "contracts_by_date": self.contracts_by_date,
            "history_limit": self.history_limit,
            "snapshot_limit": self.snapshot_limit,
            "collection_mode": self.collection_mode,
            "skipped_weekdays": list(self.skipped_weekdays),
        }


def _source_dates(frame: pd.DataFrame) -> set[str]:
    if frame.empty or "date" not in frame.columns:
        return set()
    values = pd.to_datetime(frame["date"], format="%Y%m%d", errors="coerce")
    return {
        value.date().isoformat()
        for value in values.dropna().tolist()
    }


def run_ifind_backfill(
    *,
    end_date: str,
    days: int = DEFAULT_SNAPSHOT_LIMIT,
    data_dir: str | Path = "data",
    catalog_path: str | Path | None = None,
    history_limit: int = DEFAULT_HISTORY_LIMIT,
    snapshot_limit: int = DEFAULT_SNAPSHOT_LIMIT,
    calendar_days: int | None = None,
    client: IFindHTTPClient | None = None,
    request_interval_seconds: float = 0.55,
    daily_attempts: int = 3,
    daily_retry_seconds: float = 2.0,
) -> BackfillSummary:
    """Backfill the latest verified common trading days across all five exchanges.

    The Quant API is queried by date range, then daily pipeline validation is
    applied in memory. Nothing is published unless every selected day passes.
    """

    normalized_end = iso_date(end_date)
    if days < 1:
        raise ValueError("backfill days must be positive")
    if snapshot_limit < 1:
        raise ValueError("snapshot limit must be positive")
    if daily_attempts < 1 or daily_retry_seconds < 0:
        raise ValueError("daily retry settings are invalid")

    target = Path(data_dir)
    existing_latest = read_json(target / "latest.json", default={})
    existing_date = str(existing_latest.get("trade_date") or "")
    if existing_date and existing_date > normalized_end:
        raise ValueError(
            "backfill end date cannot be older than the current latest snapshot"
        )

    end_value = date.fromisoformat(normalized_end)
    span = calendar_days or max(90, (days * 7 + 4) // 5 + 35)
    if span < days:
        raise ValueError("calendar-day window cannot be smaller than backfill days")
    start_value = end_value - timedelta(days=span)
    normalized_start = start_value.isoformat()

    catalog = load_catalog(catalog_path)
    http_client = client or IFindHTTPClient(timeout=90)
    generated_at = datetime.now(ZoneInfo("Asia/Shanghai"))
    results = []
    failures: list[str] = []
    skipped_weekdays: list[str] = []
    collection_mode = "range"
    try:
        history_by_exchange: dict[str, pd.DataFrame] = {}
        dates_by_exchange: dict[str, set[str]] = {}
        for exchange in COMMODITY_EXCHANGES:
            frame = collect_futures_universe_history(
                normalized_start,
                normalized_end,
                exchange,
                catalog.products_for_exchange(exchange),
                client=http_client,
                request_interval_seconds=request_interval_seconds,
            )
            history_by_exchange[exchange] = frame
            dates_by_exchange[exchange] = _source_dates(frame)

        common_dates = set.intersection(*dates_by_exchange.values())
        eligible_dates = sorted(
            trade_date
            for trade_date in common_dates
            if normalized_start <= trade_date <= normalized_end
        )
        if len(eligible_dates) < days:
            counts = ", ".join(
                f"{exchange}={len(dates_by_exchange[exchange])}"
                for exchange in COMMODITY_EXCHANGES
            )
            raise RuntimeError(
                f"iFinD returned only {len(eligible_dates)} common trading days; "
                f"requested {days} ({counts})"
            )
        selected_dates = eligible_dates[-days:]
        for trade_date in selected_dates:
            result = run_pipeline(
                trade_date,
                data_dir=target,
                include_options=False,
                publish=False,
                provider="ifind",
                include_official_auxiliary=False,
                ifind_prefetched=history_by_exchange,
                now=generated_at,
            )
            if result.verified:
                results.append(result)
            else:
                failures.append(
                    f"{trade_date}: " + "; ".join(result.validation_errors[:5])
                )
    except IFindHTTPError as exc:
        if "code -4210" not in str(exc):
            raise
        collection_mode = "daily_fallback"
        cursor = end_value
        while cursor >= start_value and len(results) < days:
            if cursor.weekday() < 5:
                trade_date = cursor.isoformat()
                result = None
                for attempt in range(1, daily_attempts + 1):
                    result = run_pipeline(
                        trade_date,
                        data_dir=target,
                        include_options=False,
                        publish=False,
                        provider="ifind",
                        include_official_auxiliary=False,
                        ifind_http_client=http_client,
                        ifind_request_interval_seconds=request_interval_seconds,
                        now=generated_at,
                    )
                    if result.verified:
                        break
                    if attempt < daily_attempts:
                        print(
                            f"retrying unverified weekday {trade_date} "
                            f"({attempt}/{daily_attempts})",
                            flush=True,
                        )
                        time.sleep(daily_retry_seconds)
                assert result is not None
                if result.verified:
                    results.append(result)
                    print(
                        f"verified {trade_date}: {len(result.futures_records)} contracts "
                        f"({len(results)}/{days})",
                        flush=True,
                    )
                else:
                    skipped_weekdays.append(trade_date)
                    print(f"skipped unverified weekday {trade_date}", flush=True)
            cursor -= timedelta(days=1)
        if len(results) < days:
            raise RuntimeError(
                f"iFinD daily fallback verified only {len(results)} trading days; "
                f"requested {days}"
            )
        results.sort(key=lambda result: result.trade_date)

    if failures:
        raise RuntimeError(
            "iFinD backfill validation failed; nothing published: "
            + " | ".join(failures[:5])
        )

    for result in results:
        publish_verified(
            result,
            target,
            history_limit=history_limit,
            snapshot_limit=snapshot_limit,
        )
    latest = results[-1]
    publish_verified(
        latest,
        target,
        history_limit=history_limit,
        snapshot_limit=snapshot_limit,
    )
    publish_status(latest, target)

    return BackfillSummary(
        start_date=results[0].trade_date,
        end_date=results[-1].trade_date,
        requested_days=days,
        published_days=len(results),
        published_dates=tuple(result.trade_date for result in results),
        contracts_by_date={
            result.trade_date: len(result.futures_records) for result in results
        },
        history_limit=history_limit,
        snapshot_limit=snapshot_limit,
        collection_mode=collection_mode,
        skipped_weekdays=tuple(sorted(skipped_weekdays)),
    )


__all__ = ["BackfillSummary", "run_ifind_backfill"]
