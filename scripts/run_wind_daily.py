"""Local-only supervisor. Dot signals observed login; no remote workflow dispatch."""
import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'src'))

from china_commodities.wind_daily import config, plan, state, single_runner, desk_busy, ZONE, DEFAULTS
from china_commodities.wind_daily_git import Publisher, git
from china_commodities.storage import read_json, write_json_atomic


def emit(payload):
    print(json.dumps(payload,ensure_ascii=False),flush=True)


def refresh_foundation(stage, cache, settings):
    """Keep the existing independent AKShare layers; failures do not fake freshness."""
    if not settings['include_foundation']:
        return []
    from china_commodities.collection_cache import verified_foundation_available
    eod=read_json(stage/'latest.json')['trade_date']
    hint=datetime.now(ZONE).date()-timedelta(days=1)
    while hint.weekday()>=5:
        hint-=timedelta(days=1)
    attempts=[]
    for scope,day in [('physical',eod),('external',min(str(hint),eod))]:
        if verified_foundation_available(stage,scope,day,provider='akshare'):
            continue
        state(cache/'progress.json','foundation',scope=scope,trade_date=day)
        error=None
        try:
            with (cache/'foundation.log').open('a',encoding='utf-8') as log:
                run=subprocess.run([sys.executable,'-m','china_commodities.cli','foundation',
                    '--provider','akshare','--scope',scope,'--date',day,'--data-dir',str(stage),
                    '--shadow-days','1','--force-refresh'],cwd=ROOT,stdout=log,stderr=log,
                    env=dict(os.environ,PYTHONPATH=str(ROOT/'src')),timeout=settings['foundation_timeout_seconds'])
            if run.returncode:
                error=f'AKShare subprocess exit {run.returncode}'
        except subprocess.TimeoutExpired:
            error='AKShare request timeout'
        if error:
            write_json_atomic(stage/f'{scope}/last_run_status.json',dict(schema_version=1,trade_date=day,
                source_provider='akshare',data_fresh=False,published=False,global_error=error,
                previous_valid_snapshot_retained=(stage/f'{scope}/latest.json').exists()))
        attempts.append(dict(scope=scope,requested_date=day,error=error))
    return attempts


def worker(job_path):
    job=read_json(job_path)
    if not os.environ.get('WIND_DAILY_WORKER_TOKEN') or job['token']!=os.environ['WIND_DAILY_WORKER_TOKEN']:
        raise RuntimeError('worker must be launched by the local supervisor')
    settings=job['settings']
    cache=Path(job['cache'])
    progress=cache/'progress.json'
    def available():
        if desk_busy(settings):
            raise RuntimeError('trading desk became active; collection yielded')
    available()
    from china_commodities.collectors.wind_runtime import load_wind
    sdk=load_wind(settings['sdk_dir'])
    class Native:
        def __getattr__(self,method):
            if method not in {'start','isconnected','wset','wsd','wsi'}:
                raise AttributeError(method)
            def call(*args,**kwargs):
                available()
                state(progress,'request',operation=method)
                result=getattr(sdk,method)(*args,**kwargs)
                state(progress,'collecting')
                return result
            return call
    client=Native()
    if client.start(waitTime=15).ErrorCode or not client.isconnected():
        raise RuntimeError('Wind login unavailable; wait for a new login-ready signal')
    def guard():
        available()
        if not client.isconnected():
            raise RuntimeError('Wind disconnected; no automatic reconnect')
    from china_commodities.wind_daily_collect import CachedWind, collect_day
    from china_commodities.reporting import publish_report_input
    from china_commodities.quality import validate_snapshot
    api=CachedWind(client,cache/'raw',guard,settings['request_interval_seconds'])
    publisher=Publisher(ROOT,cache,settings['branch'])
    base=job['base']
    with tempfile.TemporaryDirectory(prefix='staging-',dir=cache) as temporary:
        stage=Path(temporary)/'data'
        shutil.copytree(ROOT/'data',stage)
        results=[]
        for day in job['plan']['full_dates']:
            state(progress,'collecting',trade_date=day,layer='full')
            results.append(collect_day(api,day,stage,cache/'normalized',allowed_empty=settings['allowed_empty_products']))
        if job['plan']['night_date']:
            day=job['plan']['night_date']
            results.append(collect_day(api,day,stage,cache/'normalized',night_only=True,allowed_empty=settings['allowed_empty_products']))
        if validate_snapshot(read_json(stage/'latest.json')):
            raise RuntimeError('staged EOD validation failed')
        foundation=refresh_foundation(stage,cache,settings)
        publish_report_input(stage)
        # Do not publish any portion of a planned batch before every layer passes.
        state(progress,'publishing',results=results)
        publisher.prepare(stage,base)
        publisher.recover()
        state(progress,'published',results=results,foundation=foundation)
    # Never stop/logout/cancel the shared SDK session.


def once(settings, cache):
    status=cache/'status.json'
    if not settings['enabled']:
        return state(status,'disabled')
    publisher=Publisher(ROOT,cache,settings['branch'])
    # A prepared/committed outbox is recovered before any new vendor request.
    recovered=publisher.recover()
    publisher.sync()
    anchor=read_json(cache/'anchor.json',default={})
    work=plan(ROOT/'data',settings,anchor=anchor.get('start_date'),known_completed=anchor.get('verified_dates',[]))
    write_json_atomic(cache/'anchor.json',dict(start_date=work['start_date'],verified_dates=work['verified_dates']))
    if not work['has_work']:
        return state(status,'up_to_date',plan=work,recovered_push=recovered)
    previous=read_json(status,default={})
    retry=previous.get('retry_after')
    if retry and datetime.now(ZONE)<datetime.fromisoformat(retry):
        return dict(previous,plan=work)
    if desk_busy(settings):
        return state(status,'desk_busy',plan=work)
    pulse=cache/'login-ready.json'
    signal=read_json(pulse,default={})
    when=datetime.fromisoformat(signal['observed_at']) if signal else None
    age=(datetime.now(ZONE)-when).total_seconds() if when else None
    if age is None or not 0<=age<=settings['login_signal_ttl_seconds']:
        return state(status,'waiting_login_signal',plan=work)
    # Consume once, so a failure cannot repeatedly open login dialogs.
    pulse.replace(cache/'login-claimed.json')
    token=uuid.uuid4().hex
    write_json_atomic(cache/'job.json',dict(token=token,settings=settings,cache=str(cache),plan=work,base=git(ROOT,'rev-parse','HEAD')))
    state(cache/'progress.json','starting')
    state(status,'running',plan=work)
    with (cache/'worker.log').open('a',encoding='utf-8') as log:
        process=subprocess.Popen([sys.executable,__file__,'--worker',str(cache/'job.json')],stdout=log,stderr=log,
                                 cwd=ROOT,env=dict(os.environ,WIND_DAILY_WORKER_TOKEN=token))
        started=time.monotonic()
        failure=None
        try:
            while process.poll() is None:
                progress=read_json(cache/'progress.json',default={})
                if progress.get('phase')=='request' and (datetime.now(ZONE)-datetime.fromisoformat(progress['updated_at'])).total_seconds()>settings['request_timeout_seconds']:
                    failure='native request timeout; only this collection worker stopped'
                if time.monotonic()-started>settings['max_run_seconds']:
                    failure='batch timeout; cache retained'
                if failure:
                    process.kill();process.wait()
                    break
                time.sleep(1)
        except BaseException:
            process.kill();process.wait()
            raise
        if process.returncode or failure:
            return state(status,'retry_wait',plan=work,error=failure or 'worker failed; inspect worker.log',
                         retry_after=(datetime.now(ZONE)+timedelta(seconds=settings['retry_seconds'])).isoformat(),
                         outbox_retained=publisher.journal.exists())
    return state(status,'published',plan=work,results=read_json(cache/'progress.json',default={}).get('results',[]))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    group=p.add_mutually_exclusive_group()
    group.add_argument('--once',action='store_true')
    group.add_argument('--watch',action='store_true')
    group.add_argument('--plan',action='store_true')
    group.add_argument('--status',action='store_true')
    group.add_argument('--signal-login',action='store_true')
    group.add_argument('--init',action='store_true')
    p.add_argument('--config',type=Path,default=ROOT/'tmp/wind-daily/config.json')
    p.add_argument('--worker',type=Path,help=argparse.SUPPRESS)
    args=p.parse_args()
    if args.worker:
        worker(args.worker)
        return 0
    cache=ROOT/'tmp/wind-daily'
    cache.mkdir(parents=True,exist_ok=True)
    if args.init:
        if args.config.exists():
            raise ValueError('configuration already exists; not overwritten')
        write_json_atomic(args.config,DEFAULTS)
        emit(dict(config=str(args.config),enabled=False))
        return 0
    if args.signal_login:
        write_json_atomic(cache/'login-ready.json',dict(observed_at=datetime.now(ZONE).isoformat()))
        emit(dict(phase='login_signal_recorded',credentials_stored=False))
        return 0
    if args.status:
        emit(read_json(cache/'status.json',default=dict(phase='not_started')))
        return 0
    settings=config(args.config)
    if args.plan or not (args.once or args.watch):
        anchor=read_json(cache/'anchor.json',default={})
        emit(plan(ROOT/'data',settings,anchor=anchor.get('start_date'),known_completed=anchor.get('verified_dates',[])))
        return 0
    with single_runner(cache/'runner.lock'):
        while True:
            settings=config(args.config)
            try:
                emit(once(settings,cache))
            except Exception as exc:
                emit(state(cache/'status.json','blocked',error=str(exc),outbox_retained=(cache/'outbox.json').exists()))
                if args.once:
                    return 1
            if not args.watch:
                return 0
            time.sleep(settings['poll_seconds'])


if __name__=='__main__':
    raise SystemExit(main())
