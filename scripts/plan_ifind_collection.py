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
    verified_option_chain_available,
)


EVENING_SCHEDULE = "15 10 * * 1-5"
MORNING_SCHEDULE = "0 22 * * 0-4"


def _previous_weekday(value: date) -> str:
    """Return the prior Monday-Friday date for a completed domestic EOD retry.

    The collector deliberately has no intraday/night-session product.  At
    06:00 Shanghai time the current domestic trading day is not closed, so a
    morning retry must target the previous completed weekday instead.  Exchange
    holidays still fail closed in the normal validation path; a future official
    calendar can refine this fallback without changing the workflow contract.
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
    data_dir: str | Path = "data",
) -> dict[str, Any]:
    requested_trade_date = requested_date or datetime.now(
        ZoneInfo("Asia/Shanghai")
    ).date().isoformat()
    target_date = date.fromisoformat(requested_trade_date)
    is_manual = event_name == "workflow_dispatch"
    is_scheduled = event_name == "schedule" and event_schedule in {
        MORNING_SCHEDULE,
        EVENING_SCHEDULE,
    }
    run_domestic = is_manual or is_scheduled
    run_external = is_manual or is_scheduled
    domestic_trade_date = (
        _previous_weekday(target_date)
        if event_name == "schedule" and event_schedule == MORNING_SCHEDULE
        else target_date.isoformat()
    )
    external_trade_date = target_date.isoformat()
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
        "domestic_trade_date": domestic_trade_date,
        "external_trade_date": external_trade_date,
        "domestic_date_policy": (
            "previous_completed_weekday_eod"
            if event_name == "schedule" and event_schedule == MORNING_SCHEDULE
            else "requested_date_eod"
        ),
        "run_domestic": run_domestic,
        "run_external": run_external,
        "needs_futures": needs_futures,
        "needs_options": needs_options,
        "needs_physical": needs_physical,
        "needs_external": needs_external,
        "needs_ifind": any(
            (needs_futures, needs_options, needs_physical, needs_external)
        ),
    }


def main() -> int:
    plan = plan_collection(
        event_name=os.environ.get("GITHUB_EVENT_NAME", "workflow_dispatch"),
        event_schedule=os.environ.get("GITHUB_EVENT_SCHEDULE", ""),
        requested_date=os.environ.get("TRADE_DATE") or None,
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
