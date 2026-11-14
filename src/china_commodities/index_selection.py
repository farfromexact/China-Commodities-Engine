"""Bounded option sample for reports; the full Wind directory is metadata only."""
from datetime import date
import math
from statistics import NormalDist, median

from .index_analytics import finite, positive

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
    prior = previous.get('options_contracts',[]) if previous.get('trade_date','z')<day and previous.get('validation_passed') else []
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
        strikes = sorted({r['strike'] for r in rows})
        historical = [r for r in prior if r.get('product')==product and r.get('expiry_date')==expiry]
        sigmas = [r['iv_percent']/100 for r in historical if positive(r.get('iv_percent'))]
        sigma = median(sigmas) if sigmas else .25
        years = (date.fromisoformat(expiry)-date.fromisoformat(day)).days/365
        nearest = lambda k:min(strikes,key=lambda s:(abs(s-k),s))
        selected = [nearest(reference)]
        # ATM + four delta wings. When no earlier delta exists, use an explicitly
        # labelled cash-price/volatility proxy to choose strikes, not to price options.
        wings = []
        for side,target in [('C',.25),('P',-.25),('C',.10),('P',-.10)]:
            points = [r for r in historical if r.get('option_type')==side and finite(r.get('delta'))
                      and r.get('strike') in strikes]
            observed = min(points,key=lambda r:(abs(r['delta']-target),r['strike'])) if points else None
            if observed and abs(observed['delta']-target)<=.08:
                strike = observed['strike']
                method = 'previous_observed_delta'
            else:
                probability = abs(target)*math.exp(.015*years)
                probability = probability if side=='C' else 1-probability
                d1 = NormalDist().inv_cdf(min(.999999,max(.000001,probability)))
                strike = nearest(reference*math.exp(.5*sigma*sigma*years-d1*sigma*math.sqrt(years)))
                method = 'cash_price_volatility_proxy'
            wings.append(dict(side=side,target_delta=target,strike=strike,selection_method=method))
            if strike not in selected:
                selected.append(strike)
        oi = {}
        for row in historical:
            if row.get('strike') in strikes and finite(row.get('open_interest')):
                oi[row['strike']] = oi.get(row['strike'],0)+row['open_interest']
        for strike in sorted(oi,key=lambda k:(-oi[k],abs(k-reference),k))[:2]:
            if strike not in selected:
                selected.append(strike)
        for strike in sorted(strikes,key=lambda k:(abs(k-reference),k)):
            if len(selected)>=PAIRS_PER_EXPIRY:
                break
            if strike not in selected:
                selected.append(strike)
        selected=selected[:PAIRS_PER_EXPIRY]
        selections.extend(r for r in rows if r['strike'] in selected)
        details.append(dict(expiry_date=expiry,strikes=sorted(selected),selection_sigma=sigma,
            sigma_source='previous_sample_median_IV' if sigmas else 'fixed_25pct_selection_assumption',
            previous_delta_available=any(finite(r.get('delta')) for r in historical),wing_selection=wings))
    selections.sort(key=lambda r:(r['expiry_date'],r['strike'],r['option_type']))
    return selections,dict(common,selected_codes=[r['source_code'] for r in selections],
        selected_contract_count=len(selections),expiry_budget=3,pairs_per_expiry_budget=PAIRS_PER_EXPIRY,
        contract_budget=3*PAIRS_PER_EXPIRY*2,selection_reference=reference,
        selection_reference_basis='cash_index_price_proxy_not_parity_forward',
        selection_history_date=previous.get('trade_date') if prior else None,
        selection_reason='nearest_two_expiries_plus_main_future_month_ATM_delta_wings_prior_OI',series=details)
