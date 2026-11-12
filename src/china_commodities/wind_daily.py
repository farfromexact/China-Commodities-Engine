"""Offline planning and local coordination for Wind daily collection."""
from contextlib import contextmanager
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
from .trading_calendar import is_trading_day, previous_trading_day, night_session_dates, CALENDAR_VERSION
from .night_session import _night_window

ZONE=ZoneInfo('Asia/Shanghai')
DEFAULTS=dict(enabled=False,allow_dashboard=False,allow_parallel_collectors=False,start_date=None,eod_ready_time='18:15',poll_seconds=60,
              retry_seconds=900,max_days_per_run=5,request_interval_seconds=2.,
              request_timeout_seconds=120,max_run_seconds=7200,login_signal_ttl_seconds=900,
              desk_host='127.0.0.1',desk_port=8765,branch='main',sdk_dir=None,
              include_foundation=True,foundation_timeout_seconds=180,
              allowed_empty_products=['CZCE:LR'])


def config(path):
    result=dict(DEFAULTS)
    result.update(read_json(Path(path),default={}))
    time.fromisoformat(result['eod_ready_time'])
    for key in ('poll_seconds','retry_seconds','max_days_per_run','request_timeout_seconds','max_run_seconds','login_signal_ttl_seconds','foundation_timeout_seconds'):
        if not isinstance(result[key],int) or result[key]<=0:
            raise ValueError(f'invalid configuration: {key}')
    RequestPacer(result['request_interval_seconds'])
    if result['desk_host'] not in {'127.0.0.1','localhost','::1'}:
        raise ValueError('trading desk guard must check localhost')
    if not all(isinstance(result[k],bool) for k in ('enabled','include_foundation','allow_dashboard','allow_parallel_collectors')):
        raise ValueError('enabled/include_foundation/allow_dashboard/allow_parallel_collectors must be boolean')
    return result


def load_snapshot(path):
    path=Path(path)
    if not path.exists():
        return {}
    if path.suffix=='.gz':
        with gzip.open(path,'rt',encoding='utf-8') as stream:
            return json.load(stream)
    return read_json(path,default={})


def night_complete(root, day):
    payload=load_snapshot(Path(root)/f'night_session/{day}.json')
    if payload.get('trading_date')!=day or payload.get('source',{}).get('provider')!='wind':
        return False
    session=night_session_dates(date.fromisoformat(day))
    if session is None:
        return payload.get('collection_state')=='no_scheduled_night_session' and payload.get('calendar_version')==CALENDAR_VERSION
    coverage=payload.get('coverage',{})
    rows=payload.get('records',[])
    return bool(rows and payload.get('session_start_date')==str(session[0])
                and coverage.get('selected_contract_count')==len(rows)
                and coverage.get('unresolved_contract_count')==0
                and coverage.get('night_session_contract_count',0)>0
                and len({(r.get('exchange'),r.get('contract')) for r in rows})==len(rows)
                and all(r.get('source_provider')=='wind' and r.get('trading_date')==day
                        and r.get('record_state') in {'night_session','no_night_trade'} for r in rows))


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
    if options.get('trade_date')!=day or options.get('source_provider')!='wind':
        return False
    option_rows=options.get('records',[])
    if {(r.get('exchange'),r.get('product')) for r in option_rows}!={(p.exchange,p.product) for p in load_catalog().options}:
        return False
    if not all(r.get('source_provider')=='wind' and r.get('source_date_match') is True and r.get('source_trade_date')==day for r in option_rows):
        return False
    from .option_storage import validate_option_snapshot, OptionSnapshotValidationError
    try:
        validate_option_snapshot(options)
    except OptionSnapshotValidationError:
        return False
    return night_complete(root,day)


def plan(root, settings, now=None, anchor=None, known_completed=()):
    root=Path(root)
    now=(now or datetime.now(ZONE)).astimezone(ZONE)
    today=now.date()
    closed=today if is_trading_day(today) and now.time()>=time.fromisoformat(settings['eod_ready_time']) else previous_trading_day(today)
    start=settings.get('start_date') or anchor
    if not start:
        dates=[p.stem for p in (root/'snapshots').glob('????-??-??.json')]
        if not dates:
            raise ValueError('set start_date for a repository without an EOD baseline')
        start=max(dates)
    cursor=date.fromisoformat(start)
    missing=[]
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
            else:
                missing.append(day)
        cursor+=timedelta(days=1)
    selected=missing[:settings['max_days_per_run']]
    night_day=None
    if not missing[len(selected):] and is_trading_day(today) and today>closed and str(today)>=start:
        if night_session_dates(today) and now>=_night_window(str(today))[2] and not night_complete(root,str(today)):
            night_day=str(today)
    return dict(start_date=start,latest_completed_eod=str(closed),full_dates=selected,
                remaining_dates=missing[len(selected):],night_date=night_day,
                has_work=bool(selected or night_day),verified_dates=verified,timezone='Asia/Shanghai')


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


@contextmanager
def single_runner(path):
    """OS-owned lock releases after a crash; no stale PID deletion or process killing."""
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    stream=path.open('a+b')
    try:
        import os
        if os.name=='nt':
            import msvcrt
            if path.stat().st_size==0:
                stream.write(b'0');stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise RuntimeError('another local Wind runner is active')
    try:
        yield
    finally:
        stream.close()


def state(path, phase, **details):
    payload=dict(phase=phase,updated_at=datetime.now(ZONE).isoformat(),**details)
    write_json_atomic(Path(path),payload)
    return payload
