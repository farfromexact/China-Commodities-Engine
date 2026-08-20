from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import pandas as pd

from china_commodities.history_storage import (
    append_option_history,
    append_parquet_history,
)


def option_snapshot(trade_date: str, settle: float = 10.0) -> dict:
    return {
        "trade_date": trade_date,
        "source_provider": "ifind_http",
        "records": [
            {
                "trade_date": trade_date,
                "source_trade_date": trade_date,
                "source_date_match": True,
                "source_provider": "ifind_http",
                "exchange": "SHFE",
                "product": "CU",
                "contract": "CU2610-C-80000",
                "underlying_contract": "CU2610",
                "settle": settle,
                "greeks": {
                    "quality": "vendor_reported",
                    "selected": {"iv_percent": 20.0},
                },
            }
        ],
    }


class HistoryStorageTests(unittest.TestCase):
    def test_same_day_retry_is_deduplicated_and_last_write_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "history.parquet"
            append_parquet_history(
                path,
                [
                    {"series_key": "x", "observation_date": "2026-08-18", "value": 1.0},
                    {"series_key": "x", "observation_date": "2026-08-19", "value": 2.0},
                ],
                key_fields=("series_key", "observation_date"),
            )
            count = append_parquet_history(
                path,
                [{"series_key": "x", "observation_date": "2026-08-19", "value": 3.0}],
                key_fields=("series_key", "observation_date"),
            )

            frame = pd.read_parquet(path)
            self.assertEqual(count, 2)
            self.assertEqual(frame.loc[frame["observation_date"].eq("2026-08-19"), "value"].item(), 3.0)

    def test_incomplete_history_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "incomplete key"):
                append_parquet_history(
                    Path(temporary) / "history.parquet",
                    [{"series_key": "x", "observation_date": None, "value": 1.0}],
                    key_fields=("series_key", "observation_date"),
                )

    def test_option_history_uses_daily_partition_and_same_day_upsert(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            append_option_history(option_snapshot("2026-08-18", 10.0), data_dir)
            count = append_option_history(
                option_snapshot("2026-08-18", 11.0), data_dir
            )

            path = (
                data_dir
                / "options"
                / "history"
                / "year=2026"
                / "month=08"
                / "2026-08-18.parquet"
            )
            frame = pd.read_parquet(path)
            self.assertEqual(count, 1)
            self.assertEqual(len(frame), 1)
            self.assertEqual(frame.iloc[0]["settle"], 11.0)

    def test_option_history_retains_recent_distinct_trade_dates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            for day in (18, 19, 20):
                append_option_history(
                    option_snapshot(f"2026-08-{day}"),
                    data_dir,
                    retention_days=2,
                )

            root = data_dir / "options" / "history" / "year=2026" / "month=08"
            self.assertFalse((root / "2026-08-18.parquet").exists())
            self.assertTrue((root / "2026-08-19.parquet").exists())
            self.assertTrue((root / "2026-08-20.parquet").exists())

    def test_legacy_option_history_is_migrated_before_append(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            legacy = data_dir / "options" / "history.parquet"
            append_parquet_history(
                legacy,
                [
                    {
                        "trade_date": "2026-08-17",
                        "exchange": "SHFE",
                        "product": "CU",
                        "contract": "CU2610-C-80000",
                        "settle": 9.0,
                    }
                ],
                key_fields=("trade_date", "exchange", "contract"),
            )

            count = append_option_history(option_snapshot("2026-08-18"), data_dir)

            self.assertFalse(legacy.exists())
            migrated = (
                data_dir
                / "options"
                / "history"
                / "year=2026"
                / "month=08"
                / "2026-08-17.parquet"
            )
            self.assertTrue(migrated.exists())
            self.assertEqual(pd.read_parquet(migrated).iloc[0]["settle"], 9.0)
            self.assertEqual(count, 2)


if __name__ == "__main__":
    unittest.main()
