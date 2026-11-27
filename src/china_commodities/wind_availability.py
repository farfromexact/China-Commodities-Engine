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
