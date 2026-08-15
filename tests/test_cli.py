from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path

from china_commodities.cli import _validate


class ValidateCommandTests(unittest.TestCase):
    def test_partial_status_is_valid_observable_state(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            Path(directory, "last_run_status.json").write_text(
                json.dumps(
                    {
                        "data_fresh": False,
                        "validation_errors": ["DCE futures not fresh"],
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(_validate(argparse.Namespace(data_dir=directory)), 0)

    def test_missing_snapshot_and_status_fails(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            self.assertEqual(_validate(argparse.Namespace(data_dir=directory)), 1)


if __name__ == "__main__":
    unittest.main()
