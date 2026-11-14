from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from china_commodities.wind_daily import DEFAULTS, ZONE, plan, desk_busy, config
from china_commodities.storage import write_json_atomic


class DailyPlanTests(unittest.TestCase):
    def test_morning_only_closed_eod_and_completed_night(self):
        with TemporaryDirectory() as tmp,patch('china_commodities.wind_daily.full_complete',return_value=True),patch('china_commodities.wind_daily.night_complete',return_value=False):
            p=plan(tmp,dict(DEFAULTS,start_date='2026-11-11'),datetime(2026,11,12,8,tzinfo=ZONE))
            self.assertEqual(p['latest_completed_eod'],'2026-11-11')
            self.assertEqual(p['full_dates'],[])
            self.assertEqual(p['night_date'],'2026-11-12')
            self.assertNotIn('provisional_date',p)

    def test_no_provider_clock_gate_after_actual_session_close(self):
        with TemporaryDirectory() as tmp,patch('china_commodities.wind_daily.full_complete',return_value=False):
            for hour,minute in [(15,0),(16,0),(18,14)]:
                p=plan(tmp,dict(DEFAULTS,start_date='2026-11-12',eod_ready_time='23:59'),datetime(2026,11,12,hour,minute,tzinfo=ZONE))
                self.assertEqual(p['full_dates'],['2026-11-12'])
                self.assertNotIn('provisional_date',p)
            p=plan(tmp,dict(DEFAULTS,start_date='2026-11-12'),datetime(2026,11,12,14,59,tzinfo=ZONE))
            self.assertEqual(p['full_dates'],[])
            self.assertNotIn('provisional_date',p)

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

    def test_completed_weekend_night_is_not_repeated_and_unknown_year_fails_closed(self):
        with TemporaryDirectory() as tmp,patch('china_commodities.wind_daily.full_complete',return_value=True),patch('china_commodities.wind_daily.night_complete',return_value=True):
            p=plan(tmp,dict(DEFAULTS,start_date='2026-11-13'),datetime(2026,11,14,19,tzinfo=ZONE))
            self.assertFalse(p['has_work'])
            self.assertEqual(p['latest_completed_eod'],'2026-11-13')
            with self.assertRaisesRegex(ValueError,'calendar unavailable'):
                plan(tmp,dict(DEFAULTS,start_date='2027-01-01'),datetime(2027,1,4,19,tzinfo=ZONE))

    def test_friday_night_selected_on_weekend_and_monday_morning(self):
        with TemporaryDirectory() as tmp,patch('china_commodities.wind_daily.full_complete',return_value=True),patch('china_commodities.wind_daily.night_complete',return_value=False):
            for day,hour in [(14,4),(14,19),(15,19),(16,8)]:
                with self.subTest(day=day,hour=hour):
                    p=plan(tmp,dict(DEFAULTS,start_date='2026-11-13'),datetime(2026,11,day,hour,tzinfo=ZONE))
                    self.assertEqual(p['latest_completed_eod'],'2026-11-13')
                    self.assertEqual(p['full_dates'],[])
                    self.assertEqual(p['night_date'],'2026-11-16')

    def test_unfinished_friday_night_and_holiday_are_not_scheduled(self):
        with TemporaryDirectory() as tmp,patch('china_commodities.wind_daily.full_complete',return_value=True):
            for now,start in [(datetime(2026,11,13,19,tzinfo=ZONE),'2026-11-13'),
                              (datetime(2026,11,14,3,tzinfo=ZONE),'2026-11-13'),
                              (datetime(2026,10,3,19,tzinfo=ZONE),'2026-09-30')]:
                with self.subTest(now=now):
                    self.assertIsNone(plan(tmp,dict(DEFAULTS,start_date=start),now)['night_date'])

    def test_retention_does_not_reschedule_completed_archives(self):
        with TemporaryDirectory() as tmp,patch('china_commodities.wind_daily.full_complete',return_value=False):
            root=Path(tmp)
            write_json_atomic(root/'latest.json',dict(trade_date='2026-11-12'))
            write_json_atomic(root/'wind_daily/completed/2026-11-10.json',dict(trade_date='2026-11-10',provider='wind',layers=['futures','options','night']))
            p=plan(root,dict(DEFAULTS,start_date='2026-11-09'),datetime(2026,11,12,19,tzinfo=ZONE),known_completed=['2026-11-09'])
            self.assertEqual(p['full_dates'],['2026-11-11','2026-11-12'])

    def test_guard_is_conservative_on_uncertain_socket_errors(self):
        with patch('china_commodities.wind_daily.os.name','posix'),patch('socket.create_connection',side_effect=PermissionError):
            with self.assertRaises(RuntimeError):
                desk_busy(DEFAULTS)
        with patch('china_commodities.wind_daily.os.name','posix'),patch('socket.create_connection',side_effect=ConnectionRefusedError):
            self.assertFalse(desk_busy(DEFAULTS))
