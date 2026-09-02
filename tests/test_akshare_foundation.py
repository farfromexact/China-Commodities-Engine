from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

import pandas as pd

from china_commodities.foundation import run_foundation
from china_commodities.source_registry import CORE_PHYSICAL_PRODUCTS, EXTERNAL_TARGETS


class FakeAkShare:
    """Public-source fake that makes every provider call observable."""

    __version__ = "test-akshare"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.foreign_dates: dict[str, str] = {}

    def futures_spot_price(self, **kwargs: Any) -> pd.DataFrame:
        self.calls.append(("futures_spot_price", kwargs))
        return pd.DataFrame(
            [
                {
                    "symbol": "I",
                    "date": "2026-09-02",
                    "spot_price": 801.5,
                    "near_contract": "i2609",
                    "near_contract_price": 800.0,
                    "dominant_contract": "i2701",
                    "dominant_contract_price": 799.0,
                },
                {
                    "symbol": "RB",
                    "date": "2026-09-02",
                    "spot_price": 3012.0,
                    "near_contract": "rb2610",
                    "near_contract_price": 3000.0,
                    "dominant_contract": "rb2701",
                    "dominant_contract_price": 2990.0,
                },
            ]
        )

    def futures_foreign_hist(self, **kwargs: Any) -> pd.DataFrame:
        self.calls.append(("futures_foreign_hist", kwargs))
        symbol = str(kwargs["symbol"])
        source_date = self.foreign_dates.get(symbol, "2026-09-02")
        return pd.DataFrame(
            [
                {"date": "2026-09-01", "close": 100.0},
                {"date": source_date, "close": 101.0},
            ]
        )


class EmptyPhysicalAkShare(FakeAkShare):
    def futures_spot_price(self, **kwargs: Any) -> pd.DataFrame:
        self.calls.append(("futures_spot_price", kwargs))
        return pd.DataFrame(columns=["symbol", "date", "spot_price"])


class AkShareFoundationTests(unittest.TestCase):
    def test_akshare_provider_collects_both_domains_without_ifind(self) -> None:
        fake = FakeAkShare()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = run_foundation(
                "2026-09-02",
                provider="akshare",
                data_dir=root,
                ak_module=fake,
                shadow_days=1,
                request_interval_seconds=0,
            )

            physical = results["physical"]
            external = results["external"]
            self.assertEqual(physical["payload"]["provider"], "akshare")
            self.assertEqual(physical["payload"]["vendor"], "AKShare")
            self.assertTrue(physical["status"]["published"])
            self.assertTrue(external["status"]["published"])
            self.assertEqual(
                physical["payload"]["coverage"]["target_count"],
                len(CORE_PHYSICAL_PRODUCTS),
            )
            self.assertEqual(
                physical["payload"]["coverage"]["configured_mapping_count"],
                len(CORE_PHYSICAL_PRODUCTS),
            )
            self.assertEqual(
                external["payload"]["coverage"]["target_count"],
                len(EXTERNAL_TARGETS),
            )
            self.assertEqual(
                external["payload"]["coverage"]["configured_mapping_count"],
                17,
            )

            physical_matrix = {
                item["key"]: item for item in physical["payload"]["coverage_matrix"]
            }
            self.assertEqual(physical_matrix["LU"]["mapping_status"], "configured")
            self.assertEqual(physical_matrix["LU"]["quality_state"], "unavailable")
            physical_record = physical["payload"]["series"][0]
            self.assertEqual(physical_record["source_endpoint"], "futures_spot_price")
            self.assertEqual(physical_record["original_source"], "100ppi via AKShare")

            external_matrix = {
                item["key"]: item for item in external["payload"]["coverage_matrix"]
            }
            self.assertEqual(external_matrix["BMD_PALM"]["mapping_status"], "configured")
            self.assertEqual(external_matrix["USDCNH"]["mapping_status"], "unavailable")
            bmd = next(
                item
                for item in external["payload"]["series"]
                if item["target"] == "BMD_PALM"
            )
            self.assertEqual(bmd["indicator_id"], "AKSHARE:SINA:FCPO")
            self.assertEqual(bmd["usage"], "context_only")
            self.assertEqual(bmd["source_endpoint"], "futures_foreign_hist")

            self.assertEqual(fake.calls[0][0], "futures_spot_price")
            self.assertEqual(
                fake.calls[0][1]["vars_list"], list(CORE_PHYSICAL_PRODUCTS)
            )
            self.assertEqual(
                sum(name == "futures_foreign_hist" for name, _ in fake.calls), 17
            )
            self.assertTrue((root / "physical" / "latest.json").exists())
            self.assertTrue((root / "external" / "latest.json").exists())

    def test_external_history_marks_a_lagged_series_stale(self) -> None:
        fake = FakeAkShare()
        fake.foreign_dates["FCPO"] = "2026-08-28"
        with tempfile.TemporaryDirectory() as directory:
            result = run_foundation(
                "2026-09-06",
                provider="akshare",
                scope="external",
                data_dir=directory,
                ak_module=fake,
                shadow_days=1,
                request_interval_seconds=0,
            )["external"]

        bmd = next(
            item for item in result["payload"]["series"] if item["target"] == "BMD_PALM"
        )
        self.assertEqual(bmd["quality_state"], "stale")
        self.assertTrue(bmd["is_stale"])
        self.assertFalse(result["status"]["data_fresh"])

    def test_empty_public_response_does_not_replace_the_latest_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = run_foundation(
                "2026-09-02",
                provider="akshare",
                scope="physical",
                data_dir=root,
                ak_module=EmptyPhysicalAkShare(),
                shadow_days=1,
                request_interval_seconds=0,
            )["physical"]

            self.assertFalse(result["status"]["validation_passed"])
            self.assertFalse(result["status"]["published"])
            self.assertIn(
                "no configured AKShare target returned a usable observation",
                result["status"]["validation_errors"],
            )
            self.assertFalse((root / "physical" / "latest.json").exists())


if __name__ == "__main__":
    unittest.main()
