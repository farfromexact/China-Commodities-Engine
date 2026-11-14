from copy import deepcopy
from datetime import date
import unittest

from china_commodities.index_analytics import analyze, attach_changes, history_changes, PRICE_BASIS
from china_commodities.option_greeks import black76_price


def chain(day='2026-11-13',expiry='2026-12-18'):
    years=(date.fromisoformat(expiry)-date.fromisoformat(day)).days/365
    rows=[]
    for k in (80,90,100,110,120):
        for side in ('C','P'):
            px=black76_price(100,k,years,.015,.25,side)
            rows.append(dict(product='IO',expiry_date=expiry,strike=k,option_type=side,
                source_code=f'IO2612-{side}-{k}.CFE',contract=f'IO2612-{side}-{k}',exercise_style='european',
                settle=px,close=px,open_interest=20,volume=10,multiplier=100))
    return rows


class AnalyticsTests(unittest.TestCase):
    def test_known_parity_model_wings_and_gamma_units(self):
        rows=chain();result=analyze(rows,'2026-11-13')
        expiry=result['products']['IO']['expiries'][0];metrics=expiry['metrics']
        self.assertAlmostEqual(expiry['forward'],100,places=8)
        self.assertAlmostEqual(metrics['atm_iv'],.25,places=6)
        self.assertAlmostEqual(metrics['rr25'],0,places=6)
        self.assertAlmostEqual(metrics['bf25'],0,places=6)
        self.assertEqual(metrics['pcr_oi'],1)
        self.assertEqual(metrics['pcr_volume'],1)
        self.assertIsNone(metrics['call_oi_change'])
        self.assertEqual(metrics['greek_contract_count'],10)
        self.assertAlmostEqual(rows[0]['gamma_1pct'],rows[0]['gamma']*20*100*100**2*.01)
        self.assertFalse(result['dealer_gex'])

    def test_expiry_and_no_arbitrage_failures_do_not_fabricate_greeks(self):
        rows=chain();rows[0]['settle']=rows[0]['close']=100000
        result=analyze(rows,'2026-11-13')
        self.assertIsNone(rows[0]['iv_percent'])
        rows=chain();analyze(rows,'2026-12-18')
        self.assertTrue(all(r['delta'] is None for r in rows))
        self.assertTrue(all(r['gamma_1pct'] is None for r in rows))

    def test_zero_settlement_falls_back_to_close_and_bad_style_stays_null(self):
        rows=chain();rows[0]['settle']=0;rows[1]['exercise_style']='unknown'
        analyze(rows,'2026-11-13')
        self.assertEqual(rows[0]['analytics_price'],rows[0]['close'])
        self.assertIsNone(rows[1]['gamma'])

    def test_oi_changes_require_verified_adjacent_full_snapshot(self):
        rows=chain();prior=dict(schema_version=2,trade_date='2026-11-12',full_option_chain=True,
                              validation_passed=True,options_contracts=deepcopy(rows))
        prior['options_contracts'][0]['open_interest']=18
        attach_changes(rows,prior,'2026-11-13')
        self.assertEqual(rows[0]['oi_change'],2)
        for key,value in [('trade_date','2026-11-11'),('schema_version',1),('validation_passed',False)]:
            broken=deepcopy(prior);broken[key]=value
            attach_changes(rows,broken,'2026-11-13')
            self.assertIsNone(rows[0]['oi_change'])

    def test_history_missing_day_or_changed_symbol_never_compares_by_position(self):
        def record(day,symbol='IO2612',value=.25):
            return dict(trade_date=day,option_price_basis=PRICE_BASIS,products={'IO':{'expiries':[
                dict(symbol=symbol,metrics=dict(atm_iv=value,rr25=.1))]}})
        records=history_changes([record('2026-11-11'),record('2026-11-12'),record('2026-11-13',value=.30)])
        self.assertAlmostEqual(records[-1]['changes']['1d']['products']['IO']['IO2612']['atm_iv'],.05)
        self.assertIsNone(records[-1]['changes']['3d']['previous_date'])
        records=history_changes([record('2026-11-11'),record('2026-11-13')])
        self.assertIsNone(records[-1]['changes']['1d']['previous_date'])
        records=history_changes([record('2026-11-12','IO2611'),record('2026-11-13')])
        self.assertIsNone(records[-1]['changes']['1d']['products']['IO']['IO2612']['atm_iv'])

    def test_pcr_change_requires_same_selected_contracts_and_scope(self):
        def record(day,codes,scope='report_v1'):
            return dict(trade_date=day,option_price_basis=PRICE_BASIS,options_scope=scope,
                products={'IO':{'expiries':[dict(symbol='IO2612',metrics=dict(atm_iv=.25,pcr_oi=.6,contract_codes=codes))]}})
        changed=history_changes([record('2026-11-12',['A']),record('2026-11-13',['B'])])
        self.assertIsNone(changed[-1]['changes']['1d']['products']['IO']['IO2612']['pcr_oi'])
        same=history_changes([record('2026-11-12',['A']),record('2026-11-13',['A'])])
        self.assertEqual(same[-1]['changes']['1d']['products']['IO']['IO2612']['pcr_oi'],0)
        scope=history_changes([record('2026-11-12',['A']),record('2026-11-13',['A'],'full_chain_v2')])
        self.assertIsNone(scope[-1]['changes']['1d']['previous_date'])


if __name__=='__main__':
    unittest.main()
