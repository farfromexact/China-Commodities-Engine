from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from china_commodities.collection_cache import verified_night_session_available
from china_commodities.night_session import collect_night_session


def _write_daily_snapshot(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "latest.json").write_text(
        json.dumps(
            {
                "trade_date": "2026-08-25",
                "verified": True,
                "futures_contracts": [
                    {
                        "exchange": "SHFE",
                        "product": "CU",
                        "contract": "CU2609",
                        "trade_date": "2026-08-25",
                        "settle": 80000,
                        "close": 80100,
                    },
                    {
                        "exchange": "DCE",
                        "product": "JD",
                        "contract": "JD2609",
                        "trade_date": "2026-08-25",
                        "settle": 3500,
                        "close": 3510,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )


class FakeClient:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls: list[list[str]] = []

    def realtime_quotes(self, codes, fields):
        self.calls.append(list(codes))
        return pd.DataFrame(
            [row for row in self.rows if row["thscode"] in set(codes)]
        )


class NightSessionTests(unittest.TestCase):
    def test_accepts_only_quotes_inside_completed_night_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_daily_snapshot(root)
            client = FakeClient(
                [
                    {
                        "thscode": "CU2609.SHF",
                        "time": "2026-08-26 02:30:00",
                        "open": 80100,
                        "high": 80600,
                        "low": 79900,
                        "latest": 80400,
                        "preSettlement": 80000,
                        "volume": 1200,
                        "amount": 100000,
                        "openInterest": 200000,
                    },
                    {
                        "thscode": "JD2609.DCE",
                        "time": "2026-08-25 15:00:00",
                        "latest": 3520,
                    },
                ]
            )

            result = collect_night_session(
                "2026-08-26", data_dir=root, client=client
            )

            self.assertTrue(result["status"]["published"])
            self.assertTrue(result["status"]["data_fresh"])
            self.assertEqual(
                result["snapshot"]["night_session_date"], "2026-08-25"
            )
            self.assertEqual(
                result["status"]["coverage"]["night_session_contract_count"], 1
            )
            self.assertEqual(
                result["status"]["coverage"]["outside_night_window_count"], 1
            )
            cu = next(
                item
                for item in result["snapshot"]["records"]
                if item["contract"] == "CU2609"
            )
            self.assertEqual(cu["record_state"], "night_session")
            self.assertAlmostEqual(cu["night_return_pct"], 0.5)
            self.assertTrue((root / "night_session" / "history.parquet").exists())
            self.assertEqual(
                set(result["derivatives"]["updated_files"]),
                {
                    "contract_meta.json",
                    "last_run_status.json",
                    "latest.json",
                    "market_state_latest.json",
                    "radar_history.json",
                    "radar_latest.json",
                },
            )

            latest = json.loads((root / "latest.json").read_text(encoding="utf-8"))
            market_state = json.loads(
                (root / "market_state_latest.json").read_text(encoding="utf-8")
            )
            radar_latest = json.loads(
                (root / "radar_latest.json").read_text(encoding="utf-8")
            )
            radar_history = json.loads(
                (root / "radar_history.json").read_text(encoding="utf-8")
            )
            top_status = json.loads(
                (root / "last_run_status.json").read_text(encoding="utf-8")
            )
            contract_meta = json.loads(
                (root / "contract_meta.json").read_text(encoding="utf-8")
            )

            self.assertEqual(latest["trade_date"], "2026-08-25")
            self.assertEqual(latest["night_session"]["trading_date"], "2026-08-26")
            self.assertEqual(latest["night_session"]["record_count"], 1)
            self.assertEqual(
                market_state["night_session"]["records"][0]["contract"], "CU2609"
            )
            self.assertEqual(
                radar_latest["night_session"]["records"][0]["night_close"], 80400.0
            )
            self.assertEqual(top_status["night_session"]["record_count"], 1)
            self.assertEqual(
                contract_meta["night_session_snapshot"]["trading_date"], "2026-08-26"
            )
            self.assertEqual(radar_history["records"], [])
            self.assertEqual(len(radar_history["night_session_records"]), 1)
            self.assertEqual(
                radar_history["night_session_records"][0]["contracts"][0]["contract"],
                "CU2609",
            )

    def test_same_session_resolved_records_are_never_requested_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_daily_snapshot(root)
            first = FakeClient(
                [
                    {
                        "thscode": "CU2609.SHF",
                        "time": "2026-08-26 02:30:00",
                        "latest": 80400,
                        "preSettlement": 80000,
                    },
                    {
                        "thscode": "JD2609.DCE",
                        "time": "2026-08-25 23:00:00",
                        "settlement": 3510,
                        "preSettlement": 3510,
                    },
                ]
            )
            collect_night_session("2026-08-26", data_dir=root, client=first)
            second = FakeClient([])

            result = collect_night_session(
                "2026-08-26", data_dir=root, client=second
            )

            self.assertEqual(second.calls, [])
            self.assertEqual(result["status"]["coverage"]["request_contract_count"], 0)
            self.assertEqual(result["status"]["coverage"]["cache_hit_count"], 2)
            self.assertEqual(result["status"]["coverage"]["no_night_trade_count"], 1)
            history = json.loads(
                (root / "radar_history.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(history["night_session_records"]), 1)

    def test_partial_valid_night_snapshot_is_accepted_without_retry_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_daily_snapshot(root)
            result = collect_night_session(
                "2026-08-26",
                data_dir=root,
                client=FakeClient(
                    [
                        {
                            "thscode": "CU2609.SHF",
                            "time": "2026-08-26 02:30:00",
                            "latest": 80400,
                            "preSettlement": 80000,
                        }
                    ]
                ),
            )

            self.assertTrue(result["status"]["data_fresh"])
            self.assertTrue(result["status"]["validation_passed"])
            self.assertTrue(result["status"]["published"])
            self.assertFalse(result["status"]["coverage_complete"])
            self.assertEqual(result["status"]["coverage"]["missing_quote_count"], 1)
            self.assertTrue(verified_night_session_available(root, "2026-08-26"))

    def test_daytime_quote_does_not_replace_previous_valid_night_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_daily_snapshot(root)
            night = FakeClient(
                [
                    {
                        "thscode": "CU2609.SHF",
                        "time": "2026-08-26 02:30:00",
                        "latest": 80400,
                        "preSettlement": 80000,
                    },
                    {
                        "thscode": "JD2609.DCE",
                        "time": "2026-08-25 15:00:00",
                        "latest": 3520,
                    },
                ]
            )
            collect_night_session("2026-08-26", data_dir=root, client=night)

            result = collect_night_session(
                "2026-08-27",
                data_dir=root,
                client=FakeClient(
                    [
                        {
                            "thscode": "CU2609.SHF",
                            "time": "2026-08-27 09:05:00",
                            "latest": 81000,
                        },
                        {
                            "thscode": "JD2609.DCE",
                            "time": "2026-08-27 09:05:00",
                            "latest": 3530,
                        },
                    ]
                ),
            )

            self.assertFalse(result["status"]["published"])
            latest = json.loads(
                (root / "night_session" / "latest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(latest["trading_date"], "2026-08-26")


if __name__ == "__main__":
    unittest.main()
