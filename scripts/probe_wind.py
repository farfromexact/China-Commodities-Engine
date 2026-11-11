"""Bounded WindPy EOD canary; never publishes production snapshots.

Run with this repository's Python. SDK path is process-local. No subscriptions,
logout, automatic retries, or changes to another application's environment.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
import json
import math
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
FIELDS = "open,high,low,close,settle,volume,oi"


def normalize(result, requested_date):
    if result.ErrorCode:
        raise ValueError(f"Wind error {result.ErrorCode}: {result.Data}")
    fields = [str(x).lower() for x in result.Fields]
    if fields != FIELDS.split(",") or len(result.Data) != len(fields):
        raise ValueError("Unexpected Wind fields")
    if len(result.Times) != 1 or any(len(c) != 1 for c in result.Data):
        raise ValueError("Expected exactly one EOD observation")
    source_date = result.Times[0].strftime("%Y-%m-%d")
    if source_date != requested_date:
        raise ValueError(f"Source date mismatch: {source_date}")
    values = dict(zip(fields, [c[0] for c in result.Data]))
    if any(not isinstance(v, (float, int)) or not math.isfinite(v) for v in values.values()):
        raise ValueError("Missing or nonfinite EOD field")
    return {"source_date": source_date, "source_provider": "wind", "values": values,
            "units": "vendor_native; normalization not yet approved"}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--date", required=True, type=date.fromisoformat)
    p.add_argument("--codes", default="AU2612.SHF")
    p.add_argument("--sdk-dir", help="Official SDK folder; defaults to WIND_PYTHON_DIR/platform configuration")
    p.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    a = p.parse_args()
    codes = a.codes.split(",")
    if not 1 <= len(codes) <= 5:
        p.error("Canary accepts one to five explicit contracts")
    if not a.worker:
        try:
            return subprocess.run([sys.executable, __file__, *sys.argv[1:], "--worker"],
                                  timeout=90, check=False).returncode
        except subprocess.TimeoutExpired:
            print("Wind canary timed out; only its child process was stopped", file=sys.stderr)
            return 2
    sys.path.insert(0, str(ROOT / 'src'))
    from china_commodities.collectors.wind_runtime import load_wind
    w = load_wind(a.sdk_dir)
    report = {"requested_date": str(a.date), "generated_at": datetime.now().astimezone().isoformat(),
              "purpose": "isolated EOD canary; not production", "samples": []}
    try:
        connected = w.start(waitTime=15)
        if connected.ErrorCode:
            raise ValueError(f"Wind connection error {connected.ErrorCode}")
        for code in codes:
            started = time.monotonic()
            try:
                result = w.wsd(code, FIELDS, str(a.date), str(a.date), "")
                sample = {"code": code, "ok": True, **normalize(result, str(a.date))}
            except Exception as exc:
                sample = {"code": code, "ok": False, "error": str(exc)[:500]}
            sample["elapsed_seconds"] = round(time.monotonic() - started, 3)
            report["samples"].append(sample)
            if not sample["ok"]:
                break
            time.sleep(1)
    except Exception as exc:
        report["error"] = str(exc)[:500]
    # Deliberately no w.stop()/cancelRequest: another desktop uses this login.
    destination = ROOT / "tmp" / "wind-canary"
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".json")
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    print(f"Diagnostic report: {path}", flush=True)
    return 0 if len(report["samples"]) == len(codes) and all(x["ok"] for x in report["samples"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
