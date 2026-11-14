"""Offline regression cases from the November report audit; never loads Wind."""
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from china_commodities.storage import read_json, write_json_atomic, _snapshot_payload
from china_commodities.reporting import build_report_input
from china_commodities.historical_features import build_market_state, _make_history_observations
from china_commodities.index_analytics import analyze, ANALYTICS_VERSION
from china_commodities.option_greeks import black76_price
from china_commodities.option_sampling import bounded_strikes
from china_commodities.wind_daily import DEFAULTS, config
from china_commodities.wind_daily_collect import CachedWind, batched_eod, directory, futures
from china_commodities.wind_history import extend_plan, collect_futures_history
from china_commodities.wind_index import collect_eod, rebuild_analytics
from test_historical_features import _snapshot
from test_index_analytics import chain
from test_wind_daily_collect import FakeWind, CATALOG, response
from test_wind_index import IndexWind


class BatchWind(FakeWind):
    def __init__(self):
        super().__init__()
        self.quote_calls = []

    def wsd(self, code, fields, start, end, options):
        self.quote_calls.append((code, fields))
        if ',' not in code:
            return super().wsd(code, fields, start, end, options)
        if self.fail:
            raise AssertionError('complete/cache must avoid SDK calls')
        values = dict(open=100., high=102., low=99., close=101., settle=100.,
                      pre_settle=100., volume=10., oi=20., amt=10000.)
        return response([fields.upper()], [[values[fields]] for c in code.split(',')],
                        codes=code.split(','), times=[datetime.fromisoformat(start)-timedelta(days=int(self.stale))])


class QualityHistoryTests(unittest.TestCase):
    def test_old_successful_foundation_batch_is_not_fresh_for_new_report(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json_atomic(root/'latest.json', dict(trade_date='2026-11-13'))
            for name in ('physical', 'external'):
                write_json_atomic(root/name/'latest.json', dict(requested_date='2026-11-12',
                    generated_at='2026-11-14T20:00:00+08:00', series=[dict(source_date='2026-11-12')]))
                write_json_atomic(root/name/'last_run_status.json',dict(data_fresh=True,validation_passed=True,published=True))
            report = build_report_input(root)
            for name in ('physical','external'):
                self.assertFalse(report[name]['data_fresh'])
                self.assertTrue(report[name]['batch_data_fresh'])
                self.assertEqual(report[name]['expected_date'],'2026-11-13')
                self.assertIn(name+'_module_not_fresh',report['quality']['limitations'])
                summary = next(m for m in report['module_freshness'] if m['module']==name)
                self.assertFalse(summary['report_date_matches'])

    def test_rv_gap_cannot_be_hidden_by_twenty_older_observations(self):
        snapshots = []
        day = date(2026,10,15)
        while day <= date(2026,11,13):
            if day.weekday()<5:
                snapshots.append(_snapshot(str(day),settle=100+len(snapshots),settle_return_pct=.1*len(snapshots)))
            day += timedelta(days=1)
        complete = build_market_state(snapshots)['products'][0]
        missing = build_market_state([s for s in snapshots if s['trade_date'] not in ('2026-11-03','2026-11-04')])['products'][0]
        self.assertIsNotNone(complete['realized_vol_20d_annualized_pct'])
        self.assertIsNone(missing['realized_vol_20d_annualized_pct'])
        self.assertIsNotNone(missing['realized_vol_sample_annualized_pct'])
        self.assertEqual(missing['realized_vol_window']['missing_dates'],['2026-11-03','2026-11-04'])

    def test_dense_wings_qualify_but_same_sparse_put_cannot_be_two_targets(self):
        sparse = chain()
        result = analyze(sparse,'2026-11-13')['products']['IO']['expiries'][0]['metrics']
        self.assertIsNone(result['rr25'])
        self.assertIsNotNone(result['approximate_metrics']['rr25'])
        dense = []
        years = 35/365
        for k in range(75,126):
            for side in ('C','P'):
                row = dict(sparse[0],strike=k,option_type=side,source_code=f'IO2612-{side}-{k}.CFE',
                           contract=f'IO2612-{side}-{k}')
                row['settle'] = row['close'] = black76_price(100,k,years,.015,.25,side)
                dense.append(row)
        good = analyze(dense,'2026-11-13')['products']['IO']['expiries'][0]['metrics']
        self.assertTrue(good['skew_ready'])
        self.assertAlmostEqual(good['rr25'],0,places=6)
        duplicated = chain(expiry='2026-11-20')
        observed = analyze(duplicated,'2026-11-13')['products']['IO']['expiries'][0]['metrics']
        for side in ('C','P'):
            wings = {abs(w['target_delta']):w for w in observed['wing_observations'] if w['side']==side}
            if wings[.1]['strike'] == wings[.25]['strike']:
                self.assertFalse(wings[.1]['target_ready'])

    def test_no_single_day_return_is_inferred_across_missing_dates_or_settlement(self):
        def observations(days):
            rows=[(day,{}, {('SHFE','RB2701'):dict(settle=settle)}) for day,settle in days]
            return _make_history_observations(rows,'SHFE','RB2701')[0]
        gap=observations([('2026-11-02',100),('2026-11-05',110)])
        self.assertIsNone(gap[-1]['settle_return_pct'])
        missing=observations([('2026-11-11',100),('2026-11-12',None),('2026-11-13',110)])
        self.assertIsNone(missing[-1]['settle_return_pct'])

    def test_selection_reprojects_stale_delta_and_keeps_distinct_wings_within_budget(self):
        old = [dict(strike=80,delta=-.25,iv_percent=20,open_interest=999)]
        strikes, details = bounded_strikes(range(60,141),100,35/365,7,old)
        self.assertEqual(len(strikes),7)
        self.assertIn(100,strikes)
        wings = details['wing_selection']
        self.assertEqual(len({w['strike'] for w in wings}),4)
        self.assertNotEqual(wings[0]['strike'],80)
        self.assertIn(80,strikes)  # prior OI still useful, but not labelled a current delta wing

    def test_history_budget_finds_old_gaps_without_replaying_daily_layers(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            write_json_atomic(root/'latest.json',dict(trade_date='2026-11-13',futures_contracts=[{}]))
            work=dict(latest_completed_eod='2026-11-13',has_work=False,start_date='2026-11-12',
                      full_dates=[],remaining_dates=[],index_dates=[],remaining_index_dates=[])
            with patch('china_commodities.wind_history.futures_complete',side_effect=lambda r,d:d not in ('2026-11-03','2026-11-04')), \
                 patch('china_commodities.wind_index.complete',return_value=False):
                result=extend_plan(deepcopy(work),root,DEFAULTS)
                self.assertEqual([x['date'] for x in result['history_tasks']],['2026-11-03','2026-11-04'])
                self.assertTrue(all(x['module']=='commodity_futures' for x in result['history_tasks']))
                self.assertTrue(result['deferred_history_tasks'])
                disabled=extend_plan(deepcopy(work),root,DEFAULTS,budget=0)
                self.assertFalse(disabled['has_work'])
                self.assertEqual(disabled['history_tasks'],[])
                intraday=extend_plan(dict(work,intraday={'cutoff':'fixed'}),root,DEFAULTS)
                self.assertEqual(intraday['history_tasks'],[])

    def test_batched_history_validates_dates_preserves_latest_and_never_fetches_options_night(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp); sdk=BatchWind(); api=CachedWind(sdk,root/'raw',lambda:None,0)
            universe,_=directory(api,'2026-11-13',catalog=CATALOG)
            latest=_snapshot_payload(futures(api,'2026-11-13',universe))
            write_json_atomic(root/'latest.json',latest)
            write_json_atomic(root/'snapshots/2026-11-13.json',latest)
            before=(root/'latest.json').read_bytes()
            sdk.quote_calls.clear()
            with patch.object(sdk,'wsi',side_effect=AssertionError('no night requests')):
                collect_futures_history(api,'2026-11-04',root,catalog=CATALOG)
            self.assertEqual(len(sdk.quote_calls),9)
            self.assertEqual((root/'latest.json').read_bytes(),before)
            self.assertFalse((root/'options').exists())
            self.assertFalse((root/'wind_daily/completed/2026-11-04.json').exists())
            sdk.fail=True
            self.assertEqual(collect_futures_history(api,'2026-11-04',root,catalog=CATALOG)['state'],'already_complete')
            sdk.fail=False; sdk.stale=True
            with self.assertRaisesRegex(ValueError,'date/shape'):
                batched_eod(api,sorted(universe),'settle','2026-11-03')
            sdk.stale=False
            self.assertEqual(len(batched_eod(api,sorted(universe),'settle','2026-11-03')),5)

    def test_index_backfill_rebuilds_adjacent_changes_without_rewinding_latest(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp); sdk=IndexWind();api=CachedWind(sdk,root/'raw',lambda:None,0)
            collect_eod(api,'2026-11-13',root,include_books=False)
            first=read_json(root/'index_derivatives/latest.json')
            self.assertIsNone(first['options_contracts'][0]['oi_change'])
            collect_eod(api,'2026-11-12',root,include_books=False)
            latest=read_json(root/'index_derivatives/latest.json')
            self.assertEqual(latest['trade_date'],'2026-11-13')
            prior_codes={r['source_code'] for r in read_json(root/'index_derivatives/snapshots/2026-11-12.json')['options_contracts']}
            shared=[r for r in latest['options_contracts'] if r['source_code'] in prior_codes]
            self.assertTrue(shared)
            self.assertTrue(all(r['oi_change']==0 for r in shared))
            self.assertTrue(all(r['oi_change'] is None for r in latest['options_contracts'] if r['source_code'] not in prior_codes))
            self.assertEqual(latest['analytics']['analytics_version'],ANALYTICS_VERSION)
            self.assertEqual([r['settle'] for r in first['options_contracts']], [r['settle'] for r in latest['options_contracts']])
            sdk.fail=True
            calls=len(sdk.calls)
            rebuild_analytics(root)
            self.assertEqual(len(sdk.calls),calls)

    def test_history_quote_budget_is_45_calls_for_806_contracts_and_retry_is_cached(self):
        with TemporaryDirectory() as tmp:
            sdk=BatchWind();api=CachedWind(sdk,Path(tmp)/'raw',lambda:None,0)
            codes=[f'M{n:04}.DCE' for n in range(806)]
            fields='open,high,low,close,settle,pre_settle,volume,oi,amt'
            self.assertEqual(len(batched_eod(api,codes,fields,'2026-11-04')),806)
            self.assertEqual(len(sdk.quote_calls),45)
            sdk.fail=True
            batched_eod(api,codes,fields,'2026-11-04')
            self.assertEqual(len(sdk.quote_calls),45)
            self.assertEqual(api.metrics['cache_hits'],45)

    def test_history_budget_config_rejects_unbounded_or_negative_values(self):
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.json'
            for value in (-1,11,True,1.5):
                write_json_atomic(path,dict(max_history_days_per_run=value))
                with self.assertRaises(ValueError):config(path)
