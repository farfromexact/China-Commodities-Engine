from __future__ import annotations

import unittest

from china_commodities.promotion import update_shadow_state


class PromotionTests(unittest.TestCase):
    def test_requires_five_unique_valid_dates_and_deduplicates_same_day(self) -> None:
        state = None
        for day in (17, 18, 18, 19, 20):
            state = update_shadow_state(
                state,
                requested_date=f"2026-08-{day:02d}",
                validation_passed=True,
            )
        self.assertEqual(state["consecutive_pass_count"], 4)
        self.assertFalse(state["activated"])
        state = update_shadow_state(
            state,
            requested_date="2026-08-21",
            validation_passed=True,
        )
        self.assertTrue(state["activated"])
        self.assertTrue(state["promotion_allowed"])

    def test_failed_new_date_resets_shadow_but_never_demotes_activation(self) -> None:
        state = None
        for day in range(1, 6):
            state = update_shadow_state(
                state,
                requested_date=f"2026-08-{day:02d}",
                validation_passed=True,
            )
        state = update_shadow_state(
            state,
            requested_date="2026-08-06",
            validation_passed=False,
        )
        self.assertTrue(state["activated"])
        self.assertFalse(state["promotion_allowed"])


if __name__ == "__main__":
    unittest.main()
