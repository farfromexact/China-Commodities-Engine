"""Independent, advisory exchange EOD comparison. No iFinD requests or promotion."""
from datetime import datetime, timezone
from pathlib import Path

from .collectors.exchange_eod_adapter import ExchangeEODClient, contract_key, normalize_document, number
from .storage import read_json, write_json_if_changed


def compare_exchange(exchange, rows, baseline, trade_date):
    def keyed(items):
        result = {}
        for row in items:
            if row.get("exchange") != exchange:
                continue
            key = contract_key(row.get("contract"))
            if not key or key in result:
                raise ValueError("missing or duplicate concrete contract identity")
            if row.get("source_trade_date") != trade_date or row.get("source_date_match") is not True:
                raise ValueError("source date evidence does not match requested date")
            result[key] = row
        return result
    official, vendor = keyed(rows), keyed(baseline)
    if not official:
        raise ValueError("no normalized official futures")
    if not vendor:
        return {"exchange": exchange, "state": "baseline_unavailable", "official_contracts": len(official)}
    fields, differences = {}, []
    matched = sorted(official.keys() & vendor.keys())
    for field in ("settle", "close", "pre_settle"):
        comparable = differing = 0
        for contract in matched:
            left, right = number(vendor[contract].get(field)), number(official[contract].get(field))
            if left is None or right is None or left <= 0 or right <= 0:
                continue
            comparable += 1
            if abs(left-right) > 1e-6:
                differing += 1
                if len(differences) < 100:
                    differences.append({"contract": contract, "field": field, "vendor_minus_official": left-right})
        fields[field] = {"comparable": comparable, "different": differing,
                         "equal": comparable-differing, "absolute_tolerance": 1e-6}
    difference_count = sum(v["different"] for v in fields.values())
    return {"exchange": exchange, "state": "differences" if difference_count else
            "matched" if fields["settle"]["comparable"] else "not_comparable",
            "official_contracts": len(official), "vendor_contracts": len(vendor),
            "matched_contracts": len(matched), "vendor_only_count": len(vendor.keys()-official.keys()),
            "official_only_count": len(official.keys()-vendor.keys()), "fields": fields,
            "differences": differences, "differences_truncated": difference_count > len(differences),
            "source_date_basis": sorted({r.get("source_date_basis", "unknown") for r in official.values()})}


def run_crosscheck(trade_date, *, data_dir="data", raw_dir="tmp/exchange-crosscheck/raw", output_dir=None, client=None):
    root = Path(data_dir)
    snapshot = read_json(root / "latest.json", default={})
    baseline = snapshot.get("futures_contracts", []) if snapshot.get("trade_date") == trade_date else []
    # Do not compare an official feed against itself after a future source switch.
    modules = {m.get("scope"): m for m in snapshot.get("source", {}).get("modules", [])
               if m.get("dataset") == "futures"}
    def is_vendor(row):
        provider = row.get("source_provider")
        if provider:
            return str(provider).startswith("ifind")
        module = modules.get(row.get("exchange"), {})
        return (snapshot.get("source", {}).get("provider") == "ifind"
                and module.get("upstream_source") == "iFinD Quant API"
                and module.get("state") == "ok" and not module.get("is_fallback")
                and module.get("source_date_match") is True
                and module.get("source_trade_date") == trade_date)
    baseline = [r for r in baseline if is_vendor(r)]
    client = client or ExchangeEODClient(Path(raw_dir), timeout=15)
    results = []
    for exchange in ("SHFE", "INE", "DCE", "CZCE", "GFEX"):
        try:
            document = client.fetch(exchange, "futures", trade_date)
            result = compare_exchange(exchange, normalize_document(document), baseline, trade_date)
        except Exception as exc:
            result = {"exchange": exchange, "state": "source_error", "error": str(exc)[:400]}
        results.append(result)
    compared = sum(r["state"] in {"matched", "differences"} for r in results)
    report = {"schema_version": 1, "trade_date": trade_date,
              "generated_at": datetime.now(timezone.utc).isoformat(),
              "mode": "advisory_independent_exchange_eod", "ifind_requests": 0,
              "state": "complete" if compared == 5 else "partial" if compared else "unavailable",
              "all_compared_prices_equal": compared == 5 and all(r["state"] == "matched" for r in results),
              "exchanges": results, "blocks_publication": False,
              "limitations": ["No source replacement or full-market certification.",
                              "Volume/OI/turnover conventions are not compared.",
                              "Date-echo evidence is weaker than a dated exchange report."]}
    write_json_if_changed((Path(output_dir) if output_dir else root / "crosschecks") / "futures_latest.json", report)
    return report
