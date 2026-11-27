"""Offline planning and local coordination for Wind daily collection."""
from datetime import date, datetime, time, timedelta
import gzip
import json
import os
from pathlib import Path
import socket
import subprocess
from zoneinfo import ZoneInfo

from .wind_coordination import RequestPacer
from .catalog import load_catalog
from .storage import read_json, write_json_atomic
from .trading_calendar import is_trading_day, previous_trading_day, next_trading_day, night_session_dates, CALENDAR_VERSION
from .night_session import _night_window

ZONE=ZoneInfo('Asia/Shanghai')
DEFAULTS=dict(start_date=None,max_days_per_run=5,request_interval_seconds=0.,
              max_history_days_per_run=2,
              branch='main',sdk_dir=None,allowed_empty_products=['CZCE:LR'],
              desk_host='127.0.0.1',desk_port=8765,index_options_scope='report_v1')


def config(path):
    result=dict(DEFAULTS)
    saved=read_json(Path(path),default={})
    # Ignore removed scheduling/login/lock switches in existing local configs.
    result.update({k:v for k,v in saved.items() if k in DEFAULTS})
    count=result['max_days_per_run']
    if isinstance(count,bool) or not isinstance(count,int) or count<=0:
        raise ValueError('invalid configuration: max_days_per_run')
    history_count = result['max_history_days_per_run']
    if isinstance(history_count,bool) or not isinstance(history_count,int) or not 0<=history_count<=10:
        raise ValueError('invalid configuration: max_history_days_per_run (0..10)')
    RequestPacer(result['request_interval_seconds'])
    from .index_selection import SCOPES
    if result['index_options_scope'] not in SCOPES:
        raise ValueError('invalid configuration: index_options_scope')
    return result


def load_snapshot(path):
    path=Path(path)
    if not path.exists():
        return {}
    if path.suffix=='.gz':
        with gzip.open(path,'rt',encoding='utf-8') as stream:
            return json.load(stream)
    return read_json(path,default={})


def night_complete(root, day, *, allow_partial=False):
    payload=load_snapshot(Path(root)/f'night_session/{day}.json')
    if payload.get('trading_date')!=day or payload.get('source',{}).get('provider')!='wind':
        return False
    session=night_session_dates(date.fromisoformat(day))
    if session is None:
        return payload.get('collection_state')=='no_scheduled_night_session' and payload.get('calendar_version')==CALENDAR_VERSION
    coverage=payload.get('coverage',{})
    rows=payload.get('records',[])
    allowed={'night_session','no_night_trade'}
    resolved=coverage.get('unresolved_contract_count')==0 and coverage.get('night_session_contract_count',0)>0
    if allow_partial and coverage.get('partial_contracts'):
        allowed.update(('query_error','missing_quote','missing_price'))
        partial=[r.get('source_code') for r in rows if r.get('missing_fields') or r.get('record_state') not in ('night_session','no_night_trade')]
        unresolved=sum(r.get('record_state') not in ('night_session','no_night_trade') for r in rows)
        resolved=partial==coverage['partial_contracts'] and unresolved==coverage.get('unresolved_contract_count')
    elif coverage.get('partial_contracts'):
        resolved=False
    if allow_partial and payload.get('collection_state')=='sampled_no_night_trades':
        resolved=all(r.get('record_state')=='no_night_trade' for r in rows) and coverage.get('no_night_trade_count')==len(rows)
    if coverage.get('unavailable_product_directories') and not allow_partial:
        resolved=False
    return bool(rows and payload.get('session_start_date')==str(session[0])
                and coverage.get('selected_contract_count')==len(rows)
                and resolved
                and len({(r.get('exchange'),r.get('contract')) for r in rows})==len(rows)
                and all(r.get('source_provider')=='wind' and r.get('trading_date')==day
                        and r.get('record_state') in allowed for r in rows))


def full_complete(root, day):
    root=Path(root)
    future=load_snapshot(root/f'snapshots/{day}.json')
    rows=future.get('futures_contracts',[])
    if future.get('trade_date')!=day or future.get('source',{}).get('provider')!='wind' or not rows:
        return False
    if {r.get('exchange') for r in rows}!=set(load_catalog().exchanges):
        return False
    if not all(r.get('source_provider')=='wind' and r.get('source_date_match') is True and r.get('source_trade_date')==day for r in rows):
        return False
    from .quality import validate_snapshot
    if validate_snapshot(future):
        return False
    options=load_snapshot(root/f'options/snapshots/{day}.json.gz')
    return options_observed(options,day,load_catalog()) and night_complete(root,day,allow_partial=True)


def options_observed(options,day,catalog):
    if options.get('trade_date')!=day or options.get('source_provider')!='wind':
        return False
    option_rows=options.get('records',[])
    found={r.get('exchange','')+':'+r.get('product','') for r in option_rows}
    missing=set(options.get('coverage',{}).get('failed_products',[]))
    declared={p.get('product_key') for p in options.get('collection_plans',[]) if p.get('state') in ('missing_current_underlying_or_chain','no_dated_option_quotes')}
    if missing!=declared or found & missing or (found | missing)!={p.exchange+':'+p.product for p in catalog.options}:
        return False
    if not all(r.get('source_provider')=='wind' and r.get('source_date_match') is True and r.get('source_trade_date')==day for r in option_rows):
        return False
    from .option_storage import validate_option_snapshot, OptionSnapshotValidationError
    try:
        validate_option_snapshot(options)
    except OptionSnapshotValidationError:
        return False
    return True


def eod_session_complete(day, now=None):
    """Actual domestic commodity day session boundary, not a vendor release delay."""
    now=(now or datetime.now(ZONE)).astimezone(ZONE)
    target=date.fromisoformat(day) if isinstance(day,str) else day
    return is_trading_day(target) and (target<now.date() or
        (target==now.date() and now.time()>=time(15)))


def intraday_window(now):
    """One launch-time cutoff, in Shanghai time, including night-session rollover."""
    now=now.astimezone(ZONE)
    today=now.date()
    if is_trading_day(today) and time(9)<=now.time()<time(15):
        day=today
        start=datetime.combine(today,time(9),tzinfo=ZONE)
        session='day'
    else:
        evening=today if now.time()>=time(21) else today-timedelta(days=1)
        if not (now.time()>=time(21) or now.time()<time(2,30)) or not is_trading_day(evening):
            return None
        day=next_trading_day(evening)
        dates=night_session_dates(day)
        if dates is None or dates[0]!=evening:
            return None
        start=datetime.combine(evening,time(21),tzinfo=ZONE)
        session='night'
    cutoff=now-timedelta(minutes=5)
    cutoff=cutoff.replace(minute=cutoff.minute//5*5,second=0,microsecond=0)
    return dict(trading_date=str(day),session=session,session_start=start.isoformat(),
                cutoff=cutoff.isoformat(),started_at=now.isoformat())


def _with_rates_plan(work,root,settings,now,excluded_funding=(),attempted=()):
    from . import wind_treasury,wind_funding
    for name,module in [('treasury',wind_treasury),('funding',wind_funding)]:
        current=module.window(now)
        missing=[]
        if current is None:
            closed=now.date() if module.eod_ready(str(now.date()),now) else previous_trading_day(now.date())
            cursor=date.fromisoformat(work['start_date'])
            while cursor<=closed:
                day=str(cursor)
                if (is_trading_day(cursor) and not (name=='funding' and day in excluded_funding)
                        and (name+'_eod',day) not in attempted and not module.complete(root,day)):
                    missing.append(day)
                cursor+=timedelta(days=1)
        if name == 'funding' and current and wind_funding.published_intraday_covers(root, current):
            current = None
        if current and (name+'_intraday',current['trading_date']) in attempted:
            current=None
        work[name+'_intraday']=current
        limit=settings['max_days_per_run']
        work[name+'_dates']=missing[:limit]
        work['remaining_'+name+'_dates']=missing[limit:]
        work['has_work']=bool(work['has_work'] or current or missing)
    return work


def plan(root, settings, now=None, anchor=None, known_completed=(),excluded_funding=(),history_budget=None,attempted=()):
    from .wind_index import complete as index_complete
    from .wind_commodity_details import FIRST_DATE, needs_collection
    root=Path(root)
    now=(now or datetime.now(ZONE)).astimezone(ZONE)
    today=now.date()
    intraday=intraday_window(now)
    if intraday:
        baseline=max((p.stem for p in (root/'snapshots').glob('????-??-??.json')),default=intraday['trading_date'])
        work=dict(start_date=settings.get('start_date') or anchor or baseline,
                    full_dates=[],remaining_dates=[],index_dates=[],remaining_index_dates=[],night_date=None,intraday=intraday,
                    has_work=True,verified_dates=list(known_completed),timezone='Asia/Shanghai')
        from .wind_history import extend_plan
        return extend_plan(_with_rates_plan(work,root,settings,now,excluded_funding,attempted),root,settings,history_budget)
    closed=today if eod_session_complete(today,now) else previous_trading_day(today)
    start=settings.get('start_date') or anchor
    if not start:
        dates=[p.stem for p in (root/'snapshots').glob('????-??-??.json')]
        if not dates:
            raise ValueError('set start_date for a repository without an EOD baseline')
        start=max(dates)
    cursor=date.fromisoformat(start)
    missing=[]
    index_missing=[]
    detail_missing=[]
    verified=[]
    while cursor<=closed:
        if is_trading_day(cursor):
            day=str(cursor)
            receipt=read_json(root/f'wind_daily/completed/{day}.json',default={})
            latest=read_json(root/'latest.json',default={}).get('trade_date','')
            retained=all(p.exists() for p in (root/f'snapshots/{day}.json',root/f'options/snapshots/{day}.json.gz',root/f'night_session/{day}.json'))
            archived=(day<latest and not retained and (day in known_completed or
                      (receipt.get('trade_date')==day and receipt.get('provider')=='wind' and receipt.get('layers')==['futures','options','night'])))
            if archived or full_complete(root,day):
                verified.append(day)
                if day >= FIRST_DATE and retained and ('commodity_details',day) not in attempted and needs_collection(root, day, now):
                    detail_missing.append(day)
            elif ('commodity_eod',day) not in attempted:
                missing.append(day)
            if ('index_eod',day) not in attempted and not index_complete(root,day,scope=settings.get('index_options_scope','report_v1')):
                index_missing.append(day)
        cursor+=timedelta(days=1)
    selected=missing[:settings['max_days_per_run']]
    night_day=None
    if not missing[len(selected):]:
        # Friday night belongs to Monday, but finishes on Saturday morning.
        # Select by the session's actual end, not the launch date's weekday.
        target=next_trading_day(closed)
        if (str(target)>=start and night_session_dates(target)
                and now>=_night_window(str(target))[2]
                and ('commodity_night',str(target)) not in attempted and not night_complete(root,str(target),allow_partial=True)):
            night_day=str(target)
    index_dates=index_missing[:settings['max_days_per_run']]
    detail_limit=settings['max_days_per_run']
    detail_dates=sorted(set(detail_missing[:detail_limit]).union(day for day in selected if day >= FIRST_DATE and ('commodity_details',day) not in attempted))
    work=dict(start_date=start,latest_completed_eod=str(closed),full_dates=selected,
                index_dates=index_dates,remaining_index_dates=index_missing[len(index_dates):],
                remaining_dates=missing[len(selected):],night_date=night_day,
                commodity_detail_dates=detail_dates,remaining_commodity_detail_dates=detail_missing[detail_limit:],
                has_work=bool(selected or night_day or index_dates or detail_dates),verified_dates=verified,timezone='Asia/Shanghai')
    work['field_repairs']=[]
    for module in ('index_derivatives','treasury_futures'):
        published=read_json(root/module/'latest.json',default={})
        day=published.get('trade_date')
        coverage=published.get('coverage',published).get('field_availability') or {}
        stamp=published.get('fields_updated_at') or published.get('generated_at')
        if (coverage.get('missing_by_field') and day and start<=day<=str(closed) and stamp
                and datetime.fromisoformat(stamp)<now and (module+'_fields',day) not in attempted):
            work['field_repairs'].append(dict(module=module,date=day))
    work['has_work']=work['has_work'] or bool(work['field_repairs'])
    from .wind_history import extend_plan
    return extend_plan(_with_rates_plan(work,root,settings,now,excluded_funding,attempted),root,settings,history_budget)


def desk_busy(settings):
    if os.name=='nt':
        return _windows_desk_busy(settings['desk_port'])
    try:
        with socket.create_connection((settings['desk_host'],settings['desk_port']),timeout=1):
            return True
    except ConnectionRefusedError:
        return False
    except OSError as exc:
        # An ambiguous network/permission error is never permission to start SDK work.
        raise RuntimeError('cannot establish trading desk guard state') from exc


def _windows_desk_busy(port):
    """Read the Windows TCP table directly for the local desk guard."""
    if not isinstance(port,int) or isinstance(port,bool) or not 1<=port<=65535:
        raise RuntimeError('cannot establish trading desk guard state: invalid port')
    try:
        result=subprocess.run(
            ['netstat.exe','-ano','-p','tcp'],
            capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=10)
    except (OSError,subprocess.TimeoutExpired) as exc:
        raise RuntimeError('cannot query Windows TCP listener state') from exc
    if result.returncode!=0 or not result.stdout.strip():
        raise RuntimeError('cannot query Windows TCP listener state')
    target=str(port)
    for line in result.stdout.splitlines():
        fields=line.split()
        if len(fields)<5 or fields[0].upper()!='TCP':
            continue
        local_port=fields[1].rsplit(':',1)[-1]
        if local_port==target and fields[3].upper()=='LISTENING':
            return True
    return False


def state(path, phase, **details):
    payload=dict(phase=phase,updated_at=datetime.now(ZONE).isoformat(),**details)
    write_json_atomic(Path(path),payload)
    return payload
