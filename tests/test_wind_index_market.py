from copy import deepcopy
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from china_commodities.wind_index_market import parse_book, cash_market, books, BOOK_FIELDS
from china_commodities.wind_index import full_quotes, validate, collect_eod, complete
from china_commodities.wind_daily_collect import CachedWind
from china_commodities.storage import read_json,write_json_atomic
from test_wind_index import IndexWind
from test_wind_daily_collect import response


class MarketTests(unittest.TestCase):
    def test_vendor_clock_not_request_time_or_eod_quote(self):
        values=dict(rt_bid1=.6,rt_ask1=.8,rt_bsize1=55.,rt_asize1=32.,rt_date=20261113.,rt_time=153744.)
        quote=parse_book('IO2611-C-4300.CFE',values,'2026-11-13','2026-11-14T19:54:57+08:00')
        self.assertEqual(quote['quote_date'],'2026-11-13')
        self.assertEqual(quote['record_state'],'out_of_session_vendor_clock')
        self.assertFalse(quote['eligible_for_eod_analytics'])
        self.assertFalse(quote['execution_ready'])
        values['rt_time']=145959.
        self.assertEqual(parse_book('x',values,'2026-11-13','now')['record_state'],'valid_values_time_unverified')
        self.assertEqual(parse_book('x',values,'2026-11-12','now')['record_state'],'stale_or_different_date')
        values['rt_time']=float('nan')
        self.assertEqual(parse_book('x',values,'2026-11-13','now')['record_state'],'missing_or_invalid_vendor_time')

    def test_shares_remain_raw_and_missing_previous_day_is_explicit(self):
        with TemporaryDirectory() as tmp:
            api=CachedWind(IndexWind(),Path(tmp),lambda:None,0)
            cash=cash_market(api,'2026-11-13')
            self.assertEqual(cash['status'],'raw_shares_unit_pending')
            for etf in cash['etfs'].values():
                self.assertEqual(etf['share_change_raw'],10)
                self.assertIsNone(etf['total_shares'])
                self.assertIsNone(etf['estimated_net_creation_redemption_cny'])
                self.assertFalse(etf['share_unit_verified'])
            sdk=IndexWind(); original=sdk.wsd
            def missing(code,fields,start,end,opts):
                if fields=='unit_total':return response(['UNIT_TOTAL'],[[100.]],codes=[code],times=[datetime.fromisoformat(end)])
                return original(code,fields,start,end,opts)
            sdk.wsd=missing
            cash=cash_market(CachedWind(sdk,Path(tmp)/'missing',lambda:None,0),'2026-11-13')
            self.assertTrue(all(e['status']=='missing' for e in cash['etfs'].values()))

    def test_books_ignore_old_cache_and_never_subscribe(self):
        class SDK:
            calls=0
            def wsq(self,codes,fields,options):
                self.calls+=1
                return response(fields.upper().split(','),[[1,.6,.8,55,32,20261113,145959,100]],codes=[codes])
        with TemporaryDirectory() as tmp:
            sdk=SDK();api=CachedWind(sdk,Path(tmp),lambda:None,0)
            with patch('china_commodities.wind_index_market.datetime') as clock:
                from china_commodities.wind_daily import ZONE
                clock.now.return_value=datetime(2026,11,14,19,tzinfo=ZONE)
                # parse_book also needs datetime.strptime.
                clock.strptime.side_effect=datetime.strptime
                for _ in range(2):self.assertEqual(books(api,['IO2611-C-4300.CFE'],'2026-11-13')['observed_contract_count'],1)
                self.assertEqual(books(api,['x'],'2026-11-12')['status'],'not_requested_for_historical_day')
            self.assertEqual(sdk.calls,2)

    def test_batch_wrong_date_identity_or_missing_field_invalidates_cache(self):
        for fault in ('missing','identity','date'):
            with self.subTest(fault=fault),TemporaryDirectory() as tmp:
                sdk=IndexWind();original=sdk.wsd
                def broken(code,fields,start,end,opts):
                    r=original(code,fields,start,end,opts)
                    if fault=='missing':r.Data[0][0]=None
                    if fault=='identity':r.Codes.reverse()
                    if fault=='date':r.Times=[datetime(2026,11,12)]
                    return r
                sdk.wsd=broken;api=CachedWind(sdk,Path(tmp),lambda:None,0)
                with self.assertRaises(ValueError):full_quotes(api,['IO2611-C-100.CFE','IO2611-P-100.CFE'],'2026-11-13')

    def test_entire_paired_expiry_cannot_be_dropped_and_legacy_not_mislabelled(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);api=CachedWind(IndexWind(),root/'raw',lambda:None,0)
            collect_eod(api,'2026-11-13',root,include_books=False,options_scope='full_chain_v2')
            full=read_json(root/'index_derivatives/latest.json')
            broken=deepcopy(full)
            broken['options_contracts']=[r for r in broken['options_contracts'] if r['expiry_date']!='2026-12-18']
            self.assertIn('index option full directory coverage mismatch',validate(broken))
            broken=deepcopy(full);broken['cash_market']['etfs']['IH']['estimated_net_creation_redemption_cny']=100
            self.assertTrue(validate(broken))
            legacy=deepcopy(full);legacy.update(schema_version=1,options_scope='core_v1',full_option_chain=False)
            for plan in legacy['collection_plans']:
                plan.update(full_chain=False,contract_budget=1000)
            self.assertEqual(validate(legacy),[])
            write_json_atomic(root/'index_derivatives/snapshots/2026-11-13.json',legacy)
            self.assertFalse(complete(root,'2026-11-13'))
            collect_eod(api,'2026-11-13',root,include_books=False)
            self.assertEqual(read_json(root/'index_derivatives/legacy_core/snapshots/2026-11-13.json'),legacy)
            self.assertTrue(complete(root,'2026-11-13'))


if __name__=='__main__':
    unittest.main()
