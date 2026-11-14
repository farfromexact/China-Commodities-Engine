"""Deterministic index-option research, using explicit prices and contract metadata.

Ported methodology: China-Options-Engine at a4c16a18d16d039a30e9c162950096c9333a0a49,
engine.py / futures_link.py. No network, vendor Greeks, inferred expiry, or dealer sign.
"""
from datetime import date, datetime, time
import math
from statistics import median

from .option_greeks import implied_volatility, _normal_cdf, _normal_pdf
from .trading_calendar import previous_trading_day

PRICE_BASIS = 'wind_settlement_fallback_close'
RATE = 0.015
DIRECT = {'IH': 'HO', 'IF': 'IO', 'IC': None, 'IM': 'MO'}


def finite(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def positive(x):
    return finite(x) and x > 0


def price(row):
    if positive(row.get('settle')):
        return row['settle']
    return row.get('close') if positive(row.get('close')) else None


def analyze(options, day, *, rate=RATE, scope='full_chain_v2'):
    """Full-expiry PCR and parity/Black-76, retaining unavailable IV as null.

    IV is annualized decimal; delta and gamma differentiate the inferred forward.
    Delta wings match the nearest observed delta, as in the source repository.
    """
    from .wind_daily import ZONE
    groups = {}
    for row in options:
        groups.setdefault((row['product'], row['expiry_date']), []).append(row)
    products = {}
    valuation_time = datetime.combine(date.fromisoformat(day), time(15), tzinfo=ZONE)
    for (product, expiry), contracts in sorted(groups.items()):
        expiry_time = datetime.combine(date.fromisoformat(expiry), time(15), tzinfo=ZONE)
        years = (expiry_time - valuation_time).total_seconds() / (365 * 86400)
        pairs = {}
        for row in contracts:
            pairs.setdefault(row['strike'], {})[row['option_type']] = row
        candidates = []
        if years > 0:
            for strike, sides in sorted(pairs.items()):
                call, put = price(sides.get('C', {})), price(sides.get('P', {}))
                if call is not None and put is not None:
                    forward = strike + math.exp(rate * years) * (call - put)
                    if forward > 0:
                        candidates.append((abs(call-put)/max(call+put, 1e-9), forward))
        candidates.sort(key=lambda v: v[0])
        forward = median([v for _, v in candidates[:9]]) if candidates else None
        points, gamma_nodes = [], {}
        for row in contracts:
            px = price(row)
            row.update(analytics_price=px, analytics_price_basis=PRICE_BASIS,
                       iv_percent=None, delta=None, gamma=None,
                       greeks_source='local_Black76_parity_forward',
                       greeks_units='IV_percent_delta_per_forward_point_gamma_per_forward_point_squared',
                       gamma_1pct=None)
            if forward is None or px is None or row['exercise_style'] != 'european':
                continue
            intrinsic = math.exp(-rate*years) * max(
                forward-row['strike'] if row['option_type']=='C' else row['strike']-forward, 0)
            if px <= intrinsic + 1e-8:
                continue
            iv = implied_volatility(px, forward=forward, strike=row['strike'],
                time_to_expiry_years=years, rate=rate, option_type=row['option_type'], exercise_style='european')
            if iv is None:
                continue
            d1 = (math.log(forward/row['strike']) + .5*iv*iv*years)/(iv*math.sqrt(years))
            discount = math.exp(-rate*years)
            delta = discount * (_normal_cdf(d1) if row['option_type']=='C' else _normal_cdf(d1)-1)
            gamma = discount * _normal_pdf(d1)/(forward*iv*math.sqrt(years))
            exposure = gamma * row['open_interest'] * row['multiplier'] * forward**2 * .01
            row.update(iv_percent=iv*100, delta=delta, gamma=gamma, gamma_1pct=exposure)
            points.append((row['option_type'], delta, iv, row['strike']))
            gamma_nodes[row['strike']] = gamma_nodes.get(row['strike'], 0) + abs(exposure)
        def wing(side, target):
            available = [p for p in points if p[0]==side]
            return min(available, key=lambda p: (abs(p[1]-target), p[3]))[2] if available else None
        wing_observations = []
        for side,target in [('C',.5),('P',-.5),('C',.25),('P',-.25),('C',.1),('P',-.1)]:
            available = [p for p in points if p[0]==side]
            observed = min(available,key=lambda p:(abs(p[1]-target),p[3])) if available else None
            wing_observations.append(dict(side=side,target_delta=target,
                observed_delta=observed[1] if observed else None,strike=observed[3] if observed else None,
                delta_distance=abs(observed[1]-target) if observed else None))
        atm = [v for v in (wing('C', .5), wing('P', -.5)) if v is not None]
        atm_iv = sum(atm)/len(atm) if atm else None
        c25, p25 = wing('C', .25), wing('P', -.25)
        metrics = dict(atm_iv=atm_iv, call25_iv=c25, put25_iv=p25,
            call10_iv=wing('C', .1), put10_iv=wing('P', -.1),
            rr25=c25-p25 if c25 is not None and p25 is not None else None,
            bf25=(c25+p25)/2-atm_iv if None not in (c25,p25,atm_iv) else None,
            gamma_peaks=[dict(strike=k,abs_gamma_1pct=v) for k,v in
                         sorted(gamma_nodes.items(),key=lambda p:(-p[1],p[0]))[:5]],
            greek_contract_count=len(points), contract_count=len(contracts),
            greek_coverage=len(points)/len(contracts), pricing_basis=PRICE_BASIS,wing_observations=wing_observations)
        metrics.update(pcr_scope='full_expiry' if scope=='full_chain_v2' else 'selected_strikes_only',
            gamma_scope='full_expiry_valid_greeks' if scope=='full_chain_v2' else 'selected_strikes_valid_greeks_only',
            contract_codes=sorted(r['source_code'] for r in contracts))
        for side, name in [('C','call'), ('P','put')]:
            selected = [r for r in contracts if r['option_type']==side]
            metrics[name+'_oi'] = sum(r['open_interest'] for r in selected)
            metrics[name+'_volume'] = sum(r['volume'] for r in selected)
            changes = [r.get('oi_change') for r in selected]
            metrics[name+'_oi_change'] = sum(changes) if all(finite(v) for v in changes) else None
        metrics['pcr_oi'] = metrics['put_oi']/metrics['call_oi'] if metrics['call_oi'] else None
        metrics['pcr_volume'] = metrics['put_volume']/metrics['call_volume'] if metrics['call_volume'] else None
        series = dict(symbol=contracts[0]['contract'].split('-')[0], expiry=expiry,
            forward=forward, t_years=max(years,0), metrics=metrics,
            analytics_state='expired' if years<=0 else 'ok' if points else 'unavailable')
        products.setdefault(product, dict(expiries=[]))['expiries'].append(series)
    return dict(price_basis=PRICE_BASIS, collection_scope=scope, valuation_time=valuation_time.isoformat(),
        risk_free_rate=rate, rate_basis='explicit_fixed_assumption', iv_unit='annualized_decimal',
        delta_basis='parity_implied_forward', wing_method='nearest_observed_delta_no_interpolation',
        dealer_gex=False, products=products)


def attach_changes(options, previous, day):
    expected = str(previous_trading_day(date.fromisoformat(day)))
    usable = (previous.get('trade_date')==expected and previous.get('schema_version')==2
              and previous.get('validation_passed') is True)
    lookup = {r['source_code']:r for r in previous.get('options_contracts', [])} if usable else {}
    for row in options:
        prior = lookup.get(row['source_code'], {})
        value = prior.get('open_interest')
        row['oi_change'] = row['open_interest']-value if finite(value) else None
        row['previous_date'] = expected if finite(value) else None


def futures_summary(futures, cash, analytics, day):
    summaries, linkage = {}, {}
    for product in DIRECT:
        contracts = sorted([r for r in futures if r['product']==product], key=lambda r:r['expiry_date'])
        active = [r for r in contracts if r['volume']>0 or r['open_interest']>0]
        if not active:
            summaries[product] = dict(status='empty', contracts=contracts)
            continue
        main = max(active,key=lambda r:(r['volume'],r['open_interest']))
        later = [r for r in active if r['expiry_date']>main['expiry_date']]
        remaining = [r for r in active if r['source_code']!=main['source_code']]
        following = later[0] if later else max(remaining,key=lambda r:(r['volume'],r['open_interest']),default=None)
        index = cash['indices'].get(product, {})
        spot = index.get('close')
        basis = main['close']-spot if positive(spot) else None
        dte = (date.fromisoformat(main['expiry_date'])-date.fromisoformat(day)).days
        gap = ((date.fromisoformat(following['expiry_date'])-date.fromisoformat(main['expiry_date'])).days
               if following else 0)
        summaries[product] = dict(status='ok',front_contract=active[0],main_contract=main,next_contract=following,
            total_volume=sum(r['volume'] for r in contracts),total_open_interest=sum(r['open_interest'] for r in contracts),
            total_oi_change=sum(r['oi_change'] for r in contracts) if all(finite(r.get('oi_change')) for r in contracts) else None,
            main_volume_share=main['volume']/sum(r['volume'] for r in contracts) if sum(r['volume'] for r in contracts)>0 else None,
            main_oi_share=main['open_interest']/sum(r['open_interest'] for r in contracts) if sum(r['open_interest'] for r in contracts)>0 else None,
            next_minus_main_points=following['close']-main['close'] if following else None,
            annualized_roll_pct_inferred=(following['close']/main['close']-1)*365/gap if gap>0 else None,
            cash_index_code=index.get('source_code'),cash_index_close=spot,cash_basis_points=basis,
            cash_basis_pct=basis/spot if basis is not None else None,
            annualized_cash_basis_pct_inferred=basis/spot*365/dte if basis is not None and dte>0 else None,
            cash_basis_note='close minus cash index; calendar DTE; no dividend or financing adjustment',
            reference_etf=cash['etfs'].get(product))
        option = DIRECT[product]
        exact = next((e for e in analytics.get('products',{}).get(option,{}).get('expiries',[])
                      if e['symbol']==str(option)+main['contract'][2:]),None) if option else None
        forward = exact.get('forward') if exact else None
        linkage[product] = dict(direct_option_product=option,matched_option_symbol=exact['symbol'] if exact else None,
            linkage_status='same_month' if exact else 'no_direct_index_option' if option is None else 'missing_same_month',
            option_forward=forward, future_minus_option_forward_points=main['close']-forward if positive(forward) else None)
    return summaries, linkage


def radar(payload):
    return dict(schema_version=2,trade_date=payload['trade_date'],date=payload['trade_date'],
        source_provider='wind',data_fresh=True,option_price_basis=PRICE_BASIS,
        options_scope=payload['options_scope'],
        products=payload['analytics']['products'],futures=payload['futures_summary'],
        cash_market=payload['cash_market'],futures_option_linkage=payload['futures_option_linkage'],
        coverage=payload['coverage'],limitations=payload['limitations'])


def history_changes(records):
    """Only compare same series with a contiguous verified trading-day window."""
    by_date = {r['trade_date']:r for r in records}
    for record in records:
        target = date.fromisoformat(record['trade_date'])
        for lag in (1,3,5,20):
            prior_day = target
            chain = []
            for _ in range(lag):
                prior_day = previous_trading_day(prior_day)
                chain.append(str(prior_day))
            previous = by_date.get(str(prior_day)) if all(d in by_date for d in chain) else None
            previous = previous if (previous and previous['option_price_basis']==record['option_price_basis']
                and previous.get('options_scope')==record.get('options_scope')) else None
            comparison = dict(previous_date=previous['trade_date'] if previous else None,products={},futures={})
            if previous:
                for product, data in record['products'].items():
                    prior = {e['symbol']:e for e in previous['products'].get(product,{}).get('expiries',[])}
                    comparison['products'][product] = {}
                    for expiry in data['expiries']:
                        before = prior.get(expiry['symbol'],{}).get('metrics',{})
                        same_universe = (bool(before.get('contract_codes'))
                            and before['contract_codes']==expiry['metrics'].get('contract_codes'))
                        comparison['products'][product][expiry['symbol']] = {
                            k:expiry['metrics'][k]-before[k] if finite(expiry['metrics'].get(k)) and finite(before.get(k))
                               and (same_universe or k in ('atm_iv','rr25','bf25')) else None
                            for k in ('atm_iv','rr25','bf25','pcr_oi','pcr_volume','call_volume','put_volume','call_oi','put_oi')}
                for product, summary in record.get('futures',{}).items():
                    prior = previous.get('futures',{}).get(product,{})
                    current_main, prior_main = summary.get('main_contract',{}), prior.get('main_contract',{})
                    same = bool(current_main.get('source_code') and current_main.get('source_code')==prior_main.get('source_code'))
                    comparison['futures'][product] = dict(same_main_contract=same,
                        main_close_change=current_main['close']-prior_main['close'] if same else None,
                        cash_basis_change=summary['cash_basis_points']-prior['cash_basis_points']
                            if same and finite(summary.get('cash_basis_points')) and finite(prior.get('cash_basis_points')) else None)
            record.setdefault('changes',{})[str(lag)+'d'] = comparison
    return records
