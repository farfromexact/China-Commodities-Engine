from __future__ import annotations

import unittest

import pandas as pd

from china_commodities.catalog import load_catalog
from china_commodities.normalize import (
    normalize_basis,
    normalize_contract_info,
    normalize_futures,
    normalize_member_rankings,
    normalize_option_series_volatility,
    normalize_options,
    normalize_warehouse,
)


class NormalizeTests(unittest.TestCase):
    def test_futures_keeps_close_and_settlement_returns_separate(self) -> None:
        raw = pd.DataFrame(
            [
                {
                    "symbol": "rb2610",
                    "variety": "RB",
                    "open": "3,000",
                    "high": 3100,
                    "low": 2990,
                    "close": 3080,
                    "settle": 3060,
                    "pre_settle": 3000,
                    "volume": "1,000",
                    "open_interest": "2,000",
                    "turnover": 3,
                }
            ]
        )
        item = normalize_futures(raw, "SHFE", "20260814")[0]
        self.assertEqual(item["contract"], "RB2610")
        self.assertAlmostEqual(item["close_return_pct"], 2.6666667)
        self.assertAlmostEqual(item["settle_return_pct"], 2.0)

    def test_futures_separates_shfe_and_ine_products(self) -> None:
        raw = pd.DataFrame(
            [
                {
                    "symbol": "CU2610",
                    "variety": "CU",
                    "close": 100,
                    "settle": 100,
                    "pre_settle": 99,
                },
                {
                    "symbol": "SC2610",
                    "variety": "SC",
                    "close": 500,
                    "settle": 500,
                    "pre_settle": 490,
                },
                {
                    "symbol": "EC2610",
                    "variety": "EC",
                    "close": 1200,
                    "settle": 1200,
                    "pre_settle": 1180,
                },
            ]
        )

        shfe = normalize_futures(raw, "SHFE", "20260814")
        ine = normalize_futures(raw, "INE", "20260814")

        self.assertEqual([item["product"] for item in shfe], ["CU"])
        self.assertEqual(
            [item["product"] for item in ine],
            ["EC", "SC"],
        )

    def test_proxy_basis_is_explicit(self) -> None:
        raw = pd.DataFrame(
            [{"symbol": "I", "spot_price": 700, "near_contract": "i2609", "near_contract_price": 710, "near_basis": 10}]
        )
        item = normalize_basis(raw, "2026-08-14")[0]
        self.assertEqual(item["basis_kind"], "proxy_basis")
        self.assertEqual(item["spot_source"], "100ppi")

    def test_warehouse_prefers_published_total(self) -> None:
        raw = {
            "SR": pd.DataFrame(
                [
                    {"仓库编号": "001", "仓单数量": 10, "当日增减": 1},
                    {"仓库编号": "总计", "仓单数量": 10, "当日增减": 1},
                ]
            )
        }
        item = normalize_warehouse(raw, "CZCE", "2026-08-14", load_catalog())[0]
        self.assertEqual(item["warehouse_quantity"], 10.0)
        self.assertEqual(item["aggregation_method"], "source_total")

    def test_option_contract_parts(self) -> None:
        raw = pd.DataFrame(
            [{"合约代码": "CU2609C100000", "成交量": 3, "持仓量": 4, "隐含波动率(%)": 25}]
        )
        item = normalize_options(raw, "SHFE", "CU", "铜期权", "2026-08-14")[0]
        self.assertEqual(item["underlying_contract"], "CU2609")
        self.assertEqual(item["option_type"], "C")
        self.assertEqual(item["strike"], 100000.0)

    def test_option_series_iv_converts_decimal_to_percent(self) -> None:
        raw = pd.DataFrame(
            [{"合约系列": "cu2609", "隐含波动率": 0.153875}]
        )
        self.assertAlmostEqual(
            normalize_option_series_volatility(raw)["CU2609"], 15.3875
        )

    def test_member_ranking_is_reported_distribution_not_client_direction(self) -> None:
        raw = {
            "cu2609": pd.DataFrame(
                [
                    {"symbol": "CU2609", "variety": "CU", "rank": 1, "vol": "1,000", "vol_chg": 2, "long_open_interest": 100, "long_open_interest_chg": 10, "short_open_interest": 80, "short_open_interest_chg": 5},
                    {"symbol": "CU2609", "variety": "CU", "rank": 2, "vol": 500, "vol_chg": -1, "long_open_interest": 50, "long_open_interest_chg": -2, "short_open_interest": 60, "short_open_interest_chg": 3},
                ]
            )
        }
        item = normalize_member_rankings(raw, "SHFE", "2026-08-14")[0]
        self.assertEqual(item["reported_long_open_interest"], 150.0)
        self.assertEqual(item["reported_short_open_interest"], 140.0)
        self.assertEqual(item["reported_net_long_minus_short"], 10.0)
        self.assertFalse(item["participant_direction_inferred"])

    def test_contract_info_parses_only_published_risk_parameters(self) -> None:
        raw = pd.DataFrame(
            [
                {
                    "产品名称": "鲜苹果期货",
                    "合约代码": "AP610",
                    "产品代码": "AP",
                    "最小变动价位": "1.00元/吨",
                    "最小变动价值": "10.00元",
                    "交易单位": "10吨/手",
                    "第一交易日": "2025-10-23",
                    "最后交易日待国家公布2025年节假日安排后进行调整": "2026-10-21",
                    "最后交割日": "2026-10-26",
                    "交易保证金率": "9.00%",
                    "涨跌停板": "±8%",
                }
            ]
        )
        item = normalize_contract_info(raw, "CZCE", "2026-08-14")[0]
        self.assertEqual(item["multiplier"], 10.0)
        self.assertEqual(item["tick_size"], 1.0)
        self.assertEqual(item["tick_value"], 10.0)
        self.assertEqual(item["margin_rate_percent"], 9.0)
        self.assertEqual(item["price_limit_percent"], 8.0)
        self.assertEqual(item["last_trading_day"], "2026-10-21")


if __name__ == "__main__":
    unittest.main()
