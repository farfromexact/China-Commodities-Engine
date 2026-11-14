from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from china_commodities.wind_daily import ZONE
import unittest

from china_commodities.catalog import ProductCatalog, OptionProduct
from china_commodities.storage import read_json, write_json_atomic
from china_commodities.wind_daily_collect import CachedWind, collect_day, directory, futures


CODES={'SHFE':'AU2612.SHF','INE':'SC2612.INE','DCE':'M2701.DCE','CZCE':'SA701.CZC','GFEX':'SI2701.GFE'}
PRODUCTS={'SHFE':'AU','INE':'SC','DCE':'M','CZCE':'SA','GFEX':'SI'}
CATALOG=ProductCatalog({}, {}, {ex:(PRODUCTS[ex],) for ex in CODES},
                       tuple(OptionProduct(ex,PRODUCTS[ex],PRODUCTS[ex]) for ex in CODES))


def response(fields,rows,codes=(),times=()):
    return SimpleNamespace(ErrorCode=0,Fields=fields,Codes=list(codes),Times=list(times),
                           Data=[list(values) for values in zip(*rows)] if rows else [])


class FakeWind:
    def __init__(self):
        self.calls=0
        self.fail=False
        self.stale=False
        self.change_directory=False

    def wset(self,name,parameters):
        self.calls+=1
        if self.fail:
            raise AssertionError('cache should avoid SDK calls')
        params=dict(part.split('=',1) for part in parameters.split(';'))
        if name=='futurecc':
            root=params['wind_code']
            code=next(c for c in CODES.values() if c.split('.')[1]==root.split('.')[1])
            rows=[[code,'2020-01-01','2027-12-01']]
            if self.change_directory and root=='AU.SHF' and params['startdate']=='2026-11-12':
                rows.append(['AU2702.SHF','2026-11-12','2027-12-01'])
            return response(['wind_code','contract_issue_date','last_trade_date'],rows)
        code=params['us_code']
        contract,suffix=code.split('.')
        rows=[]
        for strike in range(95,105):
            for kind,side in [('认购','C'),('认沽','P')]:
                rows.append([code,f'{contract}{side}{strike}.{suffix}','2020-01-01','2027-01-01',kind,strike,'欧式'])
        return response(['us_code','option_code','first_tradedate','last_tradedate','call_put','strike_price','exe_type'],rows)

    def wsd(self,code,fields,start,end,options):
        self.calls+=1
        if self.fail:
            raise AssertionError('cache should avoid SDK calls')
        values=dict(open=100.,high=102.,low=99.,close=101.,settle=100.,pre_settle=100.,volume=10.,oi=20.,amt=10000.,
                    us_impliedvol=.2,delta=.5,gamma=.01)
        day=datetime.fromisoformat(start)-timedelta(days=1 if self.stale else 0)
        return response(fields.upper().split(','),[[values[f] for f in fields.split(',')]],codes=[code],times=[day])

    def wsi(self,code,fields,start,end,options):
        self.calls+=1
        if self.fail:
            raise AssertionError('cache should avoid SDK calls')
        when=datetime.fromisoformat(start)+timedelta(hours=1,minutes=5)
        return response(fields.upper().split(','),[[100.,102.,99.,101.,10.]],codes=[code],times=[when])


class CollectionTests(unittest.TestCase):
    def test_weekend_night_uses_friday_bars_and_preserves_friday_eod(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            data=root/'data'
            prior=dict(trade_date='2026-11-13',futures_contracts=[])
            write_json_atomic(data/'latest.json',prior)
            write_json_atomic(data/'snapshots/2026-11-13.json',prior)
            write_json_atomic(data/'options/latest.json',dict(trade_date='2026-11-13'))
            api=CachedWind(FakeWind(),root/'raw',lambda:None,interval=0)
            result=collect_day(api,'2026-11-16',data,root/'normalized',night_only=True,
                               catalog=CATALOG,now=datetime(2026,11,14,19,tzinfo=ZONE))
            self.assertEqual(result['mode'],'night')
            night=read_json(data/'night_session/latest.json')
            self.assertEqual(night['trading_date'],'2026-11-16')
            self.assertEqual(night['session_start_date'],'2026-11-13')
            self.assertEqual(night['session_end_date'],'2026-11-14')
            self.assertEqual(night['reference_eod_trade_date'],'2026-11-13')
            self.assertEqual(read_json(data/'latest.json'),prior)
            self.assertEqual(read_json(data/'options/latest.json')['trade_date'],'2026-11-13')
            self.assertFalse((data/'wind_daily/completed/2026-11-16.json').exists())

    def test_three_layers_resume_without_vendor_calls_and_record_missing_prior(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            data=root/'data'
            sdk=FakeWind()
            api=CachedWind(sdk,root/'raw',lambda:None,interval=0)
            # A new listing may not appear in the previous EOD reference.
            write_json_atomic(data/'snapshots/2026-11-10.json',dict(trade_date='2026-11-10',futures_contracts=[]))
            result=collect_day(api,'2026-11-11',data,root/'normalized',catalog=CATALOG)
            self.assertEqual(result['directory_count'],5)
            self.assertEqual(len(read_json(data/'latest.json')['futures_contracts']),5)
            self.assertEqual(read_json(data/'options/latest.json')['record_count'],10)
            night=read_json(data/'night_session/2026-11-11.json')
            self.assertTrue(all(r['return_vs_settlement_pct'] is None for r in night['records']))
            self.assertEqual(len(night['records']),5)
            before=sdk.calls
            sdk.fail=True
            collect_day(api,'2026-11-11',data,root/'normalized',catalog=CATALOG)
            self.assertEqual(sdk.calls,before)
            self.assertEqual(read_json(data/'wind_daily/completed/2026-11-11.json')['layers'],['futures','options','night'])
            sdk.fail=False
            collect_day(api,'2026-11-12',data,root/'normalized',catalog=CATALOG,now=datetime(2026,11,13,tzinfo=ZONE))
            sdk.fail=True
            collect_day(api,'2026-11-11',data,root/'normalized',catalog=CATALOG)
            self.assertEqual(read_json(data/'latest.json')['trade_date'],'2026-11-12')
            self.assertEqual(read_json(data/'options/latest.json')['trade_date'],'2026-11-12')
            self.assertEqual(read_json(data/'night_session/latest.json')['trading_date'],'2026-11-12')

    def test_preclose_final_is_blocked_before_requests(self):
        with TemporaryDirectory() as tmp:
            sdk=FakeWind()
            api=CachedWind(sdk,Path(tmp)/'raw',lambda:None,interval=0)
            with self.assertRaisesRegex(ValueError,'unfinished day session'):
                collect_day(api,'2026-11-12',Path(tmp)/'data',Path(tmp)/'normal',catalog=CATALOG,now=datetime(2026,11,12,14,tzinfo=ZONE))
            self.assertEqual(sdk.calls,0)

    def test_provisional_never_writes_eod_receipt_or_night_even_if_fields_complete(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            sdk=FakeWind()
            api=CachedWind(sdk,root/'provisional/raw',lambda:None,interval=0)
            with patch('china_commodities.wind_daily_collect.night',side_effect=AssertionError('no unfinished night')):
                result=collect_day(api,'2026-11-12',root/'data',root/'normal',catalog=CATALOG,provisional=True,now=datetime(2026,11,12,1,tzinfo=ZONE))
            self.assertEqual(result['mode'],'provisional')
            self.assertFalse(result['published'])
            self.assertGreater(sdk.calls,0)
            self.assertFalse((root/'data/latest.json').exists())
            self.assertFalse((root/'data/wind_daily/completed/2026-11-12.json').exists())
            self.assertFalse((root/'data/night_session/latest.json').exists())

    def test_directory_is_queried_for_each_day_and_allows_listing_changes(self):
        with TemporaryDirectory() as tmp:
            sdk=FakeWind();sdk.change_directory=True
            api=CachedWind(sdk,tmp,lambda:None,interval=0)
            old,_=directory(api,'2026-11-11',catalog=CATALOG)
            new,_=directory(api,'2026-11-12',catalog=CATALOG)
            self.assertEqual(len(old),5)
            self.assertEqual(len(new),6)

    def test_stale_eod_blocks_and_invalid_cache_is_removed(self):
        with TemporaryDirectory() as tmp:
            sdk=FakeWind();sdk.stale=True
            api=CachedWind(sdk,tmp,lambda:None,interval=0)
            universe,_=directory(api,'2026-11-11',catalog=CATALOG)
            with self.assertRaisesRegex(ValueError,'date/shape'):
                futures(api,'2026-11-11',universe)
            sdk.stale=False
            self.assertTrue(futures(api,'2026-11-11',universe).verified)

    def test_holiday_no_night_is_explicit_and_never_calls_wsi(self):
        with TemporaryDirectory() as tmp:
            sdk=FakeWind()
            sdk.wsi=lambda *args: self.fail('holiday must not query WSI')
            api=CachedWind(sdk,Path(tmp)/'raw',lambda:None,interval=0)
            collect_day(api,'2026-10-08',Path(tmp)/'data',Path(tmp)/'normal',night_only=True,catalog=CATALOG)
            s=read_json(Path(tmp)/'data/night_session/2026-10-08.json')
            self.assertEqual(s['collection_state'],'no_scheduled_night_session')
            self.assertEqual(s['records'],[])

    def test_busy_guard_prevents_even_a_cached_request(self):
        with TemporaryDirectory() as tmp:
            sdk=FakeWind()
            def busy():
                raise RuntimeError('desk busy')
            api=CachedWind(sdk,tmp,busy,interval=0)
            with self.assertRaisesRegex(RuntimeError,'desk busy'):
                api.call('wset','futurecc','anything')
            self.assertEqual(sdk.calls,0)
