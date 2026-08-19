from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from china_commodities.collectors.ifind_option_adapter import IFindOptionDataError


class OptionCollectScriptTests(unittest.TestCase):
    def test_example_config_cannot_be_used_as_verified_config(self) -> None:
        from scripts.collect_ifind_options import load_reports

        with self.assertRaisesRegex(IFindOptionDataError, "placeholder"):
            load_reports(Path("config/ifind_options.example.json"))

    def test_loads_explicit_verified_report(self) -> None:
        from scripts.collect_ifind_options import load_reports

        payload = {
            "reports": [
                {
                    "report_name": "p_verified",
                    "function_parameters": {"date": "{trade_date_compact}"},
                    "output_parameters": {},
                    "field_map": {"contract": "optionCode"},
                }
            ]
        }
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            path = Path(temporary) / "options.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            reports = load_reports(path)
        self.assertEqual(reports[0].report_name, "p_verified")


if __name__ == "__main__":
    unittest.main()
