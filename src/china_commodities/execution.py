"""Fail-closed futures execution boundary; research remains independent.

Quotes are rebuilt from a trusted snapshot, never accepted on provenance labels
alone. EOD references are dated planning inputs, not live executable bid/asks.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from functools import lru_cache
import hashlib
import json
import math
import re

from .catalog import load_catalog
from .features import contract_month


@lru_cache(maxsize=1)
def _catalog():
    return load_catalog()


def fingerprint(payload: dict) -> str:
    return 'sha256:' + hashlib.sha256(json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(',', ':'),
        allow_nan=False,
    ).encode('utf-8')).hexdigest()


def number(value, *, positive=False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('numeric field missing or invalid')
    try:
        value = float(value)
    except OverflowError as exc:
        raise ValueError('numeric field out of range') from exc
    if not math.isfinite(value) or (value <= 0 if positive else value < 0):
        raise ValueError('numeric field out of range')
    return float(value)


def canonical_contract(code: str, exchange: str, source_date: str) -> str:
    """Resolve a dated concrete contract; reject continuous/index/options codes."""
    match = re.fullmatch(r'([A-Z]+)(\d{3,4})', str(code).upper())
    if not match or exchange not in _catalog().exchanges:
        raise ValueError('not a concrete commodity futures contract')
    product, suffix = match.groups()
    if product not in _catalog().products_for_exchange(exchange):
        raise ValueError('contract/exchange mismatch')
    if len(suffix) == 3 and exchange != 'CZCE':
        raise ValueError('three-digit code outside CZCE')
    month = contract_month(code, source_date)
    if month is None:
        raise ValueError('invalid delivery month')
    # Restrict decade inference to the collector universe, including year rollover.
    year = date.fromisoformat(source_date).year
    if not year - 1 <= month.year <= year + 4:
        raise ValueError('delivery year outside supported universe')
    return f'{product}{month.year % 100:02d}{month.month:02d}'


def quote_book(snapshot: dict) -> dict:
    """Every eligible quote includes a hash of the full canonical snapshot.

    observed_at is collection/publication observation time, NOT exchange tick
    time. The distinction is explicit and no historical time is fabricated.
    """
    quotes, rejected = [], []
    try:
        sid = fingerprint(snapshot)
    except (TypeError, ValueError):
        return {'snapshot_id':None,'source_date':snapshot.get('trade_date'),
                'quotes':[],'rejected':[{'reason':'invalid_snapshot_serialization'}],
                'requires_output_validation':True}
    day = snapshot.get('trade_date')
    observed = snapshot.get('generated_at')
    source = snapshot.get('source')
    provider = source.get('provider') if isinstance(source, dict) else None
    rows = snapshot.get('futures_contracts')
    rows = rows if isinstance(rows, list) else []
    rows = [row for row in rows if isinstance(row, dict)]
    counts = {}
    for row in rows:
        try:
            code = canonical_contract(row.get('contract'), row.get('exchange'), day)
            key = (row['exchange'], code)
            counts[key] = counts.get(key, 0) + 1
        except (ValueError, TypeError, KeyError):
            pass
    for row in rows:
        try:
            if snapshot.get('verified') is not True:
                raise ValueError('snapshot_not_verified')
            row_provider = row.get('source_provider') or provider
            if not isinstance(row_provider,str) or not row_provider.strip() or not observed:
                raise ValueError('missing_source_or_observation')
            stamp = datetime.fromisoformat(observed)
            if stamp.tzinfo is None or stamp.date() < date.fromisoformat(day):
                raise ValueError('invalid_observation_time')
            if row.get('source_trade_date') != day or row.get('trade_date') != day or row.get('source_date_match') is not True:
                raise ValueError('source_date_mismatch')
            code = canonical_contract(row.get('contract'), row.get('exchange'), day)
            if counts[(row['exchange'], code)] != 1:
                raise ValueError('ambiguous_duplicate_contract')
            for kind in ('close', 'settle'):
                if row.get(kind) is None:
                    continue
                price = number(row[kind], positive=True)
                quotes.append({
                    'contract_code': code, 'exchange_contract_code': row['contract'],
                    'exchange': row['exchange'], 'price': price, 'price_type': kind,
                    'instrument_type': 'futures', 'source': row_provider,
                    'source_date': day, 'observed_at': observed,
                    'observed_at_kind': 'snapshot_generated_at', 'snapshot_id': sid,
                })
        except (ValueError, TypeError, KeyError) as exc:
            rejected.append({'contract_code': row.get('contract'), 'reason': str(exc)})
    return {'snapshot_id': sid, 'source_date': day, 'quotes': quotes, 'rejected': rejected,
            'usage': 'dated_reference_only_revalidate_for_live_orders',
            'requires_output_validation': True}


def validate_quote(quote: dict, snapshot: dict, as_of_date: str) -> dict:
    """Re-resolve every field against the trusted source; do not trust a label."""
    date.fromisoformat(as_of_date)
    number(quote.get('price'), positive=True)
    if snapshot.get('trade_date') != as_of_date:
        raise ValueError('stale_snapshot_for_requested_date')
    code = canonical_contract(quote.get('contract_code'), quote.get('exchange'), as_of_date)
    candidates = [q for q in quote_book(snapshot)['quotes']
                  if q['contract_code'] == code and q['exchange'] == quote.get('exchange')
                  and q['price_type'] == quote.get('price_type')]
    if len(candidates) != 1:
        raise ValueError('unverifiable_futures_quote')
    expected = candidates[0]
    for key, value in expected.items():
        if key == 'contract_code':
            continue  # Dated CZCE exchange and terminal aliases are equivalent.
        if quote.get(key) != value:
            raise ValueError('quote_mismatch:' + key)
    return expected


def portfolio_pnl(legs: list[dict], exit_prices: list[float]) -> dict:
    """Currency PnL, with explicit round-trip per-lot costs and price slippage."""
    if not legs or len(legs) != len(exit_prices):
        raise ValueError('one exit price required per leg')
    gross = costs = long_value = short_value = 0.0
    details = []
    for leg, exit_price in zip(legs, exit_prices):
        lots = number(leg['lots'], positive=True)
        if not lots.is_integer() or leg['side'] not in ('long', 'short'):
            raise ValueError('invalid lots or side')
        multiplier = number(leg['multiplier'], positive=True)
        entry = number(leg['quote']['price'], positive=True)
        exit_price = number(exit_price, positive=True)
        fee = number(leg['round_trip_fee_per_lot']) * lots
        slip = number(leg['round_trip_slippage_price']) * lots * multiplier
        quantity = lots * multiplier
        sign = 1 if leg['side'] == 'long' else -1
        pnl = sign * quantity * (exit_price - entry)
        gross += pnl
        costs += fee + slip
        if sign == 1: long_value += quantity * entry
        else: short_value += quantity * entry
        details.append({'quantity':quantity,'entry_notional':quantity*entry,
                        'gross_pnl':pnl,'fee':fee,'slippage_cost':slip})
    if not all(math.isfinite(value) for value in (
        gross, costs, long_value, short_value, long_value + short_value,
        gross - costs,
    )):
        raise ValueError('portfolio_arithmetic_overflow')
    return {'currency':'CNY','legs':details,'long_notional':long_value,
            'short_notional':short_value,'net_notional':long_value-short_value,
            'gross_notional':long_value+short_value,
            'notional_imbalance_pct':100*abs(long_value-short_value)/(long_value+short_value),
            'gross_pnl':gross,'costs':costs,'net_pnl':gross-costs,
            'quote_spread':legs[0]['quote']['price']-legs[1]['quote']['price'] if len(legs)==2 else None,
            'spread_is_position_pnl':False}


def gate_card(card: dict, snapshot: dict, metadata: dict, *, as_of_date: str) -> dict:
    """Only this sanitized execution envelope may be rendered as a trade card.

    Research is preserved verbatim but never promoted into the execution object.
    Risk triggers are net portfolio CNY amounts, not unweighted spread levels.
    """
    result = {'id':card.get('id'), 'research':deepcopy(card.get('research')),
              'status':'research_only','execution':None,'validation_errors':[]}
    try:
        if set(card) - {'id','research','legs','risk'}:
            raise ValueError('unsupported_execution_fields')
        legs = deepcopy(card['legs'])
        if not isinstance(legs,list) or not legs:
            raise ValueError('missing_legs')
        price_types, seen = set(), set()
        for leg in legs:
            if set(leg) != {'quote','lots','side','multiplier','round_trip_fee_per_lot','round_trip_slippage_price'}:
                raise ValueError('missing_or_unsupported_leg_fields')
            leg['quote'] = validate_quote(leg['quote'], snapshot, as_of_date)
            q = leg['quote']
            key = (q['exchange'],q['contract_code'])
            if key in seen: raise ValueError('duplicate_position_leg')
            seen.add(key)
            price_types.add(q['price_type'])
            matches = []
            for m in metadata.get('contracts', []):
                try:
                    if m.get('exchange') == q['exchange'] and canonical_contract(m.get('contract'),m.get('exchange'),as_of_date)==q['contract_code']:
                        matches.append(m)
                except (ValueError,TypeError):
                    continue
            if len(matches)!=1: raise ValueError('missing_or_ambiguous_contract_metadata')
            m = matches[0]
            if metadata.get('trade_date')!=as_of_date or m.get('source_date')!=as_of_date or m.get('is_stale') or not m.get('vendor'):
                raise ValueError('unverified_contract_metadata')
            if number(leg['multiplier'],positive=True)!=number(m.get('multiplier'),positive=True):
                raise ValueError('multiplier_mismatch')
            expiry = m.get('last_trading_day')
            if not expiry or date.fromisoformat(expiry)<date.fromisoformat(as_of_date):
                raise ValueError('missing_expiry_or_expired_contract')
        if len(price_types)!=1: raise ValueError('mixed_price_types')
        risk = card['risk']
        if set(risk) != {'max_loss_cny','take_profit_cny'}:
            raise ValueError('risk_requires_portfolio_cny_thresholds')
        number(risk['max_loss_cny'],positive=True)
        number(risk['take_profit_cny'],positive=True)
        metrics = portfolio_pnl(legs,[x['quote']['price'] for x in legs])
        if metrics['costs'] >= risk['max_loss_cny']:
            raise ValueError('costs_exceed_risk_budget')
        result.update(status='validated_reference_plan',execution={
            'as_of_date':as_of_date,'legs':legs,'entry_metrics':metrics,
            'metadata_id':fingerprint(metadata),
            'risk':dict(risk, trigger_basis='portfolio_net_pnl_cny',
                        stop_condition='net_pnl <= -max_loss_cny',
                        take_profit_condition='net_pnl >= take_profit_cny'),
            'limitations':[
                'EOD reference plan; refresh quotes before submitting live orders.',
                'Spread signal is not position PnL; stops do not guarantee maximum loss.',
                'Cross-product quality, region, tax and delivery terms are not interchangeable.',
            ],
        })
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        result['validation_errors'].append(str(exc))
    return result
