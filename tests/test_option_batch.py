from __future__ import annotations

import unittest

from china_commodities.catalog import OptionProduct
from china_commodities.collectors.ifind_http_adapter import IFindHTTPError
from china_commodities.option_batch import collect_option_market_snapshot


PRODUCTS = (
    OptionProduct("SHFE", "CU", "铜期权"),
    OptionProduct("DCE", "I", "铁矿石期权"),
    OptionProduct("GFEX", "LC", "碳酸锂"),
)


def _record(universe, trade_date: str) -> dict:
    product = universe.product.upper()
    contract = f"{product}2609C100"
    return {
        "trade_date": trade_date,
        "exchange": universe.exchange.upper(),
        "product": product,
        "contract": contract,
    }


class FakeClient:
    pass


class OptionBatchTests(unittest.TestCase):
    def test_full_market_combines_every_product(self) -> None:
        def collect_one(trade_date, *, universes, **kwargs):
            record = _record(universes[0], trade_date)
            return {
                "records": [record],
                "universe_contract_count": 1,
                "quote_contract_count": 1,
                "quote_coverage_complete": True,
            }

        snapshot, status = collect_option_market_snapshot(
            "2026-08-19",
            client=FakeClient(),
            option_products=PRODUCTS,
            minimum_product_coverage=1.0,
            collect_one=collect_one,
        )

        self.assertIsNotNone(snapshot)
        self.assertEqual(len(snapshot["records"]), 3)
        self.assertTrue(snapshot["coverage"]["scope_complete"])
        self.assertTrue(status["coverage"]["publish_eligible"])
        self.assertEqual(status["coverage"]["attempted_product_count"], 3)

    def test_one_product_failure_is_visible_but_does_not_abort(self) -> None:
        def collect_one(trade_date, *, universes, **kwargs):
            universe = universes[0]
            if universe.product == "I":
                raise ValueError("exchange directory unavailable")
            return {
                "records": [_record(universe, trade_date)],
                "universe_contract_count": 1,
                "quote_contract_count": 1,
                "quote_coverage_complete": True,
            }

        snapshot, status = collect_option_market_snapshot(
            "2026-08-19",
            client=FakeClient(),
            option_products=PRODUCTS,
            minimum_product_coverage=0.6,
            collect_one=collect_one,
        )

        self.assertIsNotNone(snapshot)
        self.assertFalse(snapshot["coverage"]["scope_complete"])
        self.assertTrue(status["coverage"]["publish_eligible"])
        self.assertEqual(status["coverage"]["failed_products"], ["DCE:I"])
        self.assertIn("ValueError", status["product_statuses"][0]["detail"])

    def test_below_coverage_threshold_is_not_publishable(self) -> None:
        def collect_one(trade_date, *, universes, **kwargs):
            universe = universes[0]
            if universe.product != "CU":
                raise ValueError("missing")
            return {
                "records": [_record(universe, trade_date)],
                "universe_contract_count": 1,
                "quote_contract_count": 1,
                "quote_coverage_complete": True,
            }

        snapshot, status = collect_option_market_snapshot(
            "2026-08-19",
            client=FakeClient(),
            option_products=PRODUCTS,
            minimum_product_coverage=0.75,
            collect_one=collect_one,
        )

        self.assertIsNotNone(snapshot)
        self.assertFalse(status["coverage"]["publish_eligible"])
        self.assertFalse(status["data_fresh"])

    def test_ifind_http_failure_stops_repeated_global_calls(self) -> None:
        calls = []

        def collect_one(trade_date, *, universes, **kwargs):
            calls.append(universes[0].product)
            raise IFindHTTPError("iFinD get_access_token failed")

        snapshot, status = collect_option_market_snapshot(
            "2026-08-19",
            client=FakeClient(),
            option_products=PRODUCTS,
            collect_one=collect_one,
        )

        self.assertIsNone(snapshot)
        self.assertEqual(len(calls), 1)
        self.assertEqual(status["coverage"]["failed_product_count"], 1)
        self.assertEqual(status["coverage"]["skipped_product_count"], 2)
        self.assertEqual(
            status["product_statuses"][1]["status"],
            "skipped_global_ifind_error",
        )

    def test_duplicate_contracts_make_snapshot_invalid(self) -> None:
        def collect_one(trade_date, *, universes, **kwargs):
            record = _record(universes[0], trade_date)
            record["contract"] = "DUP2609C100"
            return {
                "records": [record],
                "universe_contract_count": 1,
                "quote_contract_count": 1,
                "quote_coverage_complete": True,
            }

        snapshot, status = collect_option_market_snapshot(
            "2026-08-19",
            client=FakeClient(),
            option_products=PRODUCTS,
            collect_one=collect_one,
        )

        self.assertIsNone(snapshot)
        self.assertEqual(status["duplicate_contract_count"], 2)
        self.assertFalse(status["coverage"]["publish_eligible"])


if __name__ == "__main__":
    unittest.main()
