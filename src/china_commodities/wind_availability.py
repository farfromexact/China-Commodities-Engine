"""Field availability for identity- and date-checked Wind observations."""
from .index_analytics import finite


class WindUnavailable(ValueError):
    """Wind supplied no usable observation for this request."""


FUTURES_FIELDS = ('open', 'high', 'low', 'close', 'settle', 'pre_settle', 'volume', 'open_interest', 'turnover')
OPTION_FIELDS = ('close', 'settle', 'pre_settle', 'volume', 'open_interest')


def numeric_quote(quote, fields, positive=()):
    for field in fields:
        value = quote[field]
        if not finite(value) or value < 0 or (field in positive and value == 0):
            quote[field] = None
    return quote


def field_coverage(*groups):
    count = complete = 0
    missing = {}
    for rows, fields in groups:
        for row in rows:
            absent = [field for field in fields if row.get(field) is None]
            count += 1
            complete += not absent
            for field in absent:
                missing.setdefault(field, []).append(row['source_code'])
    return dict(status='partial' if missing else 'complete', contract_count=count,
                complete_contract_count=complete, missing_by_field={k:sorted(v) for k,v in missing.items()})


def valid_fields(row, fields, partial):
    return all(field in row and ((partial and row[field] is None)
               or (finite(row[field]) and row[field] >= 0)) for field in fields)


def no_dated_quote(fields):
    return dict.fromkeys(fields.split(',')) | dict(source_trade_date=None,source_date_match=False,
        record_state='no_dated_quote',source_date_basis='dated contract directory; no WindPy.wsd observation')


def declared_undated(row, fields):
    return (row.get('record_state')=='no_dated_quote' and row.get('source_trade_date') is None
            and row.get('source_date_match') is False and all(row.get(k) is None for k in fields))


def repair_fields(api, day, root, module):
    """Read only missing values on a subsequent launch and preserve all present values."""
    from datetime import date, datetime
    from pathlib import Path
    from . import wind_index, wind_treasury
    from .storage import read_json, write_json_if_changed
    from .trading_calendar import previous_trading_day
    from .wind_daily import ZONE
    from .wind_daily_collect import batched_eod

    index=module=='index_derivatives'
    directory=Path(root)/module
    path=directory/'snapshots'/f'{day}.json'
    snapshot=read_json(path)
    validate=wind_index.validate if index else wind_treasury.validate
    if snapshot['trade_date']!=day or validate(snapshot):
        raise ValueError('invalid input for dated field repair')
    futures=snapshot['futures_contracts'] if index else snapshot['contracts']
    groups=[(futures,FUTURES_FIELDS)]
    if index:
        groups.append((snapshot['options_contracts'],OPTION_FIELDS))
    container=snapshot['coverage'] if index else snapshot
    rows={r['source_code']:r for records,_ in groups for r in records}
    futures_codes={r['source_code'] for r in futures}
    changes=0
    for field,codes in container['field_availability']['missing_by_field'].items():
        native={'open_interest':'oi','turnover':'amt'}.get(field,field)
        # A cached negative answer is obsolete on a new explicit launch.
        for offset in range(0,len(codes),200):
            api.discard('wsd',','.join(codes[offset:offset+200]),native,day,day,'Days=Trading;Fill=Blank')
        values=batched_eod(api,codes,native,day,undated=set())
        for code,quote in values.items():
            positive=('close','settle','pre_settle') if code in futures_codes else ()
            value=numeric_quote(quote,(native,),positive)[native]
            if value is None:
                continue
            row=rows[code]
            if row[field] is not None:
                raise ValueError('field repair would replace a valid observation')
            row[field]=value
            if row.get('record_state')=='no_dated_quote':
                del row['record_state']
                row.update(source_trade_date=day,source_date_match=True,source_date_basis='WindPy.wsd.Times')
            changes+=1
    if changes:
        previous=read_json(directory/'snapshots'/f'{previous_trading_day(date.fromisoformat(day))}.json',default={})
        prior={r['source_code']:r.get('open_interest') for r in previous.get('futures_contracts' if index else 'contracts',[])}
        for r in futures:
            close,settle=r['close'],r['pre_settle']
            if index:
                r['close_vs_pre_settle_pct']=(close/settle-1)*100 if close is not None and settle else None
            else:
                r['close_change_points']=close-settle if close is not None and settle is not None else None
            before=prior.get(r['source_code'])
            r['oi_change']=r['open_interest']-before if finite(before) and finite(r['open_interest']) else None
        if not index:
            snapshot['products']=wind_treasury.summary(futures)
        container['field_availability']=field_coverage(*groups)
        snapshot['fields_updated_at']=datetime.now(ZONE).isoformat()
        if validate(snapshot):
            raise ValueError('repaired Wind fields failed validation')
        write_json_if_changed(path,snapshot)
        if read_json(directory/'latest.json',default={}).get('trade_date')==day:
            write_json_if_changed(directory/'latest.json',snapshot)
    return dict(mode='field_repair',date=day,updated_fields=changes)
