"""Run once: sync Git, collect completed Wind sessions, validate and push data."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from china_commodities.wind_daily import config, plan, ZONE
from china_commodities.wind_daily_git import Publisher, git
from china_commodities.storage import read_json, write_json_atomic
from china_commodities.collectors.wind_runtime import load_wind
from china_commodities.wind_daily_collect import CachedWind, collect_day
from china_commodities.reporting import publish_report_input
from china_commodities.quality import validate_snapshot
from china_commodities.wind_intraday import collect_intraday
from china_commodities.wind_index import collect_eod as collect_index_eod, collect_intraday as collect_index_intraday
from china_commodities.wind_treasury import collect_eod as collect_treasury_eod, collect_intraday as collect_treasury_intraday
from china_commodities.wind_funding import collect_eod as collect_funding_eod, collect_intraday as collect_funding_intraday

TRACE = None


def save_timing():
    if TRACE is None:
        return
    payload={k:v for k,v in TRACE.items() if k not in ('clock','path')}
    payload['total_elapsed_seconds']=round(time.monotonic()-TRACE['clock'],3)
    try:
        write_json_atomic(TRACE['path'],payload)
    except OSError as exc:
        print(f'Timing file unavailable: {exc}',file=sys.stderr)


@contextmanager
def measure(phase,api=None):
    started=time.monotonic()
    metrics=dict(api.metrics) if api is not None else None
    event=dict(phase=phase,started_at=datetime.now(ZONE).isoformat(),outcome='ok')
    try:
        yield
    except Exception as exc:
        event.update(outcome='failed',error=str(exc)[:500])
        raise
    finally:
        event.update(finished_at=datetime.now(ZONE).isoformat(),elapsed_seconds=round(time.monotonic()-started,3))
        if metrics is not None:
            event['wind']={k:round(api.metrics[k]-v,6) for k,v in metrics.items()}
        if TRACE is not None:
            TRACE['events'].append(event)
            emit(dict(timing=event))
            save_timing()


def measured_collect(label,api,function,*args,**kwargs):
    with measure(label,api):
        return function(api,*args,**kwargs)


def emit(payload):
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def finish(cache, phase, **details):
    payload = dict(phase=phase, updated_at=datetime.now(ZONE).isoformat(), **details)
    # A display/status file must never prevent collection or publication.
    try:
        write_json_atomic(cache / 'status.json', payload)
    except OSError as exc:
        print(f'Status file unavailable: {exc}', file=sys.stderr)
    return payload


def once(settings, cache, now=None,excluded_funding=()):
    now = now or datetime.now(ZONE)
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    publisher = Publisher(ROOT, cache, settings['branch'])
    print('同步 Git...', flush=True)
    with measure('git_sync'):
        recovered = publisher.recover()
        publisher.sync()
    anchor = read_json(cache / 'anchor.json', default={})
    work = plan(ROOT / 'data', settings, now=now, anchor=anchor.get('start_date'),
                known_completed=anchor.get('verified_dates', []),excluded_funding=excluded_funding)
    write_json_atomic(cache / 'anchor.json', dict(start_date=work['start_date'], verified_dates=work['verified_dates']))
    if not work['has_work']:
        return finish(cache, 'up_to_date', plan=work, recovered_push=recovered)

    base = git(ROOT, 'rev-parse', 'HEAD')
    print('连接 Wind...', flush=True)
    with measure('wind_connect'):
        sdk = load_wind(settings['sdk_dir'])
        started = sdk.start(waitTime=15)
        if started.ErrorCode or not sdk.isconnected():
            raise RuntimeError(f'Wind 连接失败（{started.ErrorCode}），请登录官方 Wind 后重新启动。')
    # Vendor errors are handled by CachedWind; do not probe the desktop/session per request.
    api = CachedWind(sdk, cache / 'raw', lambda: None, settings['request_interval_seconds'])
    with tempfile.TemporaryDirectory(prefix='staging-', dir=cache) as temporary:
        stage = Path(temporary) / 'data'
        with measure('stage_copy'):
            shutil.copytree(ROOT / 'data', stage)
        results = []
        if work.get('intraday'):
            print(f"提取盘中快照，截止 {work['intraday']['cutoff']}...", flush=True)
            results.append(measured_collect('commodity_intraday',api,collect_intraday, work['intraday'], stage,
                                            allowed_empty=settings['allowed_empty_products']))
            results.append(measured_collect('index_intraday',api,collect_index_intraday, work['intraday'], stage,options_scope=settings['index_options_scope']))
        if work.get('treasury_intraday'):
            print(f"提取国债期货分钟快照，截止 {work['treasury_intraday']['cutoff']}...",flush=True)
            results.append(measured_collect('treasury_intraday',api,collect_treasury_intraday,work['treasury_intraday'],stage))
        if work.get('funding_intraday'):
            print(f"提取交易所回购分钟快照，截止 {work['funding_intraday']['cutoff']}...",flush=True)
            results.append(measured_collect('funding_intraday',api,collect_funding_intraday,work['funding_intraday'],stage))
        for day in work['full_dates']:
            print(f'{day}: 提取期货、核心期权及夜盘...', flush=True)
            results.append(measured_collect('commodity_eod:'+day,api,collect_day, day, stage, cache / 'normalized',
                                       allowed_empty=settings['allowed_empty_products']))
        if work['night_date']:
            day = work['night_date']
            print(f'{day}: 提取已完成夜盘...', flush=True)
            results.append(measured_collect('commodity_night:'+day,api,collect_day, day, stage, cache / 'normalized', night_only=True,
                                       allowed_empty=settings['allowed_empty_products']))
        for day in work.get('index_dates', []):
            print(f"{day}: 提取股指期货、股指期权（{settings['index_options_scope']}）及现金市场...", flush=True)
            results.append(measured_collect('index_eod:'+day,api,collect_index_eod, day, stage,options_scope=settings['index_options_scope']))
        for day in work.get('treasury_dates',[]):
            print(f'{day}: 提取TS/TF/T/TL国债期货...',flush=True)
            results.append(measured_collect('treasury_eod:'+day,api,collect_treasury_eod,day,stage))
        deferred=set(excluded_funding)
        for day in work.get('funding_dates',[]):
            print(f'{day}: 提取银行间与交易所回购利率...',flush=True)
            result=measured_collect('funding_eod:'+day,api,collect_funding_eod,day,stage)
            results.append(result)
            if not result['coverage_complete']:
                deferred.add(day)
                print(f"{day}: 回购面板部分缺失（{result['available']}/{result['expected']}）；保留缺失状态，本次不循环重试。",flush=True)
        with measure('validation_and_report'):
            errors = [] if work.get('intraday') else validate_snapshot(read_json(stage / 'latest.json'))
            if errors:
                raise RuntimeError(f'数据校验失败: {errors}')
            publish_report_input(stage)
        print('校验通过，提交并推送数据...', flush=True)
        with measure('git_publish'):
            publisher.prepare(stage, base)
            publisher.recover()
    # Never stop/logout/cancel the shared Wind session.
    return finish(cache, 'published', plan=work, results=results,deferred_funding_dates=sorted(deferred))


def main(argv=None):
    global TRACE
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', action='store_true', help='只查看缺失日期，不连接 Wind 或 Git')
    parser.add_argument('--once', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--config', type=Path, default=ROOT / 'tmp/wind-daily/config.json')
    args = parser.parse_args(argv)
    cache = ROOT / 'tmp/wind-daily'
    started_at = datetime.now(ZONE)
    deferred_funding=set()
    if not args.plan:
        TRACE=dict(started_at=started_at.isoformat(),clock=time.monotonic(),events=[],outcome='running',
            path=cache/'run-timings'/(started_at.strftime('%Y%m%d-%H%M%S-%f')+'.json'),
            scope='single foreground run; cached requests separated from SDK calls')
    try:
        settings = config(args.config)
        if args.plan:
            anchor = read_json(cache / 'anchor.json', default={})
            emit(plan(ROOT / 'data', settings, now=started_at, anchor=anchor.get('start_date'),
                      known_completed=anchor.get('verified_dates', [])))
            return 0
        while True:
            result = once(settings, cache, now=started_at,excluded_funding=deferred_funding)
            emit(result)
            deferred_funding.update(result.get('deferred_funding_dates',[]))
            if not any(result['plan'].get(key) for key in ('remaining_dates','remaining_index_dates','remaining_treasury_dates','remaining_funding_dates')):
                if TRACE is not None:
                    TRACE.update(outcome=result['phase'],deferred_funding_dates=sorted(deferred_funding))
                    save_timing()
                    emit(dict(timing_path=str(TRACE['path']),total_elapsed_seconds=round(time.monotonic()-TRACE['clock'],3)))
                return 0
    except Exception as exc:
        if TRACE is not None:
            TRACE.update(outcome='failed',error=str(exc)[:500])
            save_timing()
        emit(finish(cache, 'failed', error=str(exc)))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
