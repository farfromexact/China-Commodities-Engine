from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from test_wind_daily_collect import FakeWind, CATALOG, response
from china_commodities.wind_daily import ZONE, DEFAULTS, plan, intraday_window
from china_commodities.wind_daily_collect import CachedWind
from china_commodities.wind_intraday import collect_intraday, quote, current_market_snapshot
from china_commodities.storage import read_json, write_json_atomic


class MinuteWind(FakeWind):
    missing = False
    future = False

    def wsi(self, code, fields, start, end, options):
        self.calls += 1
        when = datetime.fromisoformat(end) + timedelta(minutes=5 if self.future else 0)
        if self.missing and code.startswith('AU'):
            return response(fields.upper().split(','), [], codes=[code], times=[]).__class__(
                ErrorCode=0, Fields=fields.upper().split(','), Codes=[code], Times=[], Data=[[] for _ in range(5)])
        return response(fields.upper().split(','), [[100.,102.,99.,101.,10.]], codes=[code], times=[when])


class IntradayTests(unittest.TestCase):
    def window(self):
        return intraday_window(datetime(2026,11,13,10,23,44,tzinfo=ZONE))

    def test_cutoff_rounding_and_same_result_in_other_host_timezone(self):
        local = datetime(2026,11,13,10,23,44,tzinfo=ZONE)
        window = intraday_window(local)
        self.assertEqual(window['cutoff'], '2026-11-13T10:15:00+08:00')
        self.assertEqual(window, intraday_window(local.astimezone(timezone.utc)))
        self.assertEqual(intraday_window(datetime(2026,11,13,10,20,tzinfo=ZONE))['cutoff'],window['cutoff'])

    def test_day_close_switch_and_lunch_keeps_intraday_mode(self):
        settings = dict(DEFAULTS,start_date='2026-11-13')
        with TemporaryDirectory() as tmp, patch('china_commodities.wind_daily.full_complete',return_value=False):
            for hour,minute in [(10,20),(12,0),(14,59)]:
                work = plan(tmp,settings,datetime(2026,11,13,hour,minute,tzinfo=ZONE))
                self.assertIn('intraday',work)
                self.assertEqual(work['full_dates'],[])
            work = plan(tmp,settings,datetime(2026,11,13,15,0,tzinfo=ZONE))
            self.assertNotIn('intraday',work)
            self.assertEqual(work['full_dates'],['2026-11-13'])

    def test_friday_night_and_saturday_early_hours_belong_to_monday(self):
        evening = intraday_window(datetime(2026,11,13,23,0,tzinfo=ZONE))
        morning = intraday_window(datetime(2026,11,14,1,0,tzinfo=ZONE))
        self.assertEqual(evening['trading_date'],'2026-11-16')
        self.assertEqual(evening['session_start'],morning['session_start'])
        self.assertIsNone(intraday_window(datetime(2026,11,14,12,0,tzinfo=ZONE)))
        self.assertIsNone(intraday_window(datetime(2026,9,30,22,0,tzinfo=ZONE)))

    def test_quotes_publish_separately_and_repeat_cutoff_reuses_cache(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json_atomic(root/'data/latest.json',dict(trade_date='2026-11-12',futures_contracts=[]))
            before = (root/'data/latest.json').read_bytes()
            sdk = MinuteWind()
            api = CachedWind(sdk,root/'raw',lambda:None,interval=0)
            result = collect_intraday(api,self.window(),root/'data',catalog=CATALOG)
            snapshot = read_json(root/'data/intraday/latest.json')
            self.assertEqual(result['coverage']['futures']['quoted'],5)
            self.assertGreater(result['coverage']['options']['quoted'],0)
            for row in snapshot['futures_contracts']+snapshot['options_contracts']:
                self.assertEqual(row['source_timestamp'],self.window()['cutoff'])
                self.assertIsNone(row['settle'])
                self.assertIsNone(row['greeks'])
            self.assertEqual((root/'data/latest.json').read_bytes(),before)
            calls = sdk.calls
            collect_intraday(api,self.window(),root/'data',catalog=CATALOG)
            self.assertEqual(sdk.calls,calls)
            self.assertEqual(current_market_snapshot(root/'data')['mode'],'intraday')
            write_json_atomic(root/'data/latest.json',dict(trade_date='2026-11-13'))
            self.assertEqual(current_market_snapshot(root/'data')['mode'],'eod')

    def test_no_bar_remains_missing_without_previous_close_substitution(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            sdk=MinuteWind(); sdk.missing=True
            api=CachedWind(sdk,root/'raw',lambda:None,interval=0)
            collect_intraday(api,self.window(),root/'data',catalog=CATALOG)
            snapshot=read_json(root/'data/intraday/latest.json')
            row=next(r for r in snapshot['futures_contracts'] if r['product']=='AU')
            self.assertEqual(row['record_state'],'missing')
            self.assertIsNone(row['close'])
            self.assertIn('SHFE:AU',snapshot['coverage']['missing_option_products'])

    def test_future_bar_blocks_publication_and_is_evicted_from_cache(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            sdk=MinuteWind(); sdk.future=True
            api=CachedWind(sdk,root/'raw',lambda:None,interval=0)
            with self.assertRaisesRegex(ValueError,'outside cutoff'):
                collect_intraday(api,self.window(),root/'data',catalog=CATALOG)
            self.assertFalse((root/'data/intraday/latest.json').exists())
            self.assertFalse(list((root/'raw').glob('wsi/*.json')))

    def test_opening_without_a_delayed_complete_bar_makes_no_vendor_request(self):
        with TemporaryDirectory() as tmp:
            sdk=MinuteWind()
            api=CachedWind(sdk,Path(tmp)/'raw',lambda:None,interval=0)
            window=intraday_window(datetime(2026,11,13,9,5,tzinfo=ZONE))
            with self.assertRaisesRegex(ValueError,'5'):
                collect_intraday(api,window,Path(tmp)/'data',catalog=CATALOG)
            self.assertEqual(sdk.calls,0)
