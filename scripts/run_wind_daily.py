"""Run once: sync Git, collect completed Wind sessions, validate and push data."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import sys
import tempfile

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


def once(settings, cache):
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    publisher = Publisher(ROOT, cache, settings['branch'])
    print('同步 Git...', flush=True)
    recovered = publisher.recover()
    publisher.sync()
    anchor = read_json(cache / 'anchor.json', default={})
    work = plan(ROOT / 'data', settings, anchor=anchor.get('start_date'),
                known_completed=anchor.get('verified_dates', []))
    write_json_atomic(cache / 'anchor.json', dict(start_date=work['start_date'], verified_dates=work['verified_dates']))
    if not work['has_work']:
        return finish(cache, 'up_to_date', plan=work, recovered_push=recovered)

    base = git(ROOT, 'rev-parse', 'HEAD')
    print('连接 Wind...', flush=True)
    sdk = load_wind(settings['sdk_dir'])
    started = sdk.start(waitTime=15)
    if started.ErrorCode or not sdk.isconnected():
        raise RuntimeError(f'Wind 连接失败（{started.ErrorCode}），请登录官方 Wind 后重新启动。')
    # Vendor errors are handled by CachedWind; do not probe the desktop/session per request.
    api = CachedWind(sdk, cache / 'raw', lambda: None, settings['request_interval_seconds'])
    with tempfile.TemporaryDirectory(prefix='staging-', dir=cache) as temporary:
        stage = Path(temporary) / 'data'
        shutil.copytree(ROOT / 'data', stage)
        results = []
        for day in work['full_dates']:
            print(f'{day}: 提取期货、核心期权及夜盘...', flush=True)
            results.append(collect_day(api, day, stage, cache / 'normalized',
                                       allowed_empty=settings['allowed_empty_products']))
        if work['night_date']:
            day = work['night_date']
            print(f'{day}: 提取已完成夜盘...', flush=True)
            results.append(collect_day(api, day, stage, cache / 'normalized', night_only=True,
                                       allowed_empty=settings['allowed_empty_products']))
        errors = validate_snapshot(read_json(stage / 'latest.json'))
        if errors:
            raise RuntimeError(f'数据校验失败: {errors}')
        publish_report_input(stage)
        print('校验通过，提交并推送数据...', flush=True)
        publisher.prepare(stage, base)
        publisher.recover()
    # Never stop/logout/cancel the shared Wind session.
    return finish(cache, 'published', plan=work, results=results)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', action='store_true', help='只查看缺失日期，不连接 Wind 或 Git')
    parser.add_argument('--once', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--config', type=Path, default=ROOT / 'tmp/wind-daily/config.json')
    args = parser.parse_args(argv)
    cache = ROOT / 'tmp/wind-daily'
    try:
        settings = config(args.config)
        if args.plan:
            anchor = read_json(cache / 'anchor.json', default={})
            emit(plan(ROOT / 'data', settings, anchor=anchor.get('start_date'),
                      known_completed=anchor.get('verified_dates', [])))
            return 0
        while True:
            result = once(settings, cache)
            emit(result)
            if not result['plan']['remaining_dates']:
                return 0
    except Exception as exc:
        emit(finish(cache, 'failed', error=str(exc)))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
