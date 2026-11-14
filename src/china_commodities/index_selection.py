"""Bounded option sample for reports; the full Wind directory is metadata only."""
from datetime import date
from .option_sampling import bounded_strikes
from .trading_calendar import previous_trading_day

from .index_analytics import positive

REPORT_SCOPE = 'report_v1'
FULL_SCOPE = 'full_chain_v2'
SCOPES = (REPORT_SCOPE,FULL_SCOPE)
PAIRS_PER_EXPIRY = 7


def select(directory, day, reference, *, scope=REPORT_SCOPE, previous=None, main_month=None):
    if scope not in SCOPES:
        raise ValueError('invalid index_options_scope')
    product = directory[0]['product']
    common = dict(product_key='CFFEX:'+product,collection_scope=scope,
        directory_source='WindPy.wset.optionchain',directory_scope='all_listed_index_option_months_and_strikes',
        directory_contract_count=len(directory),directory_codes=[r['source_code'] for r in directory],
        full_chain=scope==FULL_SCOPE)
    if scope==FULL_SCOPE:
        return directory,dict(common,selected_codes=common['directory_codes'],selected_contract_count=len(directory))
    if not positive(reference):
        raise ValueError('index report sample needs a valid selection reference')
    previous = previous or {}
    # Earlier samples can guide selection, never stand in for today's observations.
    prior = previous.get('options_contracts',[]) if (previous.get('trade_date') == str(previous_trading_day(date.fromisoformat(day)))
             and previous.get('validation_passed')) else []
    expiries = sorted({r['expiry_date'] for r in directory if r['expiry_date']>day})
    chosen = expiries[:2]
    matched = next((r['expiry_date'] for r in directory if r['contract'].split('-')[0][2:]==main_month
                    and r['expiry_date']>day),None)
    if matched and matched not in chosen:
        chosen.append(matched)
    if not chosen:
        raise ValueError('no unexpired index option series for report sample')
    selections, details = [], []
    for expiry in sorted(chosen):
        rows = [r for r in directory if r['expiry_date']==expiry]
        pairs = {}
        for row in rows:
            pair = pairs.setdefault(row['strike'], {})
            if row['option_type'] in pair:
                raise ValueError('duplicate index option side/strike')
            pair[row['option_type']] = row
        strikes = sorted(k for k, sides in pairs.items() if set(sides) == {'C', 'P'})
        if not strikes:
            raise ValueError('index report sample needs paired strikes')
        historical = [r for r in prior if r.get('product')==product and r.get('expiry_date')==expiry]
        years = (date.fromisoformat(expiry)-date.fromisoformat(day)).days/365
        selected, detail = bounded_strikes(strikes, reference, years, PAIRS_PER_EXPIRY, historical)
        selections.extend(r for r in rows if r['strike'] in selected)
        details.append(dict(expiry_date=expiry,strikes=selected, **detail))
    selections.sort(key=lambda r:(r['expiry_date'],r['strike'],r['option_type']))
    return selections,dict(common,selected_codes=[r['source_code'] for r in selections],
        selected_contract_count=len(selections),expiry_budget=3,pairs_per_expiry_budget=PAIRS_PER_EXPIRY,
        contract_budget=3*PAIRS_PER_EXPIRY*2,selection_reference=reference,
        selection_reference_basis='cash_index_price_proxy_not_parity_forward',
        selection_history_date=previous.get('trade_date') if prior else None,
        selection_reason='nearest_two_expiries_plus_main_future_month_ATM_delta_wings_prior_OI',series=details)
