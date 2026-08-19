from __future__ import annotations

import unittest

from china_commodities.option_quality import assess_option_snapshot_quality


TRADE_DATE = "2026-08-19"
EXPIRY = "2026-09-24"


def _record(
    contract: str,
    option_type: str,
    *,
    expiry: str | None = None,
    exercise_style: str = "unknown",
    bid: float | None = None,
    ask: float | None = None,
    model: bool = False,
    vendor: bool = True,
    source_date_match: bool = True,
    source_trade_date: str = TRADE_DATE,
    underlying_settle: float | None = 106000.0,
) -> dict:
    vendor_greeks = {
        "iv_percent": 20.0,
        "delta": 0.5 if option_type == "C" else -0.5,
        "gamma": 0.001,
        "vega": 100.0,
        "theta": -10.0,
        "rho": 5.0,
    }
    model_greeks = {
        "model": "black_76",
        "iv_percent": 20.0,
        "delta": 0.5 if option_type == "C" else -0.5,
        "gamma": 0.001,
        "vega": 100.0,
        "theta": -10.0,
        "rho": 5.0,
    }
    if model and vendor:
        quality = "vendor_and_model"
        selected_source = "model"
    elif model:
        quality = "model_derived"
        selected_source = "model"
    elif vendor:
        quality = "vendor_reported"
        selected_source = "vendor"
    else:
        quality = "unavailable"
        selected_source = None
    greeks = {
        "quality": quality,
        "selected_source": selected_source,
        "selected": model_greeks if model else vendor_greeks if vendor else None,
        "vendor": vendor_greeks if vendor else None,
        "model": model_greeks if model else None,
        "dealer_position_direction_known": False,
    }
    return {
        "trade_date": TRADE_DATE,
        "source_trade_date": source_trade_date,
        "source_date_match": source_date_match,
        "source_provider": "ifind_http",
        "exchange": "SHFE",
        "product": "CU",
        "contract": contract,
        "underlying_contract": "CU2609",
        "expiry_date": expiry,
        "option_type": option_type,
        "strike": 106000.0,
        "exercise_style": exercise_style,
        "underlying_settle": underlying_settle,
        "iv_percent": 20.0 if vendor or model else None,
        "bid": bid,
        "ask": ask,
        "open_interest": 10.0,
        "greeks": greeks,
    }


def _snapshot(records: list[dict], **overrides) -> dict:
    snapshot = {
        "schema_version": 1,
        "trade_date": TRADE_DATE,
        "universe_contract_count": len(records),
        "quote_contract_count": len(records),
        "quote_coverage_complete": True,
        "universe_source": "exchange_eod_via_akshare",
        "records": records,
    }
    snapshot.update(overrides)
    return snapshot


class OptionQualityTests(unittest.TestCase):
    def test_current_like_cu_chain_is_chain_only(self) -> None:
        records = [
            _record("CU2609C106000", "C"),
            _record("CU2609P106000", "P"),
        ]
        result = assess_option_snapshot_quality(_snapshot(records))

        self.assertEqual(result["status"], "chain_only")
        self.assertTrue(result["full_chain_verified"])
        self.assertFalse(result["surface_ready"])
        self.assertFalse(result["execution_ready"])
        self.assertFalse(result["model_greeks_ready"])
        self.assertTrue(result["vendor_risk_available"])
        self.assertTrue(result["vendor_risk_units_as_reported"])
        self.assertFalse(result["dealer_gamma_direction_known"])
        self.assertFalse(result["all_inputs_ifind"])
        self.assertTrue(any("not all inputs are iFinD" in item for item in result["limitations"]))
        self.assertEqual(result["expiry_coverage"], 0.0)
        self.assertEqual(result["exercise_style_coverage"], 0.0)
        self.assertEqual(result["bid_ask_coverage"], 0.0)
        self.assertEqual(result["series_count"], 1)
        self.assertEqual(result["balanced_call_put_series_pct"], 1.0)

    def test_complete_balanced_chain_is_surface_ready(self) -> None:
        records = [
            _record(
                "CU2609C106000",
                "C",
                expiry=EXPIRY,
                exercise_style="european",
                bid=100.0,
                ask=110.0,
                model=True,
            ),
            _record(
                "CU2609P106000",
                "P",
                expiry=EXPIRY,
                exercise_style="european",
                bid=100.0,
                ask=110.0,
                model=True,
            ),
        ]
        result = assess_option_snapshot_quality(_snapshot(records))

        self.assertEqual(result["status"], "surface_ready")
        self.assertTrue(result["full_chain_verified"])
        self.assertTrue(result["surface_ready"])
        self.assertTrue(result["execution_ready"])
        self.assertTrue(result["model_greeks_ready"])
        self.assertTrue(result["vendor_risk_available"])
        self.assertEqual(result["expiry_coverage"], 1.0)
        self.assertEqual(result["iv_coverage"], 1.0)
        self.assertEqual(result["model_greeks_coverage"], 1.0)
        self.assertEqual(result["balanced_call_put_series_pct"], 1.0)

    def test_partial_market_scope_is_labeled_partial_chain(self) -> None:
        records = [
            _record("CU2609C106000", "C"),
            _record("CU2609P106000", "P"),
        ]
        result = assess_option_snapshot_quality(
            _snapshot(
                records,
                coverage={
                    "expected_product_count": 64,
                    "successful_product_count": 60,
                    "product_coverage": 60 / 64,
                    "scope_complete": False,
                },
            )
        )

        self.assertEqual(result["status"], "partial_chain")
        self.assertTrue(result["full_chain_verified"])
        self.assertTrue(result["product_scope_declared"])
        self.assertFalse(result["full_product_scope_verified"])
        self.assertEqual(result["product_coverage"], 60 / 64)
        self.assertFalse(result["vendor_risk_available"])
        self.assertTrue(any("full-market scope is incomplete" in item for item in result["limitations"]))

    def test_complete_declared_scope_preserves_surface_readiness(self) -> None:
        records = [
            _record(
                "CU2609C106000",
                "C",
                expiry=EXPIRY,
                exercise_style="european",
                bid=100.0,
                ask=110.0,
                model=True,
            ),
            _record(
                "CU2609P106000",
                "P",
                expiry=EXPIRY,
                exercise_style="european",
                bid=100.0,
                ask=110.0,
                model=True,
            ),
        ]
        result = assess_option_snapshot_quality(
            _snapshot(
                records,
                coverage={
                    "expected_product_count": 64,
                    "successful_product_count": 64,
                    "product_coverage": 1.0,
                    "scope_complete": True,
                },
            )
        )

        self.assertEqual(result["status"], "surface_ready")
        self.assertTrue(result["full_product_scope_verified"])

    def test_missing_quote_record_is_invalid(self) -> None:
        records = [_record("CU2609C106000", "C")]
        result = assess_option_snapshot_quality(
            _snapshot(records, universe_contract_count=2, quote_contract_count=2)
        )

        self.assertEqual(result["status"], "invalid")
        self.assertFalse(result["full_chain_verified"])
        self.assertTrue(any("counts are inconsistent" in item for item in result["limitations"]))

    def test_stale_source_date_is_invalid(self) -> None:
        records = [
            _record(
                "CU2609C106000",
                "C",
                source_date_match=False,
                source_trade_date="2026-08-18",
            ),
            _record("CU2609P106000", "P", source_date_match=False, source_trade_date="2026-08-18"),
        ]
        result = assess_option_snapshot_quality(_snapshot(records))

        self.assertEqual(result["status"], "invalid")
        self.assertFalse(result["full_chain_verified"])
        self.assertEqual(result["source_date_match_pct"], 0.0)

    def test_duplicate_contract_is_invalid(self) -> None:
        records = [
            _record("CU2609C106000", "C"),
            _record("CU2609C106000", "C"),
        ]
        result = assess_option_snapshot_quality(_snapshot(records))

        self.assertEqual(result["status"], "invalid")
        self.assertFalse(result["full_chain_verified"])
        self.assertEqual(result["unique_contract_count"], 1)
        self.assertTrue(any("uniqueness failed" in item for item in result["limitations"]))

    def test_vendor_greeks_do_not_make_model_ready(self) -> None:
        records = [
            _record(
                "CU2609C106000",
                "C",
                expiry=EXPIRY,
                exercise_style="european",
                vendor=True,
                model=False,
            ),
            _record(
                "CU2609P106000",
                "P",
                expiry=EXPIRY,
                exercise_style="european",
                vendor=True,
                model=False,
            ),
        ]
        result = assess_option_snapshot_quality(_snapshot(records))

        self.assertEqual(result["vendor_greeks_coverage"], 1.0)
        self.assertEqual(result["model_greeks_coverage"], 0.0)
        self.assertFalse(result["model_greeks_ready"])

    def test_ratios_are_json_safe_and_clamped(self) -> None:
        result = assess_option_snapshot_quality(
            {
                "trade_date": TRADE_DATE,
                "records": [],
                "universe_contract_count": 0,
                "quote_contract_count": 0,
                "quote_coverage_complete": False,
            }
        )

        for key in (
            "source_date_match_pct",
            "underlying_settle_coverage",
            "expiry_coverage",
            "exercise_style_coverage",
            "iv_coverage",
            "vendor_greeks_coverage",
            "model_greeks_coverage",
            "bid_ask_coverage",
            "open_interest_coverage",
            "balanced_call_put_series_pct",
        ):
            self.assertGreaterEqual(result[key], 0.0)
            self.assertLessEqual(result[key], 1.0)
        self.assertEqual(result["status"], "invalid")


if __name__ == "__main__":
    unittest.main()
