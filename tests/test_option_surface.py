from __future__ import annotations

import unittest

from china_commodities.option_surface import build_option_surface


def record(contract: str, expiry: str, option_type: str, strike: float) -> dict:
    return {
        "trade_date": "2026-08-19",
        "source_trade_date": "2026-08-19",
        "source_date_match": True,
        "source_provider": "ifind_http",
        "exchange": "SHFE",
        "product": "CU",
        "contract": contract,
        "underlying_contract": "CU2610",
        "expiry_date": expiry,
        "option_type": option_type,
        "strike": strike,
        "settle": 1000.0,
        "open_interest": 20.0,
        "underlying_settle": 80000.0,
        "greeks": {
            "quality": "vendor_reported",
            "selected_source": "vendor",
            "selected": {
                "iv_percent": 20.0 if option_type == "C" else 22.0,
                "delta": 0.25 if option_type == "C" else -0.25,
            },
        },
    }


class OptionSurfaceTests(unittest.TestCase):
    def test_never_mixes_expiries_and_bid_ask_only_blocks_execution(self) -> None:
        records = [
            record("CU2610C80000", "2026-09-24", "C", 80000),
            record("CU2610P80000", "2026-09-24", "P", 80000),
            record("CU2610C81000", "2026-10-22", "C", 81000),
            record("CU2610P81000", "2026-10-22", "P", 81000),
        ]
        surface = build_option_surface(
            {
                "trade_date": "2026-08-19",
                "generated_at": "2026-08-19T18:00:00+08:00",
                "source_provider": "ifind_http",
                "records": records,
            }
        )

        self.assertEqual(surface["series_count"], 2)
        self.assertEqual(surface["surface_ready_count"], 2)
        self.assertEqual(surface["positioning_ready_count"], 2)
        self.assertEqual(surface["execution_ready_count"], 0)
        self.assertEqual(
            {item["expiry_date"] for item in surface["surfaces"]},
            {"2026-09-24", "2026-10-22"},
        )
        self.assertTrue(all(item["contract_count"] == 2 for item in surface["surfaces"]))

    def test_iv_and_source_date_gates_are_per_expiry(self) -> None:
        records = [
            record("CU2610C80000", "2026-09-24", "C", 80000),
            record("CU2610P80000", "2026-09-24", "P", 80000),
        ]
        records[1]["greeks"]["selected"].pop("iv_percent")
        surface = build_option_surface(
            {"trade_date": "2026-08-19", "records": records}
        )
        self.assertFalse(surface["surfaces"][0]["surface_ready"])
        self.assertEqual(surface["surfaces"][0]["iv_coverage"], 0.5)

        records[1]["greeks"]["selected"]["iv_percent"] = 22.0
        records[1]["source_trade_date"] = "2026-08-18"
        surface = build_option_surface(
            {"trade_date": "2026-08-19", "records": records}
        )
        self.assertFalse(surface["surfaces"][0]["surface_ready"])
        self.assertEqual(surface["surfaces"][0]["source_date_match_pct"], 0.5)

    def test_open_interest_below_ninety_percent_does_not_block_surface(self) -> None:
        records = [
            record("CU2610C80000", "2026-09-24", "C", 80000),
            record("CU2610P80000", "2026-09-24", "P", 80000),
        ]
        records[1]["open_interest"] = None
        output = build_option_surface(
            {"trade_date": "2026-08-19", "records": records}
        )["surfaces"][0]
        self.assertTrue(output["surface_ready"])
        self.assertFalse(output["positioning_ready"])


if __name__ == "__main__":
    unittest.main()
