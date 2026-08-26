from __future__ import annotations

import unittest
from pathlib import Path


class WorkflowScheduleTests(unittest.TestCase):
    def test_twice_daily_schedule_has_distinct_night_and_daytime_roles(self) -> None:
        workflow = (
            Path(__file__).resolve().parents[1] / ".github" / "workflows" / "daily.yml"
        ).read_text(encoding="utf-8")

        self.assertIn('name: Split EOD China Commodities Data', workflow)
        self.assertIn('- cron: "0 22 * * 0-4"', workflow)
        self.assertIn('- cron: "15 10 * * 1-5"', workflow)
        self.assertEqual(workflow.count("python -m china_commodities.cli run"), 1)
        self.assertEqual(workflow.count("python scripts/collect_ifind_options.py"), 1)
        self.assertEqual(workflow.count("--surface-shadow-days 1"), 1)
        self.assertIn("python scripts/plan_ifind_collection.py", workflow)
        self.assertIn("steps.collection_plan.outputs.needs_ifind == 'true'", workflow)
        self.assertIn("steps.collection_plan.outputs.needs_night_session == 'true'", workflow)
        self.assertIn("steps.collection_plan.outputs.needs_futures == 'true'", workflow)
        self.assertIn("steps.collection_plan.outputs.needs_options == 'true'", workflow)
        self.assertIn("steps.collection_plan.outputs.needs_physical == 'true'", workflow)
        self.assertIn("steps.collection_plan.outputs.needs_external == 'true'", workflow)
        self.assertIn(
            "python -m china_commodities.cli report-input --repair-futures-history",
            workflow,
        )
        self.assertEqual(
            workflow.count(
                'python -m china_commodities.cli run --date "${DOMESTIC_TRADE_DATE}"'
            ),
            1,
        )
        self.assertEqual(
            workflow.count(
                'python -m china_commodities.cli night-session --trade-date "${NIGHT_TRADING_DATE}"'
            ),
            1,
        )
        self.assertEqual(
            workflow.count(
                'python scripts/collect_ifind_options.py --all-products --date "${DOMESTIC_TRADE_DATE}"'
            ),
            1,
        )
        self.assertEqual(
            workflow.count(
                'python -m china_commodities.cli foundation --scope physical --date "${DOMESTIC_TRADE_DATE}"'
            ),
            1,
        )
        self.assertEqual(
            workflow.count(
                'python -m china_commodities.cli foundation --scope external --date "${EXTERNAL_TRADE_DATE}"'
            ),
            1,
        )
        self.assertEqual(
            workflow.count(
                "DOMESTIC_TRADE_DATE: ${{ steps.collection_plan.outputs.domestic_trade_date }}"
            ),
            3,
        )
        self.assertEqual(
            workflow.count(
                "EXTERNAL_TRADE_DATE: ${{ steps.collection_plan.outputs.external_trade_date }}"
            ),
            1,
        )
        self.assertEqual(
            workflow.count(
                "NIGHT_TRADING_DATE: ${{ steps.collection_plan.outputs.night_trading_date }}"
            ),
            1,
        )
        self.assertIn("night_session_only", workflow)


if __name__ == "__main__":
    unittest.main()
