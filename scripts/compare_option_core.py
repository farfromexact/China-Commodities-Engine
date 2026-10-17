"""Run one isolated live core collection and compare with stored full chains.

Never publishes to data/, never re-requests an old full chain. Credential values
are neither written nor printed. Only quote payloads and request sizes are saved.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import json
from pathlib import Path
import time

from china_commodities.catalog import load_catalog
from china_commodities.collectors.ifind_http_adapter import IFindHTTPClient
from china_commodities.collectors.ifind_option_adapter import _response_rows
from china_commodities.option_batch import collect_option_market_snapshot
from china_commodities.option_core import CORE_PROFILE, CoreOptionCollector
from china_commodities.option_storage import read_option_latest, publish_option_eod
from china_commodities.option_surface import build_option_surface
from china_commodities.reporting import publish_report_input
from china_commodities.storage import read_json, write_json_if_changed, write_json_gzip_if_changed


class MeasuredClient(IFindHTTPClient):
    def __init__(self):
        super().__init__(minimum_request_interval_seconds=.55)
        self.requests = []
        self.quotes = []

    def request(self, endpoint, payload):
        codes = str(payload.get("codes") or "").split(",")
        indicators = str(payload.get("indicators") or "").split(",")
        entry = {"endpoint": endpoint, "codes": codes,
                 "contract_count": len(codes), "indicator_count": len(indicators),
                 "requested_field_cells": len(codes)*len(indicators)}
        self.requests.append(entry)
        started = time.monotonic()
        try:
            result = super().request(endpoint, payload)
            rows = _response_rows(result)
            entry.update(status="success", returned_rows=len(rows))
            # The quotation endpoint contains market data, never token headers.
            self.quotes.extend({k: v for k, v in r.items()
                                if k in {"thscode", "time", *indicators}} for r in rows)
            return result
        except Exception as exc:
            entry.update(status="failed", error_type=type(exc).__name__)
            raise
        finally:
            entry["seconds"] = round(time.monotonic()-started, 3)


def grouped(rows):
    groups = defaultdict(list)
    for r in rows:
        groups[(r.get("exchange"), r.get("product"))].append(r)
    return groups


def compare(baseline, current):
    old, new = baseline.get("records", []), current.get("records", [])
    old_by_code = {(r["exchange"], r["contract"]): r for r in old}
    matched = [old_by_code[(r["exchange"], r["contract"])] for r in new
               if (r["exchange"], r["contract"]) in old_by_code]
    def valid_sum(rows, field):
        values = [r[field] for r in rows if isinstance(r.get(field), (int, float))]
        return sum(values), len(values)
    def series(s):
        return {(r["exchange"], r["product"], r["underlying_contract"], r.get("expiry_date")): r
                for r in build_option_surface(s)["surfaces"]}
    old_series, new_series = series(baseline), series(current)
    atm_differences = []
    per_series = []
    for key, n in new_series.items():
        o = old_series.get(key, {})
        before, after = o.get("atm_iv_percent"), n.get("atm_iv_percent")
        diff = after-before if before is not None and after is not None else None
        if diff is not None:
            atm_differences.append(abs(diff))
        per_series.append({"key": list(key), "baseline_atm_iv": before, "live_atm_iv": after,
                           "difference_iv_points": diff,
                           "baseline_rr25": o.get("risk_reversal_25d_iv_points"),
                           "live_rr25": n.get("risk_reversal_25d_iv_points")})
    coverage = {}
    for f in ("volume", "open_interest"):
        numerator, matched_count = valid_sum(matched, f)
        denominator, baseline_count = valid_sum(old, f)
        coverage[f] = {"baseline_observed_sum": denominator,
                       "retained_baseline_observed_sum": numerator,
                       "retained_share": numerator/denominator if denominator else None,
                       "baseline_nonnull": baseline_count, "matched_nonnull": matched_count}
    differences = Counter()
    comparable = Counter()
    for r in new:
        o = old_by_code.get((r["exchange"], r["contract"]))
        if not o:
            continue
        for f in ("settle", "close", "volume", "open_interest", "iv_percent", "delta"):
            if isinstance(o.get(f), (int, float)) and isinstance(r.get(f), (int, float)):
                comparable[f] += 1
                if abs(r[f]-o[f]) > max(1e-8, abs(o[f])*1e-6):
                    differences[f] += 1
    return {"baseline_date": baseline.get("trade_date"), "current_date": current.get("trade_date"),
            "same_trade_date": baseline.get("trade_date") == current.get("trade_date"),
            "baseline_contracts": len(old), "core_contracts": len(new),
            "baseline_products": len(grouped(old)), "core_products": len(grouped(new)),
            "baseline_series": len(old_series), "core_series": len(new_series),
            "matched_contracts": len(matched), "retained_observed_activity": coverage,
            "comparable_field_counts": dict(comparable), "changed_field_counts": dict(differences),
            "comparable_atm_series": len(atm_differences),
            "atm_absolute_difference_max": max(atm_differences, default=None),
            "atm_absolute_difference_mean": sum(atm_differences)/len(atm_differences) if atm_differences else None,
            "per_series": per_series,
            "warning": "Different retrieval times and any date mismatch confound price changes; retained shares use baseline observations only. Missing OI is not zero."}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("tmp/core-live-trial/output"))
    args = parser.parse_args()
    output = args.output.resolve()
    source = Path("data").resolve()
    if output == source or source in output.parents or output in source.parents:
        raise ValueError("trial output must be isolated from production data")
    output.mkdir(parents=True, exist_ok=True)
    futures = read_json(source / "latest.json")
    day = futures["trade_date"]
    previous = read_option_latest(source)
    attempt = read_json(source / "options/attempt_latest.json.gz", default={}) or {}
    collector = CoreOptionCollector(futures_snapshot=futures, previous_snapshot=previous,
                                    force_refresh=True)
    client = MeasuredClient()
    # Fail before any directory downloads if the existing credential is unusable.
    client.get_access_token()
    started = time.monotonic()
    def collect_one(*a, **kw):
        u = kw["universes"][0]
        print(f"Collecting {u.exchange}:{u.product}", flush=True)
        return collector(*a, **kw)
    snapshot, status = collect_option_market_snapshot(
        day, client=client, option_products=load_catalog().options,
        minimum_product_coverage=0, collect_one=collect_one,
        enrich_missing_metadata=True, collection_scope=CORE_PROFILE,
    )
    plans = list(collector.plans.values())
    status["collection_plans"] = plans
    status["published"] = False
    if snapshot:
        snapshot["collection_plans"] = plans
        publish_option_eod(snapshot, output, surface_shadow_days=1)
        status["published"] = True
        write_json_if_changed(output / "latest.json", futures)
        write_json_if_changed(output / "options/last_run_status.json", status)
        publish_report_input(output)
    else:
        write_json_if_changed(output / "options/last_run_status.json", status)
    write_json_gzip_if_changed(output / "observed_vendor_quotes.json.gz", client.quotes)
    write_json_if_changed(output / "requests.json", client.requests)
    summary = {
        "generated_at": datetime.now().astimezone().isoformat(), "requested_date": day,
        "seconds": round(time.monotonic()-started, 2), "published": status["published"],
        "coverage": status["coverage"], "requests": len(client.requests),
        "requested_contract_occurrences": sum(r["contract_count"] for r in client.requests),
        "requested_field_cells": sum(r["requested_field_cells"] for r in client.requests),
        "transport_retries_included": False,
        "response_date_counts": dict(Counter(str(r.get("time"))[:10] for r in client.quotes)),
        "selected_contracts": sum(p["selected_contract_count"] for p in plans),
        "directory_contracts_for_planned_products": sum(p["directory_contract_count"] for p in plans),
        "legacy_60pct_gate_would_publish": bool(snapshot and status["coverage"]["product_coverage"] >= .6),
        "comparison_latest_published": compare(previous, snapshot) if snapshot else None,
        "comparison_latest_attempt": compare(attempt, snapshot) if snapshot and attempt.get("records") else None,
        "baseline_note": "Stored old-mode data only; no live full-chain rerun was made.",
    }
    write_json_if_changed(output / "comparison.json", summary)
    print(json.dumps({k:v for k,v in summary.items() if not k.startswith("comparison_")}, ensure_ascii=False, indent=2))
    return 0 if snapshot else 2


if __name__ == "__main__":
    raise SystemExit(main())
