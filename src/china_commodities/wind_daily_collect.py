"""Dated Wind collection; no login, scheduling, Git, or shared-session control."""
from datetime import date, datetime
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import time
import shutil
import tempfile
from zoneinfo import ZoneInfo

import pandas as pd
from .catalog import load_catalog
from .option_core import select_core_contracts
from .option_storage import validate_option_snapshot, publish_option_eod
from .pipeline import run_pipeline
from .storage import read_json, write_json_if_changed, publish_status, publish_verified
from .trading_calendar import night_session_dates, CALENDAR_VERSION
from .night_session import _night_window
# These tested conversion/publication helpers never load an SDK on import.
from scripts.backfill_wind_derivatives import clean, historical_rows, OPTION_FIELDS
from scripts.publish_wind_backfill import build_night, replace_night_history

SUFFIX = dict(SHFE='SHF', INE='INE', DCE='DCE', CZCE='CZC', GFEX='GFE')
FIELDS = 'open,high,low,close,settle,pre_settle,volume,oi,amt'


class CachedWind:
    def __init__(self, client, root, guard, interval=1.):
        self.client, self.root, self.guard, self.interval = client, Path(root), guard, interval
        self.last_request = 0.

    def path(self, method, args):
        key = hashlib.sha256(json.dumps([method,args],ensure_ascii=False).encode()).hexdigest()
        return self.root/(key+'.json')

    def discard(self, method, *args):
        self.path(method,args).unlink(missing_ok=True)

    def call(self, method, *args):
        self.guard()
        path=self.path(method,args)
        if path.exists():
            return read_json(path)
        time.sleep(max(0.,self.interval-(time.monotonic()-self.last_request)))
        self.guard()
        r=getattr(self.client,method)(*args)
        self.last_request=time.monotonic()
        raw=clean(dict(error=r.ErrorCode,codes=list(r.Codes),fields=list(r.Fields),times=list(r.Times),data=r.Data))
        if raw['error']:
            raise ValueError(f'Wind {method} error {raw["error"]}')
        if method in {'wsd','wsi'} and [str(c).upper() for c in raw['codes']] != [args[0].upper()]:
            raise ValueError('Wind contract identity mismatch')
        write_json_if_changed(path,raw)
        return raw

    def eod(self, code, fields, day):
        args=(code,fields,day,day,'Days=Trading;Fill=Blank')
        try:
            rows=historical_rows(self.call('wsd',*args),fields)
            if len(rows)!=1 or rows[0]['timestamp'][:10]!=day:
                raise ValueError('EOD date/shape mismatch')
            if rows[0].get('settle') is None:
                raise ValueError('EOD settlement not yet available')
            return rows[0]
        except (ValueError,KeyError):
            self.discard('wsd',*args)
            raise


def directory(api, day, *, catalog=None, allowed_empty=('CZCE:LR',)):
    catalog=catalog or load_catalog()
    universe={}
    empty=[]
    for exchange,products in catalog.exchanges.items():
        for product in products:
            key=f'{exchange}:{product}'
            params=f'startdate={day};enddate={day};wind_code={product}.{SUFFIX[exchange]}'
            raw=api.call('wset','futurecc',params)
            try:
                fields=[f.lower() for f in raw['fields']]
                if raw['data'] and len(raw['data'])!=len(fields):
                    raise ValueError('directory shape mismatch')
                if raw['data'] and len({len(c) for c in raw['data']})>1:
                    raise ValueError('directory column lengths differ')
                rows=[dict(zip(fields,values)) for values in zip(*raw['data'])]
                if not rows:
                    if key not in allowed_empty:
                        raise ValueError(f'unexpected empty directory {key}')
                    empty.append(key)
                for row in rows:
                    code=str(row['wind_code']).upper()
                    contract,suffix=code.split('.')
                    if suffix!=SUFFIX[exchange] or ''.join(c for c in contract if c.isalpha())!=product:
                        raise ValueError('directory product/exchange mismatch')
                    if code in universe:
                        raise ValueError('duplicate directory contract')
                    issued=date.fromisoformat(str(row['contract_issue_date'])[:10])
                    expiry=date.fromisoformat(str(row['last_trade_date'])[:10])
                    if not issued<=date.fromisoformat(day)<=expiry:
                        raise ValueError('directory contract not active on target day')
                    universe[code]=dict(exchange=exchange,product=product,contract=contract,**row)
            except (ValueError,KeyError):
                api.discard('wset','futurecc',params)
                raise
    if set(r['exchange'] for r in universe.values())!=set(catalog.exchanges):
        raise ValueError('directory missing exchange')
    return universe,empty


def futures(api, day, universe):
    frames={e:[] for e in SUFFIX}
    for code,identity in sorted(universe.items()):
        q=api.eod(code,FIELDS,day)
        frames[identity['exchange']].append(dict(date=day.replace('-',''),symbol=identity['contract'],
            variety=identity['product'],source_provider='wind',source_endpoint='WindPy.wsd',source_code=code,
            **{f:q[f] for f in ('open','high','low','close','settle','pre_settle','volume')},
            open_interest=q['oi'],turnover=q['amt']/10000 if q['amt'] is not None else None,
            turnover_unit='CNY_10000',source_turnover=q['amt'],source_turnover_unit='CNY'))
    result=run_pipeline(day,provider='wind',wind_prefetched={e:pd.DataFrame(r) for e,r in frames.items()},
                        include_options=False,include_official_auxiliary=False,publish=False)
    if not result.verified or len(result.futures_records)!=len(universe):
        raise ValueError(f'futures validation failed: {result.validation_errors}')
    return result


def options(api, day, result, *, catalog=None):
    catalog=catalog or load_catalog()
    records,plans=[],[]
    for product in catalog.options:
        candidates=sorted([r for r in result.futures_records if r['exchange']==product.exchange and r['product']==product.product],
                          key=lambda r:r.get('open_interest') or 0,reverse=True)
        selected=[]
        for future in candidates:
            code=future['contract']+'.'+SUFFIX[product.exchange]
            raw=api.call('wset','optionchain',f'date={day};us_code={code};option_var=all;call_put=all')
            chain=[]
            if raw['data'] and (len(raw['data'])!=len(raw['fields']) or len({len(c) for c in raw['data']})>1):
                raise ValueError('option directory shape mismatch')
            for values in zip(*raw['data']):
                row=dict(zip(raw['fields'],values))
                if not str(row['first_tradedate'])[:10]<=day<str(row['last_tradedate'])[:10]:
                    continue
                if str(row['us_code']).upper()!=code.upper():
                    raise ValueError('option underlying mismatch')
                chain.append(dict(contract=row['option_code'].split('.')[0].upper(),source_code=row['option_code'].upper(),
                    underlying_contract=future['contract'],expiry_date=str(row['last_tradedate'])[:10],
                    option_type={'认购':'C','认沽':'P'}[row['call_put']],strike=float(row['strike_price']),
                    exercise_style={'欧式':'european','美式':'american'}.get(row['exe_type'],'unknown')))
            if chain:
                selected,plan=select_core_contracts(chain,{future['contract']:future},[],day)
                plans.append(dict(plan,product_key=f'{product.exchange}:{product.product}',
                                  directory_scope='selected_active_underlying_only',directory_source='WindPy.wset.optionchain'))
                break
        if not selected:
            for future in candidates:
                code=future['contract']+'.'+SUFFIX[product.exchange]
                api.discard('wset','optionchain',f'date={day};us_code={code};option_var=all;call_put=all')
            raise ValueError(f'missing core options {product.exchange}:{product.product}')
        for identity in selected:
            q=api.eod(identity['source_code'],OPTION_FIELDS,day)
            greeks=dict(iv_percent=q['us_impliedvol']*100 if q['us_impliedvol'] is not None else None,
                        delta=q['delta'],gamma=q['gamma'])
            records.append(dict(identity,trade_date=day,exchange=product.exchange,product=product.product,
                collection_scope='core_v1',analytics_scope='sampled_strikes',source_provider='wind',
                source_endpoint='WindPy.wsd',source_trade_date=day,source_date_match=True,
                source_date_basis='WindPy.wsd.Times',source_date_status='source_date_verified',
                close=q['close'],settle=q['settle'],pre_settle=q['pre_settle'],volume=q['volume'],
                open_interest=q['oi'],underlying_settle=future['settle'],**greeks,
                greeks=dict(quality='vendor_reported',selected_source='vendor',selected=greeks,vendor=greeks,model=None,
                            units_as_reported=False,units='IV_percent_delta_gamma_Wind_native',dealer_position_direction_known=False)))
    snapshot=dict(schema_version=1,trade_date=day,generated_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
        source_provider='wind',universe_source='wind_optionchain_active_underlying',collection_scope='core_v1',
        analytics_scope='sampled_strikes',collection_mode='end_of_day_core',intraday=False,collection_plans=plans,
        directory_contract_count=sum(p['directory_contract_count'] for p in plans),universe_contract_count=len(records),
        quote_contract_count=len(records),quote_coverage_complete=True,records=records,
        coverage=dict(expected_product_count=len(catalog.options),successful_product_count=len(plans),
                      attempted_product_count=len(plans),failed_product_count=0,skipped_product_count=0,
                      product_coverage=1.,scope_complete=True,publish_eligible=True,
                      successful_products=sorted(p['product_key'] for p in plans),failed_products=[],skipped_products=[]))
    validate_option_snapshot(snapshot)
    return snapshot


def night(api, day, universe, prior, cache):
    if night_session_dates(date.fromisoformat(day)) is None:
        snapshot=dict(schema_version=2,trading_date=day,records=[],calendar_version=CALENDAR_VERSION,
                      collection_state='no_scheduled_night_session',source=dict(provider='wind',endpoint='calendar'),
                      generated_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),frequency='night_session_snapshot')
        return snapshot,dict(snapshot,data_fresh=False,validation_passed=True,published=True,coverage_complete=True)
    _,start,end=_night_window(day)
    fields='open,high,low,close,volume'
    for code in sorted(universe):
        raw=api.call('wsi',code,fields,start.strftime('%Y-%m-%d %H:%M:%S'),end.strftime('%Y-%m-%d %H:%M:%S'),'BarSize=5;Fill=Blank')
        bars=historical_rows(raw,fields)
        stamps=[b['timestamp'] for b in bars]
        if len(stamps)!=len(set(stamps)):
            raise ValueError('duplicate night bar')
        for bar in bars:
            stamp=datetime.fromisoformat(bar['timestamp']).replace(tzinfo=ZoneInfo('Asia/Shanghai'))
            if not start<stamp<=end:
                raise ValueError('night bar outside requested session')
            if bar['volume'] is None or bar['volume']<0 or ((bar['volume'] or 0)>0 and (bar['close'] or 0)<=0):
                raise ValueError('invalid night bar activity/price')
        write_json_if_changed(Path(cache)/f'night/{day}/{code}.json',bars)
    return build_night(day,{c:dict(variety=r['product']) for c,r in universe.items()},prior,Path(cache))


def publish_night(root, snapshot, status):
    from .storage import publish_night_session_derivatives
    root=Path(root)
    newer=read_json(root/'night_session/latest.json',default={}).get('trading_date','')>snapshot['trading_date']
    payloads=[(snapshot['trading_date'],snapshot)]
    if not newer:
        payloads += [('latest',snapshot),('attempt_latest',snapshot),('last_run_status',status)]
    for name,payload in payloads:
        write_json_if_changed(root/f'night_session/{name}.json',payload)
    if snapshot.get('records'):
        replace_night_history(root/'night_session/history.parquet',snapshot)
        publish_night_session_derivatives(root,snapshot=snapshot,status=status)


@contextmanager
def preserve_newer_latest(root, day):
    """Repair historical holes without rolling the current EOD/option heads back."""
    root=Path(root)
    paths=[]
    if read_json(root/'latest.json',default={}).get('trade_date','')>day:
        paths += ['latest.json','last_run_status.json','radar_latest.json','market_state_latest.json','contract_meta.json']
    if read_json(root/'options/latest.json',default={}).get('trade_date','')>day:
        paths += ['options/'+p for p in ('latest.json','last_run_status.json','quality_latest.json','latest_shards',
                  'surface_latest.json','surface_attempt_latest.json.gz','surface_last_run_status.json','surface_shadow_state.json')]
    with tempfile.TemporaryDirectory() as temporary:
        saved=Path(temporary)
        for relative in paths:
            source=root/relative
            if source.is_dir():
                shutil.copytree(source,saved/relative)
            elif source.exists():
                (saved/relative).parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(source,saved/relative)
        try:
            yield
        finally:
            for relative in paths:
                source=saved/relative
                if (root/relative).is_dir():
                    shutil.rmtree(root/relative)
                elif (root/relative).exists():
                    (root/relative).unlink()
                if source.is_dir():
                    shutil.copytree(source,root/relative)
                elif source.exists():
                    (root/relative).parent.mkdir(parents=True,exist_ok=True)
                    shutil.copy2(source,root/relative)


def collect_day(api, day, root, cache, *, night_only=False, catalog=None, allowed_empty=('CZCE:LR',)):
    root=Path(root)
    universe,empty=directory(api,day,catalog=catalog,allowed_empty=allowed_empty)
    session=night_session_dates(date.fromisoformat(day))
    prior=read_json(root/'snapshots'/f'{session[0].isoformat()}.json') if session else None
    if session and not prior:
        # Bootstrap only the previous EOD reference in cache; never relabel it as today's EOD.
        prior_day=session[0].isoformat()
        previous,_=directory(api,prior_day,catalog=catalog,allowed_empty=allowed_empty)
        ref=futures(api,prior_day,previous)
        prior=dict(trade_date=prior_day,futures_contracts=ref.futures_records)
    result=None if night_only else futures(api,day,universe)
    option_snapshot=None if night_only else options(api,day,result,catalog=catalog)
    night_snapshot,night_status=night(api,day,universe,prior,cache)
    with preserve_newer_latest(root,day):
        _publish_full(root,day,result,option_snapshot)
    publish_night(root,night_snapshot,night_status)
    if result:
        write_json_if_changed(root/f'wind_daily/completed/{day}.json',dict(schema_version=1,trade_date=day,
            provider='wind',layers=['futures','options','night'],calendar_version=CALENDAR_VERSION,
            futures_contract_count=len(universe),option_product_count=len(option_snapshot['collection_plans'])))
    return dict(date=day,mode='night' if night_only else 'full',directory_count=len(universe),declared_empty_products=empty)


def _publish_full(root,day,result,option_snapshot):
    if result:
        old_meta=read_json(root/'contract_meta.json')
        publish_status(result,root)
        publish_verified(result,root)
        if old_meta is not None:
            write_json_if_changed(root/'contract_meta.json',old_meta)
        publish_option_eod(option_snapshot,root,surface_shadow_days=1)
        write_json_if_changed(root/'options/last_run_status.json',dict(schema_version=1,trade_date=day,
            generated_at=option_snapshot['generated_at'],source_provider='wind',collection_scope='core_v1',
            published=True,data_fresh=True,coverage=option_snapshot['coverage'],
            quote_contract_count=len(option_snapshot['records']),global_error=None))
