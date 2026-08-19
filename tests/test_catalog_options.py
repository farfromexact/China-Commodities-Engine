from __future__ import annotations

import unittest

from china_commodities.catalog import load_catalog


EXCHANGE_ORDER = ("DCE", "CZCE", "SHFE", "INE", "GFEX")

EXPECTED_NEW_OPTIONS = {
    ("DCE", "BZ"): "纯苯期权",
    ("DCE", "JM"): "焦煤期权",
    ("CZCE", "ZC"): "动力煤期权",
    ("CZCE", "PL"): "丙烯期权",
    ("SHFE", "AD"): "铸造铝合金期权",
    ("SHFE", "BU"): "石油沥青期权",
    ("SHFE", "FU"): "燃料油期权",
    ("SHFE", "SP"): "纸浆期权",
    ("SHFE", "OP"): "胶版印刷纸期权",
    ("INE", "BC"): "国际铜期权",
    ("INE", "NR"): "20号胶期权",
    ("GFEX", "PS"): "多晶硅",
    ("GFEX", "PT"): "铂",
    ("GFEX", "PD"): "钯",
}


class CatalogOptionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = load_catalog()

    def test_catalog_has_64_unique_option_products(self) -> None:
        pairs = [(item.exchange, item.product) for item in self.catalog.options]

        self.assertEqual(len(pairs), 64)
        self.assertEqual(len(set(pairs)), 64)

    def test_option_products_link_to_exchange_futures_and_names(self) -> None:
        for item in self.catalog.options:
            with self.subTest(exchange=item.exchange, product=item.product):
                self.assertIn(item.product, self.catalog.products_for_exchange(item.exchange))
                self.assertIn(item.product, self.catalog.names)
                self.assertTrue(item.symbol)

    def test_new_option_mappings_are_present(self) -> None:
        observed = {
            (item.exchange, item.product): item.symbol for item in self.catalog.options
        }

        self.assertEqual(
            {key: observed[key] for key in EXPECTED_NEW_OPTIONS},
            EXPECTED_NEW_OPTIONS,
        )

    def test_existing_representative_options_are_retained(self) -> None:
        observed = {(item.exchange, item.product) for item in self.catalog.options}

        for pair in (("SHFE", "CU"), ("INE", "SC"), ("GFEX", "LC"), ("DCE", "M")):
            with self.subTest(pair=pair):
                self.assertIn(pair, observed)

    def test_options_are_stably_ordered_by_exchange_and_product(self) -> None:
        exchange_rank = {exchange: rank for rank, exchange in enumerate(EXCHANGE_ORDER)}
        pairs = [(item.exchange, item.product) for item in self.catalog.options]
        expected = sorted(pairs, key=lambda pair: (exchange_rank[pair[0]], pair[1]))

        self.assertEqual(pairs, expected)


if __name__ == "__main__":
    unittest.main()
