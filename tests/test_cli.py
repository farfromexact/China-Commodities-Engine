from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from china_commodities.cli import COMMODITY_EXCHANGES, _parser, _run, _validate, main


class ValidateCommandTests(unittest.TestCase):
    def test_partial_status_is_valid_observable_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "last_run_status.json").write_text(
                json.dumps(
                    {
                        "data_fresh": False,
                        "validation_errors": ["DCE futures not fresh"],
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                _validate(argparse.Namespace(data_dir=directory, scope=None)), 0
            )

    def test_missing_snapshot_and_status_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                _validate(argparse.Namespace(data_dir=directory, scope=None)), 1
            )

    def test_scoped_snapshot_uses_scope_and_allows_scoped_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory, "scoped", "SHFE", "latest.json")
            snapshot.parent.mkdir(parents=True)
            snapshot.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "verified": False,
                        "scope_verified": True,
                        "trade_date": "2026-08-14",
                        "futures_contracts": [{"contract": "CU2610"}],
                        "commodity_curves": [{"product": "CU"}],
                    }
                ),
                encoding="utf-8",
            )
            with patch(
                "china_commodities.cli.validate_snapshot", return_value=[]
            ) as validate_snapshot_mock:
                self.assertEqual(
                    _validate(argparse.Namespace(data_dir=directory, scope="SHFE")),
                    0,
                )
                validate_snapshot_mock.assert_called_once_with(
                    json.loads(snapshot.read_text(encoding="utf-8")),
                    allow_scoped=True,
                )

    def test_missing_scoped_snapshot_reads_scoped_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            status = Path(directory, "scoped", "ex-dce", "last_run_status.json")
            status.parent.mkdir(parents=True)
            status.write_text(
                json.dumps(
                    {
                        "scope_data_fresh": False,
                        "validation_errors": ["SHFE futures not fresh"],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                _validate(argparse.Namespace(data_dir=directory, scope="ex-dce")),
                0,
            )


class RunCommandTests(unittest.TestCase):
    def test_exchanges_are_ordered_and_summary_contains_scope_fields(self) -> None:
        result = SimpleNamespace(
            trade_date="2026-08-14",
            verified=False,
            official_complete=False,
            included_exchanges=("SHFE", "CZCE", "GFEX"),
            excluded_exchanges=("INE", "DCE"),
            scope_verified=True,
            futures_records=[],
            contract_metadata=[],
            warehouse_records=[],
            basis_records=[],
            option_records=[],
            member_ranking_summaries=[],
            candidates=[],
            validation_errors=[],
        )
        args = _parser().parse_args(
            ["run", "--exclude-exchange", "DCE", "--exclude-exchange", "INE"]
        )
        output = StringIO()
        with patch("china_commodities.cli.run_pipeline", return_value=result) as run:
            with redirect_stdout(output):
                self.assertEqual(_run(args), 0)
        run.assert_called_once()
        self.assertEqual(
            run.call_args.kwargs["exchanges"], ("SHFE", "CZCE", "GFEX")
        )
        summary = json.loads(output.getvalue())
        self.assertEqual(summary["included_exchanges"], ["SHFE", "CZCE", "GFEX"])
        self.assertEqual(summary["excluded_exchanges"], ["INE", "DCE"])
        self.assertTrue(summary["scope_verified"])

    def test_all_exchanges_excluded_is_rejected(self) -> None:
        argv = [item for exchange in COMMODITY_EXCHANGES for item in ("--exclude-exchange", exchange)]
        with self.assertRaises(SystemExit):
            main(["run", *argv])


if __name__ == "__main__":
    unittest.main()
