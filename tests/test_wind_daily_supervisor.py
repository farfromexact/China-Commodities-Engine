"""Focused checks for the foreground collect-and-push entry."""
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch
from scripts import run_wind_daily as runner
from china_commodities.wind_daily import DEFAULTS
from china_commodities.storage import write_json_atomic


class DailyRunTests(unittest.TestCase):
    def setUp(self):
        stack = self.enterContext(ExitStack())
        self.root = Path(stack.enter_context(TemporaryDirectory()))
        self.cache = self.root / 'cache'
        self.cache.mkdir()
        write_json_atomic(self.root / 'data/latest.json', {'trade_date': '2026-11-12'})
        self.work = dict(start_date='2026-11-12', verified_dates=[], has_work=True,
                         full_dates=[], night_date='2026-11-13', remaining_dates=[])
        self.publisher = MagicMock()
        self.publisher.recover.return_value = False
        self.sdk = MagicMock()
        self.sdk.start.return_value.ErrorCode = 0
        self.sdk.isconnected.return_value = True
        for name, value in [('ROOT', self.root), ('Publisher', self.publisher), ('plan', self.work),
                            ('git', 'base'), ('load_wind', self.sdk), ('validate_snapshot', [])]:
            if name == 'ROOT':
                stack.enter_context(patch.object(runner, name, value))
            else:
                stack.enter_context(patch.object(runner, name, return_value=value))
        self.collect = stack.enter_context(patch.object(runner, 'collect_day', return_value={'date':'2026-11-13'}))
        stack.enter_context(patch.object(runner, 'publish_report_input'))
        self.index_eod = stack.enter_context(patch.object(runner, 'collect_index_eod', return_value={'mode':'index_derivatives_eod'}))
        self.index_intraday = stack.enter_context(patch.object(runner, 'collect_index_intraday', return_value={'mode':'index_derivatives_intraday'}))
        stack.enter_context(patch.object(runner, 'emit'))

    def test_direct_start_collects_and_pushes_without_signals_or_enabled_flag(self):
        result = runner.once(dict(DEFAULTS, enabled=False), self.cache)
        self.assertEqual(result['phase'], 'published')
        self.sdk.start.assert_called_once_with(waitTime=15)
        self.assertTrue(self.collect.call_args.kwargs['night_only'])
        self.publisher.prepare.assert_called_once()
        self.assertEqual(self.publisher.recover.call_count, 2)
        self.assertFalse((self.cache / 'progress.json').exists())
        self.sdk.stop.assert_not_called()

    def test_up_to_date_does_not_connect_wind(self):
        self.work.update(has_work=False, night_date=None)
        self.assertEqual(runner.once(DEFAULTS, self.cache)['phase'], 'up_to_date')
        self.sdk.start.assert_not_called()
        self.publisher.prepare.assert_not_called()

    def test_index_only_backfill_uses_same_publisher_and_blocks_partial_failure(self):
        self.work.update(night_date=None,index_dates=['2026-11-13'])
        runner.once(DEFAULTS,self.cache)
        self.collect.assert_not_called()
        self.index_eod.assert_called_once()
        self.publisher.prepare.assert_called_once()
        self.publisher.prepare.reset_mock()
        self.index_eod.side_effect=ValueError('index source date mismatch')
        with self.assertRaisesRegex(ValueError,'index source date'):
            runner.once(DEFAULTS,self.cache)
        self.publisher.prepare.assert_not_called()

    def test_bad_data_never_publishes(self):
        self.collect.side_effect = ValueError('wrong source date')
        with self.assertRaisesRegex(ValueError, 'wrong source date'):
            runner.once(DEFAULTS, self.cache)
        self.publisher.prepare.assert_not_called()

    def test_login_failure_does_not_collect(self):
        self.sdk.start.return_value.ErrorCode = -1
        with self.assertRaisesRegex(RuntimeError, 'Wind'):
            runner.once(DEFAULTS, self.cache)
        self.collect.assert_not_called()
        self.publisher.prepare.assert_not_called()

    def test_unwritable_status_does_not_block_publishing(self):
        original = runner.write_json_atomic
        def write(path, payload):
            if path.name == 'status.json':
                raise PermissionError('locked display file')
            original(path, payload)
        with patch.object(runner, 'write_json_atomic', side_effect=write):
            self.assertEqual(runner.once(DEFAULTS, self.cache)['phase'], 'published')
        self.publisher.prepare.assert_called_once()

    def test_pending_push_failure_stops_before_new_requests(self):
        self.publisher.recover.side_effect = RuntimeError('push unavailable')
        with self.assertRaisesRegex(RuntimeError, 'push unavailable'):
            runner.once(DEFAULTS, self.cache)
        self.sdk.start.assert_not_called()

    def test_intraday_uses_fixed_cutoff_and_same_auto_publisher(self):
        from datetime import datetime
        from china_commodities.wind_daily import ZONE, intraday_window
        launch=datetime(2026,11,13,14,59,tzinfo=ZONE)
        self.work.update(intraday=intraday_window(launch),night_date=None)
        with patch.object(runner,'collect_intraday',return_value={'mode':'intraday'}) as collect:
            result=runner.once(DEFAULTS,self.cache,now=launch)
        self.assertEqual(result['phase'],'published')
        self.assertEqual(collect.call_args.args[1]['cutoff'],'2026-11-13T14:50:00+08:00')
        self.collect.assert_not_called()
        self.publisher.prepare.assert_called_once()
