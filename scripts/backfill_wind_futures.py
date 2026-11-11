"""Resumable, serial Wind futures backfill. Explicit publish, no scheduler changes."""
import argparse
from datetime import datetime, date
import json
import math
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
FIELDS = 'open,high,low,close,settle,pre_settle,volume,oi,amt'


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--start', required=True, type=date.fromisoformat)
    p.add_argument('--end', required=True, type=date.fromisoformat)
    p.add_argument('--directory', required=True, type=Path)
    p.add_argument('--limit', type=int)
    p.add_argument('--sdk-dir', help='Official WindPy directory; otherwise WIND_PYTHON_PATH or installed SDK')
    p.add_argument('--publish', action='store_true')
    p.add_argument('--worker', action='store_true')
    a = p.parse_args()
    if a.start > a.end:
        p.error('invalid date range')
    cache = ROOT/'tmp/wind-backfill'/f'{a.start}_{a.end}'
    cache.mkdir(parents=True, exist_ok=True)
    docs = json.loads(a.directory.read_text(encoding='utf-8'))
    universe = {}
    for doc in docs:
        if doc['error']:
            raise ValueError('directory error')
        if not doc['data']:
            continue
        for row in zip(*doc['data']):
            row = dict(zip(doc['fields'], row))
            # This bootstrap directory must cover the whole short backfill window.
            if str(row['contract_issue_date'])[:10] > str(a.start) or str(row['last_trade_date'])[:10] < str(a.end):
                raise ValueError('Use per-day directories for changing contract universe')
            universe[row['wind_code']] = row
    codes = sorted(universe)[:a.limit] if a.limit else sorted(universe)
    if not a.publish and not a.worker:
        try:
            return subprocess.run([sys.executable, __file__, *sys.argv[1:], '--worker'], timeout=1200).returncode
        except subprocess.TimeoutExpired:
            print('Only backfill worker stopped; checkpoint retained')
            return 2
    if not a.publish:
        from china_commodities.collectors.wind_runtime import load_wind
        w = load_wind(a.sdk_dir)
        if w.start(waitTime=15).ErrorCode:
            raise ValueError('Wind login failed')
        for i, code in enumerate(codes):
            path = cache/(code+'.json')
            if path.exists():
                continue
            r = w.wsd(code, FIELDS, str(a.start), str(a.end), 'Days=Trading;Fill=Blank')
            if r.ErrorCode:
                raise ValueError(f'{code}: Wind error {r.ErrorCode}')
            if [f.lower() for f in r.Fields] != FIELDS.split(','):
                raise ValueError('unexpected fields')
            if not r.Times or any(len(c)!=len(r.Times) for c in r.Data):
                raise ValueError('malformed response')
            records=[]
            for n,t in enumerate(r.Times):
                day=t.strftime('%Y-%m-%d')
                values={f.lower():c[n] for f,c in zip(r.Fields,r.Data)}
                values={k:float(v) if isinstance(v,(int,float)) and math.isfinite(v) else None for k,v in values.items()}
                if not str(a.start)<=day<=str(a.end) or values['settle'] is None:
                    raise ValueError(f'{code}: missing settlement/date')
                records.append(dict(date=day.replace('-',''), symbol=code.split('.')[0],
                                    variety=''.join(c for c in code.split('.')[0] if c.isalpha()),
                                    source_provider='wind',source_endpoint='WindPy.wsd',source_code=code,
                                    turnover_unit='CNY_10000',source_turnover=values['amt'],source_turnover_unit='CNY',
                                    **{k:v for k,v in values.items() if k not in ('oi','amt')},
                                    open_interest=values['oi'],turnover=values['amt']/10000 if values['amt'] is not None else None))
            path.write_text(json.dumps(records,ensure_ascii=False,allow_nan=False),encoding='utf-8')
            if (i+1)%50==0 or i+1==len(codes):
                print(f'Collected {i+1}/{len(codes)}',flush=True)
            time.sleep(.5)
        print('Cache:',cache,flush=True)
        return 0
    if a.limit:
        raise ValueError('Cannot publish limited sample')
    import pandas as pd
    from china_commodities.pipeline import run_pipeline
    from china_commodities.storage import publish_verified, publish_status, read_json, write_json_if_changed
    frames={e:[] for e in ['SHFE','INE','DCE','CZCE','GFEX']}
    suffix=dict(SHF='SHFE',INE='INE',DCE='DCE',CZC='CZCE',GFE='GFEX')
    for code in codes:
        rows=json.loads((cache/(code+'.json')).read_text(encoding='utf-8'))
        for row in rows:
            row['turnover_unit']='CNY_10000'
            row['source_turnover_unit']='CNY'
            if 'source_turnover' not in row:
                row['source_turnover']=row['turnover']*10000 if row.get('turnover') is not None else None
        frames[suffix[code.split('.')[1]]].extend(rows)
    frames={e:pd.DataFrame(r) for e,r in frames.items()}
    days=sorted(set(frames['SHFE']['date']))
    results=[]
    for day in days:
        for frame in frames.values():
            if len(frame[frame.date==day]) != frame.symbol.nunique():
                raise ValueError('Incomplete per-date universe')
        result=run_pipeline(day, provider='wind',wind_prefetched=frames,
                            include_options=False,include_official_auxiliary=False,publish=False)
        if not result.verified:
            raise ValueError(str(result.validation_errors))
        results.append(result)
    old_meta=read_json(ROOT/'data/contract_meta.json', default=None)
    for result in results:
        publish_status(result, ROOT/'data')
        publish_verified(result, ROOT/'data')
        print('Published',result.trade_date,len(result.futures_records),flush=True)
    # This backfill has no new contract metadata; keep the dated prior artifact.
    if old_meta is not None:
        write_json_if_changed(ROOT/'data/contract_meta.json',old_meta)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
