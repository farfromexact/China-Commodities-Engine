from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from china_commodities.reporting import build_report_input, publish_report_input


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


if __name__ == "__main__":
    unittest.main()
