from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from china_commodities.wind_daily import DEFAULTS, ZONE, plan, single_runner, desk_busy, config
from china_commodities.storage import write_json_atomic


class DailyPlanTests(unittest.TestCase):
    def test_morning_only_closed_eod_and_completed_night(self):
        with TemporaryDirectory() as tmp,patch('china_commodities.wind_daily.full_complete',return_value=True),patch('china_commodities.wind_daily.night_complete',return_value=False):
            p=plan(tmp,dict(DEFAULTS,start_date='2026-11-11'),datetime(2026,11,12,8,tzinfo=ZONE))
            self.assertEqual(p['latest_completed_eod'],'2026-11-11')
            self.assertEqual(p['full_dates'],[])
            self.assertEqual(p['night_date'],'2026-11-12')
            self.assertEqual(p['provisional_date'],'2026-11-12')

    def test_no_provider_clock_gate_after_actual_session_close(self):
        with TemporaryDirectory() as tmp,patch('china_commodities.wind_daily.full_complete',return_value=False):
            for hour,minute in [(15,0),(16,0),(18,14),(23,0)]:
                p=plan(tmp,dict(DEFAULTS,start_date='2026-11-12',eod_ready_time='23:59'),datetime(2026,11,12,hour,minute,tzinfo=ZONE))
                self.assertEqual(p['full_dates'],['2026-11-12'])
                self.assertIsNone(p['provisional_date'])
            p=plan(tmp,dict(DEFAULTS,start_date='2026-11-12'),datetime(2026,11,12,14,59,tzinfo=ZONE))
            self.assertEqual(p['full_dates'],[])
            self.assertEqual(p['provisional_date'],'2026-11-12')

    def test_legacy_ready_clock_is_ignored(self):
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.json'
            write_json_atomic(path,dict(eod_ready_time='18:15'))
            self.assertNotIn('eod_ready_time',config(path))

    def test_early_night_is_never_collected(self):
        with TemporaryDirectory() as tmp,patch('china_commodities.wind_daily.full_complete',return_value=True):
            p=plan(tmp,dict(DEFAULTS,start_date='2026-11-11'),datetime(2026,11,12,2,tzinfo=ZONE))
            self.assertIsNone(p['night_date'])

    def test_missed_days_include_holes_and_limit_batch(self):
        with TemporaryDirectory() as tmp,patch('china_commodities.wind_daily.full_complete',side_effect=lambda root,day:day=='2026-11-10'):
            p=plan(tmp,dict(DEFAULTS,start_date='2026-11-09',max_days_per_run=2),datetime(2026,11,12,19,tzinfo=ZONE))
            self.assertEqual(p['full_dates'],['2026-11-09','2026-11-11'])
            self.assertEqual(p['remaining_dates'],['2026-11-12'])

    def test_weekend_and_holiday_fail_closed_for_unknown_year(self):
        with TemporaryDirectory() as tmp,patch('china_commodities.wind_daily.full_complete',return_value=True):
            p=plan(tmp,dict(DEFAULTS,start_date='2026-11-13'),datetime(2026,11,14,19,tzinfo=ZONE))
            self.assertFalse(p['has_work'])
            self.assertEqual(p['latest_completed_eod'],'2026-11-13')
            with self.assertRaisesRegex(ValueError,'calendar unavailable'):
                plan(tmp,dict(DEFAULTS,start_date='2027-01-01'),datetime(2027,1,4,19,tzinfo=ZONE))

    def test_retention_does_not_reschedule_completed_archives(self):
        with TemporaryDirectory() as tmp,patch('china_commodities.wind_daily.full_complete',return_value=False):
            root=Path(tmp)
            write_json_atomic(root/'latest.json',dict(trade_date='2026-11-12'))
            write_json_atomic(root/'wind_daily/completed/2026-11-10.json',dict(trade_date='2026-11-10',provider='wind',layers=['futures','options','night']))
            p=plan(root,dict(DEFAULTS,start_date='2026-11-09'),datetime(2026,11,12,19,tzinfo=ZONE),known_completed=['2026-11-09'])
            self.assertEqual(p['full_dates'],['2026-11-11','2026-11-12'])

    def test_os_lock_excludes_second_runner_and_releases(self):
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/'runner.lock'
            with single_runner(path):
                with self.assertRaisesRegex(RuntimeError,'another local'):
                    with single_runner(path):
                        self.fail('second runner acquired lock')
            with single_runner(path):
                pass

    def test_guard_is_conservative_on_uncertain_socket_errors(self):
        with patch('china_commodities.wind_daily.os.name','posix'),patch('socket.create_connection',side_effect=PermissionError):
            with self.assertRaises(RuntimeError):
                desk_busy(DEFAULTS)
        with patch('china_commodities.wind_daily.os.name','posix'),patch('socket.create_connection',side_effect=ConnectionRefusedError):
            self.assertFalse(desk_busy(DEFAULTS))
