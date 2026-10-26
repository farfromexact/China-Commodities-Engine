"""Validate downstream candidate JSON against local trusted futures artifacts."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from china_commodities.execution import gate_card


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--metadata', type=Path, required=True)
    parser.add_argument('--candidates', type=Path, required=True)
    parser.add_argument('--as-of-date', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    def read(path):
        return json.loads(path.read_text(encoding='utf-8'))
    snapshot, metadata, candidates = read(args.snapshot), read(args.metadata), read(args.candidates)
    if not isinstance(candidates, list) or any(not isinstance(c, dict) for c in candidates):
        parser.error('candidates must be a JSON array of objects')
    results = [gate_card(c, snapshot, metadata, as_of_date=args.as_of_date) for c in candidates]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    return 1 if any(r['execution'] is None for r in results) else 0


if __name__ == '__main__':
    raise SystemExit(main())
