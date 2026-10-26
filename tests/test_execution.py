from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys

from china_commodities.execution import canonical_contract, gate_card, portfolio_pnl, quote_book
from china_commodities.reporting import build_report_input


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.day = '2026-10-21'
        self.snapshot = json.loads((Path(__file__).parent / 'fixtures/sa_jm_20261021.json').read_text(encoding='utf-8'))
        quotes = quote_book(self.snapshot)['quotes']
        self.legs = []
        self.meta = {'trade_date':self.day,'contracts':[]}
        for code, side, lots, multiplier in [('SA2701','long',5,20),('JM2701','short',2,60)]:
            q = next(q for q in quotes if q['contract_code']==code and q['price_type']=='close')
            self.legs.append(dict(quote=q, side=side, lots=lots, multiplier=multiplier,
                                  round_trip_fee_per_lot=10, round_trip_slippage_price=1))
            # Synthetic verified metadata for arithmetic/gate tests; not a production fallback.
            self.meta['contracts'].append(dict(contract=q['exchange_contract_code'],exchange=q['exchange'],
                source_date=self.day,vendor='test_fixture',multiplier=multiplier,last_trading_day='2027-01-15'))
        self.card = dict(id='test',research={'thesis':'Keep inventory research intact'},legs=self.legs,
                         risk=dict(max_loss_cny=3000,take_profit_cny=5000))

    def gate(self, card=None, **kwargs):
        return gate_card(card or self.card,self.snapshot,self.meta,as_of_date=kwargs.get('day',self.day))

    def test_real_snapshot_spread(self):
        self.assertEqual(self.legs[0]['quote']['price']-self.legs[1]['quote']['price'],-461)

    def test_alias_and_decade_roll(self):
        self.assertEqual(canonical_contract('SA701','CZCE',self.day),'SA2701')
        self.assertEqual(canonical_contract('SA001','CZCE','2029-12-20'),'SA3001')
        self.assertEqual(canonical_contract('JM2701','DCE',self.day),'JM2701')
        self.card['legs'][0]['quote']['contract_code']='SA701'
        self.assertIsNotNone(self.gate()['execution'])
        for code,exchange in [('SA888','CZCE'),('SA2701C1000','CZCE'),('JM701','DCE'),('SA2701','DCE')]:
            with self.subTest(code=code),self.assertRaises(ValueError): canonical_contract(code,exchange,self.day)

    def test_spot_and_forged_futures_label_blocked(self):
        for patch in [{'price_type':'spot'},{'price':1061.43},{'source':'spot_source'},
                      {'snapshot_id':'fake'},{'observed_at':None},{'source_date':'2026-10-20'}]:
            with self.subTest(patch=patch):
                card=deepcopy(self.card);card['legs'][0]['quote'].update(patch)
                result=self.gate(card)
                self.assertIsNone(result['execution']);self.assertEqual(result['research'],card['research'])

    def test_unsynchronized_underlying_and_stale_request(self):
        self.assertIsNone(self.gate(day='2026-10-23')['execution'])
        row=next(r for r in self.snapshot['futures_contracts'] if r['contract']=='JM2701')
        row['source_trade_date']='2026-10-20'
        self.assertIsNone(self.gate()['execution'])

    def test_mixed_close_settle_rejected(self):
        self.card['legs'][1]['quote']=next(q for q in quote_book(self.snapshot)['quotes']
            if q['contract_code']=='JM2701' and q['price_type']=='settle')
        self.assertIn('mixed_price_types',self.gate()['validation_errors'])

    def test_pnl_notional_and_explicit_costs(self):
        p=portfolio_pnl(self.legs,[959,1390])
        self.assertEqual(p['gross_pnl'],3400)
        self.assertEqual(p['costs'],290)
        self.assertEqual(p['net_pnl'],3110)
        self.assertEqual(p['long_notional'],94900)
        self.assertEqual(p['short_notional'],169200)
        self.assertEqual(p['net_notional'],-74300)
        # Identical spread move can have different position PnL with unequal tonnage.
        self.assertNotEqual(p['gross_pnl'],portfolio_pnl(self.legs,[969,1400])['gross_pnl'])

    def test_invalid_lots_costs_and_risk(self):
        for field,value in [('lots',1.5),('lots',True),('multiplier',60),
                            ('round_trip_fee_per_lot',-1),('round_trip_slippage_price',float('nan'))]:
            with self.subTest(field=field):
                card=deepcopy(self.card);card['legs'][0][field]=value
                self.assertIsNone(self.gate(card)['execution'])
        self.card['risk']['max_loss_cny']=10
        self.assertIsNone(self.gate()['execution'])

    def test_metadata_missing_stale_expired(self):
        for patch in [{'multiplier':None},{'source_date':None},{'is_stale':True},{'last_trading_day':'2026-09-01'}]:
            with self.subTest(patch=patch):
                meta=deepcopy(self.meta);meta['contracts'][1].update(patch)
                self.assertIsNone(gate_card(self.card,self.snapshot,meta,as_of_date=self.day)['execution'])

    def test_duplicate_contract_and_unverified_snapshot(self):
        self.snapshot['futures_contracts'].append(deepcopy(next(r for r in self.snapshot['futures_contracts'] if r['contract']=='SA701')))
        self.assertIsNone(self.gate()['execution'])
        self.snapshot['verified']=False
        self.assertEqual(quote_book(self.snapshot)['quotes'],[])

    def test_raw_execution_fields_not_passed_through(self):
        self.card['stop_spread']=230
        self.assertIsNone(self.gate()['execution'])

    def test_corrupt_snapshot_does_not_promote_quotes(self):
        self.snapshot['futures_contracts'][0]['close']=float('nan')
        self.assertEqual(quote_book(self.snapshot)['quotes'],[])
        self.assertIsNone(self.gate()['execution'])

    def test_arithmetic_overflow_fails_closed(self):
        for lots in (1e308, 10 ** 1000):
            with self.subTest(lots_type=type(lots).__name__):
                card=deepcopy(self.card)
                card['legs'][0]['lots']=lots
                self.assertIsNone(self.gate(card)['execution'])

    def test_cli_writes_sanitized_output_and_failure_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for name,value in [('snapshot.json',self.snapshot),('metadata.json',self.meta)]:
                (root/name).write_text(json.dumps(value),encoding='utf-8')
            command=[sys.executable,'-X','utf8',str(Path(__file__).parents[1]/'scripts/validate_trade_cards.py'),
                     '--snapshot',str(root/'snapshot.json'),'--metadata',str(root/'metadata.json'),
                     '--candidates',str(root/'cards.json'),'--as-of-date',self.day,'--output',str(root/'out.json')]
            for invalid in (False,True):
                with self.subTest(invalid=invalid):
                    card=deepcopy(self.card)
                    if invalid: card['legs'][0]['quote']['price_type']='spot'
                    (root/'cards.json').write_text(json.dumps([card]),encoding='utf-8')
                    run=subprocess.run(command,capture_output=True,text=True)
                    self.assertEqual(run.returncode,int(invalid),run.stderr)
                    result=json.loads((root/'out.json').read_text(encoding='utf-8'))[0]
                    self.assertEqual(result['research'],card['research'])
                    self.assertEqual(result['execution'] is None,invalid)

    def test_report_boundary_preserves_research_and_blocks_bad_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'trade_cards').mkdir()
            self.card['legs'][0]['quote']['price_type']='spot'
            for name,value in [('latest.json',self.snapshot),('contract_meta.json',self.meta),
                               ('trade_cards/candidates.json',[self.card])]:
                (root/name).write_text(json.dumps(value),encoding='utf-8')
            report=build_report_input(root)
            self.assertTrue(report['execution_prices']['quotes'])
            self.assertEqual(report['trade_cards'][0]['status'],'research_only')
            self.assertEqual(report['trade_cards'][0]['research'],self.card['research'])
            self.assertIsNone(report['trade_cards'][0]['execution'])


if __name__=='__main__': unittest.main()
