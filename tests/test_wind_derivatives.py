import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import pandas as pd

from china_commodities.option_quality import assess_option_snapshot_quality
from china_commodities.option_storage import validate_option_snapshot, OptionSnapshotValidationError
from china_commodities.reporting import build_report_input, reconcile_main_status
from china_commodities.history_storage import _option_history_rows

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('wind_publish', ROOT/'scripts/publish_wind_backfill.py')
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class WindDerivativesTests(unittest.TestCase):
    def test_full_night_replay_replaces_day_and_preserves_older_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'history.parquet'
            base=dict(exchange='SHFE',product='AU',contract='AU2612',source_provider='ifind_http')
            pd.DataFrame([dict(base,trading_date='2026-11-09'),
                          dict(base,trading_date='2026-11-10',contract='AU2702')]).to_parquet(path,index=False)
            snap=dict(trading_date='2026-11-10',records=[
                dict(base,trading_date='2026-11-10',source_provider='wind',record_state='night_session'),
                dict(base,trading_date='2026-11-10',contract='AU2702',source_provider='wind',record_state='no_night_trade')])
            publisher.replace_night_history(path,snap)
            publisher.replace_night_history(path,snap)
            result=pd.read_parquet(path)
            self.assertEqual(len(result),2)
            self.assertEqual(set(result.contract),{'AU2612'})
            self.assertEqual(result[result.trading_date=='2026-11-09'].iloc[0].source_provider,'ifind_http')
            self.assertEqual(result[result.trading_date=='2026-11-10'].iloc[0].source_provider,'wind')

    def test_option_publication_requires_cached_source_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            path=root/'raw/options/AU2612C872.SHF.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(dict(error=0,codes=['AU2612C872.SHF'],times=['2026-11-11T00:00:00'])))
            snapshot=dict(source_provider='wind',collection_scope='core_v1',
                          collection_plans=[dict(selected_contract_count=1,directory_contract_count=10)],
                          records=[dict(source_code='AU2612C872.SHF',source_trade_date='2026-11-11')])
            result=publisher.prepare_options(snapshot,root)
            self.assertEqual(result['universe_contract_count'],1)
            self.assertEqual(result['directory_contract_count'],10)
            self.assertEqual(result['records'][0]['collection_scope'],'core_v1')
            snapshot['records'][0]['source_trade_date']='2026-11-10'
            with self.assertRaisesRegex(ValueError,'source date missing'):
                publisher.prepare_options(snapshot,root)

    def test_report_labels_wind_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            docs={
                'last_run_status.json':dict(run_date='2026-11-11',module_quality={},modules=[]),
                'latest.json':dict(trade_date='2026-11-11',source=dict(modules=[dict(dataset='options',scope='full-market',state='skipped')])),
                'options/latest.json':dict(trade_date='2026-11-11',source_provider='wind',collection_scope='core_v1'),
                'options/last_run_status.json':dict(trade_date='2026-11-11',published=True,data_fresh=True,
                    quote_contract_count=2,coverage=dict(publish_eligible=True,scope_complete=True)),
                'options/quality_latest.json':dict(quality=dict(source_date_match_pct=1.,status='core_available',
                    full_product_scope_verified=True,full_chain_verified=False)),
            }
            for name,payload in docs.items():
                path=root/name
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_text(json.dumps(payload))
            status=build_report_input(root)['quality']['futures_status']
            module=next(m for m in status['modules'] if m['dataset']=='options')
            self.assertEqual(module['upstream_source'],'Wind Quant API')
            self.assertEqual(module['source_endpoint'],'WindPy.wsd')
            self.assertEqual(status['module_quality']['options_chain'],'core_available')
            self.assertTrue(reconcile_main_status(root))
            self.assertFalse(reconcile_main_status(root))
            latest=json.loads((root/'latest.json').read_text())
            self.assertEqual(latest['module_quality']['options_chain'],'core_available')
            self.assertEqual(len(latest['source']['modules']),1)
            self.assertEqual(latest['source']['modules'][0]['scope'],'core_sample')

    def test_wind_core_retains_source_date_gate_and_no_full_chain_claim(self):
        record = dict(trade_date='2026-11-11',source_trade_date='2026-11-11',source_date_match=True,
                      source_provider='wind',contract='AU2612C872',underlying_contract='AU2612',
                      exchange='SHFE',product='AU',option_type='C',strike=872,
                      greeks=dict(quality='vendor_reported',units_as_reported=False,
                                  selected=dict(iv_percent=20,delta=.5,gamma=.01)))
        snapshot = dict(trade_date='2026-11-11',collection_scope='core_v1',
                        universe_source='wind_optionchain',records=[record])
        validate_option_snapshot(snapshot)
        quality = assess_option_snapshot_quality(snapshot)
        self.assertEqual(quality['status'],'core_available')
        self.assertTrue(quality['all_inputs_wind'])
        self.assertFalse(quality['all_inputs_ifind'])
        self.assertFalse(quality['full_chain_verified'])
        self.assertFalse(quality['execution_ready'])
        self.assertFalse(quality['vendor_risk_units_as_reported'])
        self.assertEqual(_option_history_rows(snapshot)[0]['original_source'],'Wind Quant API')
        record['source_trade_date']='2026-11-10'
        with self.assertRaises(OptionSnapshotValidationError):
            validate_option_snapshot(snapshot)

    def test_night_aggregates_across_midnight_against_prior_eod(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache=Path(tmp)
            path=cache/'night/2026-11-11/AU2612.SHF.json'
            path.parent.mkdir(parents=True)
            bars=[dict(timestamp='2026-11-10T21:05:00',open=100,high=104,low=99,close=103,volume=3),
                  dict(timestamp='2026-11-11T00:05:00',open=103,high=106,low=102,close=105,volume=4),
                  dict(timestamp='2026-11-11T00:10:00',open=105,high=105,low=105,close=105,volume=0)]
            path.write_text(json.dumps(bars))
            prior=dict(trade_date='2026-11-10',futures_contracts=[dict(exchange='SHFE',contract='AU2612',
                       product='AU',close=100,settle=100,open_interest=10)])
            snap,status=publisher.build_night('2026-11-11',{'AU2612.SHF':dict(variety='AU')},prior,cache)
            r=snap['records'][0]
            self.assertEqual(r['night_close'],105)
            self.assertEqual(r['volume'],7)
            self.assertAlmostEqual(r['return_vs_settlement_pct'],5)
            self.assertEqual(r['ohlcv_scope'],'completed_night_5min_bars')
            self.assertEqual(r['source_provider'],'wind')
            self.assertIsNone(r['settlement'])
            self.assertTrue(status['validation_passed'])
            (cache/'night_session').mkdir()
            (cache/'night_session/latest.json').write_text(json.dumps(snap))
            (cache/'night_session/last_run_status.json').write_text(json.dumps(status))
            report=build_report_input(cache)['night_session']
            self.assertEqual(report['ohlcv_scope'],'completed_night_5min_bars')
            self.assertEqual(report['products']['SHFE:AU']['representative_contract']['price_semantics'],
                             'last_completed_bar_close')
            bars.append(dict(bars[0],timestamp='2026-11-11T09:05:00'))
            path.write_text(json.dumps(bars))
            with self.assertRaisesRegex(ValueError,'outside window'):
                publisher.build_night('2026-11-11',{'AU2612.SHF':dict(variety='AU')},prior,cache)
