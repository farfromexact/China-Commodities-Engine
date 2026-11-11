"""Offline checks for the Wind canary's date and completeness gate."""
import unittest
from datetime import datetime
from types import SimpleNamespace

from scripts.probe_wind import FIELDS, normalize


class WindProbeTests(unittest.TestCase):
    def response(self):
        return SimpleNamespace(ErrorCode=0, Fields=FIELDS.upper().split(","),
                               Times=[datetime(2026, 11, 11)],
                               Data=[[1.0] for _ in FIELDS.split(",")])

    def test_valid(self):
        self.assertEqual(normalize(self.response(), "2026-11-11")["source_provider"], "wind")

    def test_stale(self):
        with self.assertRaisesRegex(ValueError, "date mismatch"):
            normalize(self.response(), "2026-11-10")

    def test_missing_value(self):
        r = self.response()
        r.Data[0] = [float("nan")]
        with self.assertRaisesRegex(ValueError, "nonfinite"):
            normalize(r, "2026-11-11")

    def test_entitlement_error(self):
        r = self.response()
        r.ErrorCode = -1
        with self.assertRaisesRegex(ValueError, "Wind error"):
            normalize(r, "2026-11-11")

    def test_wrong_shape(self):
        r = self.response()
        r.Data[0] = []
        with self.assertRaisesRegex(ValueError, "one EOD"):
            normalize(r, "2026-11-11")
