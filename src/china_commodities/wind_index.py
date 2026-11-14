"""Wind equity-index derivatives, independent of commodity scope and night data."""
from datetime import datetime
import math
from pathlib import Path
import re

from .catalog import ProductCatalog
from .storage import read_json, write_json_if_changed
from .index_selection import REPORT_SCOPE, FULL_SCOPE, SCOPES, PAIRS_PER_EXPIRY, select as select_for_report

FUTURES = ('IF', 'IH', 'IC', 'IM')
# Index options are on cash indices, not on the corresponding index futures.
OPTIONS = {'IO': '000300.SH', 'HO': '000016.SH', 'MO': '000852.SH'}
CATALOG = ProductCatalog({}, {}, {'CFFEX': FUTURES}, ())
VERSION = 2
OPTION_EOD_FIELDS = ('close','settle','pre_settle','volume','oi')


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def daily_quote(api, code, fields, day):
    from .wind_daily_collect import historical_rows
    args = (code, fields, day, day, 'Days=Trading;Fill=Blank')
    try:
        rows = historical_rows(api.call('wsd', *args), fields)
        if len(rows) != 1 or rows[0]['timestamp'][:10] != day:
            raise ValueError('index derivatives source date mismatch')
        return rows[0]
    except (ValueError, KeyError):
        api.discard('wsd', *args)
        raise


def option_directory(api, day, product):
    index = OPTIONS[product]
    args = ('optionchain', f'date={day};us_code={index};option_var=all;call_put=all')
    try:
        raw = api.call('wset', *args)
        fields = [f.lower() for f in raw['fields']]
        if not raw['data'] or len(raw['data']) != len(fields) or len({len(c) for c in raw['data']}) != 1:
            raise ValueError('index option directory shape mismatch')
        result = []
        seen = set()
        for values in zip(*raw['data']):
            row = dict(zip(fields, values))
            code = str(row['option_code']).upper()
            match = re.fullmatch(product + r'(\d{4})-([CP])-(\d+(?:\.\d+)?)\.CFE', code)
            if (not match or row['us_code'].upper() != index
                    or row['option_var'].upper() != product + '.CFE' or code in seen):
                raise ValueError('index option directory identity mismatch')
            seen.add(code)
            issued, expiry = str(row['first_tradedate'])[:10], str(row['last_tradedate'])[:10]
            if not issued <= day <= expiry:
                raise ValueError('index option not active on target date')
            if match[2] != {'认购': 'C', '认沽': 'P'}[row['call_put']] or float(match[3]) != float(row['strike_price']):
                raise ValueError('index option strike/side mismatch')
            result.append(dict(exchange='CFFEX', product=product, contract=code.split('.')[0],
                source_code=code, underlying_contract=index, underlying_index=index,
                option_type=match[2], strike=float(row['strike_price']), expiry_date=expiry,
                listed_date=issued, exercise_style='european' if row['exe_type']=='欧式' else 'unknown',
                multiplier=row['multiplier'], price_unit='index_points', multiplier_unit='CNY_per_point'))
        if not result:
            raise ValueError('empty index option directory')
        pairs = {}
        for row in result:
            pairs.setdefault((row['expiry_date'],row['strike']),set()).add(row['option_type'])
        if any(sides!={'C','P'} for sides in pairs.values()):
            raise ValueError('index option directory has an unpaired strike')
        return sorted(result, key=lambda r: (r['expiry_date'], r['strike'], r['option_type']))
    except (KeyError, ValueError):
        api.discard('wset', *args)
        raise


def select_options(api, day, product, reference, reference_time=None):
    from .option_core import select_core_contracts
    rows = option_directory(api, day, product)
    index = OPTIONS[product]
    selected, plan = select_core_contracts(rows, {index: dict(settle=reference, source_trade_date=day)}, [], day)
    # The generic selector uses a 'forward' field; here the input is explicitly cash-index price.
    plan['selection_index_price'] = plan.pop('selection_forward')
    plan['selection_index_date'] = plan.pop('selection_forward_date')
    plan.update(product_key='CFFEX:'+product, underlying_index=index,
                selection_price_basis='cash_index_close', selection_price_timestamp=reference_time,
                selection_reason='nearest_unexpired_month_atm_and_nearest_paired_strikes',
                directory_source='WindPy.wset.optionchain', directory_scope='all_listed_index_option_months')
    return selected, plan


def validate(payload):
    """Read legacy core snapshots; enforce declared sample or full scope for v2."""
    errors = []
    day = payload.get('trade_date')
    version = payload.get('schema_version')
    modern = version==VERSION
    full = modern and payload.get('options_scope')==FULL_SCOPE
    if version not in (1,VERSION) or payload.get('source_provider') != 'wind' or not day:
        errors.append('index derivatives schema/source/date missing')
    futures, options = payload.get('futures_contracts', []), payload.get('options_contracts', [])
    if {r.get('product') for r in futures} != set(FUTURES):
        errors.append('index futures product coverage incomplete')
    if {r.get('product') for r in options} != set(OPTIONS):
        errors.append('index options product coverage incomplete')
    codes = [r.get('source_code') for r in futures+options]
    if len(codes) != len(set(codes)):
        errors.append('duplicate index derivative contract')
    for rows, required in [(futures, ('open','high','low','close','settle','pre_settle','volume','open_interest','turnover')),
                           (options, ('close','settle','pre_settle','volume','open_interest'))]:
        for row in rows:
            code = row.get('source_code', '')
            if (row.get('source_trade_date') != day or row.get('source_date_match') is not True
                    or row.get('source_provider') != 'wind' or row.get('exchange') != 'CFFEX'):
                errors.append(f'index derivative date/source mismatch: {code}')
            if any(not number(row.get(f)) or row[f] < 0 for f in required):
                errors.append(f'index derivative missing/invalid core fields: {code}')
            elif (row in futures or not modern) and (row['close'] <= 0 or row['settle'] <= 0):
                errors.append(f'index derivative nonpositive price: {code}')
            if row in futures:
                if not re.fullmatch(r'(IF|IH|IC|IM)\d{4}\.CFE', code) or not code.startswith(row.get('product','?')):
                    errors.append(f'index futures identity mismatch: {code}')
                if all(number(row.get(f)) for f in ('open','high','low','close')) and not row['low'] <= min(row['open'],row['close']) <= max(row['open'],row['close']) <= row['high']:
                    errors.append(f'index futures invalid OHLC: {code}')
            else:
                product = row.get('product')
                if (row.get('underlying_index') != OPTIONS.get(product)
                        or not re.fullmatch(str(product)+r'\d{4}-[CP]-\d+(?:\.\d+)?\.CFE', code)
                        or not number(row.get('underlying_index_close')) or row['underlying_index_close']<=0):
                    errors.append(f'index option identity/reference mismatch: {code}')
                match = re.fullmatch(str(product)+r'(\d{4})-([CP])-(\d+(?:\.\d+)?)\.CFE',code)
                if (modern and (not match or match[2]!=row.get('option_type')
                        or float(match[3])!=row.get('strike')
                        or not row.get('listed_date','z')<=str(day)<=row.get('expiry_date','')
                        or not number(row.get('multiplier')) or row['multiplier']<=0)):
                    errors.append(f'index option metadata mismatch: {code}')
                for field in ('iv_percent','delta','gamma'):
                    if row.get(field) is not None and not number(row[field]):
                        errors.append(f'index option invalid {field}: {code}')
    plans = payload.get('collection_plans', [])
    if {p.get('product_key') for p in plans} != {'CFFEX:'+p for p in OPTIONS} or len(plans)!=len(OPTIONS):
        errors.append('index option selection plans incomplete')
    for plan in plans:
        selected = [r for r in options if 'CFFEX:'+r.get('product','') == plan.get('product_key')]
        if modern:
            expected_options = plan.get('directory_codes',[])
            expected_selected = expected_options if full else plan.get('selected_codes',[])
            if (not expected_options or len(expected_options)!=len(set(expected_options))
                    or plan.get('directory_contract_count')!=len(expected_options)
                    or not expected_selected or len(expected_selected)!=len(set(expected_selected))
                    or not set(expected_selected).issubset(set(expected_options))
                    or set(expected_selected)!={r.get('source_code') for r in selected}
                    or len(selected)!=plan.get('selected_contract_count') or plan.get('full_chain') is not full):
                errors.append('index option full directory coverage mismatch' if full else 'index option report sample coverage mismatch')
            if not full and (len(selected)>3*PAIRS_PER_EXPIRY*2 or len(selected)>plan.get('contract_budget',0)
                    or len({r.get('expiry_date') for r in selected})>3):
                errors.append('index option report sample budget exceeded')
        elif (len(selected) != plan.get('selected_contract_count') or len(selected)>plan.get('contract_budget',0)
                or plan.get('full_chain') is not False):
            errors.append('index option core selection count mismatch')
        pairs = {}
        for row in selected:
            pairs.setdefault((row.get('expiry_date'),row.get('strike')),set()).add(row.get('option_type'))
        if not pairs or any(sides != {'C','P'} for sides in pairs.values()):
            errors.append('index option core selection is not paired')
        if modern and not full:
            for expiry in {r.get('expiry_date') for r in selected}:
                if len({r.get('strike') for r in selected if r.get('expiry_date')==expiry})>PAIRS_PER_EXPIRY:
                    errors.append('index option per-expiry report sample budget exceeded')
    expected = payload.get('futures_directory_codes', [])
    if not expected or len(expected)!=len(set(expected)) or set(expected)!={r.get('source_code') for r in futures}:
        errors.append('index futures directory coverage mismatch')
    if modern:
        from .index_analytics import PRICE_BASIS
        if payload.get('options_scope') not in SCOPES or payload.get('full_option_chain') is not full:
            errors.append('index option collection scope missing')
        analytics = payload.get('analytics',{})
        if (analytics.get('price_basis')!=PRICE_BASIS or analytics.get('collection_scope')!=payload.get('options_scope')
                or set(analytics.get('products',{}))!=set(OPTIONS)):
            errors.append('index option analysis products/basis missing')
        for product in OPTIONS:
            expiries = analytics.get('products',{}).get(product,{}).get('expiries',[])
            if ({e.get('expiry') for e in expiries}!={r.get('expiry_date') for r in options if r['product']==product}
                    or len({e.get('symbol') for e in expiries})!=len(expiries)):
                errors.append('index option analysis expiry coverage mismatch')
            for expiry in expiries:
                metrics = expiry.get('metrics',{})
                actual = [r.get('source_code') for r in options if r['product']==product and r['expiry_date']==expiry.get('expiry')]
                if (metrics.get('contract_codes')!=sorted(actual) or metrics.get('contract_count')!=len(actual)
                        or metrics.get('pcr_scope')!=('full_expiry' if full else 'selected_strikes_only')):
                    errors.append('index option analysis sample scope/count mismatch')
        cash = payload.get('cash_market',{})
        if cash.get('trade_date')!=day or set(cash.get('etfs',{}))!=set(FUTURES):
            errors.append('index cash market dates/coverage missing')
        for etf in cash.get('etfs',{}).values():
            if etf.get('share_unit_verified') is not False or any(etf.get(k) is not None for k in
                    ('total_shares','share_change','estimated_net_creation_redemption_cny')):
                errors.append('unverified ETF unit must not produce normalized shares or CNY flow')
    return errors


def complete(root, day, scope=REPORT_SCOPE):
    payload = read_json(Path(root)/f'index_derivatives/snapshots/{day}.json', default={})
    accepted = (REPORT_SCOPE,FULL_SCOPE) if scope==REPORT_SCOPE else (FULL_SCOPE,)
    return (payload.get('trade_date') == day and payload.get('schema_version')==VERSION
            and payload.get('options_scope') in accepted and not validate(payload))


def full_quotes(api, codes, day):
    """Wind supports multiple codes with one WSD field; require exact dates/codes."""
    result = {c:{} for c in codes}
    for offset in range(0,len(codes),200):
        batch = codes[offset:offset+200]
        for field in OPTION_EOD_FIELDS:
            args = (','.join(batch),field,day,day,'Days=Trading;Fill=Blank')
            try:
                raw = api.call('wsd',*args)
                if (raw['codes']!=batch or [f.lower() for f in raw['fields']]!=[field]
                        or len(raw['times'])!=1 or raw['times'][0][:10]!=day
                        or len(raw['data'])!=1 or len(raw['data'][0])!=len(batch)
                        or any(not number(v) or v<0 for v in raw['data'][0])):
                    raise ValueError('full option quote date, coverage or value mismatch')
                for code,value in zip(batch,raw['data'][0]):
                    result[code][field] = value
            except (KeyError,ValueError):
                api.discard('wsd',*args)
                raise
    return result


def collect_eod(api, day, root, *, include_books=True, options_scope=REPORT_SCOPE):
    from .wind_daily import ZONE, eod_session_complete
    from .wind_daily_collect import directory, FIELDS
    from .index_analytics import analyze, attach_changes, futures_summary, radar, history_changes
    from .wind_index_market import cash_market, books
    from .trading_calendar import previous_trading_day
    from datetime import date
    if not eod_session_complete(day):
        raise ValueError('index derivatives unfinished day session')
    previous = read_json(Path(root)/'index_derivatives/snapshots'/f'{previous_trading_day(date.fromisoformat(day))}.json',default={})
    if previous and validate(previous):
        previous = {}
    universe, _ = directory(api, day, catalog=CATALOG, allowed_empty=())
    common = dict(trade_date=day, source_trade_date=day, source_date_match=True,
                  source_date_basis='WindPy.wsd.Times', source_provider='wind', source_endpoint='WindPy.wsd',
                  exchange='CFFEX', price_unit='index_points', volume_unit='contracts', night_session=False)
    futures, options, plans = [], [], []
    for code, identity in sorted(universe.items()):
        q = daily_quote(api, code, FIELDS, day)
        futures.append(dict(common, source_code=code, contract=identity['contract'],product=identity['product'],
            listed_date=str(identity['contract_issue_date'])[:10],expiry_date=str(identity['last_trade_date'])[:10],
            **{f:q[f] for f in ('open','high','low','close','settle','pre_settle','volume')},
            open_interest=q['oi'],turnover=q['amt'],turnover_unit='CNY',
            close_vs_pre_settle_pct=(q['close']/q['pre_settle']-1)*100 if q['close'] is not None and q['pre_settle'] else None))
    for product, index in OPTIONS.items():
        spot = daily_quote(api, index, 'close', day)['close']
        if not number(spot) or spot<=0:
            raise ValueError('index option missing cash index reference')
        main = max([r for r in futures if r['product']=={'IO':'IF','HO':'IH','MO':'IM'}[product]],
                   key=lambda r:(r['volume'],r['open_interest']))
        selected,plan = select_for_report(option_directory(api,day,product),day,spot,
            scope=options_scope,previous=previous,main_month=main['contract'][2:])
        plans.append(plan)
        quotes = full_quotes(api,plan['selected_codes'],day)
        for identity in selected:
            q = quotes[identity['source_code']]
            options.append(dict(common, **{k:v for k,v in identity.items() if k not in common},
                **{f:q[f] for f in ('close','settle','pre_settle','volume')}, open_interest=q['oi'],
                underlying_index_close=spot, underlying_source_trade_date=day))
    attach_changes(options,previous,day)
    prior_futures = {r['source_code']:r for r in previous.get('futures_contracts',[])}
    for row in futures:
        prior = prior_futures.get(row['source_code'],{}).get('open_interest')
        row['oi_change'] = row['open_interest']-prior if number(prior) else None
    analytics = analyze(options,day,scope=options_scope)
    cash = cash_market(api,day)
    summaries, linkage = futures_summary(futures,cash,analytics,day)
    payload = dict(schema_version=VERSION, trade_date=day, source_provider='wind',frequency='EOD',
        generated_at=datetime.now(ZONE).isoformat(),options_scope=options_scope,full_option_chain=options_scope==FULL_SCOPE,
        futures_directory_codes=sorted(universe),futures_contracts=futures,options_contracts=options,
        collection_plans=plans,night_session='not_applicable',analytics=analytics,cash_market=cash,
        futures_summary=summaries,futures_option_linkage=linkage,
        coverage=dict(futures_products=list(FUTURES),options_products=list(OPTIONS),
                      futures_contract_count=len(futures),option_contract_count=len(options),
                      option_directory_contract_count=sum(p['directory_contract_count'] for p in plans)),
        limitations=[('Full coverage against the target-day Wind directory; not an independent exchange audit.' if options_scope==FULL_SCOPE
                     else 'Report sample only; PCR and Gamma describe selected strikes, not whole-market positioning.'),
                    'Settlement/fallback-close Black76 on parity forward; fixed 1.5% rate assumption.',
                    'Delta wings use nearest observed delta; Gamma is unsigned concentration, not dealer positioning.',
                    'ETF share unit unverified; raw changes only; CNY flow estimates disabled.',
                    'WSQ books are separate observations with unverified vendor time; never EOD pricing inputs.'])
    errors = validate(payload)
    if errors:
        raise ValueError('; '.join(errors))
    payload['validation_passed'] = True
    root = Path(root)/'index_derivatives'
    old = read_json(root/'snapshots'/f'{day}.json',default={})
    if old.get('schema_version')==1:
        if old.get('trade_date')!=day or validate(old):
            raise ValueError('existing legacy index snapshot failed validation')
        write_json_if_changed(root/'legacy_core/snapshots'/f'{day}.json',old)
    write_json_if_changed(root/'snapshots'/f'{day}.json',payload)
    if read_json(root/'latest.json',default={}).get('trade_date','')<=day:
        write_json_if_changed(root/'latest.json',payload)
        write_json_if_changed(root/'radar_latest.json',radar(payload))
    # Books are optional and cannot invalidate verified EOD settlement data.
    if include_books:
        observed = books(api,[r['source_code'] for r in options],day)
        if observed['status']!='not_requested_for_historical_day':
            write_json_if_changed(root/'books/latest.json',observed)
    records = []
    for path in sorted((root/'snapshots').glob('????-??-??.json'),reverse=True):
        snapshot = read_json(path)
        if snapshot.get('trade_date')!=path.stem:
            raise ValueError('index history snapshot filename/date mismatch')
        if snapshot.get('schema_version')!=VERSION:
            continue  # legacy core snapshots retained unchanged, never relabelled full-chain
        failures = validate(snapshot)
        if failures or snapshot.get('validation_passed') is not True:
            raise ValueError('index history snapshot failed validation: '+str(failures))
        records.append(radar(snapshot))
        if len(records)>=60:
            break
    records = history_changes(list(reversed(records)))
    write_json_if_changed(root/'radar_history.json',dict(schema_version=VERSION,records=records,
        retained_trading_days=60,price_basis=analytics['price_basis']))
    return dict(date=day,mode='index_derivatives_eod',**payload['coverage'])


def collect_intraday(api, window, root, *, options_scope=REPORT_SCOPE):
    from .wind_daily import ZONE
    from .wind_daily_collect import directory
    from .wind_intraday import quote, coverage, stamp
    if window['session'] != 'day':
        return dict(mode='index_derivatives',state='no_night_session')
    # The shared commodity window begins at 09:00; CFFEX begins at 09:30.
    window = dict(window,session_start=window['trading_date']+'T09:30:00+08:00')
    if stamp(window['cutoff'])<=stamp(window['session_start']):
        return dict(mode='index_derivatives',state='no_completed_day_bar')
    day = window['trading_date']
    universe, _ = directory(api,day,catalog=CATALOG,allowed_empty=())
    common = dict(exchange='CFFEX',source_provider='wind',source_endpoint='WindPy.wsi',
                  settle=None,open_interest=None,greeks=None,price_unit='index_points')
    futures = [dict(common,source_code=code,contract=r['contract'],product=r['product'],**quote(api,code,window))
               for code,r in sorted(universe.items())]
    options, plans = [], []
    for product,index in OPTIONS.items():
        ref = quote(api,index,window)
        if ref['record_state']!='quote':
            plans.append(dict(product_key='CFFEX:'+product,state='missing_current_index'))
            continue
        # Five-minute volume is not daily main-contract volume. Use the previous
        # verified EOD main month; if absent, nearest two option expiries suffice.
        previous = read_json(Path(root)/'index_derivatives/latest.json',default={})
        if previous and validate(previous):
            previous = {}
        future_product = {'IO':'IF','HO':'IH','MO':'IM'}[product]
        main = previous.get('futures_summary',{}).get(future_product,{}).get('main_contract',{})
        selected,plan = select_for_report(option_directory(api,day,product),day,ref['close'],scope=options_scope,
            previous=previous,main_month=main.get('contract','')[2:] or None)
        plans.append(plan)
        for identity in selected:
            options.append(dict(common,**{k:v for k,v in identity.items() if k not in common},
                                **quote(api,identity['source_code'],window)))
    if not any(r['record_state']=='quote' for r in futures):
        raise ValueError('no current index futures quotes')
    payload = dict(schema_version=VERSION,mode='intraday',**window,validation_passed=True,
        source_provider='wind',generated_at=datetime.now(ZONE).isoformat(),options_scope=options_scope,
        full_option_directory=len(plans)==3 and all(p.get('directory_codes') for p in plans),
        full_option_chain=options_scope==FULL_SCOPE,
        selected_quote_coverage_complete=all(r['record_state']=='quote' for r in futures+options) and len(plans)==3 and all(not p.get('state') for p in plans),
        books_state='not_requested_WSQ_cannot_represent_fixed_delayed_cutoff',
        unavailable_fields=['settle','open_interest','greeks'],volume_unit='contracts_in_selected_5min_bar',
        futures_contracts=futures,options_contracts=options,collection_plans=plans,
        coverage=dict(futures=coverage(futures),options=coverage(options),
                      missing_option_products=[p['product_key'] for p in plans if p.get('state')]))
    root=Path(root)/'index_derivatives/intraday'
    existing=read_json(root/'latest.json',default={})
    if existing.get('cutoff','')>window['cutoff']:
        raise ValueError('refusing older index intraday snapshot')
    write_json_if_changed(root/f'{day}.json',payload)
    write_json_if_changed(root/'latest.json',payload)
    return dict(mode='index_derivatives_intraday',trading_date=day,coverage=payload['coverage'])


def report_view(root, expected_date):
    root=Path(root)
    eod=read_json(root/'index_derivatives/latest.json',default={})
    intra=read_json(root/'index_derivatives/intraday/latest.json',default={})
    current=dict(mode='eod',path='data/index_derivatives/latest.json',trade_date=eod.get('trade_date'),options_scope=eod.get('options_scope'))
    if intra.get('validation_passed') and intra.get('cutoff','')>(eod.get('trade_date','')+'T15:00:00+08:00'):
        current=dict(mode='intraday',path='data/index_derivatives/intraday/latest.json',
                     trading_date=intra['trading_date'],cutoff=intra['cutoff'],coverage=intra['coverage'],options_scope=intra.get('options_scope'))
    return dict(trade_date=eod.get('trade_date'),expected_eod_date=expected_date,
                eod_date_matches=eod.get('trade_date')==expected_date,available=bool(eod),
                options_scope=eod.get('options_scope'),night_session='not_applicable',coverage=eod.get('coverage',{}),
                current_market_snapshot=current,analytics=eod.get('analytics'),cash_market=eod.get('cash_market'),
                futures_summary=eod.get('futures_summary'),futures_option_linkage=eod.get('futures_option_linkage'),
                source_paths=dict(radar='data/index_derivatives/radar_latest.json',
                    history='data/index_derivatives/radar_history.json',books='data/index_derivatives/books/latest.json'),
                limitations=eod.get('limitations',[]))
