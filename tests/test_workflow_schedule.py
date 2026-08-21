from __future__ import annotations

import unittest
from pathlib import Path


class WorkflowScheduleTests(unittest.TestCase):
    def test_split_eod_schedule_updates_only_completed_markets(self) -> None:
        workflow = (
            Path(__file__).resolve().parents[1] / ".github" / "workflows" / "daily.yml"
        ).read_text(encoding="utf-8")

        self.assertIn('name: Split EOD China Commodities Data', workflow)
        self.assertIn('- cron: "0 22 * * 0-4"', workflow)
        self.assertIn('- cron: "15 10 * * 1-5"', workflow)
        self.assertEqual(workflow.count("python -m china_commodities.cli run"), 2)
        self.assertEqual(workflow.count("python scripts/collect_ifind_options.py"), 2)
        self.assertEqual(workflow.count("--surface-shadow-days 1"), 2)
        self.assertIn("python scripts/plan_ifind_collection.py", workflow)
        self.assertIn("steps.collection_plan.outputs.needs_ifind == 'true'", workflow)
        self.assertIn("steps.collection_plan.outputs.needs_futures == 'true'", workflow)
        self.assertIn("steps.collection_plan.outputs.needs_options == 'true'", workflow)
        self.assertIn("steps.collection_plan.outputs.needs_physical == 'true'", workflow)
        self.assertIn("steps.collection_plan.outputs.needs_external == 'true'", workflow)
        self.assertIn(
            "python -m china_commodities.cli report-input --repair-futures-history",
            workflow,
        )
        self.assertEqual(
            workflow.count("python -m china_commodities.cli foundation --scope physical"),
            2,
        )
        self.assertEqual(
            workflow.count("python -m china_commodities.cli foundation --scope external"),
            2,
        )


if __name__ == "__main__":
    unittest.main()
