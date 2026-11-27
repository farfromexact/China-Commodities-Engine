"""Dated Wind collection; no login, scheduling, Git, or shared-session control."""
from datetime import date, datetime
from contextlib import contextmanager
import hashlib
import math
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
from .storage import read_json, write_json_if_changed, write_json_gzip_if_changed, publish_status, publish_verified
from .trading_calendar import night_session_dates, CALENDAR_VERSION, previous_trading_day
from .night_session import _night_window
from .wind_daily import eod_session_complete, night_complete, options_observed, ZONE
from .wind_availability import FUTURES_FIELDS, OPTION_FIELDS as CORE_OPTION_FIELDS, WindUnavailable, field_coverage, numeric_quote
# These tested conversion/publication helpers never load an SDK on import.
from scripts.backfill_wind_derivatives import clean, historical_rows, OPTION_FIELDS
from scripts.publish_wind_backfill import build_night, replace_night_history

SUFFIX = dict(SHFE='SHF', INE='INE', DCE='DCE', CZCE='CZC', GFEX='GFE', CFFEX='CFE')
FIELDS = 'open,high,low,close,settle,pre_settle,volume,oi,amt'
WIND_NO_DATA_AVAILABLE = -40520007


class CachedWind:
    def __init__(self, client, root, guard, interval=1., *, batch_quotes=False):
        self.client, self.root, self.guard, self.interval = client, Path(root), guard, interval
        self.batch_quotes = batch_quotes
        self.session_error = None
        self.last_request = 0.
        self.metrics = dict(cache_hits=0,sdk_requests=0,sdk_seconds=0.,sdk_exceptions=0,vendor_errors=0)

    def path(self, method, args):
        key = hashlib.sha256(json.dumps([method,args],ensure_ascii=False).encode()).hexdigest()
        return self.root/(key+'.json')

    def discard(self, method, *args):
        self.path(method,args).unlink(missing_ok=True)

    def call(self, method, *args):
        self.guard()
        path=self.path(method,args)
        if path.exists():
            self.metrics['cache_hits']+=1
            return read_json(path)
        if self.session_error:
            raise WindUnavailable(self.session_error)
        time.sleep(max(0.,self.interval-(time.monotonic()-self.last_request)))
        self.guard()
        started=time.monotonic()
        self.metrics['sdk_requests']+=1
        try:
            r=getattr(self.client,method)(*args)
        except Exception:
            self.metrics['sdk_exceptions']+=1
            raise
        finally:
            self.metrics['sdk_seconds']+=time.monotonic()-started
        self.last_request=time.monotonic()
        raw=clean(dict(error=r.ErrorCode,codes=list(r.Codes),fields=list(r.Fields),times=list(r.Times),data=r.Data))
        no_history = raw['error']==WIND_NO_DATA_AVAILABLE or (
            method=='wst' and raw['error']==-40520010 and
            raw['data']==[['WSTService: Server Not Found Data.']])
        if method in {'wsi','wst'} and no_history:
            # A known contract may have no observations inside the historical window.
            # Keep the vendor code in the raw cache for audit and never fabricate bars.
            fields=[field.strip().lower() for field in args[1].split(',')]
            raw=dict(error=0,vendor_error_code=raw['error'],no_data_available=True,
                     codes=[args[0]],fields=fields,times=[],data=[[] for _ in fields])
        if raw['error']:
            self.metrics['vendor_errors']+=1
            if raw['error'] in (-40520000,-40521000,-40521001):
                self.session_error=f'Wind session/transport error {raw["error"]}'
            if no_history:
                raise WindUnavailable(f'Wind {method} has no observations for this request')
            raise WindUnavailable(f'Wind {method} error {raw["error"]}')
        if method in {'wsd','wsi','wst'} and [str(c).upper() for c in raw['codes']] != [c.strip().upper() for c in args[0].split(',')]:
            raise ValueError('Wind contract identity mismatch')
        write_json_if_changed(path,raw)
        return raw

    def eod(self, code, fields, day):
        args=(code,fields,day,day,'Days=Trading;Fill=Blank')
        try:
            rows=historical_rows(self.call('wsd',*args),fields)
            if not rows:
                raise WindUnavailable(f'no dated EOD quote: {code}')
            if len(rows)!=1 or rows[0]['timestamp'][:10]!=day:
                raise ValueError('EOD date/shape mismatch')
            return rows[0]
        except (ValueError,KeyError):
            self.discard('wsd',*args)
            raise


def directory(api, day, *, catalog=None, allowed_empty=('CZCE:LR',), missing_products=None):
    catalog=catalog or load_catalog()
    universe={}
    empty=[]
    for exchange,products in catalog.exchanges.items():
        for product in products:
            key=f'{exchange}:{product}'
            params=f'startdate={day};enddate={day};wind_code={product}.{SUFFIX[exchange]}'
            try:
                raw=api.call('wset','futurecc',params)
            except WindUnavailable:
                if missing_products is None:
                    raise
                missing_products.append(key)
                continue
            try:
                fields=[f.lower() for f in raw['fields']]
                if raw['data'] and len(raw['data'])!=len(fields):
                    raise ValueError('directory shape mismatch')
                if raw['data'] and len({len(c) for c in raw['data']})>1:
                    raise ValueError('directory column lengths differ')
                rows=[dict(zip(fields,values)) for values in zip(*raw['data'])]
                if not rows:
                    if key not in allowed_empty:
                        if missing_products is not None:
                            missing_products.append(key)
                            continue
                        raise WindUnavailable(f'unexpected empty directory {key}')
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
    if not universe or (missing_products is None and set(r['exchange'] for r in universe.values())!=set(catalog.exchanges)):
        raise WindUnavailable('directory missing exchange')
    return universe,empty


def batched_eod(api, codes, fields, day, batch_size=200, *, undated=None):
    """One field, multiple codes per WSD; no fallback that silently multiplies requests."""
    result = {code:{} for code in codes}
    for offset in range(0, len(codes), batch_size):
        batch = codes[offset:offset+batch_size]
        dated=False
        for field in fields.split(','):
            args = (','.join(batch), field, day, day, 'Days=Trading;Fill=Blank')
            try:
                raw = api.call('wsd', *args)
                if not raw['times'] and not any(raw['data']):
                    raise WindUnavailable('no dated WSD response')
                if (raw['codes'] != batch or [f.lower() for f in raw['fields']] != [field]
                        or len(raw['times']) != 1 or raw['times'][0][:10] != day
                        or len(raw['data']) != 1 or len(raw['data'][0]) != len(batch)):
                    raise ValueError('batched EOD date/shape/identity mismatch')
                for code, value in zip(batch, raw['data'][0]):
                    if value is not None and (isinstance(value, bool) or not isinstance(value,(int,float)) or not math.isfinite(value)):
                        value=None
                    result[code][field] = value
                dated=True
            except WindUnavailable:
                for code in batch:
                    result[code][field]=None
            except (KeyError, ValueError):
                api.discard('wsd', *args)
                raise
        if not dated:
            if undated is None:
                raise WindUnavailable(f'no dated EOD responses for {batch[0]} through {batch[-1]}')
            undated.update(batch)
    return result


def daily_quotes(api, codes, fields, day):
    """Resume existing single-contract responses, then batch the unrequested contracts."""
    quotes, missing, undated = {}, [], set()
    for code in sorted(codes):
        args = (code,fields,day,day,'Days=Trading;Fill=Blank')
        if not api.batch_quotes or api.path('wsd',args).is_file():
            try:
                quotes[code] = api.eod(code,fields,day)
            except WindUnavailable:
                continue
        else:
            missing.append(code)
    if missing:
        quotes.update(batched_eod(api,missing,fields,day,undated=undated))
        for code in undated:
            del quotes[code]
    return quotes


def futures(api, day, universe, *, batched=False, data_root=None):
    frames={e:[] for e in SUFFIX}
    quotes = batched_eod(api, sorted(universe), FIELDS, day) if batched else daily_quotes(api,universe,FIELDS,day)
    for code in sorted(quotes):
        identity=universe[code]
        q=numeric_quote(quotes[code],FIELDS.split(','),('close','settle','pre_settle'))
        frames[identity['exchange']].append(dict(date=day.replace('-',''),symbol=identity['contract'],
            variety=identity['product'],source_provider='wind',source_endpoint='WindPy.wsd',source_code=code,
            **{f:q[f] for f in ('open','high','low','close','settle','pre_settle','volume')},
            open_interest=q['oi'],turnover=q['amt']/10000 if q['amt'] is not None else None,
            turnover_unit='CNY_10000',source_turnover=q['amt'],source_turnover_unit='CNY'))
    if not quotes:
        raise WindUnavailable('no dated commodity futures quotes')
    result=run_pipeline(day,provider='wind',wind_prefetched={e:pd.DataFrame(r) for e,r in frames.items()},
                        include_options=False,include_official_auxiliary=False,publish=False)
    expected_codes=set(quotes)
    if {r.get('source_code') for r in result.futures_records}!=expected_codes or len(result.futures_records)!=len(quotes):
        raise ValueError(f'futures validation failed: {result.validation_errors}')
    coverage=field_coverage((result.futures_records,FUTURES_FIELDS))
    coverage['directory_contract_count']=len(universe)
    coverage['undated_contracts']=sorted(universe.keys()-quotes.keys())
    if coverage['undated_contracts']:
        coverage['status']='partial'
    result.quality_metrics['wind_eod_fields']=coverage
    if not result.verified:
        missing_coverage=bool(result.validation_errors) and all(
            e.startswith(('no liquid concrete contracts for ','no normalized futures contracts for '))
            or e.endswith(' futures not fresh: state=empty, records=0') for e in result.validation_errors)
        if not missing_coverage:
            raise ValueError(f'futures validation failed: {result.validation_errors}')
        if data_root is None:
            raise WindUnavailable(f'incomplete futures coverage for {day}')
        coverage['status']='partial'
        write_json_gzip_if_changed(Path(data_root)/'wind_daily/available_futures'/f'{day}.json.gz',
            dict(trade_date=day,source_provider='wind',source_endpoint='WindPy.wsd',field_availability=coverage,
                 limitations=result.validation_errors,futures_contracts=result.futures_records))
    return result


def option_selection(api, day, futures_records, *, catalog=None, reference_field="settle", allow_missing=False, previous=()):
    catalog=catalog or load_catalog()
    selections,plans=[],[]
    for product in catalog.options:
        candidates=sorted([r for r in futures_records if r['exchange']==product.exchange and r['product']==product.product],
                          key=lambda r:r.get('open_interest') or 0,reverse=True)
        selected=[]
        for future in candidates:
            if not future.get(reference_field):
                continue
            code=future['contract']+'.'+SUFFIX[product.exchange]
            try:
                raw=api.call('wset','optionchain',f'date={day};us_code={code};option_var=all;call_put=all')
            except WindUnavailable:
                if not allow_missing:
                    raise
                continue
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
                reference=dict(future,settle=future[reference_field])
                selected,plan=select_core_contracts(chain,{future['contract']:reference},previous,day)
                plans.append(dict(plan,selection_price_field=reference_field,
                                  selection_price_timestamp=future.get('source_timestamp'),
                                  selection_open_interest_date=future.get('selection_open_interest_date',future.get('source_trade_date')),
                                  product_key=f'{product.exchange}:{product.product}',
                                  directory_scope='selected_active_underlying_only',directory_source='WindPy.wset.optionchain'))
                break
        if not selected and allow_missing:
            plans.append(dict(product_key=f'{product.exchange}:{product.product}',selected_contract_count=0,
                              state='missing_current_underlying_or_chain'))
            continue
        if not selected:
            for future in candidates:
                code=future['contract']+'.'+SUFFIX[product.exchange]
                api.discard('wset','optionchain',f'date={day};us_code={code};option_var=all;call_put=all')
            raise ValueError(f'missing core options {product.exchange}:{product.product}')
        selections.append((product,selected,future))
    return selections,plans


def options(api, day, futures_records, *, catalog=None, previous=()):
    catalog=catalog or load_catalog()
    records=[]
    selections,plans=option_selection(api,day,futures_records,catalog=catalog,previous=previous,allow_missing=True)
    if not selections:
        raise WindUnavailable('no dated commodity option sample available')
    codes=[row['source_code'] for _,selected,_ in selections for row in selected]
    quotes=daily_quotes(api,codes,OPTION_FIELDS,day)
    for product,selected,future in selections:
        for identity in selected:
            if identity['source_code'] not in quotes:
                continue
            q=numeric_quote(quotes[identity['source_code']],('close','settle','pre_settle','volume','oi','us_impliedvol'))
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
    if not records:
        raise WindUnavailable('no dated commodity option quotes')
    observed_products={r['exchange']+':'+r['product'] for r in records}
    for selection in plans:
        if selection['product_key'] not in observed_products:
            selection.setdefault('state','no_dated_option_quotes')
    missing_products=sorted(p['product_key'] for p in plans if p.get('state'))
    snapshot=dict(schema_version=1,trade_date=day,generated_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
        source_provider='wind',universe_source='wind_optionchain_active_underlying',collection_scope='core_v1',
        analytics_scope='sampled_strikes',collection_mode='end_of_day_core',intraday=False,collection_plans=plans,
        directory_contract_count=sum(p.get('directory_contract_count',0) for p in plans),universe_contract_count=len(codes),
        quote_contract_count=len(records),quote_coverage_complete=len(records)==len(codes),records=records,
        coverage=dict(expected_product_count=len(catalog.options),successful_product_count=len(observed_products),
                      attempted_product_count=len(plans),failed_product_count=len(missing_products),skipped_product_count=0,
                      product_coverage=len(observed_products)/len(catalog.options),scope_complete=not missing_products,publish_eligible=True,
                      successful_products=sorted(p['product_key'] for p in plans if not p.get('state')),
                      failed_products=missing_products,skipped_products=[],undated_contracts=sorted(set(codes)-quotes.keys()),
                      field_availability=field_coverage((records,CORE_OPTION_FIELDS))))
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
    unavailable={}
    for code in sorted(universe):
        try:
            raw=api.call('wsi',code,fields,start.strftime('%Y-%m-%d %H:%M:%S'),end.strftime('%Y-%m-%d %H:%M:%S'),'BarSize=5;Fill=Blank')
            bars=historical_rows(raw,fields)
        except WindUnavailable as exc:
            unavailable[code]=str(exc)
            bars=[]
        stamps=[b['timestamp'] for b in bars]
        if len(stamps)!=len(set(stamps)):
            raise ValueError('duplicate night bar')
        for bar in bars:
            stamp=datetime.fromisoformat(bar['timestamp']).replace(tzinfo=ZoneInfo('Asia/Shanghai'))
            if not start<stamp<=end:
                raise ValueError('night bar outside requested session')
            numeric_quote(bar,fields.split(','),('open','high','low','close'))
        write_json_if_changed(Path(cache)/f'night/{day}/{code}.json',bars)
    return build_night(day,{c:dict(variety=r['product']) for c,r in universe.items()},prior,Path(cache),
                       allow_partial=True,unavailable=unavailable)


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


def reusable_published_night(root, day, universe, missing_products=()):
    """A verified published session is reusable without this machine's raw cache."""
    if not night_complete(root, day, allow_partial=True):
        return False
    payload = read_json(Path(root)/f'night_session/{day}.json')
    if night_session_dates(date.fromisoformat(day)) is None:
        return True
    _, start, end = _night_window(day)
    expected = {(row['exchange'], row['contract']) for row in universe.values()}
    actual = {(row.get('exchange'), row.get('contract')) for row in payload['records']}
    extra={(r['exchange'],r['contract']) for r in payload['records'] if r['exchange']+':'+r['product'] in missing_products}
    return (actual - extra == expected and payload.get('session_start') == start.isoformat()
            and payload.get('session_end') == end.isoformat())


def collect_day(api, day, root, cache, *, night_only=False, catalog=None, allowed_empty=('CZCE:LR',), provisional=False, now=None):
    started=(now or datetime.now(ZONE)).astimezone(ZONE)
    if not night_only and not provisional and not eod_session_complete(day,started):
        raise ValueError('unfinished day session: use provisional collection; EOD publication is forbidden')
    if night_only and night_session_dates(date.fromisoformat(day)) and started<_night_window(day)[2]:
        raise ValueError('night session has not completed')
    root=Path(root)
    missing_products=[]
    universe,empty=directory(api,day,catalog=catalog,allowed_empty=allowed_empty,missing_products=missing_products)
    reuse_night = reusable_published_night(root, day, universe,missing_products)
    session=night_session_dates(date.fromisoformat(day))
    prior=read_json(root/'snapshots'/f'{session[0].isoformat()}.json') if session else None
    if session and not prior and not reuse_night:
        # Bootstrap only the previous EOD reference in cache; never relabel it as today's EOD.
        prior_day=session[0].isoformat()
        try:
            previous,_=directory(api,prior_day,catalog=catalog,allowed_empty=allowed_empty)
            ref=futures(api,prior_day,previous)
            prior=dict(trade_date=prior_day,futures_contracts=ref.futures_records)
        except WindUnavailable:
            prior=dict(trade_date=prior_day,futures_contracts=[])
    from .quality import validate_snapshot
    published=read_json(root/f'snapshots/{day}.json',default={}) if not night_only and not provisional else {}
    rows=published.get('futures_contracts',[])
    reuse_futures=(published.get('trade_date')==day and not validate_snapshot(published)
                   and published.get('source',{}).get('provider')=='wind' and len(rows)==len(universe)
                   and {r.get('source_code') for r in rows}==set(universe)
                   and all(r.get('source_provider')=='wind' and r.get('source_trade_date')==day and r.get('source_date_match') is True for r in rows))
    unavailable={}
    result=None
    if not night_only and not reuse_futures:
        try:
            result=futures(api,day,universe,data_root=None if provisional else root)
        except WindUnavailable as exc:
            rows=[]
            unavailable['futures']=str(exc)
    if result:
        rows=result.futures_records
        if missing_products:
            result.quality_metrics['wind_eod_fields'].update(status='partial',unavailable_product_directories=sorted(missing_products))
    previous_options = read_json(root/'options/snapshots'/f'{previous_trading_day(date.fromisoformat(day))}.json.gz',default={}) if not night_only else {}
    previous_rows = []
    if previous_options.get('trade_date') == str(previous_trading_day(date.fromisoformat(day))):
        try:
            validate_option_snapshot(previous_options)
            previous_rows = previous_options.get('records', [])
        except ValueError:
            pass  # invalid earlier input cannot guide selection
    if result and not result.verified:
        unavailable['futures']=f'data/wind_daily/available_futures/{day}.json.gz'
    if missing_products:
        unavailable['product_directories']=sorted(missing_products)
    option_snapshot=None
    saved_options=read_json(root/f'options/snapshots/{day}.json.gz',default={}) if reuse_futures else {}
    reuse_options=reuse_futures and options_observed(saved_options,day,catalog or load_catalog())
    if not night_only and not reuse_options and rows:
        try:
            option_snapshot=options(api,day,rows,catalog=catalog,previous=previous_rows)
        except WindUnavailable as exc:
            unavailable['options']=str(exc)
    elif not night_only and not rows:
        unavailable['options']='no dated futures available for sample selection'
    if provisional:
        # Even if the clock crosses close during collection, this candidate can never publish.
        return dict(date=day,mode='provisional',as_of=started.isoformat(),published=False,
                    final=False,directory_count=len(universe),declared_empty_products=empty)
    new_night=None
    if not reuse_night:
        try:
            new_night=night(api,day,universe,prior,cache)
        except WindUnavailable as exc:
            if night_only:
                raise
            unavailable['night']=str(exc)
    with preserve_newer_latest(root,day):
        _publish_full(root,day,result,option_snapshot)
    if new_night:
        if missing_products:
            for item in new_night:
                item['coverage']['unavailable_product_directories']=sorted(missing_products)
            new_night[1]['coverage_complete']=False
        publish_night(root,*new_night)
    if not night_only and not unavailable:
        sampled=option_snapshot or saved_options
        write_json_if_changed(root/f'wind_daily/completed/{day}.json',dict(schema_version=1,trade_date=day,
            provider='wind',layers=['futures','options','night'],calendar_version=CALENDAR_VERSION,
            futures_contract_count=len(rows),option_product_count=sampled['coverage']['successful_product_count']))
    return dict(date=day,mode='night' if night_only else 'full',directory_count=len(universe),
                declared_empty_products=empty,unavailable_product_directories=missing_products,
                futures_reused=reuse_futures,options_reused=reuse_options,night_reused=reuse_night,unavailable=unavailable)


def _publish_full(root,day,result,option_snapshot):
    if result and result.verified:
        old_meta=read_json(root/'contract_meta.json')
        publish_status(result,root)
        publish_verified(result,root)
        if old_meta is not None:
            write_json_if_changed(root/'contract_meta.json',old_meta)
    if option_snapshot:
        publish_option_eod(option_snapshot,root,surface_shadow_days=1)
        write_json_if_changed(root/'options/last_run_status.json',dict(schema_version=1,trade_date=day,
            generated_at=option_snapshot['generated_at'],source_provider='wind',collection_scope='core_v1',
            published=True,data_fresh=True,coverage=option_snapshot['coverage'],
            quote_contract_count=len(option_snapshot['records']),global_error=None))
