"""Run an independent commodity EOD backup without any iFinD credentials."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from china_commodities.exchange_backup import collect_backup, load_baseline, publish_backup
from china_commodities.collectors.exchange_eod_adapter import ReplayExchangeEODClient
from china_commodities.option_universe import normalize_openctp_option_directory


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="Completed exchange trading date (YYYY-MM-DD)")
    parser.add_argument("--output", type=Path, default=Path("data/backup"))
    parser.add_argument("--baseline-dir", type=Path, default=Path("data"))
    parser.add_argument("--replay-raw", type=Path, help="Rebuild from archived date directory, without network access")
    parser.add_argument("--risk-free-rate", type=float, help="Optional explicit annual decimal model assumption")
    parser.add_argument("--rate-source", help="Required with --risk-free-rate; distinguish a scenario from an observed rate")
    parser.add_argument("--model-workers", type=int, choices=range(1, 5), default=1)
    parser.add_argument("--require-80", action="store_true", help="Exit 2 if global core product coverage is below 80%%")
    args = parser.parse_args(argv)
    if args.risk_free_rate is not None and not args.rate_source:
        parser.error("--risk-free-rate requires --rate-source")
    client = ReplayExchangeEODClient(args.replay_raw) if args.replay_raw else None
    metadata_loader = None
    if args.replay_raw:
        def metadata_loader(day, products):
            path = args.replay_raw / "openctp-metadata.json.gz"
            raw = gzip.decompress(path.read_bytes())
            audit = json.loads(path.with_suffix(".audit.json").read_text(encoding="utf-8"))
            if hashlib.sha256(raw).hexdigest() != audit["sha256"]:
                raise ValueError("metadata archive checksum mismatch")
            return normalize_openctp_option_directory(json.loads(raw)["data"], trade_date=day, option_products=products)
    snapshot = collect_backup(args.date, args.output, client=client, metadata_loader=metadata_loader,
                              risk_free_rate=args.risk_free_rate, rate_source=args.rate_source,
                              model_workers=args.model_workers,
                              progress=lambda message: print(message, flush=True))
    report = publish_backup(snapshot, args.output, baseline=load_baseline(args.baseline_dir, args.date))
    print(json.dumps({"trade_date": args.date, "coverage": report["coverage"],
                      "capabilities": report["capabilities"], "comparison": report["comparison"],
                      "report": str((args.output / "last_run_status.json").resolve())}, ensure_ascii=False, indent=2))
    return 2 if args.require_80 and not report["coverage"]["publish_eligible"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
