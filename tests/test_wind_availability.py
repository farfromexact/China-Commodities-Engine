from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from china_commodities import wind_index, wind_treasury
from china_commodities.storage import read_json
from china_commodities.wind_daily_collect import CachedWind, FIELDS
from test_wind_index import IndexWind
from test_wind_rates import RatesWind


class MissingCore:
    def wsd(self, code, fields, *args):
        result = super().wsd(code, fields, *args)
        if fields == FIELDS and code.startswith(('IM', 'TL')):
            for key in ('settle', 'oi'):
                result.Data[fields.split(',').index(key)][0] = 0 if key == 'settle' else None
        return result


class PartialIndex(MissingCore, IndexWind):
    pass


class PartialTreasury(MissingCore, RatesWind):
    pass


class FieldAvailabilityTests(unittest.TestCase):
    def test_available_fields_publish_with_exact_missing_contracts(self):
        for module, sdk, name, key in (
                (wind_index, PartialIndex(), 'index_derivatives', 'futures_contracts'),
                (wind_treasury, PartialTreasury(), 'treasury_futures', 'contracts')):
            with self.subTest(name=name), TemporaryDirectory() as temp:
                root = Path(temp)
                api = CachedWind(sdk,root/'raw',lambda:None,0)
                module.collect_eod(api,'2026-11-13',root)
                snapshot = read_json(root/name/'latest.json')
                coverage = snapshot.get('coverage',snapshot)['field_availability']
                self.assertEqual(coverage['status'],'partial')
                absent = [r for r in snapshot[key] if r['settle'] is None]
                self.assertTrue(absent)
                self.assertEqual(coverage['missing_by_field']['settle'],sorted(r['source_code'] for r in absent))
                self.assertTrue(all(r['close']>0 and r['open_interest'] is None for r in absent))
                self.assertFalse(module.validate(snapshot))
                self.assertTrue(module.complete(root,'2026-11-13'))
                broken = deepcopy(snapshot)
                broken.get('coverage',broken)['field_availability']['missing_by_field'].clear()
                self.assertTrue(module.validate(broken))
