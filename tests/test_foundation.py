from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from china_commodities.derivations import (
    calculate_basis,
    calculate_import_parity,
    convert_unit_value,
)
from china_commodities.foundation import collect_foundation_domain
from china_commodities.storage import read_json


class FakeEDBClient:
    def __init__(self, observations=None, error: Exception | None = None):
        self.observations = observations or {}
        self.error = error
        self.calls = []

    def edb_series(self, indicator_ids, *, start_date, end_date):
        self.calls.append((tuple(indicator_ids), start_date, end_date))
        if self.error is not None:
            raise self.error
        indicator_id = indicator_ids[0]
        observation_date, value = self.observations.get(
            indicator_id, ("2026-08-19", 100.0)
        )
        return pd.DataFrame(
            [
                {
                    "indicator_id": indicator_id,
                    "observation_date": observation_date,
                    "value": value,
                }
            ]
        )


class FoundationTests(unittest.TestCase):
    def test_default_shadow_gate_promotes_only_on_fifth_valid_run_date(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, run_date in enumerate(
                ("2026-08-20", "2026-08-21", "2026-08-24", "2026-08-25", "2026-08-26"),
                start=1,
            ):
                result = collect_foundation_domain(
                    "physical",
                    run_date,
                    data_dir=root,
                    client=FakeEDBClient(
                        observations={
                            indicator: (run_date, 100.0)
                            for indicator in (
                                "S011038838",
                                "S005948590",
                                "S005696248",
                                "S011318489",
                            )
                        }
                    ),
                )
                self.assertEqual(
                    result["status"]["published"], index == 5
                )
            self.assertTrue((root / "physical" / "latest.json").exists())
            self.assertTrue((root / "physical" / "attempt_latest.json").exists())

    def test_physical_publishes_full_matrix_basis_and_parquet_history(self) -> None:
        observations = {
            "S011038838": ("2026-08-15", 15000.0),
            "S005948590": ("2026-08-19", 1200.0),
            "S005696248": ("2026-08-15", 4000.0),
            "S011318489": ("2026-08-15", 300.0),
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "latest.json").write_text(
                json.dumps(
                    {
                        "trade_date": "2026-08-20",
                        "commodity_curves": [
                            {
                                "exchange": "DCE",
                                "product": "JM",
                                "main_contract": {
                                    "contract": "JM2609",
                                    "settle": 1000.0,
                                },
                            }
                        ],
                        "warehouse_inventory": [],
                    }
                ),
                encoding="utf-8",
            )
            result = collect_foundation_domain(
                "physical",
                "2026-08-20",
                data_dir=root,
                client=FakeEDBClient(observations),
                shadow_days=1,
                now=datetime.fromisoformat("2026-08-20T19:00:00+08:00"),
            )

            payload = result["payload"]
            self.assertEqual(len(payload["coverage_matrix"]), 20)
            self.assertEqual(len(payload["series"]), 4)
            self.assertFalse(payload["intraday_used"])
            self.assertFalse(payload["production_uses_natural_language_search"])
            self.assertIsNone(payload["fundamental_score"])
            jm_basis = payload["basis"][0]
            self.assertEqual(jm_basis["formula"], "spot - futures")
            self.assertEqual(jm_basis["value"], 200.0)
            self.assertEqual(jm_basis["quality_grade"], "C")
            self.assertFalse(jm_basis["eligible_for_physical_score"])
            self.assertTrue((root / "physical" / "history.parquet").exists())
            stored = read_json(root / "physical" / "latest.json")
            self.assertEqual(stored["requested_date"], "2026-08-20")
            self.assertTrue(
                all(record["indicator_id"] for record in stored["series"])
            )

    def test_permission_failure_carries_previous_valid_observations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            collect_foundation_domain(
                "physical",
                "2026-08-20",
                data_dir=root,
                client=FakeEDBClient(),
                shadow_days=1,
            )
            second = collect_foundation_domain(
                "physical",
                "2026-08-21",
                data_dir=root,
                client=FakeEDBClient(error=RuntimeError("permission denied")),
                shadow_days=1,
            )

            self.assertTrue(
                all(record["carried_forward"] for record in second["payload"]["series"])
            )
            self.assertTrue(
                all(status["state"] == "no_permission" for status in second["status"]["series"])
            )
            self.assertFalse(second["status"]["data_fresh"])

    def test_external_stale_series_is_explicit_and_never_used_as_parity(self) -> None:
        observations = {"S024923811": ("2026-07-01", 900.0)}
        with tempfile.TemporaryDirectory() as temporary:
            result = collect_foundation_domain(
                "external",
                "2026-08-20",
                data_dir=temporary,
                client=FakeEDBClient(observations),
            )
            matrix = {
                item["key"]: item for item in result["payload"]["coverage_matrix"]
            }
            record = next(
                item
                for item in result["payload"]["series"]
                if item["target"] == "BMD_PALM"
            )
            self.assertEqual(matrix["BMD_PALM"]["quality_state"], "stale")
            self.assertEqual(record["usage"], "context_only")
            self.assertTrue(record["is_stale"])
            self.assertEqual(matrix["WTI"]["quality_state"], "unavailable")

    def test_unit_basis_and_parity_alignment_fail_closed(self) -> None:
        self.assertEqual(convert_unit_value(2, "元/公斤", "元/吨"), 2000.0)
        with self.assertRaisesRegex(ValueError, "unverified unit conversion"):
            convert_unit_value(1, "foo", "bar")
        basis = calculate_basis(
            {"value": 105.0, "quality_state": "fresh", "basis_quality": "D"},
            {"settle": 100.0, "contract": "X2609"},
        )
        self.assertEqual(basis["value"], 5.0)
        definition = {
            "parity_key": "TEST",
            "status": "verified",
            "required_legs": ["overseas", "domestic"],
            "contract_legs": ["overseas", "domestic"],
            "max_time_gap_days": 1,
            "quality_aligned": True,
            "tax_treatment_verified": True,
            "freight_treatment_verified": True,
        }
        legs = {
            "overseas": {
                "value": 100.0,
                "coefficient": 1.0,
                "observation_date": "2026-08-19",
                "unit": "USD/t",
                "currency": "USD",
                "quality": "A",
                "contract_month": "2026-09",
            },
            "domestic": {
                "value": 700.0,
                "coefficient": -1.0,
                "observation_date": "2026-08-19",
                "unit": "CNY/t",
                "currency": "CNY",
                "quality": "A",
                "contract_month": "2026-10",
            },
        }
        parity = calculate_import_parity(definition, legs)
        self.assertIsNone(parity["value"])
        self.assertIn("contract_month_mismatch", parity["missing_reason"])


if __name__ == "__main__":
    unittest.main()
