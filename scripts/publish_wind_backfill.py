"""Validate the complete three-layer bundle before local publication.

No network calls. Use --output tmp/... for a staged publication first. Git
commit/push is a separate deliberate action after report validation.
"""
import argparse
from datetime import date, datetime
import json
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo

import pandas as pd
from china_commodities.pipeline import run_pipeline
from china_commodities.option_storage import validate_option_snapshot, publish_option_eod
from china_commodities.catalog import load_catalog
from china_commodities.night_session import (
    _night_window, _coverage, _record_from_quote, normalize_night_session_snapshot,
    normalize_night_session_status,
)
from china_commodities.history_storage import append_parquet_history
from china_commodities.storage import (
    read_json, write_json_if_changed, publish_verified, publish_status,
    publish_night_session_derivatives,
)

ROOT = Path(__file__).resolve().parents[1]
SUFFIX = dict(SHF='SHFE', INE='INE', DCE='DCE', CZC='CZCE', GFE='GFEX')


def prepare_options(snapshot, cache):
    """Apply core scope consistently and verify the cached historical evidence."""
    if snapshot.get('source_provider') != 'wind' or snapshot.get('collection_scope') != 'core_v1':
        raise ValueError('expected Wind core snapshot')
    plans=snapshot['collection_plans']
    selected=sum(p['selected_contract_count'] for p in plans)
    if selected != len(snapshot['records']):
        raise ValueError('selected option quotes incomplete')
    for row in snapshot['records']:
        raw=read_json(cache/'raw/options'/(row['source_code']+'.json'))
        if raw['error'] or [c.upper() for c in raw['codes']] != [row['source_code'].upper()]:
            raise ValueError('option historical contract mismatch')
        if row['source_trade_date'] not in [t[:10] for t in raw['times']]:
            raise ValueError('option historical source date missing')
        row.update(collection_scope='core_v1',analytics_scope='sampled_strikes',
                   source_date_basis='WindPy.wsd.Times')
    snapshot.update(directory_contract_count=sum(p['directory_contract_count'] for p in plans),
                    universe_contract_count=selected,quote_coverage_complete=True,
                    collection_mode='end_of_day_core',intraday=False)
    return snapshot


def build_night(day, universe, prior, cache):
    session_date, start, end = _night_window(day)
    if prior['trade_date'] != session_date:
        raise ValueError('night prior EOD date mismatch')
    previous = {(r['exchange'],r['contract']):r for r in prior['futures_contracts']}
    records=[]
    for code, future in sorted(universe.items()):
        exchange = SUFFIX[code.split('.')[1]]
        contract = code.split('.')[0]
        # Newly listed contracts have no prior EOD; returns remain unavailable.
        ref = previous.get((exchange,contract),{})
        context = dict(exchange=exchange,contract=contract,product=future['variety'],source_code=code,
                       prior_eod_trade_date=session_date,prior_eod_settlement=ref.get('settle'),
                       prior_eod_close=ref.get('close'),previous_day_close=ref.get('close'),
                       previous_day_settlement=ref.get('settle'),previous_day_open_interest=ref.get('open_interest'))
        bars = read_json(cache/f'night/{day}/{code}.json',default=None)
        if bars is None:
            raise ValueError(f'missing night cache {code} {day}')
        for b in bars:
            timestamp=datetime.fromisoformat(b['timestamp']).replace(tzinfo=ZoneInfo('Asia/Shanghai'))
            if not start < timestamp <= end:
                raise ValueError('night timestamp outside window')
        traded=[b for b in bars if (b.get('volume') or 0)>0 and (b.get('close') or 0)>0]
        if traded:
            traded.sort(key=lambda b:b['timestamp'])
            if any(b.get(k) is None for b in traded for k in ('open','high','low','close','volume')):
                raise ValueError('incomplete traded bar')
            quote=dict(time=traded[-1]['timestamp'],open=traded[0]['open'],
                       high=max(b['high'] for b in traded),low=min(b['low'] for b in traded),
                       latest=traded[-1]['close'],volume=sum(b['volume'] for b in traded))
            r=_record_from_quote(context,quote,trading_date=day,session_date=session_date,
                                 window_start=start,window_end=end)
        else:
            r=_record_from_quote(context,None,trading_date=day,session_date=session_date,
                                 window_start=start,window_end=end)
            r.update(record_state='no_night_trade',quality_state='not_applicable',
                     missing_reason='Wind historical query returned no positive-volume night bar')
        r.update(source_provider='wind',source_endpoint='WindPy.wsi',
                 source=dict(provider='wind',endpoint='WindPy.wsi',code=code),
                 timestamp_semantics='last_traded_5min_bar_end',bar_count=len(bars),traded_bar_count=len(traded))
        records.append(r)
    coverage=_coverage(records,len(records))
    if not coverage['night_session_contract_count'] or coverage['unresolved_contract_count']:
        raise ValueError('night coverage invalid')
    generated=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
    snapshot=normalize_night_session_snapshot(dict(schema_version=2,trading_date=day,
        night_session_date=session_date,session_start_date=session_date,session_end_date=end.date().isoformat(),
        generated_at=generated,frequency='night_session_snapshot',reference_eod_trade_date=session_date,
        source=dict(provider='wind',endpoint='WindPy.wsi'),price_semantics='last_completed_bar_close',
        ohlcv_scope='completed_night_5min_bars',official_night_close_verified=False,
        intraday_used=True,coverage=coverage,records=records,
        limitations=dict(is_separate_from_daily_eod=True,bar_size_minutes=5,
                         zero_volume_bars_excluded=True,open_interest_unavailable=True)),daily_snapshot=prior)
    status=normalize_night_session_status(dict(schema_version=2,trading_date=day,
        night_session_date=session_date,generated_at=generated,data_fresh=True,
        validation_passed=True,published=True,coverage_complete=True,coverage=coverage,
        validation_errors=[],coverage_warnings=[],source_provider='wind'),snapshot)
    return snapshot,status


def replace_night_history(path, snapshot):
    """A complete dated replay supersedes earlier realtime observations that day."""
    old=pd.read_parquet(path) if path.exists() else pd.DataFrame()
    if not old.empty:
        old=old[old['trading_date'].astype(str)!=snapshot['trading_date']]
    rows=old.to_dict('records')+[r for r in snapshot['records'] if r['record_state']=='night_session']
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=path.parent) as temporary:
        staged=Path(temporary)/'history.parquet'
        append_parquet_history(staged,rows,key_fields=('trading_date','exchange','contract'),
            sort_fields=('trading_date','exchange','product','contract'),retention_field='trading_date',retention_days=252)
        staged.replace(path)


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--start',type=date.fromisoformat,required=True)
    p.add_argument('--end',type=date.fromisoformat,required=True)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    cache=ROOT/'tmp/wind-backfill'/f'{a.start}_{a.end}'
    futures={}
    frames={e:[] for e in SUFFIX.values()}
    for path in sorted(cache.glob('*.json')):
        for row in read_json(path):
            day=datetime.strptime(row['date'],'%Y%m%d').date().isoformat()
            row['turnover_unit']='CNY_10000'
            row['source_turnover_unit']='CNY'
            row.setdefault('source_turnover',row['turnover']*10000 if row.get('turnover') is not None else None)
            futures.setdefault(day,{})[path.stem]=row
            frames[SUFFIX[path.stem.split('.')[1]]].append(row)
    days=sorted(futures)
    if not days or days[0]!=str(a.start) or days[-1]!=str(a.end):
        raise ValueError('missing requested dates')
    frames={e:pd.DataFrame(rows) for e,rows in frames.items()}
    bundles=[]
    prior=read_json(ROOT/'data/snapshots'/f'{_night_window(days[0])[0]}.json')
    for day in days:
        if len(futures[day])!=806:
            raise ValueError('incomplete futures universe')
        result=run_pipeline(day,provider='wind',wind_prefetched=frames,include_options=False,
                            include_official_auxiliary=False,publish=False)
        if not result.verified:
            raise ValueError(result.validation_errors)
        options=prepare_options(read_json(cache/f'options/{day}.json'),cache)
        validate_option_snapshot(options)
        expected={(p.exchange,p.product) for p in load_catalog().options}
        if {(r['exchange'],r['product']) for r in options['records']}!=expected or options['trade_date']!=day:
            raise ValueError('incomplete options product/date coverage')
        night,status=build_night(day,futures[day],prior,cache)
        bundles.append((result,options,night,status))
        prior=dict(trade_date=day,futures_contracts=result.futures_records)
    # No writes to destination before every layer/date passes.
    old_meta=read_json(a.output/'contract_meta.json',default=None)
    for result,options,night,status in bundles:
        publish_status(result,a.output)
        publish_verified(result,a.output)
        publish_option_eod(options,a.output,surface_shadow_days=1)
        option_status=dict(schema_version=1,trade_date=result.trade_date,generated_at=options['generated_at'],
                           source_provider='wind',collection_scope='core_v1',data_fresh=True,published=True,
                           coverage=options['coverage'],quote_contract_count=len(options['records']),global_error=None)
        write_json_if_changed(a.output/'options/last_run_status.json',option_status)
        for name,payload in [('latest',night),('attempt_latest',night),('last_run_status',status)]:
            write_json_if_changed(a.output/f'night_session/{name}.json',payload)
        replace_night_history(a.output/'night_session/history.parquet',night)
        publish_night_session_derivatives(a.output,snapshot=night,status=status)
        print(json.dumps(dict(date=result.trade_date,futures=len(result.futures_records),
                              options=len(options['records']),night=status['coverage'])),flush=True)
    if old_meta is not None:
        write_json_if_changed(a.output/'contract_meta.json',old_meta)


if __name__=='__main__':
    main()
