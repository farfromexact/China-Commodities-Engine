"""Wind cash indices, raw ETF shares, and separately dated order-book observations."""
from datetime import date, datetime, time

from .index_analytics import finite, positive
from .trading_calendar import previous_trading_day

INDICES = {'IH':'000016.SH','IF':'000300.SH','IC':'000905.SH','IM':'000852.SH'}
ETFS = {'IH':'510050.SH','IF':'510300.SH','IC':'510500.SH','IM':'512100.SH'}
BOOK_FIELDS = 'rt_last,rt_bid1,rt_ask1,rt_bsize1,rt_asize1,rt_date,rt_time,rt_vol'


def cash_market(api, day):
    from .wind_index import daily_quote
    previous = str(previous_trading_day(date.fromisoformat(day)))
    payload = dict(trade_date=day,source_provider='wind',indices={},etfs={},errors=[])
    for product, code in INDICES.items():
        try:
            q = daily_quote(api,code,'close,pre_close,amt',day)
            if not positive(q['close']):
                raise ValueError('missing cash index close')
            payload['indices'][product] = dict(source_code=code,source_trade_date=day,source_endpoint='WindPy.wsd',
                close=q['close'],pre_close=q['pre_close'],turnover=q['amt'],turnover_unit='CNY',status='ok')
        except ValueError as exc:
            payload['indices'][product] = dict(source_code=code,status='missing',close=None,error=str(exc))
            payload['errors'].append(f'{code}: {exc}')
        etf = ETFS[product]
        item = dict(source_code=etf,source_trade_date=day,source_endpoint='WindPy.wsd',
            share_field='unit_total',share_unit='Wind_native_unverified',share_unit_verified=False,
            total_shares=None,share_change=None,estimated_net_creation_redemption_cny=None,
            flow_method='share_change * market_close; disabled until share unit verified')
        args = (etf,'unit_total',previous,day,'Days=Trading;Fill=Blank')
        try:
            from .wind_daily_collect import historical_rows
            rows = historical_rows(api.call('wsd',*args),'unit_total')
            values = {r['timestamp'][:10]:r['unit_total'] for r in rows}
            if len(rows)!=2 or set(values)!={previous,day} or not all(positive(v) for v in values.values()):
                api.discard('wsd',*args)
                raise ValueError('ETF share dates, shape, or values incomplete')
            q = daily_quote(api,etf,'close',day)
            if not positive(q['close']):
                raise ValueError('ETF close missing')
            item.update(status='unit_pending',close=q['close'],price_unit='CNY_per_fund_unit',
                total_shares_raw=values[day],previous_total_shares_raw=values[previous],
                previous_date=previous,share_change_raw=values[day]-values[previous],
                share_change_pct=values[day]/values[previous]-1,share_change_pct_unit='fraction')
        except ValueError as exc:
            item.update(status='missing',total_shares_raw=None,previous_total_shares_raw=None,
                        share_change_raw=None,previous_date=None,error=str(exc))
            payload['errors'].append(f'{etf}: {exc}')
        payload['etfs'][product] = item
    payload['status'] = 'raw_shares_unit_pending' if not payload['errors'] else 'partial'
    return payload


def parse_book(code, values, requested_day, observed_at):
    """WSQ Times is request time. Keep vendor date/time separate and unverified."""
    result = dict(source_code=code,source_provider='wind',source_endpoint='WindPy.wsq',
        observed_at=observed_at,raw_fields=values,quote_date=None,vendor_clock=None,
        bid=values.get('rt_bid1'),ask=values.get('rt_ask1'),bid_size=values.get('rt_bsize1'),
        ask_size=values.get('rt_asize1'),last=values.get('rt_last'),volume=values.get('rt_vol'),
        time_verified=False,eligible_for_eod_analytics=False,execution_ready=False,
        timestamp_basis='RT_DATE_and_RT_TIME_unverified_vendor_clock')
    try:
        raw_day, raw_time = values['rt_date'], values['rt_time']
        if not finite(raw_day) or int(raw_day)!=raw_day or not finite(raw_time) or int(raw_time)!=raw_time:
            raise ValueError('invalid date/time')
        quote_date = datetime.strptime(str(int(raw_day)),'%Y%m%d').date()
        clock = datetime.strptime(f'{int(raw_time):06d}','%H%M%S').time()
        result.update(quote_date=str(quote_date),vendor_clock=clock.isoformat())
        if str(quote_date)!=requested_day:
            state = 'stale_or_different_date'
        elif not (time(9,30)<=clock<=time(11,30) or time(13)<=clock<=time(15)):
            state = 'out_of_session_vendor_clock'
        elif (not positive(result['bid']) or not positive(result['ask']) or result['bid']>result['ask']
              or not positive(result['bid_size']) or not positive(result['ask_size'])):
            state = 'invalid_book'
        else:
            state = 'valid_values_time_unverified'
        if positive(result['bid']) and positive(result['ask']) and result['ask']>=result['bid']:
            mid = (result['bid']+result['ask'])/2
            result['mid'] = mid if (result['ask']-result['bid'])/mid<=.60 else None
    except (KeyError,TypeError,ValueError,OverflowError):
        state = 'missing_or_invalid_vendor_time'
    result['record_state'] = state
    return result


def books(api, codes, day):
    from .wind_daily import ZONE, eod_session_complete
    now = datetime.now(ZONE)
    latest = str(now.date() if eod_session_complete(str(now.date()),now) else previous_trading_day(now.date()))
    result = dict(requested_trade_date=day,source_provider='wind',records=[],errors=[],
        generated_at=now.isoformat(),batches=[],
        purpose='separate_vendor_snapshot_not_15_00_EOD',time_verified=False,execution_ready=False)
    if day!=latest:
        result['status']='not_requested_for_historical_day'
        return result
    for offset in range(0,len(codes),50):
        batch = codes[offset:offset+50]
        args = (','.join(batch),BOOK_FIELDS,'')
        # A WSQ observation must never reuse a previous launch's request cache.
        api.discard('wsq',*args)
        requested_at = datetime.now(ZONE).isoformat()
        try:
            raw = api.call('wsq',*args)
            received_at = datetime.now(ZONE).isoformat()
            result['batches'].append(dict(requested_at=requested_at,received_at=received_at,
                response_times=raw.get('times',[]),response_time_basis='response_metadata_not_quote_time',codes=batch))
            fields = [str(f).lower() for f in raw['fields']]
            if (raw['codes']!=batch or fields!=BOOK_FIELDS.split(',') or len(raw['data'])!=len(fields)
                    or any(len(c)!=len(batch) for c in raw['data'])):
                raise ValueError('order book response identity or shape mismatch')
            for code,values in zip(batch,zip(*raw['data'])):
                result['records'].append(parse_book(code,dict(zip(fields,values)),day,datetime.now(ZONE).isoformat()))
        except (ValueError,AttributeError,RuntimeError,OSError) as exc:
            result['errors'].append(str(exc))
            break  # no repeated failing requests across the rest of the chain
        finally:
            api.discard('wsq',*args)
    result.update(status='observed_time_unverified' if len(result['records'])==len(codes) else 'partial',
                  requested_contract_count=len(codes),observed_contract_count=len(result['records']))
    return result
