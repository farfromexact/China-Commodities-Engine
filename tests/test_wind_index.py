from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from china_commodities.wind_daily import DEFAULTS, ZONE, plan
from china_commodities.wind_daily_collect import CachedWind
from china_commodities.wind_index import OPTIONS, collect_eod, collect_intraday, complete, validate, report_view
from china_commodities.storage import read_json, write_json_atomic
from test_wind_daily_collect import response


class IndexWind:
    def __init__(self):
        self.stale=False
        self.wrong_underlying=False
        self.fail=False
        self.calls=[]

    def wset(self,name,parameters):
        self.calls.append((name,parameters))
        if self.fail: raise AssertionError('should reuse cache')
        params=dict(part.split('=',1) for part in parameters.split(';'))
        if name=='futurecc':
            product=params['wind_code'].split('.')[0]
            return response(['wind_code','contract_issue_date','last_trade_date'],
                            [[product+'2611.CFE','2026-01-01','2026-11-20']])
        index=params['us_code']
        product=next(k for k,v in OPTIONS.items() if v==index)
        rows=[]
        for month,expiry in [('2611','2026-11-20'),('2612','2026-12-18')]:
            for strike in range(90,111):
                for side,kind in [('C','认购'),('P','认沽')]:
                    rows.append(['BAD' if self.wrong_underlying else index,product+'.CFE',
                        f'{product}{month}-{side}-{strike}.CFE','欧式',strike,kind,'2026-01-01',expiry,100])
        return response(['us_code','option_var','option_code','exe_type','strike_price','call_put',
                         'first_tradedate','last_tradedate','multiplier'],rows)

    def wsd(self,code,fields,start,end,options):
        self.calls.append(('wsd',code))
        if self.fail: raise AssertionError('should reuse cache')
        values=dict(open=99.,high=102.,low=98.,close=100.,settle=100.,pre_settle=99.,pre_close=99.,volume=12.,oi=30.,amt=100000.,
                    unit_total=1000000.,us_impliedvol=None,delta=.5,gamma=.01)
        codes=code.split(',')
        times=[datetime.fromisoformat(start)-timedelta(days=int(self.stale))]
        if fields=='unit_total':
            times.append(datetime.fromisoformat(end)-timedelta(days=int(self.stale)))
            return response(['UNIT_TOTAL'],[[1000000.],[1000010.]],codes=codes,times=times)
        rows=[]
        for c in codes:
            q=dict(values)
            if '-C-' in c or '-P-' in c:
                strike=float(c.rsplit('-',1)[1].split('.')[0])
                px=10+(100-strike)/2*(1 if '-C-' in c else -1)
                q.update(close=px,settle=px,pre_settle=px)
            rows.append([q[f] for f in fields.split(',')])
        return response(fields.upper().split(','),rows,codes=codes,times=times)

    def wsi(self,code,fields,start,end,options):
        self.calls.append(('wsi',code))
        return response(fields.upper().split(','),[[99.,102.,98.,100.,12.]],codes=[code],times=[datetime.fromisoformat(end)])


class IndexTests(unittest.TestCase):
    def test_eod_complete_scope_dates_units_cache_and_report(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp); sdk=IndexWind(); api=CachedWind(sdk,root/'raw',lambda:None,0)
            write_json_atomic(root/'latest.json',{'trade_date':'2026-11-13','sentinel':'commodity'})
            result=collect_eod(api,'2026-11-13',root,include_books=False,options_scope='full_chain_v2')
            data=read_json(root/'index_derivatives/latest.json')
            self.assertEqual(result['futures_contract_count'],4)
            self.assertEqual({r['product'] for r in data['options_contracts']},set(OPTIONS))
            self.assertEqual(validate(data),[])
            self.assertTrue(complete(root,'2026-11-13'))
            self.assertEqual({r['expiry_date'] for r in data['options_contracts']},{'2026-11-20','2026-12-18'})
            self.assertEqual(len(data['options_contracts']),252)
            self.assertEqual(data['options_scope'],'full_chain_v2')
            self.assertTrue(any(r['iv_percent'] is not None for r in data['options_contracts']))
            self.assertEqual(data['futures_contracts'][0]['turnover'],100000.)
            self.assertEqual(data['futures_contracts'][0]['turnover_unit'],'CNY')
            self.assertEqual(read_json(root/'latest.json')['sentinel'],'commodity')
            self.assertTrue(report_view(root,'2026-11-13')['eod_date_matches'])
            self.assertFalse(report_view(root,'2026-11-16')['eod_date_matches'])
            sdk.fail=True
            collect_eod(api,'2026-11-13',root,include_books=False,options_scope='full_chain_v2')
            for mutation in ('date','missing_product','missing_quote','duplicate','missing_side'):
                bad=deepcopy(data)
                if mutation=='date': bad['options_contracts'][0]['source_trade_date']='2026-11-12'
                if mutation=='missing_product': bad['futures_contracts']=[r for r in bad['futures_contracts'] if r['product']!='IC']
                if mutation=='missing_quote': bad['futures_contracts'][0]['close']=None
                if mutation=='duplicate': bad['options_contracts'].append(bad['options_contracts'][0])
                if mutation=='missing_side': bad['options_contracts'].pop(0)
                with self.subTest(mutation=mutation): self.assertTrue(validate(bad))

    def test_stale_quote_and_wrong_underlying_never_publish(self):
        for attribute in ('stale','wrong_underlying'):
            with self.subTest(attribute=attribute),TemporaryDirectory() as tmp:
                sdk=IndexWind();setattr(sdk,attribute,True)
                api=CachedWind(sdk,Path(tmp)/'raw',lambda:None,0)
                with self.assertRaises(ValueError): collect_eod(api,'2026-11-13',tmp)
                self.assertFalse((Path(tmp)/'index_derivatives/latest.json').exists())

    def test_intraday_day_only_no_fake_night_and_no_eod_overwrite(self):
        with TemporaryDirectory() as tmp:
            sdk=IndexWind();api=CachedWind(sdk,Path(tmp)/'raw',lambda:None,0)
            window=dict(trading_date='2026-11-13',session='night',session_start='2026-11-12T21:00:00+08:00',
                        cutoff='2026-11-12T22:00:00+08:00',started_at='2026-11-12T22:05:00+08:00')
            self.assertEqual(collect_intraday(api,window,tmp)['state'],'no_night_session')
            self.assertEqual(sdk.calls,[])
            window.update(session='day',cutoff='2026-11-13T09:25:00+08:00')
            self.assertEqual(collect_intraday(api,window,tmp)['state'],'no_completed_day_bar')
            window['cutoff']='2026-11-13T10:15:00+08:00'
            collect_intraday(api,window,tmp)
            data=read_json(Path(tmp)/'index_derivatives/intraday/latest.json')
            self.assertEqual(data['session_start'],'2026-11-13T09:30:00+08:00')
            self.assertEqual(data['coverage']['futures']['quoted'],4)
            self.assertEqual(len(data['collection_plans']),3)
            self.assertFalse((Path(tmp)/'index_derivatives/latest.json').exists())

    def test_existing_commodity_completion_cannot_hide_missing_index_dates(self):
        with TemporaryDirectory() as tmp,patch('china_commodities.wind_daily.full_complete',return_value=True),patch('china_commodities.wind_daily.night_complete',return_value=True):
            now=datetime(2026,11,14,19,tzinfo=ZONE)
            work=plan(tmp,dict(DEFAULTS,start_date='2026-11-11',max_days_per_run=2),now)
            self.assertEqual(work['full_dates'],[])
            self.assertEqual(work['index_dates'],['2026-11-11','2026-11-12'])
            self.assertEqual(work['remaining_index_dates'],['2026-11-13'])
            self.assertTrue(work['has_work'])


if __name__=='__main__':
    unittest.main()
