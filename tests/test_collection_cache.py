from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from china_commodities.collection_cache import (
    verified_foundation_available,
    verified_futures_available,
    verified_night_session_available,
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
        {
            "trade_date": trade_date,
            "source_provider": "ifind_http",
            "record_count": 10,
            "coverage": {"publish_eligible": True, "scope_complete": True},
        },
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
    seed_verified_night(root, trade_date)


def seed_verified_night(root: Path, trade_date: str) -> None:
    write_json(
        root / "night_session" / "latest.json",
        {
            "trading_date": trade_date,
            "frequency": "night_session_snapshot",
            "intraday_used": True,
            "records": [{"exchange": "SHFE", "contract": "CU2609"}],
        },
    )
    write_json(
        root / "night_session" / "last_run_status.json",
        {
            "trading_date": trade_date,
            "data_fresh": True,
            "validation_passed": True,
            "published": True,
            "coverage": {
                "night_session_contract_count": 1,
                "unresolved_contract_count": 0,
            },
        },
    )


class CollectionCacheTests(unittest.TestCase):
    def test_same_date_verified_modules_are_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed_verified(root)
            self.assertTrue(verified_futures_available(root, "2026-08-19"))
            self.assertTrue(verified_option_chain_available(root, "2026-08-19"))
            self.assertTrue(verified_night_session_available(root, "2026-08-19"))
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

    def test_schedules_check_all_modules_with_safe_target_dates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            morning = plan_collection(
                event_name="schedule",
                event_schedule=MORNING_SCHEDULE,
                requested_date="2026-08-25",
                data_dir=root,
            )
            self.assertTrue(morning["run_domestic"])
            self.assertTrue(morning["run_external"])
            self.assertTrue(morning["run_night_session"])
            self.assertEqual(morning["domestic_trade_date"], "2026-08-24")
            self.assertEqual(morning["external_trade_date"], "2026-08-24")
            self.assertEqual(morning["night_trading_date"], "2026-08-25")
            self.assertEqual(morning["execution_profile"], "completed_eod_recovery")
            self.assertEqual(
                morning["domestic_date_policy"],
                "previous_completed_weekday_eod",
            )
            self.assertEqual(
                morning["external_date_policy"],
                "previous_completed_weekday_daily",
            )
            self.assertFalse(morning["validate_full_market"])
            self.assertTrue(morning["needs_futures"])
            self.assertTrue(morning["needs_night_session"])
            self.assertTrue(morning["needs_options"])
            self.assertTrue(morning["needs_physical"])
            self.assertTrue(morning["needs_external"])
            evening = plan_collection(
                event_name="schedule",
                event_schedule=EVENING_SCHEDULE,
                requested_date="2026-08-25",
                data_dir=root,
            )
            self.assertTrue(evening["run_domestic"])
            self.assertTrue(evening["run_external"])
            self.assertFalse(evening["run_night_session"])
            self.assertEqual(evening["domestic_trade_date"], "2026-08-25")
            self.assertEqual(evening["external_trade_date"], "2026-08-25")
            self.assertEqual(evening["execution_profile"], "current_or_historical_eod")
            self.assertEqual(evening["domestic_date_policy"], "requested_date_eod")
            self.assertEqual(evening["external_date_policy"], "requested_date_daily")
            self.assertTrue(evening["validate_full_market"])
            self.assertTrue(evening["needs_futures"])
            self.assertFalse(evening["needs_night_session"])
            self.assertTrue(evening["needs_options"])
            self.assertTrue(evening["needs_physical"])
            self.assertTrue(evening["needs_external"])

    def test_monday_morning_uses_previous_friday_for_domestic_eod(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = plan_collection(
                event_name="schedule",
                event_schedule=MORNING_SCHEDULE,
                requested_date="2026-08-24",
                data_dir=Path(directory),
            )
            self.assertEqual(plan["domestic_trade_date"], "2026-08-21")
            self.assertEqual(plan["external_trade_date"], "2026-08-21")
            self.assertEqual(plan["night_trading_date"], "2026-08-24")

    def test_morning_reuses_completed_domestic_and_external_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed_verified(root, trade_date="2026-08-24")
            seed_verified_night(root, "2026-08-25")

            plan = plan_collection(
                event_name="schedule",
                event_schedule=MORNING_SCHEDULE,
                requested_date="2026-08-25",
                data_dir=root,
            )

            self.assertFalse(plan["needs_futures"])
            self.assertFalse(plan["needs_night_session"])
            self.assertFalse(plan["needs_options"])
            self.assertFalse(plan["needs_physical"])
            self.assertFalse(plan["needs_external"])
            self.assertFalse(plan["needs_ifind"])

    def test_manual_current_day_before_eod_uses_completed_eod_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = plan_collection(
                event_name="workflow_dispatch",
                event_schedule="",
                requested_date="2026-08-27",
                collection_mode="full",
                data_dir=Path(directory),
                now=datetime(2026, 8, 27, 7, 44, tzinfo=ZoneInfo("Asia/Shanghai")),
            )

            self.assertEqual(plan["execution_profile"], "completed_eod_recovery")
            self.assertEqual(plan["domestic_trade_date"], "2026-08-26")
            self.assertEqual(plan["external_trade_date"], "2026-08-26")
            self.assertEqual(plan["night_trading_date"], "2026-08-27")
            self.assertTrue(plan["run_night_session"])
            self.assertFalse(plan["validate_full_market"])

    def test_manual_current_day_after_eod_uses_current_eod_without_night_query(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = plan_collection(
                event_name="workflow_dispatch",
                event_schedule="",
                requested_date="2026-08-27",
                collection_mode="full",
                data_dir=Path(directory),
                now=datetime(2026, 8, 27, 18, 16, tzinfo=ZoneInfo("Asia/Shanghai")),
            )

            self.assertEqual(plan["execution_profile"], "current_or_historical_eod")
            self.assertEqual(plan["domestic_trade_date"], "2026-08-27")
            self.assertEqual(plan["external_trade_date"], "2026-08-27")
            self.assertFalse(plan["run_night_session"])
            self.assertTrue(plan["validate_full_market"])

    def test_later_failed_attempt_does_not_invalidate_promoted_completed_date(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed_verified(root, trade_date="2026-08-26")
            write_json(
                root / "last_run_status.json",
                {
                    "run_date": "2026-08-27",
                    "primary_provider": "ifind",
                    "data_fresh": False,
                    "validation_errors": ["unclosed EOD"],
                },
            )
            write_json(
                root / "options" / "last_run_status.json",
                {
                    "trade_date": "2026-08-27",
                    "data_fresh": False,
                    "published": False,
                    "coverage": {"publish_eligible": False, "scope_complete": False},
                },
            )
            for domain in ("physical", "external"):
                write_json(
                    root / domain / "last_run_status.json",
                    {
                        "requested_date": "2026-08-27",
                        "validation_passed": False,
                        "published": False,
                    },
                )

            self.assertTrue(verified_futures_available(root, "2026-08-26"))
            self.assertTrue(verified_option_chain_available(root, "2026-08-26"))
            self.assertTrue(
                verified_foundation_available(root, "physical", "2026-08-26")
            )
            self.assertTrue(
                verified_foundation_available(root, "external", "2026-08-26")
            )

    def test_manual_night_only_does_not_schedule_unclosed_daytime_eod(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = plan_collection(
                event_name="workflow_dispatch",
                event_schedule="",
                requested_date="2026-08-26",
                collection_mode="night_session_only",
                data_dir=root,
            )
            self.assertTrue(plan["run_night_session"])
            self.assertEqual(plan["execution_profile"], "night_session_only")
            self.assertTrue(plan["needs_night_session"])
            self.assertFalse(plan["run_domestic"])
            self.assertFalse(plan["run_external"])
            self.assertFalse(plan["needs_futures"])
            self.assertFalse(plan["needs_options"])
            self.assertFalse(plan["needs_physical"])
            self.assertFalse(plan["needs_external"])
            self.assertFalse(plan["validate_full_market"])


if __name__ == "__main__":
    unittest.main()
