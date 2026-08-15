"""Unit tests for the injectable AKShare adapter."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

import pandas as pd


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_DIR))

from china_commodities.collectors.akshare_adapter import (  # noqa: E402
    COMMODITY_EXCHANGES,
    akshare_version,
    collect_basis_daily,
    collect_contract_info,
    collect_dce_realtime_fallback,
    collect_futures_daily,
    collect_member_rankings,
    collect_option_daily,
    collect_option_volatility_daily,
    collect_warehouse_receipt,
)


class FakeAkShare:
    """Small fake module that records calls without network access."""

    __version__ = "9.9.9-test"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.frame = pd.DataFrame()

    def get_futures_daily(self, **kwargs: Any) -> pd.DataFrame:
        self.calls.append(("get_futures_daily", kwargs))
        return self.frame

    def futures_warehouse_receipt_dce(self, **kwargs: Any) -> Any:
        self.calls.append(("futures_warehouse_receipt_dce", kwargs))
        return {"route": "dce"}

    def futures_warehouse_receipt_czce(self, **kwargs: Any) -> Any:
        self.calls.append(("futures_warehouse_receipt_czce", kwargs))
        return {"route": "czce"}

    def futures_shfe_warehouse_receipt(self, **kwargs: Any) -> Any:
        self.calls.append(("futures_shfe_warehouse_receipt", kwargs))
        return {"route": "shfe"}

    def futures_gfex_warehouse_receipt(self, **kwargs: Any) -> Any:
        self.calls.append(("futures_gfex_warehouse_receipt", kwargs))
        return {"route": "gfex"}

    def futures_spot_price(self, **kwargs: Any) -> pd.DataFrame:
        self.calls.append(("futures_spot_price", kwargs))
        return self.frame

    def option_hist_dce(self, **kwargs: Any) -> pd.DataFrame:
        self.calls.append(("option_hist_dce", kwargs))
        return self.frame

    def option_hist_czce(self, **kwargs: Any) -> pd.DataFrame:
        self.calls.append(("option_hist_czce", kwargs))
        return self.frame

    def option_hist_shfe(self, **kwargs: Any) -> pd.DataFrame:
        self.calls.append(("option_hist_shfe", kwargs))
        return self.frame

    def option_hist_gfex(self, **kwargs: Any) -> pd.DataFrame:
        self.calls.append(("option_hist_gfex", kwargs))
        return self.frame

    def option_vol_shfe(self, **kwargs: Any) -> pd.DataFrame:
        self.calls.append(("option_vol_shfe", kwargs))
        return self.frame

    def futures_symbol_mark(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {"exchange": "大连商品交易所", "symbol": "铁矿石", "mark": "tks_qh"},
                {"exchange": "上海期货交易所", "symbol": "铜", "mark": "cu_qh"},
            ]
        )

    def futures_zh_realtime(self, symbol: str) -> pd.DataFrame:
        self.calls.append(("futures_zh_realtime", {"symbol": symbol}))
        return pd.DataFrame(
            [
                {"symbol": "I0", "exchange": "dce", "trade": 700, "settlement": 0, "presettlement": 690, "prevsettlement": 690, "open": 695, "high": 705, "low": 690, "volume": 100, "position": 200, "tradedate": "2026-08-14"},
                {"symbol": "I2609", "exchange": "dce", "trade": 701, "settlement": 0, "presettlement": 690, "prevsettlement": 690, "open": 695, "high": 705, "low": 690, "volume": 100, "position": 200, "tradedate": "2026-08-14"},
                {"symbol": "I2610", "exchange": "dce", "trade": 702, "settlement": 0, "presettlement": 691, "prevsettlement": 691, "open": 696, "high": 706, "low": 691, "volume": 101, "position": 201, "tradedate": "2026-08-14"},
            ]
        )

    def get_shfe_rank_table(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_shfe_rank_table", kwargs))
        return {}

    def get_dce_rank_table(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_dce_rank_table", kwargs))
        return {}

    def get_rank_table_czce(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_rank_table_czce", kwargs))
        return {}

    def futures_gfex_position_rank(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("futures_gfex_position_rank", kwargs))
        return {}

    def futures_contract_info_shfe(self, **kwargs: Any) -> pd.DataFrame:
        self.calls.append(("futures_contract_info_shfe", kwargs))
        return self.frame

    def futures_contract_info_ine(self, **kwargs: Any) -> pd.DataFrame:
        self.calls.append(("futures_contract_info_ine", kwargs))
        return self.frame

    def futures_contract_info_dce(self, **kwargs: Any) -> pd.DataFrame:
        self.calls.append(("futures_contract_info_dce", kwargs))
        return self.frame

    def futures_contract_info_czce(self, **kwargs: Any) -> pd.DataFrame:
        self.calls.append(("futures_contract_info_czce", kwargs))
        return self.frame

    def futures_contract_info_gfex(self, **kwargs: Any) -> pd.DataFrame:
        self.calls.append(("futures_contract_info_gfex", kwargs))
        return self.frame


class RaisingFakeAkShare(FakeAkShare):
    """Fake module used to prove adapter errors are propagated."""

    def get_futures_daily(self, **kwargs: Any) -> pd.DataFrame:
        self.calls.append(("get_futures_daily", kwargs))
        raise RuntimeError("upstream failure")


class AkShareAdapterTests(unittest.TestCase):
    def test_futures_normalizes_date_and_returns_empty_frame_unchanged(self) -> None:
        fake = FakeAkShare()

        result = collect_futures_daily("2026-08-14", "DCE", ak_module=fake)

        self.assertIs(result, fake.frame)
        self.assertEqual(
            fake.calls[-1],
            (
                "get_futures_daily",
                {"start_date": "20260814", "end_date": "20260814", "market": "DCE"},
            ),
        )

    def test_warehouse_receipt_routes_all_exchanges(self) -> None:
        expected = {
            "DCE": "futures_warehouse_receipt_dce",
            "CZCE": "futures_warehouse_receipt_czce",
            "SHFE": "futures_shfe_warehouse_receipt",
            "INE": "futures_shfe_warehouse_receipt",
            "GFEX": "futures_gfex_warehouse_receipt",
        }

        for exchange, function_name in expected.items():
            with self.subTest(exchange=exchange):
                fake = FakeAkShare()
                collect_warehouse_receipt("20260814", exchange, ak_module=fake)
                self.assertEqual(
                    fake.calls[-1],
                    (function_name, {"date": "20260814"}),
                )

    def test_basis_omits_vars_list_for_none_and_materializes_products(self) -> None:
        fake = FakeAkShare()

        collect_basis_daily("2026-08-14", ak_module=fake)
        self.assertEqual(fake.calls[-1], ("futures_spot_price", {"date": "20260814"}))

        collect_basis_daily("20260814", products=("铁矿石", "铜"), ak_module=fake)
        self.assertEqual(
            fake.calls[-1],
            ("futures_spot_price", {"date": "20260814", "vars_list": ["铁矿石", "铜"]}),
        )

    def test_option_routes_ine_to_shfe_and_normalizes_date(self) -> None:
        expected = {
            "DCE": "option_hist_dce",
            "CZCE": "option_hist_czce",
            "SHFE": "option_hist_shfe",
            "INE": "option_hist_shfe",
            "GFEX": "option_hist_gfex",
        }

        for exchange, function_name in expected.items():
            with self.subTest(exchange=exchange):
                fake = FakeAkShare()
                result = collect_option_daily(
                    "2026-08-14", exchange, "m2601", ak_module=fake
                )
                self.assertIs(result, fake.frame)
                self.assertEqual(
                    fake.calls[-1],
                    (
                        function_name,
                        {"symbol": "m2601", "trade_date": "20260814"},
                    ),
                )

    def test_unsupported_exchange_raises_value_error(self) -> None:
        fake = FakeAkShare()

        with self.assertRaises(ValueError):
            collect_futures_daily("20260814", "LME", ak_module=fake)
        with self.assertRaises(ValueError):
            collect_warehouse_receipt("20260814", "LME", ak_module=fake)
        with self.assertRaises(ValueError):
            collect_option_daily("20260814", "LME", "m2601", ak_module=fake)
        self.assertEqual(fake.calls, [])

    def test_upstream_exception_is_not_swallowed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "upstream failure"):
            collect_futures_daily("20260814", "SHFE", ak_module=RaisingFakeAkShare())

    def test_version_uses_injected_module(self) -> None:
        self.assertEqual(akshare_version(FakeAkShare()), "9.9.9-test")

    def test_supported_exchange_constant_is_fixed(self) -> None:
        self.assertEqual(COMMODITY_EXCHANGES, ("SHFE", "INE", "DCE", "CZCE", "GFEX"))

    def test_dce_fallback_drops_i0_but_keeps_concrete_september_and_october(self) -> None:
        result = collect_dce_realtime_fallback(
            "2026-08-14", ak_module=FakeAkShare(), max_workers=1
        )
        self.assertEqual(result["symbol"].tolist(), ["I2609", "I2610"])
        self.assertEqual(result["variety"].tolist(), ["I", "I"])
        self.assertEqual(result["close"].tolist(), [701, 702])
        self.assertTrue(result["symbol"].ne("I0").all())
        self.assertTrue(result["settle"].isna().all())
        self.assertEqual(result.attrs["product_coverage"], 1.0)

    def test_dce_fallback_rejects_other_trade_date(self) -> None:
        result = collect_dce_realtime_fallback(
            "2026-08-13", ak_module=FakeAkShare(), max_workers=1
        )
        self.assertTrue(result.empty)

    def test_member_ranking_routes_and_czce_omits_product_filter(self) -> None:
        expected = {
            "SHFE": ("get_shfe_rank_table", {"date": "20260814", "vars_list": ["CU"]}),
            "INE": ("get_shfe_rank_table", {"date": "20260814", "vars_list": ["CU"]}),
            "DCE": ("get_dce_rank_table", {"date": "20260814", "vars_list": ["CU"]}),
            "CZCE": ("get_rank_table_czce", {"date": "20260814"}),
            "GFEX": ("futures_gfex_position_rank", {"date": "20260814", "vars_list": ["CU"]}),
        }
        for exchange, call in expected.items():
            with self.subTest(exchange=exchange):
                fake = FakeAkShare()
                collect_member_rankings(
                    "2026-08-14", exchange, ["CU"], ak_module=fake
                )
                self.assertEqual(fake.calls[-1], call)

    def test_contract_info_date_semantics(self) -> None:
        expected = {
            "SHFE": ("futures_contract_info_shfe", {"date": "20260814"}),
            "INE": ("futures_contract_info_ine", {"date": "20260814"}),
            "DCE": ("futures_contract_info_dce", {}),
            "CZCE": ("futures_contract_info_czce", {"date": "20260814"}),
            "GFEX": ("futures_contract_info_gfex", {}),
        }
        for exchange, call in expected.items():
            with self.subTest(exchange=exchange):
                fake = FakeAkShare()
                collect_contract_info("2026-08-14", exchange, ak_module=fake)
                self.assertEqual(fake.calls[-1], call)

    def test_shfe_option_volatility_route(self) -> None:
        fake = FakeAkShare()
        collect_option_volatility_daily(
            "2026-08-14", "INE", "原油期权", ak_module=fake
        )
        self.assertEqual(
            fake.calls[-1],
            (
                "option_vol_shfe",
                {"symbol": "原油期权", "trade_date": "20260814"},
            ),
        )
        with self.assertRaises(ValueError):
            collect_option_volatility_daily(
                "2026-08-14", "DCE", "铁矿石期权", ak_module=fake
            )


if __name__ == "__main__":
    unittest.main()
