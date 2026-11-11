"""Validate and summarize a saved futurecc audit without supplier requests."""
import argparse
from collections import Counter
from datetime import date
import json
from pathlib import Path
import re


def summarize(items, catalog, day, previous):
    suffix = dict(SHFE='SHF', INE='INE', DCE='DCE', CZCE='CZC', GFEX='GFE')
    expected = {p+'.'+suffix[e] for e, products in catalog['exchanges'].items() for p in products}
    seen, contracts, errors, empty = set(), {}, [], []
    for item in items:
        label = item['label'].removeprefix('directory:')
        if label not in expected or label in seen:
            errors.append('unexpected/duplicate product '+label)
        seen.add(label)
        if item['error']:
            errors.append('API error '+label)
            continue
        fields, columns = item['fields'], item['data']
        if not fields and not columns:
            empty.append(label)
            continue
        if not columns or len(fields) != len(columns) or len({len(c) for c in columns}) != 1:
            errors.append('malformed directory '+label)
            continue
        if not columns[0]:
            errors.append('empty directory '+label)
        for values in zip(*columns):
            row = dict(zip(fields, values))
            code = row['wind_code']
            product, exchange = label.split('.')
            if not re.fullmatch(re.escape(product)+r'\d{3,4}\.'+re.escape(exchange), code):
                errors.append('unexpected contract '+code)
            if code in contracts:
                errors.append('duplicate contract '+code)
            if not (str(row['contract_issue_date'])[:10] <= day <= str(row['last_trade_date'])[:10]):
                errors.append('inactive contract '+code)
            contracts[code] = row
    old = {r['source_code'] for r in previous['futures_contracts'] if r.get('source_code')}
    return dict(date=day, expected_products=len(expected), queried_products=len(seen),
                missing_products=sorted(expected-seen), contract_count=len(contracts),
                empty_products=empty,
                exchanges=dict(Counter(c.split('.')[1] for c in contracts)), errors=errors,
                comparison_date=previous['trade_date'],
                added_since_previous=sorted(set(contracts)-old), missing_since_previous=sorted(old-set(contracts)),
                limitation='Catalog-scoped coverage; not independent discovery of newly listed products')


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('audit', type=Path)
    p.add_argument('--date', required=True, type=date.fromisoformat)
    a = p.parse_args()
    root = Path(__file__).resolve().parents[1]
    result = summarize(json.loads(a.audit.read_text()), json.loads((root/'config/products.json').read_text()),
                       str(a.date), json.loads((root/'data/latest.json').read_text()))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(int(bool(result['errors'] or result['missing_products'])))
