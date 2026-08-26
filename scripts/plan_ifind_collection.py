"""Plan GitHub Actions collection without making any vendor request."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from china_commodities.collection_cache import (
    verified_foundation_available,
    verified_futures_available,
    verified_night_session_available,
    verified_option_chain_available,
)


EVENING_SCHEDULE = "15 10 * * 1-5"
MORNING_SCHEDULE = "0 22 * * 0-4"


def _previous_weekday(value: date) -> str:
    """Return the prior Monday-Friday date for a completed domestic EOD retry.

    At 06:00 Shanghai time the current domestic daytime EOD is not closed, so
    the daily retry must target the previous completed weekday.  Night-session
    collection has its own current-trading-date key and is not derived here.
    Exchange holidays still fail closed in the normal validation path; a future
    official calendar can refine this fallback without changing the workflow
    contract.
    """

    cursor = value - timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor -= timedelta(days=1)
    return cursor.isoformat()


def plan_collection(
    *,
    event_name: str,
    event_schedule: str,
    requested_date: str | None,
    collection_mode: str = "full",
    data_dir: str | Path = "data",
) -> dict[str, Any]:
    requested_trade_date = requested_date or datetime.now(
        ZoneInfo("Asia/Shanghai")
    ).date().isoformat()
    target_date = date.fromisoformat(requested_trade_date)
    mode = str(collection_mode or "full").strip().lower()
    if mode not in {"full", "night_session_only"}:
        raise ValueError("collection_mode must be full or night_session_only")
    is_manual = event_name == "workflow_dispatch"
    is_scheduled = event_name == "schedule" and event_schedule in {
        MORNING_SCHEDULE,
        EVENING_SCHEDULE,
    }
    run_night_session = bool(
        (
            is_manual
            or (is_scheduled and event_schedule == MORNING_SCHEDULE)
        )
        and mode in {"full", "night_session_only"}
    )
    run_domestic = bool((is_manual or is_scheduled) and mode == "full")
    run_external = bool((is_manual or is_scheduled) and mode == "full")
    domestic_trade_date = (
        _previous_weekday(target_date)
        if event_name == "schedule" and event_schedule == MORNING_SCHEDULE
        else target_date.isoformat()
    )
    external_trade_date = target_date.isoformat()
    night_trading_date = target_date.isoformat()
    needs_night_session = bool(
        run_night_session
        and not verified_night_session_available(data_dir, night_trading_date)
    )
    needs_futures = bool(
        run_domestic
        and not verified_futures_available(data_dir, domestic_trade_date)
    )
    needs_options = bool(
        run_domestic
        and not verified_option_chain_available(data_dir, domestic_trade_date)
    )
    needs_physical = bool(
        run_domestic
        and not verified_foundation_available(
            data_dir, "physical", domestic_trade_date
        )
    )
    needs_external = bool(
        run_external
        and not verified_foundation_available(
            data_dir, "external", external_trade_date
        )
    )
    return {
        "requested_date": target_date.isoformat(),
        "collection_mode": mode,
        "domestic_trade_date": domestic_trade_date,
        "external_trade_date": external_trade_date,
        "night_trading_date": night_trading_date,
        "domestic_date_policy": (
            "previous_completed_weekday_eod"
            if event_name == "schedule" and event_schedule == MORNING_SCHEDULE
            else "requested_date_eod"
        ),
        "run_domestic": run_domestic,
        "run_external": run_external,
        "run_night_session": run_night_session,
        "needs_night_session": needs_night_session,
        "needs_futures": needs_futures,
        "needs_options": needs_options,
        "needs_physical": needs_physical,
        "needs_external": needs_external,
        "needs_ifind": any(
            (
                needs_night_session,
                needs_futures,
                needs_options,
                needs_physical,
                needs_external,
            )
        ),
    }


def main() -> int:
    plan = plan_collection(
        event_name=os.environ.get("GITHUB_EVENT_NAME", "workflow_dispatch"),
        event_schedule=os.environ.get("GITHUB_EVENT_SCHEDULE", ""),
        requested_date=os.environ.get("TRADE_DATE") or None,
        collection_mode=os.environ.get("COLLECTION_MODE", "full"),
        data_dir=os.environ.get("DATA_DIR", "data"),
    )
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with Path(output_path).open("a", encoding="utf-8") as handle:
            for key, value in plan.items():
                rendered = str(value).lower() if isinstance(value, bool) else str(value)
                handle.write(f"{key}={rendered}\n")
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
