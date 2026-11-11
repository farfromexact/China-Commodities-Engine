"""Independent official files against saved Wind diagnostics; no Wind calls."""
import argparse
import json
from pathlib import Path
from china_commodities.collectors.exchange_eod_adapter import ExchangeEODClient, normalize_document, contract_key


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--date', default='2026-11-11')
    p.add_argument('--directory', type=Path, required=True)
    a = p.parse_args()
    root = Path(__file__).resolve().parents[1]
    out = root/'tmp/wind-canary/official'
    client = ExchangeEODClient(out/'raw', timeout=12)
    directory = json.loads(a.directory.read_text())
    suffix = dict(SHFE='SHF', INE='INE', DCE='DCE', CZCE='CZC', GFEX='GFE')
    results = []
    for exchange in suffix:
        for kind in ['futures','options']:
            try:
                doc = client.fetch(exchange, kind, a.date)
                rows = normalize_document(doc)
                item = dict(exchange=exchange, kind=kind, rows=rows, evidence=doc.evidence)
                if kind == 'futures':
                    codes = set()
                    for d in directory:
                        if d['label'].endswith('.'+suffix[exchange]) and 'wind_code' in d['fields']:
                            codes.update(contract_key(c) for c in d['data'][d['fields'].index('wind_code')])
                    official = {contract_key(r['contract']) for r in rows}
                    item.update(wind_only=sorted(codes-official), official_only=sorted(official-codes))
                print(exchange, kind, len(rows), 'rows', flush=True)
            except Exception as exc:
                item = dict(exchange=exchange, kind=kind, error=str(exc)[:500])
                print(exchange, kind, item['error'], flush=True)
            results.append(item)
            out.mkdir(parents=True, exist_ok=True)
            (out/'audit.json').write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str))
    return int(any('error' in r for r in results))


if __name__ == '__main__':
    raise SystemExit(main())
