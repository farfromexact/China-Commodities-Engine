"""Pure Wind EOD normalization. No SDK imports or production routing yet.

2026-11-09 five-exchange canary established the legacy iFinD turnover
scale: Wind AMT / 10,000. Preserve the native amount and explicit units.
Do not apply this scale to unknown providers or historical AKShare records.
"""
import math


def normalize_eod(values, *, source_date, requested_date, code, price_unit, multiplier):
    if source_date != requested_date:
        raise ValueError('Wind source date mismatch')
    fields = ('open', 'high', 'low', 'close', 'settle', 'volume', 'oi', 'amt')
    for key in fields:
        v = values.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError('Invalid Wind field: '+key)
    if not price_unit or not isinstance(multiplier, (int, float)) or not math.isfinite(multiplier) or multiplier <= 0:
        raise ValueError('Missing Wind unit metadata')
    if any(values[k] < 0 for k in ('volume', 'oi', 'amt')):
        raise ValueError('Negative activity field')
    if not values['low'] <= min(values['open'], values['close']) <= max(values['open'], values['close']) <= values['high']:
        raise ValueError('Inconsistent OHLC')
    return dict(source_provider='wind', source_code=code, trade_date=requested_date,
                source_trade_date=source_date, source_date_match=True,
                **{k: values[k] for k in ('open','high','low','close','settle','volume')},
                open_interest=values['oi'], turnover=values['amt']/10000,
                turnover_unit='CNY_10000', source_turnover=values['amt'], source_turnover_unit='CNY',
                price_unit=price_unit, contract_multiplier=multiplier,
                volume_unit='contracts', open_interest_unit='contracts')
