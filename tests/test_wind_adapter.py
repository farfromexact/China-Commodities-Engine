import unittest
import importlib.util
from pathlib import Path

# Load the pure adapter without collectors/__init__ importing AKShare dependencies.
spec = importlib.util.spec_from_file_location('wind_adapter', Path(__file__).resolve().parents[1]
                                            / 'src/china_commodities/collectors/wind_adapter.py')
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)
normalize_eod = adapter.normalize_eod


class WindAdapterTests(unittest.TestCase):
    def normalize(self, **changes):
        values = dict(open=10, high=12, low=9, close=11, settle=10, volume=100, oi=50, amt=100000)
        values.update(changes)
        return normalize_eod(values, source_date='2026-11-11', requested_date='2026-11-11',
                             code='M2701.DCE', price_unit='人民币元/吨', multiplier=10)

    def test_amount_scale_and_activity(self):
        r = self.normalize()
        self.assertEqual(r['turnover'], 10)
        self.assertEqual(r['source_turnover'], 100000)
        self.assertEqual(r['open_interest'], 50)
        self.assertEqual(r['volume'], 100)

    def test_missing_amount_is_not_zero(self):
        with self.assertRaises(ValueError):
            self.normalize(amt=None)

    def test_zero_volume_valid(self):
        self.assertEqual(self.normalize(volume=0, amt=0)['volume'], 0)

    def test_bad_ohlc_rejected(self):
        with self.assertRaises(ValueError):
            self.normalize(close=15)
