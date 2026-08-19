from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import pandas as pd

from china_commodities.history_storage import append_parquet_history


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


if __name__ == "__main__":
    unittest.main()
