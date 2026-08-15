from __future__ import annotations

import unittest

from china_commodities.catalog import load_catalog
from china_commodities.features import (
    build_curve_features,
    contract_month,
    enrich_and_score_curves,
    select_candidates,
    summarize_options,
)


class ContractMonthTests(unittest.TestCase):
    def test_resolves_four_digit_contract(self) -> None:
        self.assertEqual(contract_month("RB2610", "2026-08-14").isoformat(), "2026-10-01")

    def test_resolves_czce_three_digit_contract(self) -> None:
        self.assertEqual(contract_month("SR609", "2026-08-14").isoformat(), "2026-09-01")


class FeatureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = load_catalog()
        self.records = [
            {
                "trade_date": "2026-08-14",
                "exchange": "SHFE",
                "product": "RB",
                "contract": "RB2610",
                "close": 3100.0,
                "settle": 3098.0,
                "pre_settle": 3050.0,
                "close_return_pct": 1.6393,
                "settle_return_pct": 1.5738,
                "volume": 100000.0,
                "open_interest": 200000.0,
                "turnover": 1.0,
            },
            {
                "trade_date": "2026-08-14",
                "exchange": "SHFE",
                "product": "RB",
                "contract": "RB2701",
                "close": 3050.0,
                "settle": 3048.0,
                "pre_settle": 3030.0,
                "close_return_pct": 0.6601,
                "settle_return_pct": 0.5941,
                "volume": 50000.0,
                "open_interest": 100000.0,
                "turnover": 1.0,
            },
        ]

    def test_curve_uses_concrete_contracts_and_explicit_sign(self) -> None:
        curves = build_curve_features(self.records, self.catalog)
        self.assertEqual(curves[0]["main_contract"]["contract"], "RB2610")
        self.assertEqual(curves[0]["nearest_liquid_contract"]["contract"], "RB2610")
        self.assertEqual(curves[0]["near_next_curve"]["curve_shape"], "backwardation")
        self.assertGreater(curves[0]["near_next_curve"]["near_minus_deferred"], 0)

    def test_option_summary_never_claims_dealer_gamma(self) -> None:
        records = [
            {"trade_date": "2026-08-14", "exchange": "SHFE", "product": "CU", "source_symbol": "铜期权", "option_type": "C", "volume": 10, "open_interest": 20, "iv_percent": 21},
            {"trade_date": "2026-08-14", "exchange": "SHFE", "product": "CU", "source_symbol": "铜期权", "option_type": "P", "volume": 20, "open_interest": 40, "iv_percent": 23},
        ]
        summary = summarize_options(records)[0]
        self.assertEqual(summary["put_call_volume_ratio"], 2.0)
        self.assertFalse(summary["dealer_gamma_known"])

    def test_candidate_is_anomaly_not_trade(self) -> None:
        curves = build_curve_features(self.records, self.catalog)
        scored = enrich_and_score_curves(curves, [], [], [])
        candidate = select_candidates(scored)[0]
        self.assertFalse(candidate["is_trade_recommendation"])
        self.assertEqual(candidate["concrete_contract"], "RB2610")
        self.assertEqual(candidate["display_order"], 1)
        self.assertEqual(candidate["score_rank"], 1)
        self.assertIn("cross_sectional_activity_score", candidate)
        self.assertNotIn("anomaly_score", candidate)
        self.assertNotIn("rank", candidate)
        self.assertTrue(candidate["evidence"]["curve"]["available"])
        self.assertFalse(candidate["evidence"]["basis"]["available"])
        self.assertFalse(candidate["evidence"]["warehouse"]["available"])
        self.assertEqual(candidate["evidence_count"], 1)

    def test_close_only_prices_do_not_form_settlement_curve_evidence(self) -> None:
        records = [dict(item, settle=None) for item in self.records]
        curves = build_curve_features(records, self.catalog)
        scored = enrich_and_score_curves(curves, [], [], [])
        self.assertIsNone(scored[0]["near_next_curve"]["curve_shape"])
        self.assertEqual(
            scored[0]["near_next_curve"]["price_quality"],
            "settlement_unavailable",
        )
        self.assertFalse(scored[0]["evidence"]["curve"]["available"])

    def test_display_order_is_distinct_from_score_rank(self) -> None:
        base = {
            "trade_date": "2026-08-14",
            "exchange": "SHFE",
            "product_name": "测试",
            "main_contract": {
                "contract": "X2610",
                "close_return_pct": 1.0,
                "volume": 100.0,
                "open_interest": 100.0,
            },
            "near_next_curve": {"curve_shape": None},
            "available_evidence_layers": [],
            "missing_evidence_layers": [],
            "evidence": {},
            "evidence_count": 0,
            "liquidity_eligible": True,
        }
        highest = dict(
            base,
            product="CU",
            sector="有色与贵金属",
            score_rank=1,
            cross_sectional_activity_score=95.0,
        )
        sector_first = dict(
            base,
            product="RB",
            sector="黑色与建材",
            score_rank=2,
            cross_sectional_activity_score=80.0,
        )
        selected = select_candidates([highest, sector_first])
        self.assertEqual(selected[0]["product"], "RB")
        self.assertEqual(selected[0]["display_order"], 1)
        self.assertEqual(selected[0]["score_rank"], 2)
        self.assertEqual(selected[1]["score_rank"], 1)


if __name__ == "__main__":
    unittest.main()
