from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from china_commodities.option_storage import (
    OptionSnapshotValidationError,
    publish_option_eod,
)
from china_commodities.storage import read_json


def snapshot(trade_date: str, contract_suffix: str = "") -> dict:
    return {
        "schema_version": 1,
        "trade_date": trade_date,
        "generated_at": f"{trade_date}T17:10:00+08:00",
        "source_provider": "ifind_http",
        "universe_source": "ifind_verified_report",
        "universe_contract_count": 2,
        "quote_contract_count": 2,
        "quote_coverage_complete": True,
        "records": [
            {
                "trade_date": trade_date,
                "exchange": "SHFE",
                "product": "CU",
                "contract": f"CU2610-C-80000{contract_suffix}",
                "underlying_contract": "CU2610",
                "expiry_date": "2026-09-24",
                "option_type": "C",
                "strike": 80000.0,
                "settle": 1200.0,
                "volume": 10,
                "open_interest": 20,
                "underlying_settle": 80100.0,
                "source_provider": "ifind_http",
                "source_trade_date": trade_date,
                "source_date_match": True,
                "greeks": {
                    "quality": "model_derived",
                    "selected": {"iv_percent": 22.0, "delta": 0.51},
                },
            },
            {
                "trade_date": trade_date,
                "exchange": "SHFE",
                "product": "CU",
                "contract": f"CU2610-P-80000{contract_suffix}",
                "underlying_contract": "CU2610",
                "expiry_date": "2026-09-24",
                "option_type": "P",
                "strike": 80000.0,
                "settle": 1100.0,
                "volume": 20,
                "open_interest": 40,
                "underlying_settle": 80100.0,
                "source_provider": "ifind_http",
                "source_trade_date": trade_date,
                "source_date_match": True,
                "greeks": {
                    "quality": "vendor_reported",
                    "selected": {"iv_percent": 24.0, "delta": -0.49},
                },
            },
        ],
    }


class OptionStorageTests(unittest.TestCase):
    def test_publishes_chain_and_compact_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            publish_option_eod(snapshot("2026-08-18"), data_dir)
            latest = read_json(data_dir / "options" / "latest.json")
            history = read_json(data_dir / "options" / "history.json")
            quality = read_json(data_dir / "options" / "quality_latest.json")
            self.assertEqual(len(latest["records"]), 2)
            self.assertTrue(latest["quality"]["full_chain_verified"])
            self.assertEqual(quality["quality"]["status"], "surface_ready")
            series = history["records"][0]["series"][0]
            self.assertEqual(series["put_call_volume_ratio"], 2.0)
            self.assertEqual(series["atm_iv_percent"], 23.0)
            self.assertFalse(series["dealer_gamma_known"])

    def test_enforces_full_chain_and_summary_retention(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            for day in range(1, 5):
                publish_option_eod(
                    snapshot(f"2026-08-0{day}", str(day)),
                    data_dir,
                    chain_limit=2,
                    summary_limit=3,
                )
            snapshots = sorted((data_dir / "options" / "snapshots").glob("*.json"))
            history = read_json(data_dir / "options" / "history.json")
            self.assertEqual([item.name for item in snapshots], ["2026-08-03.json", "2026-08-04.json"])
            self.assertEqual(
                [item["trade_date"] for item in history["records"]],
                ["2026-08-02", "2026-08-03", "2026-08-04"],
            )

    def test_rejects_non_ifind_chain(self) -> None:
        invalid = snapshot("2026-08-18")
        invalid["records"][0]["source_provider"] = "akshare"
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(OptionSnapshotValidationError, "iFinD"):
                publish_option_eod(invalid, Path(temporary))


if __name__ == "__main__":
    unittest.main()
