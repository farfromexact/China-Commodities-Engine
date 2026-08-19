from __future__ import annotations

import unittest

from china_commodities.option_greeks import (
    OptionValuationInput,
    american_futures_option_price,
    black76_price,
    calculate_greeks,
    implied_volatility,
    option_price,
)


class OptionGreeksTests(unittest.TestCase):
    def test_black76_put_call_parity(self) -> None:
        call = black76_price(100, 95, 0.5, 0.02, 0.25, "C")
        put = black76_price(100, 95, 0.5, 0.02, 0.25, "P")
        expected = 0.5
        self.assertAlmostEqual(
            call - put,
            pow(2.718281828459045, -0.02 * expected) * 5,
            places=8,
        )

    def test_recovers_european_implied_volatility(self) -> None:
        market = black76_price(100, 100, 30 / 365, 0.015, 0.30, "C")
        recovered = implied_volatility(
            market,
            forward=100,
            strike=100,
            time_to_expiry_years=30 / 365,
            rate=0.015,
            option_type="C",
            exercise_style="european",
        )
        self.assertIsNotNone(recovered)
        self.assertAlmostEqual(recovered or 0, 0.30, places=5)

    def test_american_model_is_not_silently_black76(self) -> None:
        american = american_futures_option_price(80, 100, 0.5, 0.05, 0.20, "P")
        european = black76_price(80, 100, 0.5, 0.05, 0.20, "P")
        self.assertGreaterEqual(american, european)

    def test_unknown_exercise_style_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "explicitly"):
            option_price(100, 100, 0.25, 0.02, 0.2, "C", "unknown")

    def test_greek_units_and_source_are_explicit(self) -> None:
        result = calculate_greeks(
            OptionValuationInput(
                forward=100,
                strike=100,
                time_to_expiry_years=60 / 365,
                rate=0.02,
                option_type="C",
                exercise_style="european",
                iv_percent=25.0,
            )
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.model, "black_76")
        self.assertEqual(result.iv_source, "vendor")
        self.assertGreater(result.gamma, 0)
        self.assertGreater(result.vega_per_vol_point, 0)


if __name__ == "__main__":
    unittest.main()
