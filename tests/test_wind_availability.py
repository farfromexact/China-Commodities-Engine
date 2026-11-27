from copy import deepcopy
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from china_commodities import wind_index, wind_treasury
from china_commodities.storage import read_json
from china_commodities.wind_availability import WindUnavailable, repair_fields
from china_commodities.wind_daily_collect import CachedWind, FIELDS, collect_day, daily_quotes
from test_wind_daily_collect import CATALOG, FakeWind, response
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
    def test_later_launch_checks_only_missing_fields_and_writes_only_new_values(self):
        day='2026-11-13'
        with TemporaryDirectory() as temp:
            root=Path(temp)
            sdk=PartialTreasury()
            api=CachedWind(sdk,root/'raw',lambda:None,0)
            wind_treasury.collect_eod(api,day,root)
            path=root/'treasury_futures/latest.json'
            before=path.read_bytes()
            old=read_json(path)
            expected=old['field_availability']['missing_by_field']['settle']
            requested=[]
            value=None
            def batch(codes,field,start,end,options):
                requested.append((codes.split(','),field,start,end))
                return response([field.upper()],[[value] for _ in codes.split(',')],codes=codes.split(','),times=[datetime.fromisoformat(day)])
            sdk.wsd=batch
            result=repair_fields(api,day,root,'treasury_futures')
            self.assertEqual(result['updated_fields'],0)
            self.assertEqual(path.read_bytes(),before)
            self.assertEqual(requested,[(expected,'settle',day,day),(expected,'oi',day,day)])
            value=103.
            self.assertEqual(repair_fields(api,day,root,'treasury_futures')['updated_fields'],2*len(expected))
            repaired=read_json(path)
            self.assertEqual(repaired['field_availability']['status'],'complete')
            self.assertEqual([r['close'] for r in old['contracts']],[r['close'] for r in repaired['contracts']])

    def test_missing_venue_preserves_other_dated_futures_options_and_night(self):
        day='2026-11-13'
        class MissingVenue(FakeWind):
            def wset(self,name,params):
                if name=='futurecc' and 'wind_code=SI.GFE' in params:
                    return response([],[])
                return super().wset(name,params)
        with TemporaryDirectory() as temp:
            root=Path(temp)
            api=CachedWind(MissingVenue(),root/'raw',lambda:None,0)
            # The earlier reference is optional and isn't an observation on the target date.
            from china_commodities.storage import write_json_atomic
            write_json_atomic(root/'data/snapshots/2026-11-12.json',dict(trade_date='2026-11-12',futures_contracts=[]))
            result=collect_day(api,day,root/'data',root/'normalized',catalog=CATALOG)
            saved=read_json(root/result['unavailable']['futures'])
            self.assertEqual(len(saved['futures_contracts']),4)
            self.assertTrue(all(r['source_trade_date']==day for r in saved['futures_contracts']))
            self.assertFalse((root/'data/latest.json').exists())
            options=read_json(root/'data/options/latest.json')
            self.assertEqual(options['coverage']['failed_products'],['GFEX:SI'])
            self.assertEqual(read_json(root/'data/night_session/latest.json')['trading_date'],day)

    def test_batches_reuse_single_cache_and_keep_valid_fields_when_one_field_unavailable(self):
        day='2026-11-13'
        class SDK:
            def wsd(self,codes,fields,*args):
                ids=codes.split(',')
                values={'close':101.,'settle':100.}
                return response(fields.upper().split(','),[[values[f] for f in fields.split(',')] for _ in ids],
                                codes=ids,times=[datetime.fromisoformat(day)])
        with TemporaryDirectory() as temp:
            sdk=SDK()
            api=CachedWind(sdk,Path(temp),lambda:None,0,batch_quotes=True)
            api.eod('A','close,settle',day)
            requested=[]
            original=sdk.wsd
            def wsd(codes,fields,*args):
                requested.append((codes,fields))
                if fields=='settle':
                    raise WindUnavailable('settlement unavailable')
                return original(codes,fields,*args)
            sdk.wsd=wsd
            rows=daily_quotes(api,['C','B','A'],'close,settle',day)
            self.assertEqual(requested,[('B,C','close'),('B,C','settle')])
            self.assertEqual(rows['A']['settle'],100.)
            self.assertEqual(rows['C'],{'close':101.,'settle':None})

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
