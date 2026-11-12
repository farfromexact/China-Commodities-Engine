from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from scripts import wind_oneclick as launcher


class OneClickTests(unittest.TestCase):
    def test_both_collectors_overlap_and_failure_is_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            roots={key:root/key for key in ('stock','commodities')}
            configs={key:root/(key+'.json') for key in roots}
            barrier=threading.Barrier(2,timeout=5)
            calls=[]
            def invoke(repo,args,log):
                calls.append((repo.name,args))
                if 'tick' in args or '--once' in args:
                    barrier.wait()  # Serial launch would fail this test.
                    if 'tick' in args:
                        raise launcher.CollectorError(repo,1,dict(state='failed',error='validation failed'))
                    return dict(phase='published')
                return {}
            result=launcher.collect(roots,configs,root,time.monotonic(),invoke)
            self.assertEqual(result['stock']['state'],'failed')
            self.assertEqual(result['commodities']['phase'],'published')
            self.assertTrue(any('disarm' in args for _,args in calls))
            self.assertEqual(launcher.read_json(root/'summary.json'),result)

    def test_prepare_overrides_old_limits_without_enabling_original_configs(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            roots={key:root/key for key in ('stock','commodities')}
            original={
                'stock':roots['stock']/'.private-stock-data/wind/local_daily.json',
                'commodities':roots['commodities']/'tmp/wind-daily/config.json'}
            for path in original.values():
                launcher.save_json(path,dict(enabled=False,allow_parallel_collectors=False,request_interval_seconds=2))
            launcher.save_json(roots['stock']/'.private-stock-data/wind/profile.json',dict(units_verified=True))
            script=roots['commodities']/'scripts/run_wind_daily.py'
            script.parent.mkdir(parents=True);script.touch()
            with patch.object(launcher,'python_for',return_value='python'):
                configs=launcher.prepare(roots,root/'run')
            for key,path in configs.items():
                cfg=launcher.read_json(path)
                self.assertTrue(cfg['allow_dashboard'])
                self.assertTrue(cfg['allow_parallel_collectors'])
                self.assertEqual(cfg['request_interval_seconds'],0)
                self.assertFalse(launcher.read_json(original[key])['enabled'])

    def test_expired_confirmation_refreshed_before_both_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); refreshed=threading.Event()
            def confirm(): refreshed.set()
            def invoke(*args):
                self.assertTrue(refreshed.is_set())
                return dict(state='up_to_date',phase='up_to_date')
            launcher.collect(dict(stock=root,commodities=root),dict(stock=root/'s',commodities=root/'c'),
                             root,time.monotonic()-901,invoke,confirm)

    def test_launcher_duplicate_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(launcher,'BASE',Path(directory)):
            with launcher.single_run(),self.assertRaises(RuntimeError):
                with launcher.single_run(): pass

    def test_daily_defaults(self):
        from china_commodities.wind_daily import DEFAULTS
        self.assertTrue(DEFAULTS['allow_parallel_collectors'])
        self.assertTrue(DEFAULTS['allow_dashboard'])
        self.assertEqual(DEFAULTS['request_interval_seconds'],0)
        self.assertFalse(DEFAULTS['enabled'])

    def test_explicit_serial_mode_is_ordered_and_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            order=[]
            def invoke(repo,args,log):
                if 'tick' in args or '--once' in args:
                    order.append(repo.name)
                return dict(state='up_to_date',phase='up_to_date')
            result=launcher.collect(dict(stock=root/'stock',commodities=root/'commodities'),
                dict(stock=root/'s',commodities=root/'c'),root,time.monotonic(),invoke,parallel=False)
            self.assertEqual(order,['stock','commodities'])
            self.assertEqual(len(result),2)

    def test_prepare_can_restore_limited_serial_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            roots={key:root/key for key in ('stock','commodities')}
            launcher.save_json(roots['stock']/'.private-stock-data/wind/local_daily.json',dict(enabled=False,batch_size=100))
            launcher.save_json(roots['stock']/'.private-stock-data/wind/profile.json',dict(units_verified=True))
            script=roots['commodities']/'scripts/run_wind_daily.py'
            script.parent.mkdir(parents=True);script.touch()
            with patch.object(launcher,'python_for',return_value='python'):
                configs=launcher.prepare(roots,root/'run',parallel=False,request_interval=2)
            for path in configs.values():
                cfg=launcher.read_json(path)
                self.assertFalse(cfg['allow_parallel_collectors'])
                self.assertEqual(cfg['request_interval_seconds'],2)
            self.assertEqual(launcher.read_json(configs['stock'])['batch_size'],100)
