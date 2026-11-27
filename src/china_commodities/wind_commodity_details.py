"""Dated Wind observations for the published commodity main contracts and core options."""
from collections import Counter
from datetime import date, datetime, time
import math
from pathlib import Path

from .index_analytics import finite, positive
from .storage import read_json, write_json_if_changed, write_json_gzip_if_changed
from .trading_calendar import previous_trading_day
from .wind_daily import ZONE


FIRST_DATE = '2026-11-26'
BOOK_FIELDS = 'bid1,ask1,bsize1,asize1'
RANK_FIELDS = {'long': 'long_position', 'short': 'short_position'}
DAY_OPTIONS = 'Days=Trading;Fill=Blank'


def main_contracts(futures):
    day = futures['trade_date']
    rows = {(r['exchange'], r['contract']): r for r in futures['futures_contracts']}
    result = {}
    for curve in futures['commodity_curves']:
        row = rows[curve['exchange'], curve['main_contract']['contract']]
        if (row['source_trade_date'] != day or row['source_provider'] != 'wind'
                or row['product'] != curve['product'] or row['source_code'] in result):
            raise ValueError('invalid dated main contract')
        result[row['source_code']] = {k: row[k] for k in ('exchange','product','contract','source_code')}
    return result


def daily_values(api, codes, field, day):
    """One dated value per instrument. Preserve actual zero, signed basis and blank separately."""
    if not codes:
        return {}
    args = (','.join(sorted(codes)), field, day, day, DAY_OPTIONS)
    raw = api.call('wsd', *args)
    try:
        if (raw['codes'] != sorted(codes) or [f.lower() for f in raw['fields']] != [field]
                or [t[:10] for t in raw['times']] != [day] or len(raw['data']) != 1
                or len(raw['data'][0]) != len(codes)):
            raise ValueError(f'invalid Wind {field} date, codes or shape')
        values = dict(zip(raw['codes'], raw['data'][0]))
        if any(v is not None and (not finite(v) or (field == 'st_stock' and v < 0))
               for v in values.values()):
            raise ValueError(f'invalid Wind {field} value')
    except (KeyError, ValueError):
        api.discard('wsd', *args)
        raise
    # A later user launch can check blanks without a stale negative cache hit.
    if None in values.values():
        api.discard('wsd', *args)
    return values


def member_list(api, code, side, day):
    metric = RANK_FIELDS[side]
    args = ('futureoir', f'startdate={day};enddate={day};wind_code={code};order_by={side};ranks=all')
    raw = api.call('wset', *args)
    if not raw['data'] or not any(raw['data']):
        api.discard('wset', *args)
        return []
    try:
        fields = [f.lower() for f in raw['fields']]
        required = {'date', 'ranks', 'member_name', metric, metric + '_increase'}
        if (not required.issubset(fields) or len(fields) != len(raw['data'])
                or len({len(col) for col in raw['data']}) != 1):
            raise ValueError('invalid member ranking fields or shape')
        rows = []
        for values in zip(*raw['data']):
            item = dict(zip(fields, values))
            if str(item['date'])[:10] != day:
                raise ValueError('member ranking source date mismatch')
            rank = item['ranks']
            if rank is None:  # Wind returns aggregate rows before its numbered members.
                continue
            if not positive(rank) or rank != int(rank):
                raise ValueError('invalid member rank')
            if rank > 20:
                continue
            name, quantity, change = item['member_name'], item[metric], item[metric + '_increase']
            if (not isinstance(name, str) or not name.strip() or not finite(quantity) or quantity < 0
                    or (change is not None and not finite(change))):
                raise ValueError('invalid ranked member or position')
            rows.append(dict(rank=int(rank), member=name.strip(), position=quantity, change=change))
        rows.sort(key=lambda row: row['rank'])
        validate_members(rows)
        if not rows:
            api.discard('wset', *args)
        return rows
    except (KeyError, ValueError):
        api.discard('wset', *args)
        raise


def last_tick(api, code, day, start, end):
    args = (code, BOOK_FIELDS, f'{day} {start}', f'{day} {end}', '')
    raw = api.call('wst', *args)
    try:
        fields = [f.lower() for f in raw['fields']]
        if (raw['codes'] != [code] or fields != BOOK_FIELDS.split(',') or len(raw['data']) != 4
                or any(len(column) != len(raw['times']) for column in raw['data'])):
            raise ValueError('invalid WST code, fields or shape')
        if not raw['times']:
            return None
        stamps = [datetime.fromisoformat(t) for t in raw['times']]
        stamps = [t.astimezone(ZONE).replace(tzinfo=None) if t.tzinfo else t for t in stamps]
        if (any(a > b for a, b in zip(stamps, stamps[1:]))
                or stamps[0] < datetime.fromisoformat(args[2]) or stamps[-1] > datetime.fromisoformat(args[3])):
            raise ValueError('WST tick outside requested date/window or out of sequence')
        # The longer window spans trading breaks. A session-break status tick is not a day quote.
        for idx in range(len(stamps) - 1, -1, -1):
            clock = stamps[idx].time()
            if day_quote_time(clock):
                return stamps[idx].replace(tzinfo=ZONE).isoformat(), [col[idx] for col in raw['data']]
        return None
    except (KeyError, ValueError):
        api.discard('wst', *args)
        raise


def option_book(api, code, day):
    tick = last_tick(api, code, day, '14:55:00', '15:00:00')
    if tick is None:
        tick = last_tick(api, code, day, '09:00:00', '14:55:00')
    timestamp, values = tick or (None, [None] * 4)
    bid, ask, bid_size, ask_size = values
    # Empty quote sides can be 0, NaN or the vendor's maximum-double sentinel.
    if not all(positive(v) and v < 1e300 for v in (bid, bid_size)):
        bid = bid_size = None
    if not all(positive(v) and v < 1e300 for v in (ask, ask_size)):
        ask = ask_size = None
    state = 'no_day_ticks' if not timestamp else 'no_displayed_side'
    if bid is not None and ask is not None:
        if bid <= ask:
            state = 'two_sided'
        else:
            state = 'crossed'
            bid = ask = bid_size = ask_size = None
    elif bid is not None or ask is not None:
        state = 'one_sided'
    return dict(book_state=state, book_source_endpoint='WindPy.wst', book_source_timestamp=timestamp,
                bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size)


def dated_path(root, day):
    return Path(root) / 'commodity_details' / 'snapshots' / (day + '.json.gz')


def coverage(products, books, contract_basis):
    return dict(expected_products=len(products),
                warehouse_products=sum(p['warehouse_quantity'] is not None for p in products),
                basis_products=sum(p['vendor_basis'] is not None for p in products),
                expected_basis_contracts=len(contract_basis),
                basis_contracts=sum(v is not None for v in contract_basis.values()),
                long_rank_products=sum(bool(p['long']) for p in products),
                short_rank_products=sum(bool(p['short']) for p in products),
                expected_option_contracts=len(books),
                option_book_states=dict(sorted(Counter(b['book_state'] for b in books.values()).items())))


def has_vendor_blanks(payload):
    counts = payload['coverage']
    return (counts['basis_contracts'] < counts['expected_basis_contracts']
            or any(counts[k] < counts['expected_products'] for k in
                   ('warehouse_products', 'long_rank_products', 'short_rank_products')))


def needs_collection(root, day, now):
    payload = read_json(dated_path(root, day), default={})
    if not payload:
        return True
    futures = read_json(Path(root) / 'snapshots' / (day + '.json'))
    options = read_json(Path(root) / 'options' / 'snapshots' / (day + '.json.gz'))
    try:
        validate(payload, day)
    except (KeyError, TypeError, ValueError):
        return True
    if (set(main_contracts(futures)) != {p['source_code'] for p in payload['products']}
            or set(payload['contract_basis']) != {r['source_code'] for r in futures['futures_contracts']}
            or set(payload['option_books']) != {r['source_code'] for r in options['records']}):
        return True
    return (has_vendor_blanks(payload) and not payload['checked_after_trade_date']
            and now.date().isoformat() > day)


def collect(api, day, root):
    from .option_storage import validate_option_snapshot
    from .wind_daily import eod_session_complete

    if day < FIRST_DATE or not eod_session_complete(day):
        raise ValueError('commodity detail collection requires an eligible completed day')
    root = Path(root)
    futures = read_json(root / 'snapshots' / (day + '.json'))
    options = read_json(root / 'options' / 'snapshots' / (day + '.json.gz'))
    if (futures['trade_date'] != day or options['trade_date'] != day or not futures['verified']
            or futures['source']['provider'] != 'wind' or options['source_provider'] != 'wind'):
        raise ValueError('commodity details require same-date Wind source snapshots')
    validate_option_snapshot(options)
    mains = main_contracts(futures)
    codes = {r['source_code'] for r in options['records'] if r['source_trade_date'] == day}
    if len(codes) != len(options['records']):
        raise ValueError('option detail contract identity or source date mismatch')
    prior_date = previous_trading_day(date.fromisoformat(day)).isoformat()
    old = read_json(dated_path(root, day), default={})
    if old:
        try:
            validate(old, day)
        except (KeyError, TypeError, ValueError):
            old = {}  # Rebuild from dated vendor responses in the isolated publication stage.
    saved = {r['source_code']: r for r in old.get('products', [])}
    products = [dict(**identity, warehouse_quantity=None, previous_warehouse_quantity=None,
                     vendor_basis=None, long=[], short=[]) for identity in mains.values()]
    for item in products:
        previous = saved.get(item['source_code'])
        if previous and all(previous[k] == item[k] for k in ('exchange','product','contract')):
            item.update(previous)
    for name, field, target in [('warehouse_quantity','st_stock',day),
                                ('previous_warehouse_quantity','st_stock',prior_date)]:
        missing = [r['source_code'] for r in products if r[name] is None]
        values = daily_values(api, missing, field, target)
        for item in products:
            if item['source_code'] in values:
                item[name] = values[item['source_code']]
    basis = {r['source_code']: old.get('contract_basis', {}).get(r['source_code'])
             for r in futures['futures_contracts']}
    missing_basis = sorted(code for code, value in basis.items() if value is None)
    for i in range(0, len(missing_basis), 200):
        basis.update(daily_values(api, missing_basis[i:i+200], 'anal_basis', day))
    for item in products:
        item['vendor_basis'] = basis[item['source_code']]
        for side in RANK_FIELDS:
            if not item[side]:
                item[side] = member_list(api, item['source_code'], side, day)
    saved_books = old.get('option_books', {})
    books = {code: saved_books[code] if code in saved_books else option_book(api, code, day)
             for code in sorted(codes)}
    payload = dict(schema_version=1, trade_date=day, source_provider='wind',
                   generated_at=datetime.now(ZONE).isoformat(), previous_trade_date=prior_date,
                   checked_after_trade_date=datetime.now(ZONE).date().isoformat() > day,
                   contract_scope='one_published_main_contract_per_product',
                   warehouse_field='WindPy.wsd.st_stock', warehouse_unit='Wind_native_product_specific',
                   basis_field='WindPy.wsd.anal_basis', basis_unit='underlying_contract_price_units',
                   ranking_scope='WindPy.wset.futureoir: separate top 20 longs and shorts; contracts',
                   book_scope='core_v1 last observed day-session tick through 15:00 Asia/Shanghai',
                   execution_ready=False, products=sorted(products, key=lambda r: r['source_code']),
                   option_books=books, contract_basis=basis, coverage=coverage(products, books, basis))
    validate(payload, day, set(mains), codes)
    write_json_gzip_if_changed(dated_path(root, day), payload)
    latest = root / 'commodity_details/latest.json.gz'
    if read_json(latest, default={}).get('trade_date', '') <= day:
        write_json_gzip_if_changed(latest, payload)
    return payload


def validate(payload, day, expected_mains=None, expected_options=None):
    products, books = payload['products'], payload['option_books']
    mains = {r['source_code'] for r in products}
    if (payload.get('schema_version') != 1 or payload.get('source_provider') != 'wind'
            or payload.get('trade_date') != day or not products or not books
            or len(mains) != len(products) or len({(r['exchange'],r['product']) for r in products}) != len(products)
            or (expected_mains is not None and mains != expected_mains)
            or (expected_options is not None and set(books) != expected_options)
            or coverage(products, books, payload['contract_basis']) != payload['coverage']):
        raise ValueError('invalid commodity detail coverage or date')
    basis = payload['contract_basis']
    if (payload['previous_trade_date'] != previous_trading_day(date.fromisoformat(day)).isoformat()
            or payload['execution_ready'] is not False
            or any(v is not None and not finite(v) for v in basis.values())):
        raise ValueError('invalid basis values, reference date or book policy')
    for item in products:
        if item['source_code'] not in basis or item['vendor_basis'] != basis[item['source_code']]:
            raise ValueError('main-contract basis differs from dated contract value')
        for key in ('warehouse_quantity', 'previous_warehouse_quantity'):
            if item[key] is not None and (not finite(item[key]) or item[key] < 0):
                raise ValueError('invalid warehouse quantity')
        for side in RANK_FIELDS:
            validate_members(item[side])
    for book in books.values():
        state, ts = book['book_state'], book['book_source_timestamp']
        if state not in {'two_sided','one_sided','crossed','no_displayed_side','no_day_ticks'}:
            raise ValueError('unknown option book state')
        if (ts is None) != (state == 'no_day_ticks') or book['book_source_endpoint'] != 'WindPy.wst':
            raise ValueError('missing option tick provenance')
        if ts:
            stamp = datetime.fromisoformat(ts)
            if (stamp.date().isoformat() != day or not ts.endswith('+08:00')
                    or not day_quote_time(stamp.time())):
                raise ValueError('invalid dated option tick')
        sides = 0
        for side in ('bid','ask'):
            px, size = book[side], book[side + '_size']
            if px is not None or size is not None:
                if not positive(px) or not positive(size) or max(px, size) >= 1e300:
                    raise ValueError('invalid option price or size')
                sides += 1
        if (sides != {'two_sided':2, 'one_sided':1}.get(state, 0)
                or (sides == 2 and book['bid'] > book['ask'])):
            raise ValueError('invalid option book state or spread')


def validate_members(rows):
    if (len(rows) > 20 or [r['rank'] for r in rows] != list(range(1, len(rows) + 1))
            or len({r['member'] for r in rows}) != len(rows)
            or any(not r['member'] or not finite(r['position']) or r['position'] < 0
                   or (r['change'] is not None and not finite(r['change'])) for r in rows)
            or any(a['position'] < b['position'] for a, b in zip(rows, rows[1:]))):
        raise ValueError('invalid member ranking order, identity or continuity')


def day_quote_time(clock):
    return (time(9) <= clock <= time(10,15) or time(10,30) <= clock <= time(11,30)
            or time(13,30) <= clock <= time(15))


def product_summaries(payload, futures):
    warehouse, bases, rankings = [], [], []
    day = payload['trade_date']
    # Test the sign against differences between actual closes on different months.
    # This does not identify the location, grade or price of an observed spot quote.
    prices = {}
    for row in futures['futures_contracts']:
        value = payload['contract_basis'][row['source_code']]
        if value is not None and positive(row['close']):
            prices.setdefault((row['exchange'],row['product']), []).append((row['close'], value))
    for item in payload['products']:
        base = {k:item[k] for k in ('source_code','contract','product','exchange')}
        base.update(trade_date=day, source_trade_date=day, source_provider='wind')
        quantity, previous = item['warehouse_quantity'], item['previous_warehouse_quantity']
        if quantity is not None:
            warehouse.append(dict(base, warehouse_quantity=quantity, warehouse_scope='product',
                previous_quantity=previous, previous_trade_date=payload['previous_trade_date'],
                warehouse_change=quantity-previous if previous is not None else None,
                source_unit=payload['warehouse_unit'], quantity_column='st_stock',
                source_endpoint='WindPy.wsd', aggregation_method='one_product_value',
                warehouse_quality='wind_reported_product_unit'))
        if item['vendor_basis'] is not None:
            pairs = prices.get((item['exchange'],item['product']), [])
            consistent = (len({p for p,_ in pairs}) > 1 and
                          all(math.isclose(p+b, sum(pairs[0]), rel_tol=1e-8, abs_tol=1e-7) for p,b in pairs))
            bases.append(dict(base, vendor_basis=item['vendor_basis'], source_field='anal_basis',
                basis_kind='proxy_basis', basis_quality='wind_reported_benchmark',
                basis_definition='Wind ANAL_BASIS in native sign', source_endpoint='WindPy.wsd',
                source_unit=payload['basis_unit'], spot_price=None, spot_grade=None, spot_region=None,
                sign_check='consistent_with_spot_minus_close_across_months' if consistent else 'unverified'))
        if item['long'] or item['short']:
            summary = dict(base, source_endpoint='WindPy.wset.futureoir', ranking_scope='contract',
                           ranking_reconciled=bool(item['long'] and item['short']), source_unit='contracts')
            for side in RANK_FIELDS:
                members = item[side]
                summary[side + '_member_count'] = len(members)
                summary['reported_' + side + '_open_interest'] = (
                    sum(r['position'] for r in members) if members else None)
                summary['reported_' + side + '_open_interest_change'] = (
                    sum(r['change'] for r in members) if members and all(r['change'] is not None for r in members)
                    else None)
            rankings.append(summary)
    return warehouse, bases, rankings


def publish_details(root, payload):
    """Join normalized observations into existing readers without fetching or redating core prices."""
    from .features import enrich_and_score_curves, select_candidates
    from .option_storage import publish_option_eod
    from .wind_daily_collect import preserve_newer_latest

    root, day = Path(root), payload['trade_date']
    future_path = root / 'snapshots' / (day + '.json')
    futures = read_json(future_path)
    options = read_json(root / 'options/snapshots' / (day + '.json.gz'))
    validate(payload, day, set(main_contracts(futures)), {r['source_code'] for r in options['records']})
    if set(payload['contract_basis']) != {r['source_code'] for r in futures['futures_contracts']}:
        raise ValueError('commodity basis universe differs from futures snapshot')
    warehouse, bases, rankings = product_summaries(payload, futures)
    for row in options['records']:
        row.update(payload['option_books'][row['source_code']])
    options['book_scope'] = payload['book_scope']
    with preserve_newer_latest(root, day):
        publish_option_eod(options, root, surface_shadow_days=1)
    curves = enrich_and_score_curves(futures['commodity_curves'], warehouse, bases,
                                     futures['commodity_options'], rankings)
    candidates = select_candidates(curves)
    rows_by_name = {'warehouse':warehouse,'basis':bases,'member_rankings':rankings}
    endpoints = {'warehouse':payload['warehouse_field'], 'basis':payload['basis_field'],
                 'member_rankings':'WindPy.wset.futureoir'}
    expected = len(payload['products'])
    status_rows = {}
    for name, rows in rows_by_name.items():
        count = len(rows)
        status_rows[name] = dict(dataset=name, scope='selected_main_per_product',state='ok' if count else 'empty',
            trade_date=day, requested_trade_date=day, source_trade_date=day if count else None,
            source_date_match=True if count else None, is_fresh=bool(count), is_proxy=name=='basis',
            is_fallback=False, records=count, expected_products=expected, source_function=endpoints[name],
            source_endpoint=endpoints[name], upstream_source='Wind Quant API', error=None,
            fetched_at=payload['generated_at'])
    def replace_statuses(rows):
        return [r for r in rows if r['dataset'] not in status_rows] + list(status_rows.values())

    quality = {name:('wind_reported' if len(rows)==expected else 'wind_partial' if rows else 'unavailable')
               for name,rows in rows_by_name.items()}
    quality['member_rankings'] = ('wind_reported' if len(rankings)==expected and
        all(r['ranking_reconciled'] for r in rankings) else 'wind_partial' if rankings else 'unavailable')
    reconciled = bool(rankings) and all(r['ranking_reconciled'] for r in rankings)
    futures['module_quality'].update(quality)
    futures['quality_metrics']['member_rankings_reconciled'] = reconciled
    futures['source']['modules'] = replace_statuses(futures['source']['modules'])
    futures.update(warehouse_inventory=warehouse, proxy_basis=bases, member_rankings=rankings,
                   commodity_curves=curves, heatmap_candidates=candidates,
                   commodity_details_coverage=payload['coverage'])
    write_json_if_changed(future_path, futures)
    if read_json(root/'latest.json', default={}).get('trade_date') == day:
        write_json_if_changed(root/'latest.json', futures)
        status = read_json(root/'last_run_status.json')
        status['modules'] = replace_statuses(status['modules'])
        status['module_quality'].update(quality)
        status['quality_metrics']['member_rankings_reconciled'] = reconciled
        write_json_if_changed(root/'last_run_status.json', status)
        radar = read_json(root/'radar_latest.json')
        radar['module_quality'].update(quality)
        radar['quality_metrics']['member_rankings_reconciled'] = reconciled
        radar.update(warehouse_inventory=warehouse, member_rankings=rankings, heatmap=candidates,
                     session_freshness=replace_statuses(radar['session_freshness']))
        enriched = {(r['exchange'],r['product']):r for r in curves}
        for row in radar['curve_basis']:
            found = enriched[row['exchange'],row['product']]
            row.update({k:found[k] for k in ('basis','evidence','evidence_count')})
        write_json_if_changed(root/'radar_latest.json', radar)


def collect_and_publish(api, day, root):
    payload = collect(api, day, root)
    publish_details(root, payload)
    return dict(mode='commodity_details', date=day, coverage=payload['coverage'])


def validate_join(root, futures):
    """Check that the latest futures and option readers expose the same dated observations."""
    root = Path(root)
    day = futures['trade_date']
    payload = read_json(root/'commodity_details/latest.json.gz')
    if payload is None and 'commodity_details_coverage' not in futures:
        return
    if not payload or day != payload.get('trade_date') or read_json(dated_path(root, day)) != payload:
        raise ValueError('commodity detail latest and dated snapshots disagree')
    options = read_json(root/'options/snapshots'/(day+'.json.gz'))
    validate(payload, day, set(main_contracts(futures)), {r['source_code'] for r in options['records']})
    if (futures.get('commodity_details_coverage') != payload['coverage']
            or set(payload['contract_basis']) != {r['source_code'] for r in futures['futures_contracts']}):
        raise ValueError('commodity detail coverage differs from futures snapshot')
    for rows, key in zip(product_summaries(payload, futures), ('warehouse_inventory','proxy_basis','member_rankings')):
        if futures[key] != rows:
            raise ValueError('commodity detail summary mismatch: ' + key)
    for row in options['records']:
        book = payload['option_books'][row['source_code']]
        if any(key not in row or row[key] != value for key,value in book.items()):
            raise ValueError('commodity option bid/ask differs from historical tick')
