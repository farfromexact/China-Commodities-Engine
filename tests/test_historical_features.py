from __future__ import annotations

import json
import math
import unittest
from datetime import date, timedelta
from statistics import stdev

from china_commodities.historical_features import build_market_state


def _contract_record(
    trade_date: str,
    contract: str,
    *,
    settle: float,
    settle_return_pct: float | None,
    volume: float,
    open_interest: float,
) -> dict:
    return {
        "trade_date": trade_date,
        "source_trade_date": trade_date,
        "source_date_match": True,
        "exchange": "SHFE",
        "product": "RB",
        "contract": contract,
        "settle": settle,
        "pre_settle": settle / (1 + (settle_return_pct or 0) / 100),
        "settle_return_pct": settle_return_pct,
        "volume": volume,
        "open_interest": open_interest,
    }


def _snapshot(
    trade_date: str,
    *,
    main_contract: str = "RB2609",
    near_contract: str = "RB2609",
    deferred_contract: str = "RB2610",
    curve_value: float = 1.0,
    settle: float = 100.0,
    settle_return_pct: float | None = 1.0,
    volume: float = 100.0,
    open_interest: float = 100.0,
    verified: bool = True,
    scope_verified: bool = False,
    extra_contracts: list[dict] | None = None,
) -> dict:
    futures = []
    extra_keys = {
        item.get("contract") for item in (extra_contracts or []) if isinstance(item, dict)
    }
    if main_contract not in extra_keys:
        futures.append(
            _contract_record(
                trade_date,
                main_contract,
                settle=settle,
                settle_return_pct=settle_return_pct,
                volume=volume,
                open_interest=open_interest,
            )
        )
    if extra_contracts:
        futures.extend(extra_contracts)
    return {
        "trade_date": trade_date,
        "verified": verified,
        "scope_verified": scope_verified,
        "commodity_curves": [
            {
                "trade_date": trade_date,
                "exchange": "SHFE",
                "product": "RB",
                "product_name": "螺纹钢",
                "sector": "黑色与建材",
                "main_contract": {
                    "contract": main_contract,
                    "contract_month": "2026-09-01"
                    if main_contract == "RB2609"
                    else "2026-10-01",
                },
                "nearest_liquid_contract": {"contract": near_contract},
                "next_liquid_contract": {"contract": deferred_contract},
                "near_next_curve": {
                    "near_minus_deferred_pct": curve_value,
                },
            }
        ],
        "futures_contracts": futures,
    }


class HistoricalFeaturesTests(unittest.TestCase):
    def test_locks_latest_contract_and_never_splices_daily_main_series(self) -> None:
        snapshots = []
        for index, day in enumerate(("2026-08-17", "2026-08-18", "2026-08-19")):
            old_main = index < 2
            main_contract = "RB2609" if old_main else "RB2610"
            old_main_row = _contract_record(
                day,
                "RB2609",
                settle=100 + index,
                settle_return_pct=1.0,
                volume=100,
                open_interest=100 + index * 10,
            )
            new_main_row = _contract_record(
                day,
                "RB2610",
                settle=200 + index,
                settle_return_pct=0.5,
                volume=200,
                open_interest=200 + index * 10,
            )
            snapshots.append(
                _snapshot(
                    day,
                    main_contract=main_contract,
                    near_contract="RB2609",
                    deferred_contract="RB2610",
                    extra_contracts=[old_main_row, new_main_row],
                )
            )

        state = build_market_state(snapshots)
        product = state["products"][0]

        self.assertEqual(product["current_contract"], "RB2610")
        self.assertEqual(product["prior_main_contract"], "RB2609")
        self.assertTrue(product["main_contract_roll_flag"])
        self.assertEqual(
            {item["contract"] for item in product["history_observations"]},
            {"RB2610"},
        )
        expected = (1.005**3 - 1) * 100
        self.assertAlmostEqual(
            product["settlement_return_pct"]["3D"]["value"], expected
        )

    def test_insufficient_observations_are_null_and_reported(self) -> None:
        snapshots = [
            _snapshot(
                f"2026-08-{17 + index:02d}",
                settle=100 + index,
                volume=100 + index,
                open_interest=100 + index * 10,
            )
            for index in range(4)
        ]

        product = build_market_state(snapshots)["products"][0]

        self.assertIsNone(product["settlement_return_pct"]["5D"]["value"])
        self.assertEqual(product["settlement_return_pct"]["5D"]["observations"], 4)
        self.assertIsNone(product["realized_vol_20d_annualized_pct"])
        self.assertEqual(product["realized_vol_20d_observations"], 4)
        self.assertIsNone(product["volume_zscore"])
        self.assertEqual(product["volume_zscore_observations"], 4)
        self.assertIsNone(product["oi_change_zscore"])
        self.assertIn("realized_vol_20d_annualized_pct", product["quality"]["missing_metrics"])

    def test_missing_previous_exact_contract_breaks_one_day_metrics(self) -> None:
        first = _snapshot("2026-08-17", main_contract="RB2609")
        missing = _snapshot(
            "2026-08-18",
            main_contract="RB2609",
            extra_contracts=[
                _contract_record(
                    "2026-08-18",
                    "RB2609",
                    settle=101,
                    settle_return_pct=1.0,
                    volume=100,
                    open_interest=110,
                )
            ],
        )
        missing["futures_contracts"] = []
        latest = _snapshot("2026-08-19", main_contract="RB2609", open_interest=120)

        product = build_market_state([first, missing, latest])["products"][0]

        self.assertIsNotNone(product["settlement_return_pct"]["1D"]["value"])
        self.assertIsNone(product["settlement_return_pct"]["3D"]["value"])
        self.assertIsNone(product["delta_OI_1D"])
        self.assertEqual(product["attribution_clue"], "mixed_or_flat")

    def test_twenty_daily_returns_generate_compounded_return_and_realized_vol(self) -> None:
        returns = [0.1 * (index + 1) for index in range(20)]
        snapshots = []
        for index, daily_return in enumerate(returns):
            day = date(2026, 7, 23) + timedelta(days=index)
            snapshots.append(
                _snapshot(
                    day.isoformat(),
                    settle=100 + index,
                    settle_return_pct=daily_return,
                    volume=100 + index * 2,
                    open_interest=200 + index * 3,
                )
            )

        state = build_market_state(snapshots)
        product = state["products"][0]
        return_20d = product["settlement_return_pct"]["20D"]
        expected_return = (math.prod(1 + value / 100 for value in returns) - 1) * 100
        expected_vol = stdev(returns) * math.sqrt(252)

        self.assertEqual(state["history_window"]["available_trading_days"], 20)
        self.assertEqual(return_20d["observations"], 20)
        self.assertAlmostEqual(return_20d["value"], expected_return)
        self.assertEqual(product["realized_vol_20d_observations"], 20)
        self.assertAlmostEqual(product["realized_vol_20d_annualized_pct"], expected_vol)
        json.dumps(state, ensure_ascii=False, allow_nan=False)

    def test_oi_quadrant_is_only_an_attribution_clue(self) -> None:
        first = _snapshot(
            "2026-08-18",
            settle=100,
            settle_return_pct=0.5,
            open_interest=100,
        )
        second = _snapshot(
            "2026-08-19",
            settle=101,
            settle_return_pct=1.0,
            open_interest=120,
        )

        product = build_market_state([first, second])["products"][0]

        self.assertEqual(product["attribution_clue"], "price_up_oi_up")
        state = build_market_state([first, second])
        self.assertIn("only a price/OI quadrant clue", state["methodology"]["oi_attribution"])
        self.assertIsNone(product["state_vector"]["fundamental_score"])
        self.assertIsNone(product["state_vector"]["convexity_score"])
        self.assertEqual(product["state_vector"]["trade_recommendation"], "unavailable")

    def test_curve_pair_roll_uses_only_latest_pair_history(self) -> None:
        snapshots = [
            _snapshot(
                "2026-08-14",
                near_contract="RB2610",
                deferred_contract="RB2701",
                curve_value=1,
            )
        ]
        for index, value in enumerate((2, 3, 4), start=15):
            snapshots.append(
                _snapshot(
                    f"2026-08-{index:02d}",
                    near_contract="RB2610",
                    deferred_contract="RB2701",
                    curve_value=value,
                )
            )
        snapshots.append(
            _snapshot(
                "2026-08-18",
                near_contract="RB2609",
                deferred_contract="RB2610",
                curve_value=99,
            )
        )
        snapshots.append(
            _snapshot(
                "2026-08-19",
                near_contract="RB2610",
                deferred_contract="RB2701",
                curve_value=5,
            )
        )

        product = build_market_state(snapshots)["products"][0]
        curve = product["curve"]

        self.assertEqual(curve["nearest_liquid_contract"], "RB2610")
        self.assertEqual(curve["next_liquid_contract"], "RB2701")
        self.assertEqual(curve["current"], 5.0)
        self.assertEqual(curve["observations"], 5)
        self.assertTrue(curve["pair_roll_flag"])
        self.assertIsNotNone(curve["zscore"])

    def test_unverified_is_ignored_and_duplicate_eligible_date_is_rejected(self) -> None:
        ignored = _snapshot("not-a-date", verified=False)
        valid = _snapshot("2026-08-19", scope_verified=True)
        state = build_market_state([ignored, valid])
        self.assertEqual(state["trade_date"], "2026-08-19")
        self.assertIn("unverified_snapshots_ignored", state["quality"]["warnings"])

        duplicate = _snapshot("2026-08-19")
        with self.assertRaisesRegex(ValueError, "duplicate eligible snapshot"):
            build_market_state([valid, duplicate])


if __name__ == "__main__":
    unittest.main()
