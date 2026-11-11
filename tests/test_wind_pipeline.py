import unittest
from unittest.mock import patch
import pandas as pd
from china_commodities.pipeline import run_pipeline


class WindPipelineTests(unittest.TestCase):
    def frames(self):
        return {ex:pd.DataFrame([dict(date='20261111',symbol=code,variety=product,
                    open=100,high=102,low=99,close=101,settle=101,pre_settle=100,
                    volume=10,open_interest=20,turnover=50,source_provider='wind',
                    source_code=code,source_endpoint='WindPy.wsd')])
                for ex,code,product in [('SHFE','AU2612','AU'),('INE','SC2612','SC'),
                                       ('DCE','M2701','M'),('CZCE','SA701','SA'),('GFEX','SI2701','SI')]}

    def test_wind_is_labeled_and_does_not_call_ifind(self):
        with patch('china_commodities.pipeline.IFindHTTPClient', side_effect=AssertionError('iFinD called')):
            result=run_pipeline('2026-11-11',provider='wind',wind_prefetched=self.frames(),
                                include_options=False,include_official_auxiliary=False,publish=False)
        self.assertTrue(result.verified,result.validation_errors)
        self.assertEqual(result.primary_provider,'wind')
        self.assertFalse(result.core_futures_official_complete)
        self.assertEqual(result.module_quality['futures'],'verified_vendor_primary')
        self.assertTrue(all(r['source_provider']=='wind' for r in result.futures_records))

    def test_missing_exchange_blocks_publication(self):
        frames=self.frames()
        del frames['DCE']
        result=run_pipeline('2026-11-11',provider='wind',wind_prefetched=frames,
                            include_options=False,include_official_auxiliary=False,publish=False)
        self.assertFalse(result.verified)
