"""A dated five-minute Wind snapshot, kept separate from settlement/EOD data."""
from datetime import datetime
import math
from pathlib import Path

from .storage import read_json, write_json_if_changed
from .wind_daily import ZONE
from .wind_daily_collect import directory, option_selection, historical_rows

FIELDS = 'open,high,low,close,volume'


def stamp(value):
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=ZONE) if parsed.tzinfo is None else parsed.astimezone(ZONE)


def quote(api, code, window):
    start, cutoff = stamp(window['session_start']), stamp(window['cutoff'])
    args = (code, FIELDS, start.strftime('%Y-%m-%d %H:%M:%S'),
            cutoff.strftime('%Y-%m-%d %H:%M:%S'), 'BarSize=5;Fill=Blank')
    try:
        raw = api.call('wsi', *args)
    except ValueError as exc:
        if not str(exc).startswith('Wind wsi error '):
            raise
        return dict(record_state='query_error', error=str(exc), source_timestamp=None,
                    **{field: None for field in FIELDS.split(',')})
    try:
        if len(raw['data']) != len(FIELDS.split(',')):
            raise ValueError('intraday field count mismatch')
        bars = historical_rows(raw, FIELDS)
        seen = set()
        valid = []
        for bar in bars:
            timestamp = stamp(bar['timestamp'])
            if not start < timestamp <= cutoff or timestamp in seen:
                raise ValueError('intraday bar outside cutoff/session or duplicate timestamp')
            if timestamp.minute % 5 or timestamp.second or timestamp.microsecond:
                raise ValueError('intraday timestamp is not a completed five-minute bar')
            seen.add(timestamp)
            for field in FIELDS.split(','):
                value = bar[field]
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                          or not math.isfinite(value) or value < 0):
                    raise ValueError('invalid intraday price/volume')
            if bar['close'] is not None and bar['close'] > 0:
                if all(bar[k] is not None and bar[k] > 0 for k in ('open','high','low')):
                    if not bar['low'] <= min(bar['open'],bar['close']) <= max(bar['open'],bar['close']) <= bar['high']:
                        raise ValueError('invalid intraday OHLC range')
                valid.append((timestamp, bar))
        if not valid:
            api.discard('wsi', *args)
            return dict(record_state='missing', source_timestamp=None,
                        **{field: None for field in FIELDS.split(',')})
        timestamp, bar = max(valid, key=lambda item: item[0])
        return dict(record_state='quote', source_timestamp=timestamp.isoformat(),
                    lag_to_cutoff_seconds=int((cutoff-timestamp).total_seconds()),
                    **{field: bar[field] for field in FIELDS.split(',')})
    except (KeyError, ValueError):
        api.discard('wsi', *args)
        raise


def coverage(rows):
    return dict(expected=len(rows), quoted=sum(r['record_state']=='quote' for r in rows),
                missing=sum(r['record_state']=='missing' for r in rows),
                query_errors=sum(r['record_state']=='query_error' for r in rows),
                older_than_cutoff=sum(r.get('lag_to_cutoff_seconds',0)>0 for r in rows))


def collect_intraday(api, window, root, *, catalog=None, allowed_empty=('CZCE:LR',)):
    root = Path(root)
    day = window['trading_date']
    if stamp(window['cutoff']) <= stamp(window['session_start']):
        raise ValueError('开盘后尚无符合延迟 5 分钟要求的完整行情，请稍后重新启动。')
    universe, empty = directory(api, day, catalog=catalog, allowed_empty=allowed_empty)
    previous = read_json(root/'latest.json', default={})
    # Historical interest ranks underlyings only; it is never published as a current quote.
    prior = {(r['exchange'],r['contract']):r for r in previous.get('futures_contracts',[])}
    futures, selection_rows = [], []
    common = dict(trading_date=day, source_provider='wind', source_endpoint='WindPy.wsi',
                  settle=None, open_interest=None, greeks=None)
    for code, identity in sorted(universe.items()):
        row = dict(common, source_code=code, exchange=identity['exchange'], product=identity['product'],
                   contract=identity['contract'], **quote(api,code,window))
        futures.append(row)
        old = prior.get((row['exchange'],row['contract']),{})
        historical = str(old.get('source_trade_date') or previous.get('trade_date') or '')
        usable_prior = bool(historical and historical < day)
        selection_rows.append(dict(row, source_trade_date=day,
            open_interest=old.get('open_interest') if usable_prior else None,
            selection_open_interest_date=historical if usable_prior else None))
    if not any(r['record_state']=='quote' for r in futures):
        raise ValueError('Wind 未返回本次截止时间内的有效期货分钟行情，未发布。')
    selections, plans = option_selection(api, day, selection_rows, catalog=catalog,
                                        reference_field='close', allow_missing=True)
    options = []
    for product, selected, _ in selections:
        for identity in selected:
            options.append(dict(common, **identity, exchange=product.exchange, product=product.product,
                                **quote(api,identity['source_code'],window)))
    for rows in (futures, options):
        codes = [r['source_code'] for r in rows]
        if len(codes) != len(set(codes)):
            raise ValueError('duplicate intraday contract')
    snapshot = dict(schema_version=1, mode='intraday', intraday=True, published=True,
        validation_passed=True, source_provider='wind', source_endpoint='WindPy.wsi',
        **window, generated_at=datetime.now(ZONE).isoformat(), timezone='Asia/Shanghai',
        bar_size_minutes=5, delay_minutes=5, price_unit='Wind native quote unit per contract; no conversion',
        volume_unit='contracts in the selected five-minute bar; not cumulative daily volume',
        price_basis='last available completed five-minute bar within the current session and cutoff',
        unavailable_fields=['settle','open_interest','greeks'], options_scope='core_v1',
        selection_open_interest_basis='previous EOD, used only for underlying selection',
        declared_empty_products=empty, option_selection_plans=plans,
        coverage=dict(futures=coverage(futures), options=coverage(options),
                      missing_option_products=[p['product_key'] for p in plans if p.get('state')]),
        futures_contracts=futures, options_contracts=options)
    existing = read_json(root/'intraday/latest.json', default={})
    if existing.get('cutoff') and stamp(existing['cutoff']) > stamp(window['cutoff']):
        raise ValueError('已有更新的盘中快照，拒绝用较早截止时间覆盖。')
    write_json_if_changed(root/'intraday'/f'{day}.json', snapshot)
    write_json_if_changed(root/'intraday/latest.json', snapshot)
    return dict(mode='intraday', trading_date=day, cutoff=window['cutoff'], coverage=snapshot['coverage'])


def current_market_snapshot(root):
    """Report consumers can choose the freshest dataset without relabelling EOD fields."""
    root = Path(root)
    eod = read_json(root/'latest.json', default={})
    intraday = read_json(root/'intraday/latest.json', default={})
    day = eod.get('trade_date')
    eod_time = stamp(day+'T15:00:00+08:00') if day else None
    if (intraday.get('validation_passed') is True and intraday.get('cutoff')
            and (eod_time is None or stamp(intraday['cutoff']) > eod_time)):
        return dict(mode='intraday', path='data/intraday/latest.json', as_of=intraday['cutoff'],
                    trading_date=intraday['trading_date'], coverage=intraday['coverage'])
    return dict(mode='eod', path='data/latest.json', trading_date=day,
                as_of=eod_time.isoformat() if eod_time else None)
