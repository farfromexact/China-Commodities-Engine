import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from china_commodities.futures_crosscheck import compare_exchange, run_crosscheck
from china_commodities.storage import write_json_if_changed, read_json

DAY = "2026-10-16"


def row(exchange="SHFE", **changes):
    return dict({"exchange": exchange, "contract": "CU2611", "source_trade_date": DAY,
                 "source_date_match": True, "source_provider": "ifind_http",
                 "settle": 80000, "close": 80100, "pre_settle": 79900}, **changes)


class CrosscheckTests(unittest.TestCase):
    def test_same_contract_same_date_prices_and_missing_fields(self):
        result = compare_exchange("SHFE", [row(settle=80001, close=None)], [row()], DAY)
        self.assertEqual(result["state"], "differences")
        self.assertEqual(result["fields"]["settle"]["different"], 1)
        self.assertEqual(result["fields"]["close"]["comparable"], 0)
        with self.assertRaises(ValueError):
            compare_exchange("SHFE", [row(source_trade_date="2026-10-15")], [row()], DAY)
        with self.assertRaises(ValueError):
            compare_exchange("SHFE", [row(), row()], [row()], DAY)

    def test_one_exchange_failure_is_local_and_no_source_replacement(self):
        class Client:
            def fetch(self, exchange, kind, trade_date):
                if exchange == "DCE":
                    raise ValueError("source structure changed")
                return exchange
        with tempfile.TemporaryDirectory() as tmp:
            baseline = {"trade_date": DAY, "futures_contracts": [row(x) for x in ("SHFE", "INE", "DCE", "CZCE", "GFEX")]}
            write_json_if_changed(Path(tmp)/"latest.json", baseline)
            with patch("china_commodities.futures_crosscheck.normalize_document", side_effect=lambda x: [row(x)]):
                result = run_crosscheck(DAY, data_dir=tmp, client=Client())
            self.assertEqual(result["state"], "partial")
            self.assertEqual(sum(r["state"] == "matched" for r in result["exchanges"]), 4)
            self.assertFalse(result["blocks_publication"])
            self.assertFalse(result["all_compared_prices_equal"])
            self.assertEqual(result["ifind_requests"], 0)
            self.assertEqual(read_json(Path(tmp)/"latest.json"), baseline)

    def test_production_module_provenance_is_used_without_assuming_all_rows_are_vendor(self):
        class Client:
            def fetch(self, exchange, kind, trade_date):
                return exchange
        baseline_row = row()
        del baseline_row["source_provider"]
        with tempfile.TemporaryDirectory() as tmp:
            baseline = {"trade_date": DAY, "futures_contracts": [baseline_row], "source": {
                "provider": "ifind", "modules": [{"dataset": "futures", "scope": "SHFE", "state": "ok",
                    "upstream_source": "iFinD Quant API", "source_date_match": True, "source_trade_date": DAY,
                    "is_fallback": False}]}}
            for fallback, expected in ((False, "matched"), (True, "baseline_unavailable")):
                baseline["source"]["modules"][0]["is_fallback"] = fallback
                write_json_if_changed(Path(tmp)/"latest.json", baseline)
                with patch("china_commodities.futures_crosscheck.normalize_document", side_effect=lambda x: [row(x)]):
                    result = run_crosscheck(DAY, data_dir=tmp, client=Client())
                self.assertEqual(result["exchanges"][0]["state"], expected)
