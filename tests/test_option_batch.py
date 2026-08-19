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
    def test_official_rule_registry_supplies_exercise_style(self) -> None:
        observed_styles = []

        def collect_one(trade_date, *, universes, **kwargs):
            observed_styles.append(universes[0].exercise_style)
            item = _record(universes[0], trade_date)
            item["expiry_date"] = "2026-09-24"
            return {
                "records": [item],
                "universe_contract_count": 1,
                "quote_contract_count": 1,
                "quote_coverage_complete": True,
            }

        snapshot, _ = collect_option_market_snapshot(
            "2026-08-19",
            client=FakeClient(),
            option_products=(PRODUCTS[0],),
            minimum_product_coverage=1.0,
            collect_one=collect_one,
            fallback_directory_loader=None,
        )
        self.assertEqual(observed_styles, ["american"])
        self.assertEqual(snapshot["records"][0]["exercise_style"], "american")
        self.assertTrue(
            snapshot["records"][0]["exercise_style_rule_source_url"].startswith(
                "https://"
            )
        )

    def test_openctp_enriches_missing_expiry_without_replacing_quotes(self) -> None:
        def collect_one(trade_date, *, universes, **kwargs):
            return {
                "records": [_record(universes[0], trade_date)],
                "universe_contract_count": 1,
                "quote_contract_count": 1,
                "quote_coverage_complete": True,
                "universe_source": "exchange_eod_via_akshare",
            }

        def fallback_loader(trade_date, products):
            return {
                ("SHFE", "CU"): [
                    {
                        "contract": "CU2609C100",
                        "expiry_date": "2026-09-24",
                        "universe_source": "openctp_contract_directory",
                    }
                ]
            }

        snapshot, status = collect_option_market_snapshot(
            "2026-08-19",
            client=FakeClient(),
            option_products=(PRODUCTS[0],),
            minimum_product_coverage=1.0,
            collect_one=collect_one,
            fallback_directory_loader=fallback_loader,
            enrich_missing_metadata=True,
        )
        self.assertEqual(snapshot["records"][0]["expiry_date"], "2026-09-24")
        self.assertEqual(
            snapshot["records"][0]["expiry_source"],
            "openctp_contract_directory",
        )
        self.assertTrue(status["product_statuses"][0]["metadata_enrichment_used"])

    def test_failed_primary_directory_uses_one_verified_fallback(self) -> None:
        product = OptionProduct("DCE", "I", "铁矿石期权")
        fallback_calls = []

        def fallback_loader(trade_date, products):
            fallback_calls.append((trade_date, len(products)))
            return {
                ("DCE", "I"): [
                    {
                        "trade_date": trade_date,
                        "exchange": "DCE",
                        "product": "I",
                        "contract": "I2609-C-700",
                        "universe_source": "openctp_contract_directory",
                    }
                ]
            }

        def collect_one(
            trade_date,
            *,
            universes,
            directory_records_by_product=None,
            **kwargs,
        ):
            if directory_records_by_product is None:
                raise ValueError("exchange blocked")
            record = _record(universes[0], trade_date)
            return {
                "records": [record],
                "universe_contract_count": 1,
                "quote_contract_count": 1,
                "quote_coverage_complete": True,
                "universe_source": "openctp_contract_directory",
            }

        snapshot, status = collect_option_market_snapshot(
            "2026-08-19",
            client=FakeClient(),
            option_products=(product,),
            minimum_product_coverage=1.0,
            collect_one=collect_one,
            fallback_directory_loader=fallback_loader,
        )

        self.assertIsNotNone(snapshot)
        self.assertEqual(fallback_calls, [("2026-08-19", 1)])
        self.assertTrue(status["product_statuses"][0]["fallback_used"])
        self.assertEqual(
            status["product_statuses"][0]["universe_source"],
            "openctp_contract_directory",
        )

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
            fallback_directory_loader=None,
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
            fallback_directory_loader=None,
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

    def test_market_security_denial_skips_only_named_exchange(self) -> None:
        products = (
            OptionProduct("DCE", "A", "豆一期权"),
            OptionProduct("DCE", "I", "铁矿石期权"),
            OptionProduct("GFEX", "LC", "碳酸锂期权"),
            OptionProduct("SHFE", "CU", "铜期权"),
        )
        calls = []

        def collect_one(trade_date, *, universes, **kwargs):
            universe = universes[0]
            calls.append(f"{universe.exchange}:{universe.product}")
            if universe.exchange == "DCE":
                raise IFindHTTPError(
                    "iFinD real_time_quotation failed with code -4226: "
                    "Permission denied by DCE security"
                )
            return {
                "records": [_record(universe, trade_date)],
                "universe_contract_count": 1,
                "quote_contract_count": 1,
                "quote_coverage_complete": True,
            }

        snapshot, status = collect_option_market_snapshot(
            "2026-08-19",
            client=FakeClient(),
            option_products=products,
            minimum_product_coverage=0.5,
            collect_one=collect_one,
            fallback_directory_loader=None,
        )

        self.assertIsNotNone(snapshot)
        self.assertEqual(calls, ["DCE:A", "GFEX:LC", "SHFE:CU"])
        self.assertEqual(status["coverage"]["successful_product_count"], 2)
        self.assertEqual(status["coverage"]["failed_product_count"], 1)
        self.assertEqual(status["coverage"]["skipped_product_count"], 1)
        self.assertEqual(
            status["product_statuses"][1]["status"],
            "skipped_exchange_ifind_error",
        )
        self.assertIsNone(status["global_error"])
        self.assertIn("DCE", status["exchange_errors"])

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
