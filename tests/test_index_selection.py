from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from china_commodities.index_selection import select, PAIRS_PER_EXPIRY
from china_commodities.wind_index import option_directory,collect_eod,validate,complete
from china_commodities.wind_daily_collect import CachedWind
from china_commodities.storage import read_json
from china_commodities.chat_export import export_chat_data,restore
from test_wind_index import IndexWind


class SelectionTests(unittest.TestCase):
    def test_report_bounded_two_months_atm_paired_and_full_opt_in(self):
        with TemporaryDirectory() as tmp:
            api=CachedWind(IndexWind(),Path(tmp)/'raw',lambda:None,0)
            directory=option_directory(api,'2026-11-13','IO')
            selected,plan=select(directory,'2026-11-13',100)
            self.assertEqual(len(selected),PAIRS_PER_EXPIRY*2*2)
            self.assertFalse(plan['full_chain'])
            self.assertEqual({r['expiry_date'] for r in selected},{'2026-11-20','2026-12-18'})
            self.assertIn(100,{r['strike'] for r in selected})
            self.assertEqual(len({r['source_code'] for r in selected}),len(selected))
            full,plan=select(directory,'2026-11-13',100,scope='full_chain_v2')
            self.assertEqual(len(full),len(directory))
            self.assertTrue(plan['full_chain'])

    def test_main_month_and_previous_oi_delta_guide_selection_without_querying_all_quotes(self):
        with TemporaryDirectory() as tmp:
            api=CachedWind(IndexWind(),Path(tmp)/'raw',lambda:None,0)
            directory=option_directory(api,'2026-11-13','IO')
            extra=deepcopy([r for r in directory if r['expiry_date']=='2026-12-18'])
            for r in extra:
                r.update(source_code=r['source_code'].replace('2612','2703'),contract=r['contract'].replace('2612','2703'),expiry_date='2027-03-19')
            directory+=extra
            previous=dict(trade_date='2026-11-12',validation_passed=True,options_contracts=[
                dict(product='IO',expiry_date='2026-11-20',strike=90,option_type='P',delta=-.1,iv_percent=20,open_interest=999999)])
            selected,plan=select(directory,'2026-11-13',100,previous=previous,main_month='2703')
            self.assertEqual(len(selected),42)
            self.assertIn('2027-03-19',{r['expiry_date'] for r in selected})
            self.assertIn(90,{r['strike'] for r in selected if r['expiry_date']=='2026-11-20'})
            self.assertEqual(plan['selection_history_date'],'2026-11-12')

    def test_daily_default_is_report_sample_with_honest_metrics_and_request_budget(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);sdk=IndexWind();api=CachedWind(sdk,root/'raw',lambda:None,0)
            collect_eod(api,'2026-11-13',root,include_books=False)
            payload=read_json(root/'index_derivatives/latest.json')
            self.assertEqual(payload['options_scope'],'report_v1')
            self.assertFalse(payload['full_option_chain'])
            self.assertEqual(len(payload['options_contracts']),84)
            self.assertEqual(validate(payload),[])
            self.assertTrue(complete(root,'2026-11-13'))
            self.assertFalse(complete(root,'2026-11-13',scope='full_chain_v2'))
            for product in payload['analytics']['products'].values():
                for expiry in product['expiries']:
                    self.assertEqual(expiry['metrics']['pcr_scope'],'selected_strikes_only')
                    self.assertEqual(expiry['metrics']['contract_count'],14)
            option_batches=[code for method,code in sdk.calls if method=='wsd' and '-C-' in code]
            self.assertEqual(len(option_batches),15)
            self.assertTrue(all(len(c.split(','))<=28 for c in option_batches))
            (root/'report_input_latest.json').write_text('{}',encoding='utf-8')
            export_chat_data(root)
            for name in ('index_derivatives','index_radar','index_history'):
                index=read_json(root/f'chat/{name}/index.json')
                pages=[read_json(root/e['path'].removeprefix('data/')) for e in index['parts']]
                source=read_json(root/index['source_path'].removeprefix('data/'))
                self.assertEqual(restore(pages),source)


if __name__=='__main__':
    unittest.main()
