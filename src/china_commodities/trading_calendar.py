"""Versioned domestic session calendar; never guess outside maintained years.

SHFE holiday notices (shared domestic holiday schedule, not product hours):
https://www.shfe.com.cn/publicnotice/notice/202412/t20241223_824109.html
https://www.shfe.com.cn/publicnotice/notice/202512/t20251217_829805.html
https://www.shfe.com.cn/services/calenderandholidays/tradinghours/
"""
from datetime import date, timedelta

CALENDAR_VERSION = "domestic_holidays_2025_2026_v1"
HOLIDAY_RANGES = {
    2025: (("01-01", "01-01"), ("01-28", "02-04"), ("04-04", "04-06"),
           ("05-01", "05-05"), ("05-31", "06-02"), ("10-01", "10-08")),
    2026: (("01-01", "01-03"), ("02-15", "02-23"), ("04-04", "04-06"),
           ("05-01", "05-05"), ("06-19", "06-21"), ("09-25", "09-27"),
           ("10-01", "10-07")),
}
NO_NIGHT_STARTS = frozenset((
    "2024-12-31", "2025-01-27", "2025-04-03", "2025-04-30", "2025-05-30",
    "2025-09-30", "2025-12-31", "2026-02-13", "2026-04-03", "2026-04-30",
    "2026-06-18", "2026-09-24", "2026-09-30",
))


def is_trading_day(day: date) -> bool:
    if day.year not in HOLIDAY_RANGES:
        raise ValueError(f"domestic trading calendar unavailable for {day.year}; update annual notice")
    return day.weekday() < 5 and not any(
        f"{day.year}-{start}" <= day.isoformat() <= f"{day.year}-{end}"
        for start, end in HOLIDAY_RANGES[day.year])


def previous_trading_day(day: date) -> date:
    cursor = day - timedelta(days=1)
    # The previous year's last day is sufficient for the first session in 2025.
    while cursor != date(2024, 12, 31) and not is_trading_day(cursor):
        cursor -= timedelta(days=1)
    return cursor


def next_trading_day(day: date, *, inclusive=False) -> date:
    cursor = day if inclusive else day + timedelta(days=1)
    while not is_trading_day(cursor):
        cursor += timedelta(days=1)
    return cursor


def night_session_dates(trading_day: date) -> tuple[date, date] | None:
    if not is_trading_day(trading_day):
        return None
    start = previous_trading_day(trading_day)
    if start.isoformat() in NO_NIGHT_STARTS:
        return None
    return start, start + timedelta(days=1)
