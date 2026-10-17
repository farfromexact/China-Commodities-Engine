"""Bound the legacy 18:03 trigger's wait to the explicit 18:15 EOD boundary."""
from datetime import datetime, time
import os
import time as clock
from zoneinfo import ZoneInfo
from china_commodities.trading_calendar import is_trading_day


def wait_seconds(now, requested_date=None, mode="full"):
    now = now.astimezone(ZoneInfo("Asia/Shanghai"))
    if (mode != "full" or requested_date not in (None, "", now.date().isoformat())
            or not is_trading_day(now.date()) or not time(18) <= now.time() < time(18, 15)):
        return 0
    return max(0, int((now.replace(hour=18, minute=15, second=1, microsecond=0)-now).total_seconds()))


if __name__ == "__main__":
    seconds = wait_seconds(datetime.now(ZoneInfo("Asia/Shanghai")),
                           os.environ.get("TRADE_DATE"), os.environ.get("COLLECTION_MODE") or "full")
    print(f"EOD readiness wait: {seconds} seconds; no vendor request during wait", flush=True)
    if seconds:
        clock.sleep(seconds)
