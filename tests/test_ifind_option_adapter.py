from __future__ import annotations

import unittest

import pandas as pd

from china_commodities.collectors.ifind_option_adapter import (
    ExchangeOptionUniverseConfig,
    IFindOptionDataError,
    IFindOptionReportConfig,
    collect_option_eod_from_exchange_universe,
    collect_option_eod_snapshot,
    normalize_ifind_option_rows,
    option_contract_to_ifind_code,
)


FIELD_MAP = {
    "contract": "optionCode",
    "trade_date": "tradeDate",
    "underlying_contract": "underlyingCode",
    "option_type": "callPut",
    "strike": "strikePrice",
    "expiry_date": "expiryDate",
    "settle": "settlement",
    "volume": "volume",
    "open_interest": "openInterest",
    "underlying_settle": "underlyingSettlement",
    "iv_percent": "impliedVolatility",
    "delta": "delta",
    "gamma": "gamma",
    "vega": "vega",
    "theta": "theta",
    "rho": "rho",
}


def report_config(**overrides) -> IFindOptionReportConfig:
    value = {
        "report_name": "verified-report",
        "function_parameters": {"date": "{trade_date_compact}"},
        "output_parameters": {"all": "Y"},
        "exchange": "SHFE",
        "product": "CU",
        "exercise_style": "european",
        "risk_free_rate": 0.02,
        "risk_free_rate_source": "test_input",
        "iv_input_unit": "percent",
        "quote_mode": "data_pool_only",
        "field_map": FIELD_MAP,
    }
    value.update(overrides)
    return IFindOptionReportConfig.from_dict(value)


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, endpoint, payload):
        self.calls.append((endpoint, payload))
        return self.response


class IFindOptionAdapterTests(unittest.TestCase):
    def test_converts_exchange_contract_to_ifind_code(self) -> None:
        self.assertEqual(
            option_contract_to_ifind_code("CU2609C100000", "SHFE"),
            "CU2609C100000.SHF",
        )
        self.assertEqual(
            option_contract_to_ifind_code("m2609-p-2500", "DCE"),
            "M2609P2500.DCE",
        )
        self.assertEqual(
            option_contract_to_ifind_code("A2611-MS-C-4250", "DCE"),
            "A2611MSC4250.DCE",
        )
        self.assertEqual(
            option_contract_to_ifind_code("SR611MSC4700", "CZCE"),
            "SR611MSC4700.CZC",
        )

    def test_normalizes_vendor_and_model_greeks_separately(self) -> None:
        rows = [
            {
                "optionCode": "CU2610-C-80000.SHF",
                "tradeDate": "20260818",
                "underlyingCode": "CU2610",
                "callPut": "C",
                "strikePrice": 80000,
                "expiryDate": "2026-09-24",
                "settlement": 1800,
                "volume": 100,
                "openInterest": 200,
                "underlyingSettlement": 80500,
                "impliedVolatility": 22.5,
                "delta": 0.55,
                "gamma": 0.0001,
            }
        ]
        records = normalize_ifind_option_rows(
            rows, trade_date="2026-08-18", config=report_config()
        )
        greeks = records[0]["greeks"]
        self.assertEqual(greeks["quality"], "vendor_and_model")
        self.assertEqual(greeks["selected_source"], "model")
        self.assertEqual(greeks["vendor"]["delta"], 0.55)
        self.assertEqual(greeks["model"]["model"], "black_76")
        self.assertFalse(greeks["dealer_position_direction_known"])

    def test_unknown_exercise_style_keeps_vendor_greeks_only(self) -> None:
        config = report_config(exercise_style=None)
        rows = [
            {
                "optionCode": "CU2610-P-80000.SHF",
                "tradeDate": "2026-08-18",
                "underlyingCode": "CU2610",
                "callPut": "P",
                "strikePrice": 80000,
                "impliedVolatility": 24.0,
                "delta": -0.45,
            }
        ]
        record = normalize_ifind_option_rows(
            rows, trade_date="2026-08-18", config=config
        )[0]
        self.assertEqual(record["exercise_style"], "unknown")
        self.assertEqual(record["greeks"]["quality"], "vendor_reported")
        self.assertIsNone(record["greeks"]["model"])

    def test_decimal_iv_is_normalized_to_percent(self) -> None:
        config = report_config(iv_input_unit="decimal", exercise_style=None)
        row = {
            "optionCode": "CU2610-C-80000.SHF",
            "tradeDate": "2026-08-18",
            "underlyingCode": "CU2610",
            "callPut": "C",
            "strikePrice": 80000,
            "impliedVolatility": 0.225,
        }
        record = normalize_ifind_option_rows(
            [row], trade_date="2026-08-18", config=config
        )[0]
        self.assertEqual(record["iv_percent"], 22.5)

    def test_ambiguous_iv_unit_is_rejected(self) -> None:
        with self.assertRaisesRegex(IFindOptionDataError, "iv_input_unit"):
            IFindOptionReportConfig.from_dict(
                {
                    "report_name": "x",
                    "field_map": {
                        "contract": "optionCode",
                        "iv_percent": "impliedVolatility",
                    },
                }
            )

    def test_stale_report_date_fails_closed(self) -> None:
        with self.assertRaisesRegex(IFindOptionDataError, "stale date"):
            normalize_ifind_option_rows(
                [{"optionCode": "CU2610-C-80000.SHF", "tradeDate": "20260817"}],
                trade_date="2026-08-18",
                config=report_config(),
            )

    def test_collects_data_pool_and_expands_trade_date(self) -> None:
        client = FakeClient(
            {
                "errorcode": 0,
                "tables": [
                    {
                        "table": {
                            "optionCode": ["CU2610-C-80000.SHF"],
                            "tradeDate": ["20260818"],
                            "underlyingCode": ["CU2610"],
                            "callPut": ["C"],
                            "strikePrice": [80000],
                            "expiryDate": ["2026-09-24"],
                            "settlement": [1800],
                            "volume": [100],
                            "openInterest": [200],
                            "underlyingSettlement": [80500],
                            "impliedVolatility": [22.5],
                            "delta": [0.55],
                        }
                    }
                ],
            }
        )
        result = collect_option_eod_snapshot(
            "2026-08-18", client=client, reports=[report_config()]
        )
        self.assertFalse(result["intraday"])
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(client.calls[0][0], "data_pool")
        self.assertEqual(
            client.calls[0][1]["functionpara"]["date"], "20260818"
        )

    def test_enriches_contract_universe_with_standard_realtime_greeks(self) -> None:
        class RealtimeClient:
            def __init__(self):
                self.calls = []

            def request(self, endpoint, payload):
                self.calls.append((endpoint, payload))
                if endpoint == "data_pool":
                    return {
                        "errorcode": 0,
                        "tables": [
                            {
                                "table": {
                                    "optionCode": ["CU2610-C-80000.SHF"],
                                    "underlyingCode": ["CU2610"],
                                    "callPut": ["C"],
                                    "strikePrice": [80000],
                                    "expiryDate": ["2026-09-24"],
                                }
                            }
                        ],
                    }
                return {
                    "errorcode": 0,
                    "tables": [
                        {
                            "thscode": "CU2610-C-80000.SHF",
                            "table": {
                                "latest": [1800],
                                "settlement": [1750],
                                "volume": [100],
                                "openInterest": [200],
                                "impliedVolatility": [0.225],
                                "delta": [0.55],
                            },
                        }
                    ],
                }

        config = report_config(
            quote_mode="real_time_enrich",
            iv_input_unit="decimal",
        )
        client = RealtimeClient()
        result = collect_option_eod_snapshot(
            "2026-08-18", client=client, reports=[config]
        )
        record = result["records"][0]
        self.assertEqual(record["settle"], 1750.0)
        self.assertEqual(record["iv_percent"], 22.5)
        self.assertEqual(
            record["source_endpoint"], "data_pool+real_time_quotation"
        )
        self.assertEqual(client.calls[1][0], "real_time_quotation")
        self.assertIn("impliedVolatility", client.calls[1][1]["indicators"])

    def test_collects_exchange_directory_then_ifind_quotes(self) -> None:
        class FakeAkshare:
            @staticmethod
            def option_hist_shfe(symbol, trade_date):
                self.assertEqual(symbol, "铜期权")
                self.assertEqual(trade_date, "20260819")
                return pd.DataFrame(
                    {
                        "合约代码": ["CU2609C80000", "CU2609P80000"],
                        "结算价": [1800, 1700],
                        "成交量": [100, 80],
                        "持仓量": [200, 160],
                    }
                )

        class ExchangeQuoteClient:
            def __init__(self):
                self.calls = []

            def request(self, endpoint, payload):
                self.calls.append((endpoint, payload))
                codes = payload["codes"].split(",")
                if codes != ["CU2609.SHF"]:
                    tables = []
                    for code in codes:
                        is_call = "C80000" in code
                        tables.append(
                            {
                                "thscode": code,
                                "time": "2026-08-19 15:00:00",
                                "table": {
                                    "latest": [1800 if is_call else 1700],
                                    "settlement": [1750 if is_call else 1650],
                                    "volume": [100 if is_call else 80],
                                    "openInterest": [200 if is_call else 160],
                                    "impliedVolatility": [0.225 if is_call else 0.24],
                                    "delta": [0.55 if is_call else -0.45],
                                    "gamma": [0.0001],
                                },
                            }
                        )
                    return {"errorcode": 0, "tables": tables}
                return {
                    "errorcode": 0,
                    "tables": [
                        {
                            "thscode": "CU2609.SHF",
                            "time": "2026-08-19 15:00:00",
                            "table": {"settlement": [80500]},
                        }
                    ],
                }

        client = ExchangeQuoteClient()
        result = collect_option_eod_from_exchange_universe(
            "2026-08-19",
            client=client,
            universes=[
                ExchangeOptionUniverseConfig(
                    exchange="SHFE",
                    product="CU",
                    symbol="铜期权",
                )
            ],
            ak_module=FakeAkshare(),
        )

        self.assertEqual(result["universe_contract_count"], 2)
        self.assertEqual(result["quote_contract_count"], 2)
        self.assertTrue(result["quote_coverage_complete"])
        self.assertEqual(result["records"][0]["underlying_settle"], 80500.0)
        self.assertEqual(result["records"][0]["iv_percent"], 22.5)
        self.assertEqual(
            result["records"][0]["source_endpoint"],
            "exchange_directory+real_time_quotation",
        )
        self.assertEqual(
            result["records"][0]["greeks"]["quality"], "vendor_reported"
        )
        self.assertFalse(
            result["records"][0]["greeks"]["dealer_position_direction_known"]
        )
        self.assertEqual(client.calls[0][0], "real_time_quotation")
        self.assertEqual(
            set(client.calls[0][1]["codes"].split(",")),
            {"CU2609C80000.SHF", "CU2609P80000.SHF"},
        )
        self.assertEqual(client.calls[1][1]["codes"], "CU2609.SHF")

    def test_exchange_directory_fails_on_incomplete_ifind_coverage(self) -> None:
        class FakeAkshare:
            @staticmethod
            def option_hist_shfe(symbol, trade_date):
                return pd.DataFrame(
                    {"合约代码": ["CU2609C80000", "CU2609P80000"]}
                )

        class MissingQuoteClient:
            def request(self, endpoint, payload):
                return {
                    "errorcode": 0,
                    "tables": [
                        {
                            "thscode": "CU2609C80000.SHF",
                            "time": "2026-08-19 15:00:00",
                            "table": {"settlement": [1750]},
                        }
                    ],
                }

        with self.assertRaisesRegex(IFindOptionDataError, "coverage incomplete"):
            collect_option_eod_from_exchange_universe(
                "2026-08-19",
                client=MissingQuoteClient(),
                universes=[
                    ExchangeOptionUniverseConfig(
                        exchange="SHFE", product="CU", symbol="铜期权"
                    )
                ],
                ak_module=FakeAkshare(),
            )

    def test_exchange_directory_rejects_stale_ifind_quote(self) -> None:
        class FakeAkshare:
            @staticmethod
            def option_hist_shfe(symbol, trade_date):
                return pd.DataFrame({"合约代码": ["CU2609C80000"]})

        class StaleQuoteClient:
            def request(self, endpoint, payload):
                return {
                    "errorcode": 0,
                    "tables": [
                        {
                            "thscode": "CU2609C80000.SHF",
                            "time": "2026-08-18 15:00:00",
                            "table": {"settlement": [1750]},
                        }
                    ],
                }

        with self.assertRaisesRegex(IFindOptionDataError, "source date 2026-08-18"):
            collect_option_eod_from_exchange_universe(
                "2026-08-19",
                client=StaleQuoteClient(),
                universes=[
                    ExchangeOptionUniverseConfig(
                        exchange="SHFE", product="CU", symbol="铜期权"
                    )
                ],
                ak_module=FakeAkshare(),
            )

    def test_missing_report_mapping_is_explicit(self) -> None:
        with self.assertRaisesRegex(IFindOptionDataError, "field_map"):
            IFindOptionReportConfig.from_dict(
                {"report_name": "x", "field_map": {}}
            )


if __name__ == "__main__":
    unittest.main()
