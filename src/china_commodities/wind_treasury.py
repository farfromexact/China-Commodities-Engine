"""Government-bond futures. Prices are per CNY100 face, never yields or DV01."""
from datetime import date, datetime, time, timedelta
from pathlib import Path
import re

from .catalog import ProductCatalog
from .index_analytics import finite, positive
from .storage import read_json, write_json_if_changed
from .trading_calendar import is_trading_day

PRODUCTS = {'TS':2,'TF':5,'T':10,'TL':30}
FACE_VALUE = {'TS':2000000,'TF':1000000,'T':1000000,'TL':1000000}
CATALOG = ProductCatalog({}, {}, {'CFFEX':tuple(PRODUCTS)}, ())
VERSION = 1


def eod_ready(day, now=None):
    from .wind_daily import ZONE
    now=(now or datetime.now(ZONE)).astimezone(ZONE)
    target=date.fromisoformat(day)
    return is_trading_day(target) and (target<now.date() or (target==now.date() and now.time()>=time(15,15)))


def window(now):
    from .wind_daily import ZONE
    now=now.astimezone(ZONE)
    if not is_trading_day(now.date()) or not time(9)<=now.time()<time(15,15):
        return None
    cutoff=now-timedelta(minutes=5)
    cutoff=cutoff.replace(minute=cutoff.minute//5*5,second=0,microsecond=0)
    return dict(trading_date=str(now.date()),session='day',session_start=now.replace(hour=9,minute=30,second=0,microsecond=0).isoformat(),
                cutoff=cutoff.isoformat(),started_at=now.isoformat())


def validate(payload):
    errors=[]
    day=payload.get('trade_date')
    rows=payload.get('contracts',[])
    expected=payload.get('directory_codes',[])
    if payload.get('schema_version')!=VERSION or payload.get('source_provider')!='wind' or not day:
        errors.append('treasury schema/source/date missing')
    codes=[r.get('source_code') for r in rows]
    if (not expected or len(expected)!=len(set(expected)) or len(codes)!=len(set(codes))
            or set(codes)!=set(expected) or {r.get('product') for r in rows}!=set(PRODUCTS)):
        errors.append('treasury directory coverage mismatch')
    for r in rows:
        code=r.get('source_code','')
        if (not re.fullmatch(str(r.get('product'))+r'\d{4}\.CFE',code)
                or r.get('exchange')!='CFFEX' or r.get('source_provider')!='wind'
                or r.get('source_trade_date')!=day or not r.get('listed_date','z')<=str(day)<=r.get('expiry_date','')
                or r.get('price_unit')!='CNY_per_100_CNY_face_value'):
            errors.append('treasury identity/date/unit mismatch: '+code)
        required=('open','high','low','close','settle','pre_settle','volume','open_interest','turnover')
        if any(not finite(r.get(f)) or r[f]<0 for f in required) or not positive(r.get('close')) or not positive(r.get('settle')):
            errors.append('treasury missing/invalid core fields: '+code)
        elif not r['low']<=min(r['open'],r['close'])<=max(r['open'],r['close'])<=r['high']:
            errors.append('treasury OHLC mismatch: '+code)
        if r.get('face_value_cny')!=FACE_VALUE.get(r.get('product')) or r.get('point_value_cny')!=FACE_VALUE.get(r.get('product'),0)/100:
            errors.append('treasury contract unit mismatch: '+code)
    return errors


def complete(root, day):
    p=read_json(Path(root)/f'treasury_futures/snapshots/{day}.json',default={})
    return p.get('trade_date')==day and p.get('validation_passed') is True and not validate(p)


def summary(rows):
    result={}
    for product in PRODUCTS:
        selected=sorted([r for r in rows if r['product']==product],key=lambda r:r['expiry_date'])
        active=[r for r in selected if r['volume']>0 or r['open_interest']>0]
        if not active:
            result[product]=dict(status='no_active_contracts')
            continue
        main=max(active,key=lambda r:(r['volume'],r['open_interest']))
        following=next((r for r in active if r['expiry_date']>main['expiry_date']),None)
        result[product]=dict(status='ok',tenor_years=PRODUCTS[product],main_contract=main,next_contract=following,
            total_volume=sum(r['volume'] for r in selected),total_open_interest=sum(r['open_interest'] for r in selected),
            next_minus_main_points=following['close']-main['close'] if following else None,
            total_oi_change=sum(r['oi_change'] for r in selected) if all(finite(r.get('oi_change')) for r in selected) else None)
    return result


def collect_eod(api, day, root):
    from .wind_daily import ZONE
    from .wind_daily_collect import directory,FIELDS
    from .wind_index import daily_quote
    from .trading_calendar import previous_trading_day
    if not eod_ready(day):
        raise ValueError('treasury day session not completed at 15:15')
    universe,_=directory(api,day,catalog=CATALOG,allowed_empty=())
    prior=read_json(Path(root)/f'treasury_futures/snapshots/{previous_trading_day(date.fromisoformat(day))}.json',default={})
    old={r['source_code']:r for r in prior.get('contracts',[])} if prior and not validate(prior) else {}
    rows=[]
    for code,identity in sorted(universe.items()):
        q=daily_quote(api,code,FIELDS,day)
        product=identity['product']
        previous=old.get(code,{})
        rows.append(dict(source_provider='wind',source_endpoint='WindPy.wsd',source_trade_date=day,
            exchange='CFFEX',source_code=code,contract=identity['contract'],product=product,tenor_years=PRODUCTS[product],
            listed_date=str(identity['contract_issue_date'])[:10],expiry_date=str(identity['last_trade_date'])[:10],
            **{f:q[f] for f in ('open','high','low','close','settle','pre_settle','volume')},
            open_interest=q['oi'],turnover=q['amt'],turnover_unit='CNY',volume_unit='contracts',
            price_unit='CNY_per_100_CNY_face_value',face_value_cny=FACE_VALUE[product],point_value_cny=FACE_VALUE[product]/100,
            close_change_points=q['close']-q['pre_settle'] if finite(q['close']) and finite(q['pre_settle']) else None,
            close_change_basis='previous_settlement',
            oi_change=q['oi']-previous['open_interest'] if finite(q['oi']) and finite(previous.get('open_interest')) else None))
    payload=dict(schema_version=VERSION,trade_date=day,source_provider='wind',frequency='EOD',night_session='not_applicable',
        generated_at=datetime.now(ZONE).isoformat(),directory_codes=sorted(universe),contracts=rows,products=summary(rows),
        limitations=['Prices are per CNY100 face, not yields.','No CTD, conversion-factor basis, implied repo or DV01 hedge estimates.'])
    errors=validate(payload)
    if errors:
        raise ValueError('; '.join(errors))
    payload['validation_passed']=True
    target=Path(root)/'treasury_futures'
    write_json_if_changed(target/'snapshots'/f'{day}.json',payload)
    if read_json(target/'latest.json',default={}).get('trade_date','')<=day:
        write_json_if_changed(target/'latest.json',payload)
    return dict(mode='treasury_eod',date=day,contract_count=len(rows))


def collect_intraday(api, window, root):
    from .wind_daily import ZONE
    from .wind_daily_collect import directory
    from .wind_intraday import quote,stamp,coverage
    if window['session']!='day':
        return dict(mode='treasury_intraday',state='no_night_session')
    if stamp(window['cutoff'])<=stamp(window['session_start']):
        return dict(mode='treasury_intraday',state='no_completed_bar')
    day=window['trading_date']
    universe,_=directory(api,day,catalog=CATALOG,allowed_empty=())
    rows=[]
    for code,identity in sorted(universe.items()):
        effective=dict(window)
        expiry=str(identity['last_trade_date'])[:10]
        # Last trading day has no afternoon session.
        if expiry==day and stamp(effective['cutoff']).time()>time(11,30):
            effective['cutoff']=day+'T11:30:00+08:00'
        q=quote(api,code,effective)
        if q.get('source_timestamp'):
            q['lag_to_cutoff_seconds']=int((stamp(window['cutoff'])-stamp(q['source_timestamp'])).total_seconds())
        rows.append(dict(source_provider='wind',source_endpoint='WindPy.wsi',exchange='CFFEX',
            source_code=code,product=identity['product'],contract=identity['contract'],expiry_date=expiry,
            effective_contract_cutoff=effective['cutoff'],price_unit='CNY_per_100_CNY_face_value',
            settle=None,open_interest=None,**q))
    if not any(r['record_state']=='quote' for r in rows):
        raise ValueError('no treasury minute quotes within cutoff')
    payload=dict(schema_version=VERSION,source_provider='wind',mode='intraday',**window,validation_passed=True,
        generated_at=datetime.now(ZONE).isoformat(),contracts=rows,coverage=coverage(rows),
        unavailable_fields=['settle','open_interest'],volume_unit='contracts_in_selected_5min_bar')
    target=Path(root)/'treasury_futures/intraday'
    if read_json(target/'latest.json',default={}).get('cutoff','')>window['cutoff']:
        raise ValueError('refusing older treasury intraday snapshot')
    write_json_if_changed(target/f'{day}.json',payload)
    write_json_if_changed(target/'latest.json',payload)
    return dict(mode='treasury_intraday',date=day,coverage=payload['coverage'])


def report_view(root, expected_date):
    root=Path(root)/'treasury_futures'
    eod=read_json(root/'latest.json',default={}); intra=read_json(root/'intraday/latest.json',default={})
    current=dict(mode='eod',path='data/treasury_futures/latest.json',trade_date=eod.get('trade_date'))
    if intra.get('validation_passed') and intra.get('cutoff','')>eod.get('trade_date','')+'T15:15:00+08:00':
        current=dict(mode='intraday',path='data/treasury_futures/intraday/latest.json',cutoff=intra['cutoff'],coverage=intra['coverage'])
    return dict(available=bool(eod) or current['mode']=='intraday',eod_available=bool(eod),trade_date=eod.get('trade_date'),expected_date=expected_date,
        expected_date_basis='alignment_with_commodity_EOD_not_wall_clock',
        validation_passed=eod.get('validation_passed',False),generated_at=eod.get('generated_at'),
        date_matches=eod.get('trade_date')==expected_date,products=eod.get('products',{}),
        current_market_snapshot=current,limitations=eod.get('limitations',[]))
