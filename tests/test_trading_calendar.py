from datetime import date, datetime
from zoneinfo import ZoneInfo
import tempfile
import unittest
from pathlib import Path
from china_commodities.trading_calendar import is_trading_day, night_session_dates, previous_trading_day
from china_commodities.night_session import _night_window, collect_night_session
from china_commodities.storage import write_json_if_changed
from scripts.plan_ifind_collection import plan_collection
from scripts.wait_for_eod import wait_seconds


class CalendarTests(unittest.TestCase):
    def test_friday_night_belongs_to_monday_but_ends_on_saturday(self):
        start, end = night_session_dates(date(2026, 10, 19))
        self.assertEqual((start.isoformat(), end.isoformat()), ("2026-10-16", "2026-10-17"))
        session, begin, finish = _night_window("2026-10-19")
        self.assertEqual(session, "2026-10-16")
        self.assertEqual(finish.date(), end)
        self.assertEqual((finish-begin).total_seconds(), 7.75*3600)

    def test_holidays_and_makeup_weekends_are_closed(self):
        for day in ("2026-10-01", "2026-10-07", "2026-10-10", "2026-02-23", "2026-02-28"):
            self.assertFalse(is_trading_day(date.fromisoformat(day)))
        self.assertEqual(previous_trading_day(date(2026, 10, 8)), date(2026, 9, 30))
        self.assertIsNone(night_session_dates(date(2026, 10, 8)))
        self.assertIsNone(night_session_dates(date(2026, 9, 28)))
        self.assertIsNone(night_session_dates(date(2026, 1, 5)))
        with self.assertRaises(ValueError):
            is_trading_day(date(2027, 1, 4))

    def test_holiday_night_makes_no_request(self):
        class Client:
            def realtime_quotes(self, *args):
                raise AssertionError("no night session must not query")
        with tempfile.TemporaryDirectory() as tmp:
            result = collect_night_session("2026-10-08", data_dir=tmp, client=Client())
            self.assertEqual(result["status"]["collection_state"], "no_scheduled_night_session")
            self.assertFalse((Path(tmp)/"night_session/latest.json").exists())

    def test_saturday_capture_and_holiday_recovery_use_distinct_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = plan_collection(event_name="workflow_dispatch", event_schedule="", requested_date=None,
                                   data_dir=tmp, now=datetime(2026, 10, 17, 6, 3, tzinfo=ZoneInfo("Asia/Shanghai")))
            self.assertEqual(plan["night_trading_date"], "2026-10-19")
            self.assertEqual(plan["domestic_trade_date"], "2026-10-16")
            self.assertTrue(plan["run_night_session"])
            holiday = plan_collection(event_name="workflow_dispatch", event_schedule="", requested_date=None,
                                      data_dir=tmp, now=datetime(2026, 10, 8, 6, 3, tzinfo=ZoneInfo("Asia/Shanghai")))
            self.assertEqual(holiday["domestic_trade_date"], "2026-09-30")
            self.assertEqual(holiday["external_trade_date"], "2026-10-07")
            self.assertFalse(holiday["run_night_session"])

    def test_early_evening_wait_is_bounded_and_historical_runs_do_not_wait(self):
        early = datetime(2026, 10, 16, 18, 3, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(wait_seconds(early), 721)
        self.assertEqual(wait_seconds(early, "2026-10-15"), 0)
        self.assertEqual(wait_seconds(early, mode="night_session_only"), 0)
        self.assertEqual(wait_seconds(early.replace(hour=19)), 0)
        self.assertEqual(wait_seconds(early.replace(day=17)), 0)

    def test_completed_friday_snapshot_publishes_for_monday_not_before_session_end(self):
        import pandas as pd
        class Client:
            calls = 0
            def realtime_quotes(self, codes, fields):
                self.calls += 1
                return pd.DataFrame([{"thscode": "CU2611.SHF", "time": "2026-10-16 23:00:00", "latest": 81000}])
        with tempfile.TemporaryDirectory() as tmp:
            write_json_if_changed(Path(tmp)/"latest.json", {"trade_date": "2026-10-16", "futures_contracts": [
                {"exchange": "SHFE", "product": "CU", "contract": "CU2611", "trade_date": "2026-10-16", "settle": 80000}]})
            client = Client()
            early = collect_night_session("2026-10-19", data_dir=tmp, client=client,
                now=datetime(2026, 10, 16, 22, tzinfo=ZoneInfo("Asia/Shanghai")))
            self.assertEqual(early["status"]["collection_state"], "session_not_completed")
            self.assertEqual(client.calls, 0)
            result = collect_night_session("2026-10-19", data_dir=tmp, client=client,
                now=datetime(2026, 10, 17, 6, tzinfo=ZoneInfo("Asia/Shanghai")))
            self.assertTrue(result["status"]["published"])
            self.assertEqual(result["snapshot"]["session_end_date"], "2026-10-17")
            self.assertEqual(result["snapshot"]["records"][0]["session_end_date"], "2026-10-17")
            self.assertEqual(result["snapshot"]["records"][0]["night_last_price"], 81000)
