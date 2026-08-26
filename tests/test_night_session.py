from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

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
                        "time": "2026-08-25 15:00:00",
                        "latest": 3520,
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
