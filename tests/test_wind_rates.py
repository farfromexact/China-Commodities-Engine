from copy import deepcopy
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from china_commodities import wind_treasury as treasury,wind_funding as funding
from china_commodities.wind_daily import ZONE,DEFAULTS,plan
from china_commodities.wind_daily_collect import CachedWind
from china_commodities.storage import read_json,write_json_atomic
from test_wind_daily_collect import response
from china_commodities.reporting import build_report_input
from china_commodities.chat_export import export_chat_data,restore,MAX_BYTES


class RatesWind:
    def __init__(self):
        self.calls=[];self.missing=set();self.bad_name={};self.stale=False;self.expiring=False

    def wset(self,name,parameters):
        self.calls.append(('wset',parameters))
        params=dict(v.split('=',1) for v in parameters.split(';'))
        product=params['wind_code'].split('.')[0]
        return response(['wind_code','contract_issue_date','last_trade_date'],[
            [product+'2612.CFE','2026-01-01','2026-11-13' if self.expiring else '2026-12-11'],
            [product+'2703.CFE','2026-01-01','2027-03-12']])

    def wss(self,code,fields,options):
        self.calls.append(('wss',code))
        spec=next(v for v in funding.INSTRUMENTS.values() if v['source_code']==code)
        name=self.bad_name.get(code,spec['name_token'])
        return response(['SEC_NAME'],[[name]],codes=[code])

    def wsd(self,code,fields,start,end,options):
        self.calls.append(('wsd',code))
        if fields=='close' and code in {v['source_code'] for v in funding.INSTRUMENTS.values()}:
            dates=funding.trading_window(end)
            base=2.0 if code.startswith('R') else 1.5
            return response(['CLOSE'],[[None if code in self.missing and d==end else base+i*.001]
                for i,d in enumerate(dates)],codes=[code],times=[datetime.fromisoformat(d) for d in dates])
        values=dict(open=99.,high=102.,low=98.,close=100.,settle=100.5,pre_settle=99.5,volume=12.,oi=100.,amt=500000.)
        when=datetime(2026,11,12) if self.stale else datetime.fromisoformat(start)
        return response(fields.upper().split(','),[[values[f] for f in fields.split(',')]],codes=[code],times=[when])

    def wsi(self,code,fields,start,end,options):
        self.calls.append(('wsi',code,start,end))
        return response(fields.upper().split(','),[[99.,102.,98.,100.,12.]],codes=[code],times=[datetime.fromisoformat(end)])


class TreasuryTests(unittest.TestCase):
    def test_all_products_units_main_calendar_spread_and_stale_rejection(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);sdk=RatesWind();api=CachedWind(sdk,root/'raw',lambda:None,0)
            treasury.collect_eod(api,'2026-11-13',root)
            p=read_json(root/'treasury_futures/latest.json')
            self.assertEqual(treasury.validate(p),[])
            self.assertEqual(len(p['contracts']),8)
            self.assertTrue(treasury.complete(root,'2026-11-13'))
            self.assertEqual(p['products']['TS']['main_contract']['point_value_cny'],20000)
            self.assertEqual(p['products']['T']['main_contract']['point_value_cny'],10000)
            self.assertIsNone(p['products']['T']['total_oi_change'])
            requests=api.metrics['sdk_requests']
            self.assertGreater(requests,0)
            treasury.collect_eod(api,'2026-11-13',root)
            self.assertEqual(api.metrics['sdk_requests'],requests)
            self.assertGreater(api.metrics['cache_hits'],0)
            for mutation in ('date','coverage','unit'):
                bad=deepcopy(p)
                if mutation=='date':bad['contracts'][0]['source_trade_date']='2026-11-12'
                if mutation=='coverage':bad['contracts'].pop()
                if mutation=='unit':bad['contracts'][0]['price_unit']='yield_percent'
                self.assertTrue(treasury.validate(bad))
        with TemporaryDirectory() as tmp:
            sdk=RatesWind();sdk.stale=True;api=CachedWind(sdk,Path(tmp)/'raw',lambda:None,0)
            with self.assertRaises(ValueError):treasury.collect_eod(api,'2026-11-13',tmp)
            self.assertFalse((Path(tmp)/'treasury_futures/latest.json').exists())

    def test_1515_boundary_expiry_day_and_no_night(self):
        self.assertFalse(treasury.eod_ready('2026-11-13',datetime(2026,11,13,15,0,tzinfo=ZONE)))
        self.assertTrue(treasury.eod_ready('2026-11-13',datetime(2026,11,13,15,15,tzinfo=ZONE)))
        window=treasury.window(datetime(2026,11,13,15,5,tzinfo=ZONE))
        self.assertEqual(window['cutoff'],'2026-11-13T15:00:00+08:00')
        self.assertIsNone(treasury.window(datetime(2026,11,13,21,tzinfo=ZONE)))
        with TemporaryDirectory() as tmp:
            sdk=RatesWind();sdk.expiring=True;api=CachedWind(sdk,Path(tmp)/'raw',lambda:None,0)
            treasury.collect_intraday(api,window,tmp)
            p=read_json(Path(tmp)/'treasury_futures/intraday/latest.json')
            expired=[r for r in p['contracts'] if r['expiry_date']=='2026-11-13']
            self.assertTrue(all(r['source_timestamp'].startswith('2026-11-13T11:30:00') for r in expired))
            self.assertTrue(all(r['lag_to_cutoff_seconds']==12600 for r in expired))
            self.assertFalse((Path(tmp)/'treasury_futures/latest.json').exists())


class FundingTests(unittest.TestCase):
    def test_panel_types_units_bp_changes_and_no_fake_flows(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);sdk=RatesWind();api=CachedWind(sdk,root/'raw',lambda:None,0)
            result=funding.collect_eod(api,'2026-11-13',root)
            p=read_json(root/'funding/latest.json')
            self.assertTrue(result['coverage_complete'])
            self.assertTrue(funding.complete(root,'2026-11-13'))
            self.assertEqual(funding.validate(p),[])
            self.assertEqual(len(p['records']),12)
            dr=next(r for r in p['records'] if r['key']=='DR007')
            self.assertAlmostEqual(dr['changes_bp']['1d'],.1)
            self.assertAlmostEqual(dr['changes_bp']['20d'],2.)
            spread=next(r for r in p['spreads'] if r['key']=='R007_minus_DR007')
            self.assertAlmostEqual(spread['value_bp'],50)
            self.assertEqual(dr['rate_percent'],dr['raw_value'])
            self.assertNotIn('net_flow_cny',p)

    def test_missing_or_wrong_fixing_identity_is_partial_and_retryable(self):
        for bad_name in (False,True):
            with self.subTest(bad_name=bad_name),TemporaryDirectory() as tmp:
                root=Path(tmp);sdk=RatesWind()
                if bad_name:sdk.bad_name['DR007.IB']='FDR007'
                else:sdk.missing.add('DR007.IB')
                api=CachedWind(sdk,root/'raw',lambda:None,0)
                result=funding.collect_eod(api,'2026-11-13',root)
                p=read_json(root/'funding/latest.json')
                self.assertFalse(result['coverage_complete'])
                self.assertEqual(funding.validate(p),[])
                self.assertFalse(funding.complete(root,'2026-11-13'))
                row=next(r for r in p['records'] if r['key']=='DR007')
                self.assertIsNone(row['rate_percent']);self.assertIsNone(row['source_trade_date'])
                self.assertIsNone(next(r for r in p['spreads'] if r['key']=='R007_minus_DR007')['value_bp'])
                sdk.missing.clear();sdk.bad_name.clear()
                self.assertTrue(funding.collect_eod(api,'2026-11-13',root)['coverage_complete'])
        with TemporaryDirectory() as tmp:
            sdk=RatesWind();sdk.bad_name['R001.IB']='DR001'
            api=CachedWind(sdk,Path(tmp)/'raw',lambda:None,0)
            with self.assertRaisesRegex(ValueError,'name'):funding.metadata(api,funding.INSTRUMENTS['R001'])
        with TemporaryDirectory() as tmp:
            sdk=RatesWind();sdk.bad_name['DR007.IB']='存款类机构质押式回购加权利率:7天'
            api=CachedWind(sdk,Path(tmp)/'raw',lambda:None,0)
            self.assertEqual(funding.metadata(api,funding.INSTRUMENTS['DR007']),sdk.bad_name['DR007.IB'])

    def test_intraday_exchange_only_cutoff_capped_and_daily_gate(self):
        self.assertFalse(funding.eod_ready('2026-11-13',datetime(2026,11,13,15,30,tzinfo=ZONE)))
        self.assertTrue(funding.eod_ready('2026-11-13',datetime(2026,11,13,18,tzinfo=ZONE)))
        window=funding.window(datetime(2026,11,13,17,tzinfo=ZONE))
        self.assertEqual(window['cutoff'],'2026-11-13T15:30:00+08:00')
        with TemporaryDirectory() as tmp:
            sdk=RatesWind();api=CachedWind(sdk,Path(tmp)/'raw',lambda:None,0)
            funding.collect_intraday(api,window,tmp)
            calls=[r for r in sdk.calls if r[0]=='wsi']
            self.assertEqual(len(calls),6)
            self.assertTrue(all(not r[1].endswith('.IB') for r in calls))
            self.assertFalse((Path(tmp)/'funding/latest.json').exists())

    def test_exact_live_wind_chinese_short_names_preserve_R_DR_distinction(self):
        with TemporaryDirectory() as tmp:
            sdk=RatesWind()
            sdk.bad_name={key+'.IB':value for key,value in funding.WIND_SHORT_NAMES.items()}
            api=CachedWind(sdk,Path(tmp)/'raw',lambda:None,0)
            for key in funding.WIND_SHORT_NAMES:
                self.assertEqual(funding.metadata(api,funding.INSTRUMENTS[key]),funding.WIND_SHORT_NAMES[key])
            sdk.bad_name['R007.IB']='银存间质押7日'
            spec=funding.INSTRUMENTS['R007'];api.discard('wss',spec['source_code'],'sec_name','')
            with self.assertRaises(ValueError):funding.metadata(api,spec)


class RatesPlanTests(unittest.TestCase):
    def plan(self,root,now,excluded=()):
        with patch('china_commodities.wind_daily.full_complete',return_value=True),patch('china_commodities.wind_daily.night_complete',return_value=True),patch('china_commodities.wind_index.complete',return_value=True):
            return plan(root,dict(DEFAULTS,start_date='2026-11-11',max_days_per_run=2),now,excluded_funding=excluded)

    def test_own_close_boundaries_and_latest_independently_of_commodity(self):
        with TemporaryDirectory() as tmp:
            work=self.plan(tmp,datetime(2026,11,13,15,5,tzinfo=ZONE))
            self.assertEqual(work['treasury_intraday']['cutoff'],'2026-11-13T15:00:00+08:00')
            self.assertEqual(work['treasury_dates'],[])
            self.assertIsNotNone(work['funding_intraday'])
            work=self.plan(tmp,datetime(2026,11,14,19,tzinfo=ZONE))
            self.assertEqual(work['treasury_dates'],['2026-11-11','2026-11-12'])
            self.assertEqual(work['remaining_treasury_dates'],['2026-11-13'])
            self.assertEqual(work['funding_dates'],['2026-11-11','2026-11-12'])
            self.assertTrue(work['has_work'])

    def test_partial_funding_not_retried_in_same_run_but_next_launch_retries(self):
        with TemporaryDirectory() as tmp:
            now=datetime(2026,11,14,19,tzinfo=ZONE)
            work=self.plan(tmp,now,excluded=('2026-11-11','2026-11-12'))
            self.assertEqual(work['funding_dates'],['2026-11-13'])
            self.assertEqual(work['remaining_funding_dates'],[])
            work=self.plan(tmp,now)
            self.assertEqual(work['funding_dates'],['2026-11-11','2026-11-12'])

    def test_report_freshness_and_lossless_chat_exports_keep_partial_state(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);sdk=RatesWind();sdk.missing.add('DR007.IB')
            api=CachedWind(sdk,root/'raw',lambda:None,0)
            treasury.collect_eod(api,'2026-11-13',root)
            funding.collect_eod(api,'2026-11-13',root)
            write_json_atomic(root/'latest.json',dict(trade_date='2026-11-13',futures_contracts=[],source={'provider':'wind'}))
            report=build_report_input(root)
            self.assertEqual(report['treasury_futures']['trade_date'],'2026-11-13')
            self.assertFalse(report['funding']['coverage_complete'])
            status={r['module']:r for r in report['module_freshness']}
            self.assertFalse(status['funding']['data_fresh'])
            self.assertTrue(status['treasury_futures']['data_fresh'])
            write_json_atomic(root/'report_input_latest.json',report)
            export_chat_data(root)
            for name in ('treasury_futures','funding','report'):
                index=read_json(root/f'chat/{name}/index.json')
                pages=[read_json(root/p['path'].removeprefix('data/')) for p in index['parts']]
                self.assertEqual(restore(pages),read_json(root/index['source_path'].removeprefix('data/')))
            self.assertLessEqual((root/'chat/overview.json').stat().st_size,MAX_BYTES)


if __name__=='__main__':
    unittest.main()
