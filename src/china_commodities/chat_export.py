"""Small, lossless JSON files for connector readers. No market-data requests."""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path


MAX_BYTES = 48 * 1024
SOURCES = {
    'index_derivatives': 'index_derivatives/latest.json',
    'index_derivatives_intraday': 'index_derivatives/intraday/latest.json',
    'index_radar': 'index_derivatives/radar_latest.json',
    'index_history': 'index_derivatives/radar_history.json',
    'index_books': 'index_derivatives/books/latest.json',
    'treasury_futures': 'treasury_futures/latest.json',
    'treasury_intraday': 'treasury_futures/intraday/latest.json',
    'funding': 'funding/latest.json',
    'funding_intraday': 'funding/intraday/latest.json',
    'report': 'report_input_latest.json',
    'futures': 'latest.json',
    'market_state': 'market_state_latest.json',
    'radar': 'radar_latest.json',
    'night': 'night_session/latest.json',
    'physical': 'physical/latest.json',
    'external': 'external/latest.json',
    'options_index': 'options/latest.json',
    'options_quality': 'options/quality_latest.json',
    'options_surface': 'options/surface_latest.json',
    'contract_meta': 'contract_meta.json',
    'crosscheck': 'crosschecks/futures_latest.json',
    'intraday': 'intraday/latest.json',
}


def encode(value):
    return (json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def fragments(value, path=()):
    """Only split oversized containers; each fragment keeps its exact JSON path."""
    item = {'path': list(path), 'value': value}
    if len(encode({'fragments': [item]})) <= MAX_BYTES:
        yield item
    elif isinstance(value, (dict, list)):
        yield {'path': list(path), 'container': 'object' if isinstance(value, dict) else 'array'}
        pairs = value.items() if isinstance(value, dict) else enumerate(value)
        for key, child in pairs:
            yield from fragments(child, (*path, key))
    else:
        raise ValueError(f'Chat export scalar exceeds {MAX_BYTES} bytes at {path}')


def product_keys(value):
    found = set()
    if isinstance(value, dict):
        if value.get('product_key'):
            found.add(str(value['product_key']))
        elif value.get('exchange') and value.get('product'):
            found.add(f"{value['exchange']}:{value['product']}")
        for child in value.values():
            found.update(product_keys(child))
    elif isinstance(value, list):
        for child in value:
            found.update(product_keys(child))
    return sorted(found)


def restore(parts):
    """Reassemble parsed fragments, including nulls, empty containers and list order."""
    root = None
    for part in parts:
        for item in part['fragments']:
            value = ({} if item['container'] == 'object' else []) if 'container' in item else item['value']
            path = item['path']
            if not path:
                root = value
                continue
            parent = root
            for key in path[:-1]:
                parent = parent[key]
            if isinstance(parent, list):
                if path[-1] != len(parent):
                    raise ValueError('Missing, duplicate or unordered chat array fragment')
                parent.append(value)
            else:
                if path[-1] in parent:
                    raise ValueError('Duplicate chat object fragment')
                parent[path[-1]] = value
    return root


def export_chat_data(data_dir='data'):
    root = Path(data_dir)
    target = root / 'chat'
    expected = set()

    def write(relative, value):
        raw = encode(value)
        if len(raw) > MAX_BYTES:
            raise ValueError(f'Chat file too large: {relative}')
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_bytes() != raw:
            temporary = path.with_suffix('.tmp')
            temporary.write_bytes(raw)
            temporary.replace(path)
        expected.add(path)
        return {'path': 'data/chat/' + relative, 'bytes': len(raw), 'sha256': sha(raw)}

    sources = dict(SOURCES)
    options_path = root / 'options/latest.json'
    if options_path.exists():
        options = json.loads(options_path.read_bytes())
        for entry in options.get('shards', []):
            source = (root / 'options' / entry['path']).resolve()
            if not source.is_relative_to((root / 'options').resolve()):
                raise ValueError('Option shard path escapes options directory')
            name = f"options_{entry['exchange']}_{entry['product']}"
            if name in sources:
                raise ValueError(f'Duplicate option shard: {name}')
            sources[name] = source.relative_to(root.resolve()).as_posix()

    indexes, missing = {}, []
    for name, relative in sources.items():
        source = root / relative
        if not source.exists():
            if name.startswith('options_') and name not in SOURCES:
                raise ValueError(f'Missing referenced option shard: {relative}')
            missing.append(relative)
            continue
        raw = source.read_bytes()
        payload = json.loads(gzip.decompress(raw) if source.suffix == '.gz' else raw)
        pages, current = [], []
        for item in fragments(payload):
            if current and len(encode({'fragments': current + [item]})) > MAX_BYTES:
                pages.append({'fragments': current})
                current = []
            current.append(item)
        if current:
            pages.append({'fragments': current})
        # One direct equivalence check: export must not lose or alter source fields.
        if restore(pages) != payload:
            raise ValueError(f'Chat export differs from source: {relative}')
        entries = []
        for number, page in enumerate(pages, 1):
            entry = write(f'{name}/part-{number:03d}.json', page)
            entry['sections'] = sorted({str(item['path'][0]) if item['path'] else '$' for item in page['fragments']})
            entry['product_keys'] = product_keys(page)
            entries.append(entry)
        source_bytes = raw if source.suffix == '.gz' else raw.replace(b'\r\n', b'\n')
        index = {
            'schema_version': 1, 'source_path': 'data/' + relative,
            'source_bytes': len(source_bytes), 'source_sha256': sha(source_bytes),
            'source_hash_basis': 'gzip_bytes' if source.suffix == '.gz' else 'utf8_json_with_lf_newlines',
            'content_sha256': sha(encode(payload)),
            'dates': {key: payload[key] for key in ('trade_date', 'trading_date', 'requested_date', 'generated_at', 'cutoff') if key in payload},
            'counts': {key: len(value) for key, value in payload.items() if isinstance(value, list)},
            'product_keys': product_keys(payload), 'parts': entries,
        }
        indexes[name] = write(f'{name}/index.json', index)

    report = json.loads((root / 'report_input_latest.json').read_bytes())
    overview = {key: report.get(key) for key in (
        'current_market_snapshot', 'requested_date', 'frequency', 'intraday', 'has_night_session',
        'execution_policy', 'module_freshness', 'futures', 'contract_metadata', 'index_derivatives','treasury_futures','funding')}
    # Keep overview small even in full-chain mode; full views remain losslessly
    # available in report and the separately indexed module sources.
    for name in ('index_derivatives','treasury_futures','funding'):
        view=report.get(name)
        if isinstance(view,dict):
            overview[name]={k:view[k] for k in ('available','eod_available','trade_date','expected_date',
                'expected_eod_date','date_matches','eod_date_matches','coverage','coverage_complete',
                'options_scope','validation_passed','current_market_snapshot','current_exchange_snapshot',
                'source_paths','source_path','limitations') if k in view}
    overview['limitations'] = report.get('quality', {}).get('limitations', [])
    overview['options_quality'] = report.get('quality', {}).get('options_quality', {})
    overview['product_keys'] = product_keys(report.get('products', []))
    overview_ref = write('overview.json', overview)
    manifest = {
        'schema_version': 1, 'format': 'json_path_fragments_v1', 'max_file_bytes': MAX_BYTES,
        'overview': overview_ref, 'sources': indexes, 'missing_sources': missing,
        'read_order': ['overview', 'report:products (all pages)', 'report:quality', 'candidate-specific source pages'],
        'commit_binding': 'Read manifest, indexes and parts from the same immutable Git commit; record that actual commit externally.',
        'notes': 'Lossless export, not new observations. Module dates and quality remain independent. Full source reconstruction uses all parts in index order.',
    }
    write('manifest.json', manifest)
    # Remove only obsolete generated JSON files inside this export directory.
    for path in target.rglob('*.json'):
        if path not in expected:
            path.unlink()
    return target / 'manifest.json'
