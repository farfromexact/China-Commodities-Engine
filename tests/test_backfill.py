from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from china_commodities.backfill import run_ifind_backfill
from china_commodities.collectors.ifind_http_adapter import IFindHTTPError


class RangeIFindHTTPClient:
    targets = {
        "SHF": "RB2610.SHF",
        "INE": "SC2610.INE",
        "DCE": "I2609.DCE",
        "CZC": "SR609.CZC",
        "GFE": "LC2609.GFE",
    }
    dates = ("2026-08-17", "2026-08-18", "2026-08-19")

    def history_quotes_range(self, codes, fields, *, start_date, end_date):
        rows = []
        for target in self.targets.values():
            if target not in codes:
                continue
            for trade_date in self.dates:
                if start_date <= trade_date <= end_date:
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


class DailyFallbackIFindHTTPClient(RangeIFindHTTPClient):
    def history_quotes_range(self, codes, fields, *, start_date, end_date):
        raise IFindHTTPError(
            "iFinD cmd_history_quotation failed with code -4210: bad range"
        )

    def history_quotes(self, codes, fields, trade_date):
        rows = []
        for target in self.targets.values():
            if target in codes and trade_date in self.dates:
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


class BackfillTests(unittest.TestCase):
    def test_parquet_backfill_can_exceed_twenty_day_json_window(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            summary = run_ifind_backfill(
                end_date="2026-08-19",
                days=2,
                data_dir=directory,
                history_limit=1,
                snapshot_limit=1,
                calendar_days=10,
                client=RangeIFindHTTPClient(),
                request_interval_seconds=0,
            )
            frame = pd.read_parquet(Path(directory) / "history" / "futures.parquet")
            history = json.loads(
                (Path(directory) / "radar_history.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary.published_days, 2)
            self.assertEqual(len(frame), 10)
            self.assertEqual(len(history["records"]), 1)

    def test_backfill_publishes_only_latest_requested_common_days(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            summary = run_ifind_backfill(
                end_date="2026-08-19",
                days=2,
                data_dir=directory,
                history_limit=3,
                snapshot_limit=2,
                calendar_days=10,
                client=RangeIFindHTTPClient(),
                request_interval_seconds=0,
            )

            root = Path(directory)
            self.assertEqual(summary.published_dates, ("2026-08-18", "2026-08-19"))
            self.assertEqual(
                sorted(path.name for path in (root / "snapshots").glob("*.json")),
                ["2026-08-18.json", "2026-08-19.json"],
            )
            latest = json.loads((root / "latest.json").read_text(encoding="utf-8"))
            history = json.loads(
                (root / "radar_history.json").read_text(encoding="utf-8")
            )
            self.assertEqual(latest["trade_date"], "2026-08-19")
            self.assertEqual(
                [record["trade_date"] for record in history["records"]],
                ["2026-08-18", "2026-08-19"],
            )
            self.assertEqual(latest["source"]["provider"], "ifind")
            self.assertEqual(summary.collection_mode, "range")

    def test_parameter_error_falls_back_to_verified_daily_queries(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            summary = run_ifind_backfill(
                end_date="2026-08-19",
                days=2,
                data_dir=directory,
                history_limit=3,
                snapshot_limit=2,
                calendar_days=10,
                client=DailyFallbackIFindHTTPClient(),
                request_interval_seconds=0,
            )
            self.assertEqual(summary.collection_mode, "daily_fallback")
            self.assertEqual(summary.published_dates, ("2026-08-18", "2026-08-19"))

    def test_backfill_is_all_or_nothing_when_common_dates_are_insufficient(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            with self.assertRaisesRegex(RuntimeError, "only 3 common trading days"):
                run_ifind_backfill(
                    end_date="2026-08-19",
                    days=4,
                    data_dir=directory,
                    history_limit=4,
                    snapshot_limit=4,
                    calendar_days=10,
                    client=RangeIFindHTTPClient(),
                    request_interval_seconds=0,
                )
            self.assertFalse((Path(directory) / "latest.json").exists())


if __name__ == "__main__":
    unittest.main()
