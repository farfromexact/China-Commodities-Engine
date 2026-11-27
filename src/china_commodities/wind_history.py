"""Bounded, user-started history repair, independent of complete daily batches."""
from datetime import date
from pathlib import Path

from .storage import read_json
from .trading_calendar import previous_trading_day

HISTORY_SESSIONS = 21  # 20 daily returns plus the T-20 price endpoint.


def futures_complete(root, day):
    from .quality import validate_snapshot
    payload = read_json(Path(root)/f'snapshots/{day}.json', default={})
    return (payload.get('trade_date') == day and bool(payload.get('futures_contracts'))
            and (payload.get('verified') is True or payload.get('scope_verified') is True)
            and not validate_snapshot(payload))


def extend_plan(work, root, settings, budget=None):
    """Never expand a short recent start_date into full option/night backfill."""
    from .wind_index import complete as index_complete
    work.update(history_tasks=[], deferred_history_tasks=[], history_sessions=HISTORY_SESSIONS)
    limit = settings.get('max_history_days_per_run', 2) if budget is None else budget
    work['history_budget'] = limit
    baseline = read_json(Path(root)/'latest.json', default={})
    # Intraday remains snapshot-first. History also needs a real daily baseline.
    if (work.get('intraday') or work.get('treasury_intraday') or work.get('funding_intraday')
            or not baseline.get('futures_contracts') or not work.get('latest_completed_eod')):
        return work
    cursor = date.fromisoformat(work['latest_completed_eod'])
    dates = []
    for _ in range(HISTORY_SESSIONS):
        dates.append(str(cursor))
        cursor = previous_trading_day(cursor)
    candidates = []
    regular_futures = set(work['full_dates'] + work['remaining_dates'])
    regular_index = set(work['index_dates'] + work['remaining_index_dates'])
    for day in dates:
        if day not in regular_futures and not futures_complete(root, day):
            candidates.append(dict(module='commodity_futures', date=day))
    for day in dates:
        if day not in regular_index and not index_complete(root, day, scope=settings.get('index_options_scope', 'report_v1')):
            candidates.append(dict(module='index_derivatives', date=day))
    # Repair commodity gaps first, then seed index history newest-first. Execute
    # selected days chronologically so adjacent-day deltas can be reconstructed.
    work['history_tasks'] = sorted(candidates[:limit], key=lambda item: (item['date'], item['module']))
    work['deferred_history_tasks'] = candidates[limit:]
    work['has_work'] = bool(work['has_work'] or work['history_tasks'])
    return work


def collect_futures_history(api, day, root, *, catalog=None, allowed_empty=('CZCE:LR',)):
    from .wind_daily import eod_session_complete
    from .wind_daily_collect import directory, futures
    from .storage import publish_futures_history
    if not eod_session_complete(day):
        raise ValueError('unfinished futures history session')
    if futures_complete(root, day):
        return dict(mode='futures_history', date=day, state='already_complete')
    universe, empty = directory(api, day, catalog=catalog, allowed_empty=allowed_empty)
    result = futures(api, day, universe, batched=True, data_root=root)
    if not result.verified:
        return dict(mode='futures_history',date=day,unavailable={'futures':f'data/wind_daily/available_futures/{day}.json.gz'})
    publish_futures_history(result, Path(root))
    return dict(mode='futures_history', date=day, directory_count=len(universe),
                declared_empty_products=empty, layers=['futures'], state='validated')
