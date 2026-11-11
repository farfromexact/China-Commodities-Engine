"""Serial, resumable historical option-core and night-bar backfill.

No subscriptions, logout, scheduler changes or remote publication. Run after
futures caches are complete; SDK requests are deliberately single threaded.
"""
import argparse
from datetime import date, datetime
import json
import math
from pathlib import Path
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

from china_commodities.catalog import load_catalog
from china_commodities.option_core import select_core_contracts
from china_commodities.night_session import _night_window

ROOT = Path(__file__).resolve().parents[1]
SUFFIX = dict(SHF='SHFE', INE='INE', DCE='DCE', CZC='CZCE', GFE='GFEX')
OPTION_FIELDS = 'close,settle,pre_settle,volume,oi,us_impliedvol,delta,gamma'


def clean(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    return value


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(clean(value), ensure_ascii=False, allow_nan=False),encoding='utf-8')


def historical_rows(raw, fields):
    if raw['error']:
        raise ValueError(f"Wind error {raw['error']}")
    if [f.lower() for f in raw['fields']] != fields.split(','):
        raise ValueError('unexpected historical fields')
    if any(len(col) != len(raw['times']) for col in raw['data']):
        raise ValueError('historical shape mismatch')
    return [dict(timestamp=t, **dict(zip(fields.split(','), vals)))
            for t, vals in zip(raw['times'], zip(*raw['data']))]


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--start', type=date.fromisoformat, required=True)
    p.add_argument('--end', type=date.fromisoformat, required=True)
    p.add_argument('--mode', choices=['options', 'night'], required=True)
    p.add_argument('--sdk-dir', help='Official WindPy directory; otherwise WIND_PYTHON_PATH or installed SDK')
    p.add_argument('--worker', action='store_true')
    a = p.parse_args()
    if not a.worker:
        try:
            return subprocess.run([sys.executable, __file__, *sys.argv[1:], '--worker'], timeout=3600).returncode
        except subprocess.TimeoutExpired:
            print('Backfill worker timed out; cached checkpoints retained', flush=True)
            return 2
    cache = ROOT/'tmp/wind-backfill'/f'{a.start}_{a.end}'
    futures = {}
    for path in sorted(cache.glob('*.json')):
        rows = json.loads(path.read_text(encoding='utf-8'))
        for row in rows:
            day = datetime.strptime(row['date'], '%Y%m%d').date().isoformat()
            futures.setdefault(day, {})[path.stem] = row
    days = sorted(futures)
    if not days or days[0] != str(a.start) or days[-1] != str(a.end):
        raise ValueError('futures dates not ready')
    expected = set(futures[days[0]])
    if len(expected) != 806 or any(set(futures[d]) != expected for d in days):
        raise ValueError('This bootstrap requires the verified 806-contract futures window')
    from china_commodities.collectors.wind_runtime import load_wind
    w = load_wind(a.sdk_dir)
    if w.start(waitTime=15).ErrorCode:
        raise ValueError('Wind unavailable')

    def call(key, method, *args):
        path = cache/'raw'/a.mode/(key+'.json')
        if path.exists():
            result = json.loads(path.read_text(encoding='utf-8'))
        else:
            r = getattr(w, method)(*args)
            result = clean(dict(error=r.ErrorCode, codes=r.Codes, fields=r.Fields,
                                times=r.Times, data=r.Data))
            if r.ErrorCode:
                raise ValueError(f'{key}: Wind error {r.ErrorCode}')
            save(path, result)
            time.sleep(.5)
        if method in {'wsd','wsi'} and [c.upper() for c in result['codes']] != [args[0].upper()]:
            raise ValueError('Wind response contract mismatch')
        return result

    if a.mode == 'options':
        for day in days:
            records, plans = [], {}
            for product in load_catalog().options:
                candidates = sorted([(code, row) for code, row in futures[day].items()
                                     if row['variety'] == product.product and SUFFIX[code.split('.')[1]] == product.exchange],
                                    key=lambda item: item[1].get('open_interest') or 0, reverse=True)
                selected = []
                for code, future in candidates:
                    chain = call(day+'-'+code, 'wset', 'optionchain',
                                 f'date={day};us_code={code};option_var=all;call_put=all')
                    if not chain['data'] or not chain['data'][0]:
                        continue
                    directory = []
                    for vals in zip(*chain['data']):
                        row = dict(zip(chain['fields'], vals))
                        if str(row['first_tradedate'])[:10] > day or str(row['last_tradedate'])[:10] <= day:
                            continue
                        directory.append(dict(contract=row['option_code'].split('.')[0].upper(),
                                              source_code=row['option_code'], underlying_contract=code.split('.')[0],
                                              expiry_date=str(row['last_tradedate'])[:10],
                                              option_type={'认购':'C', '认沽':'P'}[row['call_put']],
                                              strike=float(row['strike_price']),
                                              exercise_style={'欧式':'european','美式':'american'}.get(row['exe_type'],'unknown')))
                    if not directory:
                        continue
                    selected, plan = select_core_contracts(directory,
                        {code.split('.')[0]:dict(future,source_trade_date=day)}, [], day)
                    plan['directory_scope'] = 'selected_active_underlying_only'
                    plan['directory_source'] = 'WindPy.wset.optionchain'
                    plans[product.exchange+':'+product.product] = plan
                    break
                if not selected:
                    raise ValueError(f'{day} no eligible option core: {product}')
                for row in selected:
                    raw = call(row['source_code'], 'wsd', row['source_code'], OPTION_FIELDS,
                               str(a.start), str(a.end), 'Days=Trading;Fill=Blank')
                    quotes = {r['timestamp'][:10]:r for r in historical_rows(raw, OPTION_FIELDS)}
                    q = quotes[day]
                    if q['settle'] is None:
                        raise ValueError(f"missing option settlement: {row['source_code']} {day}")
                    greeks = dict(iv_percent=q['us_impliedvol']*100 if q['us_impliedvol'] is not None else None,
                                  delta=q['delta'], gamma=q['gamma'])
                    records.append(dict(row,trade_date=day,exchange=product.exchange,product=product.product,
                        collection_scope='core_v1',analytics_scope='sampled_strikes',
                        source_provider='wind',source_endpoint='WindPy.wsd',source_trade_date=day,
                        source_date_basis='WindPy.wsd.Times',
                        source_date_match=True,source_date_status='source_date_verified',
                        close=q['close'],settle=q['settle'],pre_settle=q['pre_settle'],
                        volume=q['volume'],open_interest=q['oi'],underlying_settle=future['settle'],
                        **greeks,greeks=dict(quality='vendor_reported',selected_source='vendor',
                            selected=greeks,vendor=greeks,model=None,units_as_reported=False,
                            units='IV_percent_delta_gamma_Wind_native',dealer_position_direction_known=False)))
                print(f'Options {day} {product.exchange}:{product.product} {len(selected)}', flush=True)
            snapshot = dict(schema_version=1,trade_date=day,generated_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
                source_provider='wind',universe_source='wind_optionchain_active_underlying',
                collection_scope='core_v1',analytics_scope='sampled_strikes',
                collection_plans=[dict(plan,product_key=key) for key,plan in plans.items()],
                directory_contract_count=sum(p['directory_contract_count'] for p in plans.values()),
                universe_contract_count=sum(p['selected_contract_count'] for p in plans.values()),
                quote_contract_count=len(records),quote_coverage_complete=True,
                collection_mode='end_of_day_core',intraday=False,
                coverage=dict(expected_product_count=len(load_catalog().options),successful_product_count=len(plans),
                              attempted_product_count=len(plans),failed_product_count=0,skipped_product_count=0,
                              product_coverage=1.0,scope_complete=True,publish_eligible=True,
                              successful_products=sorted(plans),failed_products=[],skipped_products=[]),
                records=records)
            from china_commodities.option_storage import validate_option_snapshot
            validate_option_snapshot(snapshot)
            save(cache/f'options/{day}.json',snapshot)
    else:
        fields = 'open,high,low,close,volume'
        start = _night_window(days[0])[1].strftime('%Y-%m-%d %H:%M:%S')
        end = _night_window(days[-1])[2].strftime('%Y-%m-%d %H:%M:%S')
        for i, code in enumerate(sorted(expected)):
            raw = call(code, 'wsi', code, fields, start, end, 'BarSize=5;Fill=Blank')
            bars = historical_rows(raw, fields)
            if len({r['timestamp'] for r in bars}) != len(bars):
                raise ValueError('duplicate bar timestamps')
            for day in days:
                _, window_start, window_end = _night_window(day)
                selected = [r for r in bars if window_start.replace(tzinfo=None) < datetime.fromisoformat(r['timestamp']).replace(tzinfo=None) <= window_end.replace(tzinfo=None)]
                save(cache/f'night/{day}/{code}.json',selected)
            if (i+1)%50 == 0 or i+1 == len(expected):
                print(f'Night {i+1}/{len(expected)}',flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
