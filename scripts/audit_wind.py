"""Read-only, bounded Wind migration evidence; diagnostics only."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--mode', choices=['sample', 'directory', 'followup', 'options', 'amounts'], default='sample')
    p.add_argument('--worker', action='store_true')
    a = p.parse_args()
    if not a.worker:
        try:
            return subprocess.run([sys.executable, __file__, *sys.argv[1:], '--worker'], timeout=240).returncode
        except subprocess.TimeoutExpired:
            print('Audit child timed out; inspect partial diagnostics.')
            return 2
    sys.path.insert(0, '/Applications/Wind API.app/Contents/python')
    from WindPy import w
    out = ROOT / 'tmp/wind-canary' / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-audit.json')
    out.parent.mkdir(parents=True, exist_ok=True)
    evidence = []
    def call(label, method, *args):
        started = time.monotonic()
        r = getattr(w, method)(*args)
        item = dict(label=label, method=method, args=args, error=int(r.ErrorCode),
                    fields=list(r.Fields), codes=list(r.Codes), times=list(r.Times), data=r.Data,
                    seconds=round(time.monotonic()-started, 3))
        evidence.append(item)
        out.write_text(json.dumps(evidence, default=str, ensure_ascii=False, indent=2))
        print(json.dumps({k:item[k] for k in ['label','error','fields','seconds']}, ensure_ascii=False), flush=True)
        time.sleep(1)
        return r
    r = w.start(waitTime=15)
    if r.ErrorCode:
        print('Connection failed', r.ErrorCode)
        return 1
    if a.mode == 'sample':
        call('units', 'wss', 'AU2612.SHF,SC2612.INE,M2701.DCE,SA701.CZC,SI2701.GFE',
             'contractmultiplier,priceunit', '')
        call('amount', 'wsd', 'AU2612.SHF', 'low,high,volume,amt', '2026-11-11', '2026-11-11', '')
        chain = call('option_chain', 'wset', 'optionchain', 'date=2026-11-11;us_code=AU2612.SHF;option_var=all;call_put=all')
        if not chain.ErrorCode and 'option_code' in [f.lower() for f in chain.Fields]:
            codes = chain.Data[[f.lower() for f in chain.Fields].index('option_code')][:2]
            for code in codes:
                call('option_eod:'+code, 'wsd', code, 'close,settle,volume,oi', '2026-11-11', '2026-11-11', '')
            if codes:
                call('option_greeks', 'wss', ','.join(codes), 'us_impliedvol,delta,gamma', 'tradeDate=20261111')
        call('night', 'wsi', 'AU2612.SHF', 'open,high,low,close,volume',
             '2026-11-10 21:00:00', '2026-11-10 21:15:00', 'BarSize=5')
    elif a.mode == 'followup':
        for field in ['contractmultiplier', 'punit']:
            call(field, 'wss', 'AU2612.SHF,SC2612.INE,M2701.DCE,SA701.CZC,SI2701.GFE', field, '')
        for code in ['AU2612.SHF','SC2612.INE','M2701.DCE','SA701.CZC','SI2701.GFE']:
            call('compare:'+code, 'wsd', code, 'close,settle,volume,oi,amt', '2026-11-09','2026-11-09','')
        call('atm_greeks', 'wss', 'AU2612C872.SHF,AU2612P872.SHF', 'us_impliedvol,delta,gamma', 'tradeDate=20261111')
        call('night_midnight', 'wsi','AU2612.SHF','close,volume','2026-11-10 23:55:00','2026-11-11 00:10:00','BarSize=5')
    elif a.mode == 'amounts':
        for code in ['AU2612.SHF','SC2612.INE','M2701.DCE','SA701.CZC','SI2701.GFE']:
            call('amount:'+code, 'wsd', code, 'close,settle,volume,oi,amt', '2026-11-11','2026-11-11','')
    elif a.mode == 'options':
        for underlying, target in [('AU2612.SHF',870.5), ('SC2612.INE',598.1),
                                   ('M2701.DCE',3137), ('SA701.CZC',968), ('SI2701.GFE',8490)]:
            chain = call('chain:'+underlying, 'wset', 'optionchain',
                         'date=2026-11-11;us_code='+underlying+';option_var=all;call_put=all')
            if chain.ErrorCode or not chain.Data:
                continue
            rows = [dict(zip(chain.Fields, values)) for values in zip(*chain.Data)]
            selected = []
            for side in ['认购','认沽']:
                candidates = [r for r in rows if r.get('call_put') == side]
                if candidates:
                    selected.append(min(candidates, key=lambda r:abs(float(r['strike_price'])-target))['option_code'])
            for code in selected:
                call('dated:'+code, 'wsd', code, 'close,settle,volume,oi,us_impliedvol,delta,gamma',
                     '2026-11-09','2026-11-11','Days=Trading;Fill=Blank')
            for day in ['20261109','20261111']:
                if selected:
                    call('greeks:'+underlying+':'+day, 'wss', ','.join(selected),
                         'us_impliedvol,delta,gamma', 'tradeDate='+day)
    else:
        catalog = json.loads((ROOT/'config/products.json').read_text())
        suffixes = dict(SHFE='SHF', INE='INE', DCE='DCE', CZCE='CZC', GFEX='GFE')
        for exchange, products in catalog['exchanges'].items():
            for product in products:
                code = product+'.'+suffixes[exchange]
                r = call('directory:'+code, 'wset', 'futurecc',
                         'startdate=2026-11-11;enddate=2026-11-11;wind_code='+code)
                if r.ErrorCode:
                    return 1
    print('Evidence:', out, flush=True)
    return int(any(x['error'] for x in evidence))


if __name__ == '__main__':
    raise SystemExit(main())
