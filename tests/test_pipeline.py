from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from china_commodities.models import ModuleStatus, PipelineResult
from china_commodities.pipeline import _merge_previous_auxiliary, run_pipeline


class FakeAkshare:
    __version__ = "test"

    PRODUCTS = {
        "SHFE": ("RB", "RB2610", "RB2701"),
        "INE": ("SC", "SC2610", "SC2612"),
        "DCE": ("I", "I2609", "I2701"),
        "CZCE": ("SR", "SR609", "SR701"),
        "GFEX": ("LC", "LC2609", "LC2611"),
    }

    def __init__(self, fail_dce: bool = False) -> None:
        self.fail_dce = fail_dce

    def get_futures_daily(self, start_date: str, end_date: str, market: str) -> pd.DataFrame:
        if market == "DCE" and self.fail_dce:
            raise RuntimeError("DCE unavailable")
        product, near, deferred = self.PRODUCTS[market]
        return pd.DataFrame(
            [
                {"symbol": near, "date": start_date, "variety": product, "open": 100, "high": 110, "low": 99, "close": 108, "settle": 107, "pre_settle": 100, "volume": 1000, "open_interest": 2000, "turnover": 10000},
                {"symbol": deferred, "date": start_date, "variety": product, "open": 99, "high": 105, "low": 98, "close": 102, "settle": 101, "pre_settle": 99, "volume": 500, "open_interest": 1000, "turnover": 5000},
            ]
        )

    def futures_shfe_warehouse_receipt(self, date: str):
        return {"RB": pd.DataFrame([{"WRTWGHTS": 100, "WRTCHANGE": -1, "ROWSTATUS": 1}])}

    def futures_warehouse_receipt_dce(self, date: str):
        return pd.DataFrame([{"品种代码": "I", "今日仓单量（手）": 10, "增减（手）": 1}])

    def futures_warehouse_receipt_czce(self, date: str):
        return {"SR": pd.DataFrame([{"仓库编号": "总计", "仓单数量": 20, "当日增减": 2}])}

    def futures_gfex_warehouse_receipt(self, date: str):
        return {"LC": pd.DataFrame([{"今日仓单量": 30, "增减": 3}])}

    def futures_spot_price(self, date: str, vars_list: list[str]) -> pd.DataFrame:
        return pd.DataFrame(
            [{"symbol": "I", "spot_price": 100, "near_contract": "I2609", "near_contract_price": 108, "dominant_contract": "I2609", "dominant_contract_price": 108, "near_basis": 8, "dom_basis": 8}]
        )

    def option_hist_dce(self, symbol: str, trade_date: str) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {"合约": "C2609-C-100", "成交量": 10, "持仓量": 20, "隐含波动率(%)": 20},
                {"合约": "C2609-P-100", "成交量": 20, "持仓量": 30, "隐含波动率(%)": 22},
            ]
        )


class FakeAkshareWithDCEUniverse(FakeAkshare):
    def futures_symbol_mark(self) -> pd.DataFrame:
        return pd.DataFrame(
            [{"exchange": "大连商品交易所", "symbol": "铁矿石"}]
        )

    def futures_zh_realtime(self, symbol: str) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "symbol": "I2609",
                    "exchange": "dce",
                    "trade": 108,
                    "open": 100,
                    "high": 110,
                    "low": 99,
                    "volume": 1000,
                    "position": 2000,
                    "tradedate": "2026-08-14",
                    "prevsettlement": 100,
                    "settlement": 0,
                },
                {
                    "symbol": "I2701",
                    "exchange": "dce",
                    "trade": 102,
                    "open": 99,
                    "high": 105,
                    "low": 98,
                    "volume": 500,
                    "position": 1000,
                    "tradedate": "2026-08-14",
                    "prevsettlement": 99,
                    "settlement": 0,
                },
            ]
        )


class FakeIFindHTTPClient:
    def history_quotes(self, codes, fields, trade_date):
        rows = []
        for code in codes:
            near = str(code).startswith("I2609")
            rows.append(
                {
                    "thscode": code,
                    "time": trade_date,
                    "open": 100 if near else 99,
                    "high": 110 if near else 105,
                    "low": 99 if near else 98,
                    "close": 108 if near else 102,
                    "settlement": 107 if near else 101,
                    "preSettlement": 100 if near else 99,
                    "volume": 1000 if near else 500,
                    "amount": 10000 if near else 5000,
                    "openInterest": 2000 if near else 1000,
                }
            )
        return pd.DataFrame(rows)


class FailingIFindHTTPClient:
    def history_quotes(self, codes, fields, trade_date):
        raise RuntimeError("iFinD unavailable")


class PrimaryIFindHTTPClient:
    TARGETS = {
        "SHF": "RB2610.SHF",
        "INE": "SC2610.INE",
        "DCE": "I2609.DCE",
        "CZC": "SR609.CZC",
        "GFE": "LC2609.GFE",
    }

    def history_quotes(self, codes, fields, trade_date):
        rows = []
        for suffix, target in self.TARGETS.items():
            if target in codes:
                rows.append(
                    {
                        "thscode": target,
                        "time": trade_date,
                        "open": 100,
                        "high": 110,
                        "low": 99,
                        "close": 108,
                        "settlement": 107,
                        "preSettlement": 100,
                        "volume": 1000,
                        "amount": 10000,
                        "openInterest": 2000,
                    }
                )
        return pd.DataFrame(rows)


class PipelineTests(unittest.TestCase):
    def test_ifind_primary_collects_all_exchanges_without_akshare_modules(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            result = run_pipeline(
                "2026-08-14",
                data_dir=directory,
                include_options=False,
                provider="ifind",
                include_official_auxiliary=False,
                ifind_http_client=PrimaryIFindHTTPClient(),
            )

            self.assertTrue(result.verified, result.validation_errors)
            self.assertEqual(result.primary_provider, "ifind")
            self.assertEqual(result.akshare_version, "not_used")
            self.assertEqual(len(result.futures_records), 5)
            self.assertFalse(result.core_futures_official_complete)
            self.assertEqual(
                result.module_quality["futures"], "verified_vendor_primary"
            )
            self.assertEqual(result.module_quality["warehouse"], "unavailable")
            self.assertTrue(
                all(
                    status.upstream_source == "iFinD Quant API"
                    for status in result.statuses
                )
            )
            payload = json.loads(
                (Path(directory) / "latest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["source"]["provider"], "ifind")

    def test_explicit_ifind_dce_failure_does_not_promote_sina_quotes(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            result = run_pipeline(
                "2026-08-14",
                data_dir=directory,
                include_options=False,
                ak_module=FakeAkshareWithDCEUniverse(fail_dce=True),
                ifind_dce_fallback=True,
                ifind_http_client=FailingIFindHTTPClient(),
            )

            self.assertFalse(result.verified)
            dce_status = next(
                status
                for status in result.statuses
                if status.dataset == "futures" and status.scope == "DCE"
            )
            self.assertEqual(dce_status.state, "error")
            self.assertIn("iFinD fallback failed", dce_status.error or "")
            self.assertFalse((Path(directory) / "latest.json").exists())

    def test_ifind_dce_eod_fallback_restores_fresh_full_market(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            result = run_pipeline(
                "2026-08-14",
                data_dir=directory,
                include_options=False,
                ak_module=FakeAkshareWithDCEUniverse(fail_dce=True),
                ifind_dce_fallback=True,
                ifind_http_client=FakeIFindHTTPClient(),
            )

            self.assertTrue(result.verified, result.validation_errors)
            dce_status = next(
                status
                for status in result.statuses
                if status.dataset == "futures" and status.scope == "DCE"
            )
            self.assertEqual(dce_status.upstream_source, "iFinD Quant API")
            self.assertEqual(dce_status.source_function, "cmd_history_quotation")
            self.assertTrue(dce_status.is_fallback)
            self.assertTrue(dce_status.is_fresh)
            self.assertTrue(dce_status.source_date_match)
            self.assertFalse(result.core_futures_official_complete)
            self.assertEqual(
                len(
                    [
                        record
                        for record in result.futures_records
                        if record["exchange"] == "DCE"
                    ]
                ),
                2,
            )

    def test_failed_auxiliary_modules_carry_forward_previous_valid_records(self) -> None:
        statuses = [
            ModuleStatus(
                dataset=dataset,
                scope=scope,
                state="error",
                trade_date="2026-08-14",
                source_function="test",
            )
            for dataset, scope in (
                ("contract_info", "SHFE"),
                ("warehouse", "SHFE"),
                ("basis", "100PPI"),
                ("member_rankings", "SHFE"),
                ("options", "SHFE:CU"),
            )
        ]
        result = PipelineResult(
            trade_date="2026-08-14",
            generated_at="2026-08-14T18:15:00+08:00",
            akshare_version="test",
            statuses=statuses,
            futures_records=[
                {"exchange": "SHFE", "product": "CU", "contract": "CU2610"}
            ],
        )
        previous_snapshot = {
            "trade_date": "2026-08-14",
            "warehouse_inventory": [
                {"trade_date": "2026-08-14", "exchange": "SHFE", "product": "CU"}
            ],
            "proxy_basis": [
                {"trade_date": "2026-08-14", "product": "CU"}
            ],
            "member_rankings": [
                {
                    "trade_date": "2026-08-14",
                    "exchange": "SHFE",
                    "product": "CU",
                    "contract": "CU2610",
                    "ranking_scope": "contract",
                    "ranking_reconciled": True,
                }
            ],
            "commodity_options": [
                {
                    "trade_date": "2026-08-14",
                    "exchange": "SHFE",
                    "product": "CU",
                    "source_symbol": "铜期权",
                    "contract_count": 100,
                }
            ],
        }
        previous_contract_meta = {
            "trade_date": "2026-08-14",
            "contracts": [
                {
                    "exchange": "SHFE",
                    "product": "CU",
                    "contract": "CU2610",
                    "multiplier": 5,
                    "tick_size": 10,
                    "tick_value": 50,
                    "last_trading_day": "2026-10-15",
                    "metadata_status": "official_partial",
                }
            ],
        }

        _merge_previous_auxiliary(
            result, previous_snapshot, previous_contract_meta
        )

        self.assertTrue(result.warehouse_records[0]["carried_forward"])
        self.assertFalse(result.warehouse_records[0]["is_stale"])
        self.assertTrue(result.basis_records[0]["carried_forward"])
        self.assertTrue(result.member_ranking_summaries[0]["carried_forward"])
        self.assertTrue(result.option_summaries[0]["carried_forward"])
        self.assertEqual(result.option_summaries[0]["contract_count"], 100)
        self.assertEqual(
            result.contract_metadata[0]["metadata_status"],
            "carried_forward_previous_valid",
        )

    def test_repeated_same_day_run_does_not_rewrite_timestamp_only_changes(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            first_now = datetime(2026, 8, 14, 18, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
            second_now = datetime(2026, 8, 14, 19, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
            run_pipeline(
                "2026-08-14",
                data_dir=directory,
                include_options=False,
                ak_module=FakeAkshare(),
                now=first_now,
            )
            root = Path(directory)
            paths = (
                root / "last_run_status.json",
                root / "latest.json",
                root / "radar_latest.json",
                root / "market_state_latest.json",
                root / "contract_meta.json",
                root / "radar_history.json",
                root / "snapshots" / "2026-08-14.json",
            )
            before = {path: path.read_bytes() for path in paths}

            run_pipeline(
                "2026-08-14",
                data_dir=directory,
                include_options=False,
                ak_module=FakeAkshare(),
                now=second_now,
            )

            self.assertEqual(before, {path: path.read_bytes() for path in paths})

    def test_repeated_same_day_run_updates_latest_without_duplicating_history(self) -> None:
        class RevisedSameDayAkshare(FakeAkshare):
            def get_futures_daily(
                self, start_date: str, end_date: str, market: str
            ) -> pd.DataFrame:
                frame = super().get_futures_daily(start_date, end_date, market)
                frame["close"] = frame["close"] + 1
                frame["settle"] = frame["settle"] + 1
                return frame

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            first_now = datetime(2026, 8, 14, 6, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            second_now = datetime(2026, 8, 14, 18, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
            run_pipeline(
                "2026-08-14",
                data_dir=directory,
                include_options=False,
                ak_module=FakeAkshare(),
                now=first_now,
            )
            run_pipeline(
                "2026-08-14",
                data_dir=directory,
                include_options=False,
                ak_module=RevisedSameDayAkshare(),
                now=second_now,
            )

            root = Path(directory)
            latest = json.loads((root / "latest.json").read_text(encoding="utf-8"))
            snapshot = json.loads(
                (root / "snapshots" / "2026-08-14.json").read_text(encoding="utf-8")
            )
            history = json.loads(
                (root / "radar_history.json").read_text(encoding="utf-8")
            )

            latest_rb = next(
                record
                for record in latest["futures_contracts"]
                if record["contract"] == "RB2610"
            )
            self.assertEqual(latest["generated_at"], second_now.isoformat())
            self.assertEqual(latest_rb["close"], 109)
            self.assertEqual(snapshot, latest)
            self.assertEqual(len(history["records"]), 1)
            self.assertEqual(history["records"][0]["trade_date"], "2026-08-14")

    def test_verified_run_publishes_expected_artifacts(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            result = run_pipeline(
                "2026-08-14",
                data_dir=directory,
                option_limit=1,
                ak_module=FakeAkshare(),
            )
            self.assertTrue(result.verified, result.validation_errors)
            self.assertEqual(len(result.futures_records), 10)
            self.assertTrue(result.core_futures_official_complete)
            self.assertFalse(result.scope_official_complete)
            self.assertEqual(result.quality_metrics["source_date_match_pct"], 100.0)
            futures_status = next(
                status
                for status in result.statuses
                if status.dataset == "futures" and status.scope == "SHFE"
            )
            self.assertTrue(futures_status.source_date_match)
            self.assertEqual(futures_status.source_trade_date, "2026-08-14")
            self.assertEqual(len(futures_status.raw_payload_sha256 or ""), 64)
            self.assertTrue((Path(directory) / "latest.json").exists())
            self.assertTrue((Path(directory) / "radar_history.json").exists())
            self.assertTrue((Path(directory) / "market_state_latest.json").exists())
            self.assertTrue((Path(directory) / "raw" / "2026-08-14" / "commodity_options.json").exists())
            payload = json.loads((Path(directory) / "latest.json").read_text(encoding="utf-8"))
            self.assertTrue(payload["verified"])
            self.assertEqual(payload["trade_date"], "2026-08-14")
            market_state = json.loads(
                (Path(directory) / "market_state_latest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(market_state["trade_date"], "2026-08-14")
            self.assertTrue(market_state["quality"]["exact_contract_only"])

    def test_partial_failure_preserves_no_false_snapshot(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            result = run_pipeline(
                "2026-08-14",
                data_dir=directory,
                include_options=False,
                ak_module=FakeAkshare(fail_dce=True),
            )
            self.assertFalse(result.verified)
            self.assertTrue(any("DCE futures not fresh" in error for error in result.validation_errors))
            self.assertTrue((Path(directory) / "last_run_status.json").exists())
            self.assertFalse((Path(directory) / "latest.json").exists())

    def test_scoped_run_excludes_dce_without_claiming_full_market(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            result = run_pipeline(
                "2026-08-14",
                data_dir=directory,
                include_options=False,
                exchanges=("SHFE", "INE", "CZCE", "GFEX"),
                ak_module=FakeAkshare(fail_dce=True),
            )

            self.assertTrue(result.scope_verified, result.validation_errors)
            self.assertFalse(result.verified)
            self.assertTrue(result.core_futures_official_complete)
            self.assertFalse(result.scope_official_complete)
            self.assertFalse(result.official_complete)
            self.assertEqual(result.scope_id, "ex-dce")
            self.assertFalse(
                any(status.scope == "DCE" for status in result.statuses)
            )

            scoped = Path(directory) / "scoped" / "ex-dce"
            self.assertTrue((scoped / "latest.json").exists())
            self.assertTrue((scoped / "radar_latest.json").exists())
            self.assertFalse((Path(directory) / "latest.json").exists())

            payload = json.loads(
                (scoped / "latest.json").read_text(encoding="utf-8")
            )
            self.assertFalse(payload["verified"])
            self.assertTrue(payload["scope_verified"])
            self.assertTrue(payload["core_futures_official_complete"])
            self.assertFalse(payload["scope_official_complete"])
            self.assertEqual(payload["coverage_scope"]["excluded_exchanges"], ["DCE"])
            self.assertTrue(payload["quality_metrics"]["coverage_penalty"])

            status = json.loads(
                (scoped / "last_run_status.json").read_text(encoding="utf-8")
            )
            self.assertFalse(status["data_fresh"])
            self.assertTrue(status["scope_data_fresh"])


if __name__ == "__main__":
    unittest.main()
