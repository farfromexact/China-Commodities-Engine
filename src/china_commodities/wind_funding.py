"""Small repo-rate panel. Bank weighted rates and exchange closes stay distinct."""
from datetime import date, datetime, time, timedelta
from pathlib import Path
import re

from .index_analytics import finite
from .storage import read_json,write_json_if_changed
from .trading_calendar import is_trading_day,previous_trading_day

VERSION=1
WIND_SHORT_NAMES={
    **{f'DR{d:03d}':f'银存间质押{d}日' for d in (1,7,14)},
    **{f'R{d:03d}':f'银行间质押{d}日' for d in (1,7,14)},
}
# Explicit Wind routes; Windows EOD accepted on 2026-11-14. Each collection still
# checks instrument names and dates, with no public-source fallback.
INSTRUMENTS={}
for prefix,market in [('DR','interbank_depository_secured'),('R','interbank_all_secured')]:
    for tenor in (1,7,14):
        key=f'{prefix}{tenor:03d}'
        INSTRUMENTS[key]=dict(source_code=key+'.IB',market=market,tenor_days=tenor,
            statistic='daily_trade_weighted_repo_rate',name_token=key)
for prefix,suffix,codes,token in [('GC','SH',('204001','204007','204014'),'GC'),
                                 ('SZ_R','SZ',('131810','131801','131802'),'R-')]:
    for tenor,code in zip((1,7,14),codes):
        key=f'{prefix}{tenor:03d}'
        INSTRUMENTS[key]=dict(source_code=code+'.'+suffix,market='exchange_'+suffix,
            tenor_days=tenor,statistic='closing_annualized_repo_quote',name_token=token+f'{tenor:03d}')


def eod_ready(day, now=None):
    from .wind_daily import ZONE
    now=(now or datetime.now(ZONE)).astimezone(ZONE)
    target=date.fromisoformat(day)
    # Conservative collection boundary for the combined panel; not a guarantee of
    # vendor release. Exact target-date observations are still required below.
    return is_trading_day(target) and (target<now.date() or (target==now.date() and now.time()>=time(18)))


def window(now):
    """Only exchange repos have minute quotes here; never label R/DR provisional EOD."""
    from .wind_daily import ZONE
    now=now.astimezone(ZONE)
    if not is_trading_day(now.date()) or not time(9)<=now.time()<time(18):
        return None
    cutoff=now-timedelta(minutes=5)
    cutoff=cutoff.replace(minute=cutoff.minute//5*5,second=0,microsecond=0)
    cutoff=min(cutoff,now.replace(hour=15,minute=30,second=0,microsecond=0))
    return dict(trading_date=str(now.date()),session='day',session_start=now.replace(hour=9,minute=30,second=0,microsecond=0).isoformat(),
        cutoff=cutoff.isoformat(),started_at=now.isoformat())


def trading_window(day, lag=20):
    dates=[date.fromisoformat(day)]
    for _ in range(lag):
        dates.append(previous_trading_day(dates[-1]))
    return [str(d) for d in reversed(dates)]


def metadata(api, spec):
    args=(spec['source_code'],'sec_name','')
    try:
        raw=api.call('wss',*args)
        if (raw['codes']!=[spec['source_code']] or [f.lower() for f in raw['fields']]!=['sec_name']
                or len(raw['data'])!=1 or len(raw['data'][0])!=1 or not isinstance(raw['data'][0][0],str)):
            raise ValueError('repo instrument metadata shape/identity mismatch')
        name=raw['data'][0][0].upper().replace('Ｒ','R').replace('－','-').replace(' ','')
        token=re.escape(spec['name_token'])
        named_code=bool(re.search(r'(?<![A-Z0-9_])'+token+r'(?![A-Z0-9])',name))
        # Exact names returned by official WindPy on 2026-11-14; do not relax the
        # R/DR or fixing distinction via substring matching.
        short_name=WIND_SHORT_NAMES.get(spec['source_code'].split('.')[0])
        exact_short=short_name is not None and name==short_name
        tenor=spec['tenor_days']
        named_tenor=bool(re.search(r'(?<!\d)'+str(tenor)+r'(?:天|日)',name) or (tenor==1 and '隔夜' in name))
        depository=('存款类' in name or '银银' in name)
        if spec['market'].startswith('interbank_'):
            named_type=('回购' in name and '加权' in name and named_tenor
                and depository==(spec['market']=='interbank_depository_secured')
                and ('银行间' in name or depository))
        else:
            named_type=('回购' in name and named_tenor)
        if not (named_code or named_type or exact_short) or '定盘' in name or 'FDR' in name or name.startswith('FR'):
            raise ValueError('repo name does not confirm configured rate type: '+name)
        return raw['data'][0][0]
    except (KeyError,ValueError):
        api.discard('wss',*args)
        raise


def series(api, spec, dates):
    from .wind_daily_collect import historical_rows
    args=(spec['source_code'],'close',dates[0],dates[-1],'Days=Trading;Fill=Blank')
    try:
        raw=api.call('wsd',*args)
        rows=historical_rows(raw,'close')
        seen=set()
        if raw['codes']!=[spec['source_code']]:
            raise ValueError('repo source identity mismatch')
        for row in rows:
            day=row['timestamp'][:10]
            if day not in dates or day in seen or (row['close'] is not None and not finite(row['close'])):
                raise ValueError('repo observation dates/values invalid')
            seen.add(day)
        return {r['timestamp'][:10]:r['close'] for r in rows}
    except (KeyError,ValueError):
        api.discard('wsd',*args)
        raise


def spreads(records):
    lookup={r['key']:r for r in records}
    result=[]
    for first,second in [('R001','DR001'),('R007','DR007'),('R014','DR014'),('DR007','DR001'),('GC001','DR001')]:
        a,b=lookup[first],lookup[second]
        compatible=(a['record_state']=='ok' and b['record_state']=='ok'
                    and a['source_trade_date']==b['source_trade_date'] and a['unit']==b['unit']=='percent_per_annum')
        result.append(dict(key=first+'_minus_'+second,first=first,second=second,
            value_bp=(a['rate_percent']-b['rate_percent'])*100 if compatible else None,
            source_trade_date=a['source_trade_date'] if compatible else None,
            comparison_basis='different_market_close_vs_daily_weighted' if first=='GC001' else 'daily_weighted_rates',
            unit_basis='configured_annual_percent_no_numeric_rescaling',not_arbitrage_signal=True))
    return result


def validate(payload):
    errors=[]
    day=payload.get('trade_date');rows=payload.get('records',[])
    if payload.get('schema_version')!=VERSION or payload.get('source_provider')!='wind' or not day:
        errors.append('funding schema/source/date missing')
    if len(rows)!=len(INSTRUMENTS) or {r.get('key') for r in rows}!=set(INSTRUMENTS):
        errors.append('funding indicator coverage mismatch')
    for r in rows:
        spec=INSTRUMENTS.get(r.get('key'),{})
        if any(r.get(k)!=spec.get(k) for k in ('source_code','market','tenor_days','statistic')) or r.get('unit')!='percent_per_annum':
            errors.append('funding identity/type/unit mismatch')
        if r.get('record_state')=='ok':
            if (r.get('source_trade_date')!=day or not finite(r.get('rate_percent'))
                    or r.get('rate_percent')!=r.get('raw_value') or not r.get('instrument_name')):
                errors.append('funding source date/value mismatch')
        elif r.get('record_state') not in ('missing','query_error') or r.get('rate_percent') is not None or r.get('source_trade_date') is not None:
            errors.append('funding missing indicator contains a fabricated observation')
    if rows and payload.get('coverage_complete')!=all(r.get('record_state')=='ok' for r in rows):
        errors.append('funding coverage flag mismatch')
    return errors


def complete(root, day):
    p=read_json(Path(root)/f'funding/snapshots/{day}.json',default={})
    return p.get('trade_date')==day and p.get('coverage_complete') is True and not validate(p)


def collect_eod(api, day, root):
    from .wind_daily import ZONE
    if not eod_ready(day):
        raise ValueError('funding combined EOD panel is not ready before 18:00')
    dates=trading_window(day)
    records=[]
    fatal=None
    for key,spec in INSTRUMENTS.items():
        row=dict(key=key,**{k:v for k,v in spec.items() if k!='name_token'},source_provider='wind',
            source_endpoint='WindPy.wsd.close',source_trade_date=None,instrument_name=None,
            raw_value=None,rate_percent=None,unit='percent_per_annum',
            unit_basis='published_instrument_definition; Wind_native_close_without_numeric_rescaling',
            unit_verification='definition_based_not_Wind_field_metadata',identity_verified=False,source_date_verified=False,
            record_state='missing',changes_bp={str(lag)+'d':None for lag in (1,3,5,20)})
        try:
            if fatal:
                raise ValueError('not queried after Wind session/transport failure: '+fatal)
            row['instrument_name']=metadata(api,spec)
            row['identity_verified']=True
            values=series(api,spec,dates)
            value=values.get(day)
            if finite(value):
                row.update(record_state='ok',source_trade_date=day,raw_value=value,rate_percent=value,source_date_verified=True)
                for lag in (1,3,5,20):
                    required=dates[-lag-1:]
                    if all(finite(values.get(d)) for d in required):
                        row['changes_bp'][str(lag)+'d']=(value-values[dates[-lag-1]])*100
            else:
                row['error']='target-date rate missing; no older value substituted'
                # Re-query the missing target on the next explicit launch.
                api.discard('wsd',spec['source_code'],'close',dates[0],dates[-1],'Days=Trading;Fill=Blank')
        except (ValueError,KeyError) as exc:
            row.update(record_state='query_error',error=str(exc)[:500])
            if any(str(code) in str(exc) for code in (-40520000,-40521000,-40521001)):
                fatal=str(exc)
        records.append(row)
    payload=dict(schema_version=VERSION,trade_date=day,source_provider='wind',frequency='EOD',
        generated_at=datetime.now(ZONE).isoformat(),records=records,
        coverage_complete=all(r['record_state']=='ok' for r in records),
        ready_time_policy='18:00 Asia/Shanghai conservative combined-panel gate; release still requires exact source date',
        spreads=spreads(records),limitations=['R/DR are weighted repo rates, not FR/FDR fixing rates.',
            'Exchange close and interbank weighted rates are different statistics.',
            'Percent units follow published instrument definitions, not Wind field metadata; no heuristic scaling.',
            'Repo-rate levels/spreads do not measure cash inflow/outflow or PBOC net injections.'])
    errors=validate(payload)
    if errors:
        raise ValueError('; '.join(errors))
    payload['validation_passed']=True
    target=Path(root)/'funding'
    write_json_if_changed(target/'snapshots'/f'{day}.json',payload)
    if read_json(target/'latest.json',default={}).get('trade_date','')<=day:
        write_json_if_changed(target/'latest.json',payload)
    return dict(mode='funding_eod',date=day,coverage_complete=payload['coverage_complete'],
                available=sum(r['record_state']=='ok' for r in records),expected=len(records))


def collect_intraday(api, window, root):
    from .wind_daily import ZONE
    from .wind_intraday import quote,stamp,coverage
    if window['session']!='day':
        return dict(mode='funding_intraday',state='no_night_session')
    if stamp(window['cutoff'])<=stamp(window['session_start']):
        return dict(mode='funding_intraday',state='no_completed_bar')
    records=[]
    for key,spec in INSTRUMENTS.items():
        if not spec['market'].startswith('exchange_'):
            continue
        q=quote(api,spec['source_code'],window)
        records.append(dict(key=key,**{k:v for k,v in spec.items() if k not in ('name_token','statistic')},
            source_provider='wind',source_endpoint='WindPy.wsi',unit='percent_per_annum',
            statistic='selected_5min_bar_last_repo_rate',**q))
    payload=dict(schema_version=VERSION,source_provider='wind',mode='intraday',**window,
        generated_at=datetime.now(ZONE).isoformat(),records=records,coverage=coverage(records),validation_passed=True,
        has_current_quotes=any(r['record_state']=='quote' for r in records),
        interbank_state='not_requested_daily_weighted_rates_not_intraday_quotes',
        volume_unit='Wind_native_unverified_not_CNY_cash_flow',
        limitations=['Exchange repo last rate in selected bar, not daily weighted rate or daily close.',
                      'Interbank R/DR remain separately dated in EOD data.'])
    target=Path(root)/'funding/intraday'
    if read_json(target/'latest.json',default={}).get('cutoff','')>window['cutoff']:
        raise ValueError('refusing older funding intraday snapshot')
    write_json_if_changed(target/f"{window['trading_date']}.json",payload)
    write_json_if_changed(target/'latest.json',payload)
    return dict(mode='funding_intraday',date=window['trading_date'],coverage=payload['coverage'])


def report_view(root, expected_date):
    root=Path(root)/'funding'
    eod=read_json(root/'latest.json',default={}); intra=read_json(root/'intraday/latest.json',default={})
    usable_intraday=intra.get('validation_passed') and intra.get('coverage',{}).get('quoted',0)>0
    observed=any(r.get('record_state')=='ok' for r in eod.get('records',[]))
    return dict(available=observed or bool(usable_intraday),eod_available=bool(eod),trade_date=eod.get('trade_date'),expected_date=expected_date,
        expected_date_basis='alignment_with_commodity_EOD_not_wall_clock',
        validation_passed=eod.get('validation_passed',False),generated_at=eod.get('generated_at'),
        date_matches=eod.get('trade_date')==expected_date,coverage_complete=eod.get('coverage_complete',False),
        records=eod.get('records',[]),spreads=eod.get('spreads',[]),limitations=eod.get('limitations',[]),
        current_exchange_snapshot=(dict(path='data/funding/intraday/latest.json',cutoff=intra.get('cutoff'),coverage=intra.get('coverage'))
            if usable_intraday and intra.get('cutoff','')>eod.get('trade_date','')+'T15:30:00+08:00' else None),
        source_path='data/funding/latest.json')
