from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import subprocess
from unittest.mock import patch, MagicMock

from scripts import run_wind_daily as runner
from china_commodities.wind_daily import DEFAULTS, ZONE
from china_commodities.storage import write_json_atomic


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache=Path(self.tmp.name)
        lock_path=patch('china_commodities.wind_coordination.collection_lock_path',return_value=self.cache/'test-collection.lock')
        lock_path.start()
        self.addCleanup(lock_path.stop)
        self.settings=dict(DEFAULTS,enabled=True,allow_dashboard=False,allow_parallel_collectors=False)
        self.work=dict(start_date='2026-11-11',verified_dates=[],has_work=True,full_dates=['2026-11-12'],night_date=None)
        self.publisher=MagicMock()
        self.publisher.recover.return_value=False
        self.publisher.journal=self.cache/'outbox.json'

    def test_disabled_does_not_even_construct_a_publisher(self):
        with patch.object(runner,'Publisher',side_effect=AssertionError):
            self.assertEqual(runner.once(DEFAULTS,self.cache)['phase'],'disabled')

    def test_up_to_date_never_starts_sdk_worker(self):
        with patch.object(runner,'Publisher',return_value=self.publisher),patch.object(runner,'plan',return_value=dict(self.work,has_work=False)),patch.object(runner.subprocess,'Popen',side_effect=AssertionError):
            self.assertEqual(runner.once(self.settings,self.cache)['phase'],'up_to_date')
            self.publisher.recover.assert_called_once()

    def test_active_desk_keeps_signal_and_never_starts_worker(self):
        write_json_atomic(self.cache/'login-ready.json',dict(observed_at=datetime.now(ZONE).isoformat()))
        with patch.object(runner,'Publisher',return_value=self.publisher),patch.object(runner,'plan',return_value=self.work),patch.object(runner,'desk_busy',return_value=True),patch.object(runner.subprocess,'Popen',side_effect=AssertionError):
            self.assertEqual(runner.once(self.settings,self.cache)['phase'],'desk_busy')
            self.assertTrue((self.cache/'login-ready.json').exists())

    def test_missing_or_expired_login_signal_never_starts_worker(self):
        with patch.object(runner,'Publisher',return_value=self.publisher),patch.object(runner,'plan',return_value=self.work),patch.object(runner,'desk_busy',return_value=False),patch.object(runner.subprocess,'Popen',side_effect=AssertionError):
            self.assertEqual(runner.once(self.settings,self.cache)['phase'],'waiting_login_signal')
            write_json_atomic(self.cache/'login-ready.json',dict(observed_at=(datetime.now(ZONE)-timedelta(hours=2)).isoformat()))
            self.assertEqual(runner.once(self.settings,self.cache)['phase'],'waiting_login_signal')

    def test_failed_worker_consumes_signal_and_enforces_retry_delay(self):
        write_json_atomic(self.cache/'login-ready.json',dict(observed_at=datetime.now(ZONE).isoformat()))
        process=MagicMock(returncode=1)
        process.poll.return_value=1
        def launch(*args,**kwargs):
            (self.cache/'login-ready.json').replace(self.cache/'login-claimed.json')
            return process
        with patch.object(runner,'Publisher',return_value=self.publisher),patch.object(runner,'plan',return_value=self.work),patch.object(runner,'desk_busy',return_value=False),patch.object(runner,'git',return_value='base'),patch.object(runner.subprocess,'Popen',side_effect=launch) as start:
            self.assertEqual(runner.once(self.settings,self.cache)['phase'],'retry_wait')
            self.assertFalse((self.cache/'login-ready.json').exists())
            self.assertEqual(runner.once(self.settings,self.cache)['phase'],'retry_wait')
            start.assert_called_once()

    def test_foundation_timeout_records_failure_and_keeps_valid_snapshot(self):
        stage=self.cache/'data'
        write_json_atomic(stage/'latest.json',dict(trade_date='2026-11-11'))
        write_json_atomic(stage/'physical/latest.json',dict(trade_date='2026-11-10',retained=True))
        with patch('china_commodities.collection_cache.verified_foundation_available',return_value=False),patch.object(runner.subprocess,'run',side_effect=subprocess.TimeoutExpired('test',1)):
            result=runner.refresh_foundation(stage,self.cache,self.settings)
        self.assertEqual({r['scope'] for r in result},{'physical','external'})
        self.assertTrue(runner.read_json(stage/'physical/latest.json')['retained'])
        self.assertFalse(runner.read_json(stage/'physical/last_run_status.json')['data_fresh'])


    def test_coexistence_passes_active_desk_but_still_requires_login(self):
        settings=dict(self.settings,allow_dashboard=True)
        with patch.object(runner,'Publisher',return_value=self.publisher),patch.object(runner,'plan',return_value=self.work),patch.object(runner,'desk_busy',side_effect=AssertionError):
            self.assertEqual(runner.once(settings,self.cache)['phase'],'waiting_login_signal')

    def test_busy_worker_preserves_login_signal_without_loading_sdk(self):
        import os
        from china_commodities.wind_coordination import collection_lock
        job=self.cache/'job.json'
        write_json_atomic(job,dict(token='test',cache=str(self.cache),settings=dict(allow_parallel_collectors=False)))
        write_json_atomic(self.cache/'login-ready.json',dict(observed_at=datetime.now(ZONE).isoformat()))
        with collection_lock(),patch.dict(os.environ,WIND_DAILY_WORKER_TOKEN='test'),patch.object(runner,'_worker_locked',side_effect=AssertionError):
            runner.worker(job)
        self.assertEqual(runner.read_json(self.cache/'progress.json')['phase'],'waiting_collector')
        self.assertTrue((self.cache/'login-ready.json').exists())

    def test_busy_worker_is_not_reported_as_published(self):
        write_json_atomic(self.cache/'login-ready.json',dict(observed_at=datetime.now(ZONE).isoformat()))
        process=MagicMock(returncode=0)
        process.poll.return_value=0
        def launch(*args,**kwargs):
            write_json_atomic(self.cache/'progress.json',dict(phase='waiting_collector'))
            return process
        with patch.object(runner,'Publisher',return_value=self.publisher),patch.object(runner,'plan',return_value=self.work),patch.object(runner,'desk_busy',return_value=False),patch.object(runner,'git',return_value='base'),patch.object(runner.subprocess,'Popen',side_effect=launch):
            self.assertEqual(runner.once(self.settings,self.cache)['phase'],'waiting_collector')
        self.assertTrue((self.cache/'login-ready.json').exists())

    def test_explicit_parallel_mode_runs_even_when_cross_engine_lock_is_held(self):
        import os
        from china_commodities.wind_coordination import collection_lock
        job=self.cache/'job.json'
        write_json_atomic(job,dict(token='test',cache=str(self.cache),settings=dict(allow_parallel_collectors=True)))
        with collection_lock(),patch.dict(os.environ,WIND_DAILY_WORKER_TOKEN='test'),patch.object(runner,'_worker_locked') as collect,patch.object(runner,'emit'):
            runner.worker(job)
        collect.assert_called_once_with(job)
