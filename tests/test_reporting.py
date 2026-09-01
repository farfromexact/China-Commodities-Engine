from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from china_commodities.reporting import (
    build_report_input,
    publish_report_input,
    reconcile_main_status,
)


class ReportingInputTests(unittest.TestCase):
    def _write(self, root: Path, relative: str, payload: dict) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_joins_market_state_and_per_series_option_quality(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write(
                root,
                "latest.json",
                {
                    "trade_date": "2026-08-20",
                    "generated_at": "2026-08-20T18:00:00+08:00",
                    "verified": True,
                    "scope_verified": True,
                    "source": {"provider": "ifind_http"},
                    "quality_metrics": {},
                    "futures_contracts": [{"contract": "I2701"}],
                    "warehouse_inventory": [],
                    "proxy_basis": [],
                    "member_rankings": [],
                },
            )
            self._write(
                root,
                "last_run_status.json",
                {
                    "run_date": "2026-08-20",
                    "generated_at": "2026-08-20T18:00:00+08:00",
                    "data_fresh": True,
                    "validation_errors": [],
                },
            )
            self._write(
                root,
                "market_state_latest.json",
                {
                    "trade_date": "2026-08-20",
                    "history_window": {"available_trading_days": 20},
                    "products": [
                        {
                            "exchange": "DCE",
                            "product": "I",
                            "product_name": "铁矿石",
                            "current_contract": "I2701",
                            "settlement_return_pct": {
                                "1D": {"value": 1.0},
                                "5D": {"value": 3.5},
                            },
                            "curve": {"current": 2.0, "zscore": 1.5},
                            "state_vector": {"price_momentum": {"score": 1}},
                            "quality": {"missing_metrics": [], "warnings": []},
                        }
                    ],
                },
            )
            for domain in ("physical", "external"):
                self._write(
                    root,
                    f"{domain}/latest.json",
                    {
                        "requested_date": "2026-08-20",
                        "generated_at": "2026-08-20T19:00:00+08:00",
                        "series": [],
                        "basis": [],
                        "import_parities": [],
                    },
                )
                self._write(
                    root,
                    f"{domain}/last_run_status.json",
                    {
                        "requested_date": "2026-08-20",
                        "generated_at": "2026-08-20T19:00:00+08:00",
                        "data_fresh": False,
                        "validation_passed": True,
                        "published": True,
                    },
                )
            self._write(
                root,
                "options/latest.json",
                {"trade_date": "2026-08-20", "record_count": 2, "coverage": {}},
            )
            self._write(
                root,
                "options/last_run_status.json",
                {"trade_date": "2026-08-20", "data_fresh": True},
            )
            self._write(
                root,
                "options/quality_latest.json",
                {
                    "trade_date": "2026-08-20",
                    "quality": {
                        "record_count": 2,
                        "product_coverage": 1.0,
                        "surface_ready": True,
                        "positioning_ready": False,
                        "execution_ready": False,
                        "iv_coverage": 1.0,
                        "open_interest_coverage": 0.5,
                        "bid_ask_coverage": 0.0,
                        "model_greeks_coverage": 0.0,
                        "limitations": ["positioning blocked"],
                    },
                },
            )
            self._write(
                root,
                "options/surface_latest.json",
                {
                    "trade_date": "2026-08-20",
                    "generated_at": "2026-08-20T21:00:00+08:00",
                    "series_count": 1,
                    "surface_ready_count": 1,
                    "positioning_ready_count": 0,
                    "execution_ready_count": 0,
                    "surfaces": [
                        {
                            "exchange": "DCE",
                            "product": "I",
                            "underlying_contract": "I2701",
                            "expiry_date": "2026-12-15",
                            "source_date_match_pct": 1.0,
                            "iv_coverage": 1.0,
                            "open_interest_coverage": 0.5,
                            "bid_ask_coverage": 0.0,
                            "surface_ready": True,
                            "positioning_ready": False,
                            "execution_ready": False,
                        }
                    ],
                },
            )
            self._write(root, "contract_meta.json", {"quality_state": "partial"})

            output = build_report_input(root)
            self.assertEqual(output["requested_date"], "2026-08-20")
            self.assertEqual(output["core_products"][0]["product"], "I")
            self.assertEqual(
                output["core_products"][0]["settlement_return_pct"]["5D"], 3.5
            )
            self.assertEqual(output["options"]["series_count"], 1)
            self.assertEqual(output["options"]["product_summaries"][0]["product"], "I")
            self.assertIn("options_positioning_not_ready", output["quality"]["limitations"])

            path = publish_report_input(root)
            self.assertEqual(path, root / "report_input_latest.json")
            self.assertTrue(path.exists())

    def test_exposes_timestamp_validated_night_session_separately_from_eod(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write(root, "latest.json", {"trade_date": "2026-08-25"})
            self._write(
                root,
                "night_session/latest.json",
                {
                    "trading_date": "2026-08-26",
                    "night_session_date": "2026-08-25",
                    "generated_at": "2026-08-26T06:03:00+08:00",
                    "session_window_start": "2026-08-25T20:00:00+08:00",
                    "session_window_end": "2026-08-26T03:45:00+08:00",
                    "coverage": {"night_session_contract_count": 1},
                    "records": [
                        {
                            "record_state": "night_session",
                            "trading_date": "2026-08-26",
                            "night_session_date": "2026-08-25",
                            "source_timestamp": "2026-08-26T02:30:00+08:00",
                            "exchange": "SHFE",
                            "product": "CU",
                            "contract": "CU2609",
                            "night_close": 80400,
                            "night_return_pct": 0.5,
                        },
                        {
                            "record_state": "outside_night_window",
                            "contract": "JD2609",
                        },
                    ],
                },
            )
            self._write(
                root,
                "night_session/last_run_status.json",
                {
                    "trading_date": "2026-08-26",
                    "data_fresh": True,
                    "validation_passed": True,
                    "published": True,
                },
            )

            output = build_report_input(root)

            # The report's canonical date remains the prior completed EOD;
            # the following trading day's night session is an explicit overlay.
            self.assertEqual(output["requested_date"], "2026-08-25")
            self.assertEqual(output["frequency"], "EOD+night_session")
            self.assertTrue(output["intraday"])
            self.assertEqual(output["night_session"]["records"][0]["contract"], "CU2609")
            self.assertEqual(len(output["night_session"]["records"]), 1)

    def test_reconciles_same_date_published_options_into_root_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write(root, "last_run_status.json", {
                "run_date": "2026-08-20",
                "generated_at": "2026-08-20T18:00:00+08:00",
                "module_quality": {
                    "options_chain": "not_collected",
                    "options_surface": "not_ready",
                },
                "quality_metrics": {},
                "modules": [
                    {
                        "dataset": "options",
                        "scope": "full-market",
                        "state": "skipped",
                    }
                ],
            })
            self._write(root, "latest.json", {
                "trade_date": "2026-08-20",
                "module_quality": {
                    "options_chain": "not_collected",
                    "options_surface": "not_ready",
                },
                "quality_metrics": {},
                "source": {
                    "modules": [
                        {
                            "dataset": "options",
                            "scope": "full-market",
                            "state": "skipped",
                        }
                    ]
                },
            })
            self._write(root, "options/last_run_status.json", {
                "trade_date": "2026-08-20",
                "generated_at": "2026-08-20T21:00:00+08:00",
                "data_fresh": True,
                "published": True,
                "quote_contract_count": 12,
                "coverage": {
                    "scope_complete": True,
                    "publish_eligible": True,
                },
            })
            self._write(root, "options/latest.json", {
                "trade_date": "2026-08-20",
                "record_count": 12,
            })
            self._write(root, "options/quality_latest.json", {
                "trade_date": "2026-08-20",
                "quality": {
                    "source_date_match_pct": 1.0,
                    "full_product_scope_verified": True,
                    "full_chain_verified": True,
                    "surface_ready": True,
                    "positioning_ready": False,
                    "execution_ready": False,
                },
            })

            report = build_report_input(root)
            self.assertEqual(
                report["quality"]["futures_status"]["module_quality"]["options_chain"],
                "verified_vendor_full_chain",
            )
            self.assertTrue(
                report["quality"]["futures_status"]["quality_metrics"]
                ["options_full_chain_verified"]
            )
            self.assertTrue(reconcile_main_status(root))
            self.assertFalse(reconcile_main_status(root))

            persisted = json.loads(
                (root / "last_run_status.json").read_text(encoding="utf-8")
            )
            option_module = next(
                item
                for item in persisted["modules"]
                if item["dataset"] == "options"
            )
            self.assertEqual(option_module["state"], "ok")
            self.assertTrue(option_module["is_fresh"])
            self.assertEqual(option_module["records"], 12)
            self.assertEqual(
                persisted["module_quality"]["options_surface"], "surface_ready"
            )
            latest = json.loads(
                (root / "latest.json").read_text(encoding="utf-8")
            )
            latest_option = next(
                item
                for item in latest["source"]["modules"]
                if item["dataset"] == "options"
            )
            self.assertEqual(latest_option["state"], "ok")
            self.assertEqual(
                latest["module_quality"]["options_chain"],
                "verified_vendor_full_chain",
            )


if __name__ == "__main__":
    unittest.main()
