"""Wind equity-index derivatives, independent of commodity scope and night data."""
from datetime import datetime
import math
from pathlib import Path
import re

from .catalog import ProductCatalog
from .storage import read_json, write_json_if_changed

FUTURES = ('IF', 'IH', 'IC', 'IM')
# Index options are on cash indices, not on the corresponding index futures.
OPTIONS = {'IO': '000300.SH', 'HO': '000016.SH', 'MO': '000852.SH'}
CATALOG = ProductCatalog({}, {}, {'CFFEX': FUTURES}, ())
VERSION = 1


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
            if expiry == day:
                continue  # core_v1 selects unexpired series; explicitly not a full chain.
            result.append(dict(exchange='CFFEX', product=product, contract=code.split('.')[0],
                source_code=code, underlying_contract=index, underlying_index=index,
                option_type=match[2], strike=float(row['strike_price']), expiry_date=expiry,
                listed_date=issued, exercise_style='european' if row['exe_type']=='欧式' else 'unknown',
                multiplier=row['multiplier'], price_unit='index_points', multiplier_unit='CNY_per_point'))
        if not result:
            raise ValueError('empty index option directory')
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
    """Validate date, identities, scope, numeric fields and paired core selection."""
    errors = []
    day = payload.get('trade_date')
    if payload.get('schema_version') != VERSION or payload.get('source_provider') != 'wind' or not day:
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
            elif row['close'] <= 0 or row['settle'] <= 0:
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
                for field in ('iv_percent','delta','gamma'):
                    if row.get(field) is not None and not number(row[field]):
                        errors.append(f'index option invalid {field}: {code}')
    plans = payload.get('collection_plans', [])
    if {p.get('product_key') for p in plans} != {'CFFEX:'+p for p in OPTIONS} or len(plans)!=len(OPTIONS):
        errors.append('index option selection plans incomplete')
    for plan in plans:
        selected = [r for r in options if 'CFFEX:'+r.get('product','') == plan.get('product_key')]
        if (len(selected) != plan.get('selected_contract_count') or len(selected)>plan.get('contract_budget',0)
                or plan.get('full_chain') is not False):
            errors.append('index option core selection count mismatch')
        pairs = {}
        for row in selected:
            pairs.setdefault((row.get('expiry_date'),row.get('strike')),set()).add(row.get('option_type'))
        if not pairs or any(sides != {'C','P'} for sides in pairs.values()):
            errors.append('index option core selection is not paired')
    expected = payload.get('futures_directory_codes', [])
    if not expected or len(expected)!=len(set(expected)) or set(expected)!={r.get('source_code') for r in futures}:
        errors.append('index futures directory coverage mismatch')
    return errors


def complete(root, day):
    payload = read_json(Path(root)/f'index_derivatives/snapshots/{day}.json', default={})
    return payload.get('trade_date') == day and not validate(payload)


def collect_eod(api, day, root):
    from .wind_daily import ZONE, eod_session_complete
    from .wind_daily_collect import directory, FIELDS, OPTION_FIELDS
    if not eod_session_complete(day):
        raise ValueError('index derivatives unfinished day session')
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
        selected, plan = select_options(api, day, product, spot)
        plans.append(plan)
        for identity in selected:
            q = daily_quote(api, identity['source_code'], OPTION_FIELDS, day)
            options.append(dict(common, **{k:v for k,v in identity.items() if k not in common},
                **{f:q[f] for f in ('close','settle','pre_settle','volume')}, open_interest=q['oi'],
                underlying_index_close=spot, underlying_source_trade_date=day,
                iv_percent=q['us_impliedvol']*100 if q['us_impliedvol'] is not None else None,
                delta=q['delta'], gamma=q['gamma'], greeks_source='Wind_vendor_reported',
                greeks_units='IV_percent_delta_gamma_Wind_native'))
    payload = dict(schema_version=VERSION, trade_date=day, source_provider='wind',frequency='EOD',
        generated_at=datetime.now(ZONE).isoformat(),options_scope='core_v1',full_option_chain=False,
        futures_directory_codes=sorted(universe),futures_contracts=futures,options_contracts=options,
        collection_plans=plans,night_session='not_applicable',
        coverage=dict(futures_products=list(FUTURES),options_products=list(OPTIONS),
                      futures_contract_count=len(futures),option_contract_count=len(options)),
        limitations=['Core option sample only; not all expiries/strikes.',
                    'No dealer positioning or execution-ready claim; missing Greeks remain null.'])
    errors = validate(payload)
    if errors:
        raise ValueError('; '.join(errors))
    payload['validation_passed'] = True
    root = Path(root)/'index_derivatives'
    write_json_if_changed(root/'snapshots'/f'{day}.json',payload)
    if read_json(root/'latest.json',default={}).get('trade_date','')<=day:
        write_json_if_changed(root/'latest.json',payload)
    return dict(date=day,mode='index_derivatives_eod',**payload['coverage'])


def collect_intraday(api, window, root):
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
        selected,plan = select_options(api,day,product,ref['close'],ref['source_timestamp'])
        plans.append(plan)
        for identity in selected:
            options.append(dict(common,**{k:v for k,v in identity.items() if k not in common},
                                **quote(api,identity['source_code'],window)))
    if not any(r['record_state']=='quote' for r in futures):
        raise ValueError('no current index futures quotes')
    payload = dict(schema_version=VERSION,mode='intraday',**window,validation_passed=True,
        source_provider='wind',generated_at=datetime.now(ZONE).isoformat(),options_scope='core_v1',
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
    current=dict(mode='eod',path='data/index_derivatives/latest.json',trade_date=eod.get('trade_date'))
    if intra.get('validation_passed') and intra.get('cutoff','')>(eod.get('trade_date','')+'T15:00:00+08:00'):
        current=dict(mode='intraday',path='data/index_derivatives/intraday/latest.json',
                     trading_date=intra['trading_date'],cutoff=intra['cutoff'],coverage=intra['coverage'])
    return dict(trade_date=eod.get('trade_date'),expected_eod_date=expected_date,
                eod_date_matches=eod.get('trade_date')==expected_date,available=bool(eod),
                options_scope='core_v1',night_session='not_applicable',coverage=eod.get('coverage',{}),
                current_market_snapshot=current)
