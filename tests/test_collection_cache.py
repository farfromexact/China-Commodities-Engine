from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from china_commodities.collection_cache import (
    verified_foundation_available,
    verified_futures_available,
    verified_option_chain_available,
)
from scripts.plan_ifind_collection import (
    EVENING_SCHEDULE,
    MORNING_SCHEDULE,
    plan_collection,
)


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def seed_verified(root: Path, trade_date: str = "2026-08-19") -> None:
    write_json(
        root / "latest.json",
        {
            "trade_date": trade_date,
            "verified": True,
            "futures_contracts": [{"contract": "RB2610"}],
        },
    )
    write_json(
        root / "last_run_status.json",
        {
            "run_date": trade_date,
            "primary_provider": "ifind",
            "data_fresh": True,
            "validation_errors": [],
        },
    )
    option_root = root / "options"
    write_json(
        option_root / "latest.json",
        {"trade_date": trade_date, "record_count": 10},
    )
    write_json(
        option_root / "last_run_status.json",
        {
            "trade_date": trade_date,
            "source_provider": "ifind_http",
            "data_fresh": True,
            "published": True,
            "global_error": None,
            "quote_contract_count": 10,
            "coverage": {"publish_eligible": True, "scope_complete": True},
        },
    )
    write_json(
        option_root / "quality_latest.json",
        {
            "trade_date": trade_date,
            "quality": {
                "full_chain_verified": True,
                "full_product_scope_verified": True,
            },
        },
    )
    for domain in ("physical", "external"):
        write_json(
            root / domain / "latest.json",
            {"requested_date": trade_date, "series": [{"series_key": domain}]},
        )
        write_json(
            root / domain / "last_run_status.json",
            {
                "requested_date": trade_date,
                "validation_passed": True,
                "published": True,
            },
        )


class CollectionCacheTests(unittest.TestCase):
    def test_same_date_verified_modules_are_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed_verified(root)
            self.assertTrue(verified_futures_available(root, "2026-08-19"))
            self.assertTrue(verified_option_chain_available(root, "2026-08-19"))
            self.assertTrue(
                verified_foundation_available(root, "physical", "2026-08-19")
            )
            self.assertFalse(verified_futures_available(root, "2026-08-20"))

    def test_manual_same_date_plan_makes_no_ifind_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed_verified(root)
            plan = plan_collection(
                event_name="workflow_dispatch",
                event_schedule="",
                requested_date="2026-08-19",
                data_dir=root,
            )
            self.assertFalse(plan["needs_ifind"])
            self.assertFalse(any(plan[key] for key in plan if key.startswith("needs_")))

    def test_partial_option_scope_is_scheduled_for_incremental_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed_verified(root)
            option_root = root / "options"
            status = json.loads(
                (option_root / "last_run_status.json").read_text(encoding="utf-8")
            )
            status["coverage"]["scope_complete"] = False
            write_json(option_root / "last_run_status.json", status)
            quality = json.loads(
                (option_root / "quality_latest.json").read_text(encoding="utf-8")
            )
            quality["quality"]["full_product_scope_verified"] = False
            write_json(option_root / "quality_latest.json", quality)

            self.assertFalse(verified_option_chain_available(root, "2026-08-19"))
            plan = plan_collection(
                event_name="workflow_dispatch",
                event_schedule="",
                requested_date="2026-08-19",
                data_dir=root,
            )
            self.assertTrue(plan["needs_options"])
            self.assertTrue(plan["needs_ifind"])

    def test_schedules_request_only_their_missing_market_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            morning = plan_collection(
                event_name="schedule",
                event_schedule=MORNING_SCHEDULE,
                requested_date="2026-08-20",
                data_dir=root,
            )
            self.assertTrue(morning["needs_external"])
            self.assertFalse(morning["needs_futures"])
            evening = plan_collection(
                event_name="schedule",
                event_schedule=EVENING_SCHEDULE,
                requested_date="2026-08-20",
                data_dir=root,
            )
            self.assertTrue(evening["needs_futures"])
            self.assertTrue(evening["needs_options"])
            self.assertTrue(evening["needs_physical"])
            self.assertFalse(evening["needs_external"])


if __name__ == "__main__":
    unittest.main()
