from __future__ import annotations

import unittest
from pathlib import Path


class WorkflowScheduleTests(unittest.TestCase):
    def test_twice_daily_beijing_schedule_updates_one_workflow(self) -> None:
        workflow = (
            Path(__file__).resolve().parents[1] / ".github" / "workflows" / "daily.yml"
        ).read_text(encoding="utf-8")

        self.assertIn('name: Twice-Daily China Commodities Data', workflow)
        self.assertIn('- cron: "0 22 * * 0-4"', workflow)
        self.assertIn('- cron: "15 10 * * 1-5"', workflow)
        self.assertEqual(workflow.count("python -m china_commodities.cli run"), 2)
        self.assertEqual(workflow.count("python scripts/collect_ifind_options.py"), 2)


if __name__ == "__main__":
    unittest.main()
