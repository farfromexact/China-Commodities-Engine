"""Independent EOD backup, capability gates and same-date iFinD comparison.

The baseline is used for evaluation/selection only, never to supply missing
backup prices, IV, Greeks or contract metadata. Outputs live in a separate
tree; the production iFinD archive is not modified by this experiment.
"""

from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from .catalog import ProductCatalog, load_catalog
from .collectors.exchange_eod_adapter import ExchangeEODClient, contract_key, normalize_document, number
from .option_greeks import OptionValuationInput, calculate_greeks
from .option_rules import load_option_rules, option_rule_for
from .option_storage import read_option_latest
from .option_surface import build_option_surface
from .option_universe import OPENCTP_OPTION_DIRECTORY_URL, normalize_openctp_option_directory
from .storage import read_json, write_json_atomic, write_json_gzip_atomic


CORE_FIELDS = ("settle", "volume", "open_interest", "underlying_settle")
IFIND_FIELDS = ("close", "open", "high", "low", "settle", "pre_settle", "volume",
                "turnover", "open_interest", "iv_percent", "delta", "gamma", "vega", "theta", "rho")


def ensure_completed_eod(trade_date: str, *, now: datetime | None = None) -> None:
    shanghai = timezone(timedelta(hours=8))
    current = (now or datetime.now(timezone.utc)).astimezone(shanghai)
    requested = date.fromisoformat(trade_date)
    if requested > current.date() or (requested == current.date() and current.time() < time(18, 15)):
        raise ValueError("Use a completed EOD date; current-day collection opens at 18:15 Asia/Shanghai")


def valid_field(row: dict, field: str) -> bool:
    value = number(row.get(field))
    if value is None:
        return False
    if field in {"settle", "underlying_settle", "iv_percent"}:
        return value > 0
    return value >= 0 if field in {"volume", "open_interest", "turnover"} else True


def product_key(row) -> str:
    return f"{row['exchange']}:{row['product']}"


def groups(records: list[dict]) -> dict[str, list[dict]]:
    result = defaultdict(list)
    for row in records:
        result[product_key(row)].append(row)
    return dict(result)


def core_coverage(records: list[dict]) -> float:
    return (sum(valid_field(row, field) for row in records for field in CORE_FIELDS)
            / (len(records) * len(CORE_FIELDS))) if records else 0.0


def _validate_rows(records: list[dict], trade_date: str) -> None:
    seen = set()
    for row in records:
        key = (row["exchange"], contract_key(row["contract"]))
        if key in seen:
            raise ValueError(f"duplicate contract {key}")
        seen.add(key)
        if row.get("trade_date") != trade_date or row.get("source_trade_date") != trade_date or row.get("source_date_match") is not True:
            raise ValueError(f"stale or unverified date for {key}")
        if row.get("source_provider") == "exchange_eod" and row.get("source_date_basis") not in {"report_date", "report_title", "response_query_date_echo"}:
            raise ValueError(f"missing exchange date evidence for {key}")


def _enrich_group(args) -> list[dict]:
    rows, futures, metadata, rate, source, steps = args
    enrich_options(rows, futures, metadata, risk_free_rate=rate,
                   rate_source=source, tree_steps=steps)
    return rows


def enrich_options(records: list[dict], futures: list[dict], metadata: dict,
                   *, risk_free_rate: float | None = None, rate_source: str | None = None,
                   tree_steps: int = 100, model_workers: int = 1) -> None:
    if risk_free_rate is not None and (not rate_source or not -0.1 <= risk_free_rate <= 1):
        raise ValueError("model rate needs a named source and a plausible decimal annual rate")
    if not 1 <= model_workers <= 4:
        raise ValueError("model_workers must be between 1 and 4")
    if risk_free_rate is not None and model_workers > 1 and len(records) > 100:
        tasks = []
        for key, rows in groups(records).items():
            exchange, product = key.split(":")
            tasks.append((rows, [row for row in futures if product_key(row) == key],
                          {(exchange, product): metadata.get((exchange, product), [])},
                          risk_free_rate, rate_source, tree_steps))
        with ProcessPoolExecutor(max_workers=model_workers) as pool:
            records[:] = [row for result in pool.map(_enrich_group, tasks) for row in result]
        return
    futures_by_key = {(r["exchange"], r["contract"]): r for r in futures}
    metadata_by_key = {(r["exchange"], contract_key(r["contract"])): r
                       for values in metadata.values() for r in values}
    rules = load_option_rules()
    for row in records:
        forward = futures_by_key.get((row["exchange"], row["underlying_contract"]), {})
        row["underlying_settle"] = forward.get("settle")
        row["underlying_source_provider"] = forward.get("source_provider")
        meta = metadata_by_key.get((row["exchange"], row["contract"]), {})
        row["expiry_date"] = meta.get("expiry_date")
        row["expiry_source"] = "openctp_current_directory" if meta else None
        rule = option_rule_for(row["exchange"], row["product"], rules=rules)
        row["exercise_style"] = rule["exercise_style"]
        row["exercise_style_rule_source_url"] = rule["source_url"]
        vendor = {field: row[field] for field in ("iv_percent", "delta") if number(row.get(field)) is not None}
        model = None
        if risk_free_rate is not None and row["expiry_date"] and valid_field(row, "underlying_settle"):
            days = (date.fromisoformat(row["expiry_date"]) - date.fromisoformat(row["trade_date"])).days
            if days > 0 and (valid_field(row, "iv_percent") or valid_field(row, "settle")):
                valuation = OptionValuationInput(
                    forward=row["underlying_settle"], strike=row["strike"],
                    time_to_expiry_years=days / 365, rate=risk_free_rate,
                    option_type=row["option_type"], exercise_style=row["exercise_style"],
                    market_price=row.get("settle"), iv_percent=row.get("iv_percent"),
                )
                try:
                    result = calculate_greeks(valuation, tree_steps=tree_steps)
                    model = result.to_dict() if result else None
                except (ValueError, OverflowError, ZeroDivisionError) as exc:
                    row["model_error"] = str(exc)
        row["greeks"] = {
            "quality": "vendor_and_model" if vendor and model else "model_derived" if model else "vendor_reported" if vendor else "unavailable",
            "selected_source": "model" if model else "vendor" if vendor else None,
            "selected": model or vendor or None, "vendor": vendor or None, "model": model,
            "vendor_provider": "exchange_eod", "dealer_position_direction_known": False,
            "model_assumptions": {"risk_free_rate": risk_free_rate, "risk_free_rate_source": rate_source,
                                  "exercise_style": row["exercise_style"], "day_count": "actual_365",
                                  "tree_steps": tree_steps, "price_basis": "settlement"},
        }


def capabilities(records: list[dict]) -> dict:
    n = len(records)
    return {
        "contracts": n,
        "core_field_coverage": core_coverage(records),
        "fields": {field: sum(valid_field(row, field) for row in records) / n if n else 0
                   for field in (*IFIND_FIELDS, "underlying_settle")},
        "expiry_coverage": sum(bool(row.get("expiry_date")) for row in records) / n if n else 0,
        "model_greeks_coverage": sum(bool(row.get("greeks", {}).get("model")) for row in records) / n if n else 0,
        "intraday_available": False, "executable_bid_ask_available": False,
        "dealer_gamma_direction_known": False,
    }


def basic_summaries(records: list[dict]) -> list[dict]:
    """Keep PCR usable without IV; missing observations are never summed as zero."""
    result = []
    for key, rows in sorted(groups(records).items()):
        summary = {"product_key": key, "contracts": len(rows)}
        for field in ("volume", "open_interest"):
            complete = all(valid_field(row, field) for row in rows)
            calls = sum(row[field] for row in rows if row["option_type"] == "C") if complete else None
            puts = sum(row[field] for row in rows if row["option_type"] == "P") if complete else None
            summary.update({f"call_{field}": calls, f"put_{field}": puts,
                            f"put_call_{field}_ratio": puts / calls if calls else None,
                            f"{field}_complete": complete})
        result.append(summary)
    return result


def collect_backup(trade_date: str, root: Path, *, catalog: ProductCatalog | None = None,
                   client: ExchangeEODClient | None = None, metadata_loader: Callable | None = None,
                   risk_free_rate: float | None = None, rate_source: str | None = None,
                   model_workers: int = 1,
                   progress: Callable[[str], None] = print) -> dict:
    trade_date = date.fromisoformat(trade_date).isoformat()
    ensure_completed_eod(trade_date)
    if risk_free_rate is not None and (not rate_source or not -0.1 <= risk_free_rate <= 1):
        raise ValueError("model rate needs a named source and a plausible decimal annual rate")
    root = Path(root)
    catalog = catalog or load_catalog()
    client = client or ExchangeEODClient(root / "raw" / trade_date)
    futures, options, statuses = [], [], []
    for exchange in catalog.exchanges:
        for kind, target in (("futures", futures), ("options", options)):
            try:
                document = client.fetch(exchange, kind, trade_date)
                rows = normalize_document(document)
                _validate_rows(rows, trade_date)
                target.extend(rows)
                entry = {"exchange": exchange, "kind": kind, "status": "ok", "contracts": len(rows),
                         "date_basis": document.evidence["source_date_basis"]}
            except Exception as exc:
                entry = {"exchange": exchange, "kind": kind, "status": "failed", "contracts": 0,
                         "error": f"{type(exc).__name__}: {exc}"}
            statuses.append(entry)
            progress(f"{exchange} {kind}: {entry['status']} ({entry['contracts']})")
    expected = {f"{p.exchange}:{p.product}" for p in catalog.options}
    unknown_products = sorted(set(groups(options)) - expected)
    options = [row for row in options if product_key(row) in expected]
    metadata_error = None
    try:
        if metadata_loader:
            metadata = metadata_loader(trade_date, catalog.options)
        else:
            import requests
            response = requests.get(OPENCTP_OPTION_DIRECTORY_URL, timeout=40)
            response.raise_for_status()
            raw = response.content
            metadata_path = root / "raw" / trade_date / "openctp-metadata.json.gz"
            import gzip
            metadata_path.parent.mkdir(parents=True, exist_ok=True)
            metadata_path.write_bytes(gzip.compress(raw, mtime=0))
            write_json_atomic(metadata_path.with_suffix(".audit.json"), {
                "url": OPENCTP_OPTION_DIRECTORY_URL, "sha256": hashlib.sha256(raw).hexdigest(),
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "scope": "current instruments; unavailable expired history remains missing",
            })
            metadata = normalize_openctp_option_directory(response.json()["data"], trade_date=trade_date, option_products=catalog.options)
    except Exception as exc:
        metadata = {}
        metadata_error = f"{type(exc).__name__}: {exc}"
    progress(f"metadata: {sum(len(rows) for rows in metadata.values())} contracts; enriching")
    enrich_options(options, futures, metadata, risk_free_rate=risk_free_rate,
                   rate_source=rate_source, model_workers=model_workers)
    product_groups = groups(options)
    successful = sorted(key for key, rows in product_groups.items() if core_coverage(rows) >= 0.8)
    coverage = {"expected_product_count": len(expected), "observed_product_count": len(product_groups),
                "successful_product_count": len(successful), "product_coverage": len(successful) / len(expected),
                "successful_products": successful, "failed_products": sorted(expected - set(successful)),
                "unexpected_products": unknown_products, "minimum_product_coverage": 0.8,
                "scope_complete": set(successful) == expected,
                "publish_eligible": len(successful) / len(expected) >= 0.8}
    snapshot = {
        "schema_version": 1, "trade_date": trade_date, "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_provider": "exchange_eod", "collection_mode": "exchange_eod_backup_shadow", "intraday": False,
        "records": options, "futures": futures, "coverage": coverage, "source_statuses": statuses,
        "capabilities": capabilities(options), "basic_summaries": basic_summaries(options),
        "metadata_error": metadata_error,
        "model_scenario_only": risk_free_rate is not None,
        "model_qualified_for_production": False,
        "limitations": ["Exchange backup is independently evaluated; not an iFinD-equivalent certification.",
                        "GFEX uses a response query-date echo; SHFE/INE/CZCE use published report dates.",
                        "Turnover is converted from CNY 10000; single/double-sided volume conventions require comparison.",
                        "Missing IV, metadata or Greeks remain unavailable; no dealer position direction is inferred.",
                        "Current OpenCTP metadata cannot reconstruct a point-in-time historical universe."],
    }
    return snapshot


def compare_ifind(snapshot: dict, baseline: dict | None) -> dict:
    """Separate matched-contract availability from field agreement and full scope."""
    if not baseline or baseline.get("trade_date") != snapshot["trade_date"]:
        return {"status": "unavailable", "reason": "no same-date iFinD baseline"}
    reference = {(r["exchange"], contract_key(r["contract"])): r for r in baseline.get("records", [])}
    backup = {(r["exchange"], contract_key(r["contract"])): r for r in snapshot["records"]}
    matched = set(reference) & set(backup)
    fields = {}
    for field in IFIND_FIELDS:
        expected_keys = [key for key, row in reference.items() if valid_field(row, field)]
        available_keys = [key for key in expected_keys if key in backup and valid_field(backup[key], field)]
        comparable = [(number(reference[key][field]), number(backup[key][field])) for key in available_keys]
        differences = [abs(a - b) for a, b in comparable]
        fields[field] = {
            "ifind_nonnull": len(expected_keys), "backup_nonnull_on_ifind_contracts": len(available_keys),
            "availability_vs_ifind": len(available_keys) / len(expected_keys) if expected_keys else None,
            "agreement_rtol_1e_4_atol_1e_6": sum(abs(a - b) <= max(1e-6, abs(a) * 1e-4) for a, b in comparable) / len(comparable) if comparable else None,
            "median_absolute_difference": sorted(differences)[len(differences) // 2] if differences else None,
        }
    core_expected = sum(valid_field(row, f) for row in reference.values() for f in CORE_FIELDS)
    core_available = sum(valid_field(reference[key], f) and valid_field(backup[key], f) for key in matched for f in CORE_FIELDS)
    return {"status": "compared", "trade_date": snapshot["trade_date"],
            "baseline_sha256": hashlib.sha256(json.dumps(baseline, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest(),
            "ifind_contracts": len(reference), "backup_contracts": len(backup), "matched_contracts": len(matched),
            "contract_coverage_vs_ifind": len(matched) / len(reference) if reference else 0,
            "core_field_coverage_vs_ifind": core_available / core_expected if core_expected else None,
            "fields": fields, "model_outputs_counted_as_vendor_fields": False,
            "note": "Availability is not accuracy. Compare close/settlement and timestamps before interpreting disagreement."}


def select_option_products(primary: dict | None, backup: dict, trade_date: str,
                           *, minimum_coverage: float = 0.8) -> dict:
    """Select whole product chains; primary failure is isolated from good backup."""
    primary = primary or {}
    primary_groups = groups(primary.get("records", [])) if primary.get("trade_date") == trade_date else {}
    backup_groups = groups(backup.get("records", [])) if backup.get("trade_date") == trade_date else {}
    output, decisions = [], []
    for key in sorted(set(primary_groups) | set(backup_groups)):
        selected = None
        reasons = []
        for source, rows in (("ifind", primary_groups.get(key, [])), ("exchange_backup", backup_groups.get(key, []))):
            if not rows:
                continue
            try:
                _validate_rows(rows, trade_date)
                if source == "ifind" and any(not str(r.get("source_provider", "")).startswith("ifind") for r in rows):
                    raise ValueError("primary source is not iFinD")
                if source == "exchange_backup" and any(r.get("source_provider") != "exchange_eod" for r in rows):
                    raise ValueError("backup source is not an approved exchange feed")
                if core_coverage(rows) < minimum_coverage:
                    raise ValueError("core field coverage is below threshold")
                selected = source
                output.extend(rows)
                break
            except ValueError as exc:
                reasons.append(f"{source}: {exc}")
        decisions.append({"product_key": key, "selected": selected, "reasons": reasons})
    return {"schema_version": 1, "trade_date": trade_date, "source_provider": "explicit_product_selection",
            "collection_mode": "experimental_effective_eod", "records": output,
            "decisions": decisions, "basic_summaries": basic_summaries(output),
            "capabilities": capabilities(output), "intraday": False}


def publish_backup(snapshot: dict, root: Path, *, baseline: dict | None = None) -> dict:
    root = Path(root)
    trade_date = snapshot["trade_date"]
    _validate_rows(snapshot["records"], trade_date)
    write_json_gzip_atomic(root / "snapshots" / f"{trade_date}.json.gz", snapshot)
    write_json_gzip_atomic(root / "attempt_latest.json.gz", snapshot)
    comparison = compare_ifind(snapshot, baseline)
    effective = select_option_products(baseline, snapshot, trade_date)
    write_json_gzip_atomic(root / "effective_latest.json.gz", effective)
    surface = build_option_surface(snapshot)
    surface["model_validation_required"] = bool(snapshot.get("model_scenario_only"))
    write_json_gzip_atomic(root / "surface_attempt_latest.json.gz", surface)
    # Last valid backup survives a later failed attempt or historical replay.
    previous = read_json(root / "latest.json.gz", default={})
    promoted = bool(snapshot["coverage"]["publish_eligible"] and snapshot["records"]
                    and (not previous or previous.get("trade_date", "") <= trade_date))
    if promoted:
        write_json_gzip_atomic(root / "latest.json.gz", snapshot)
    report = {key: value for key, value in snapshot.items() if key not in {"records", "futures", "basic_summaries"}}
    report.update(comparison=comparison, promoted=promoted, previous_valid_retained=bool(previous and not promoted),
                  futures_contract_count=len(snapshot["futures"]),
                  surface={key: surface.get(key) for key in ("series_count", "surface_ready_count", "positioning_ready_count", "execution_ready_count")},
                  effective_product_count=len(groups(effective["records"])))
    write_json_atomic(root / "last_run_status.json", report)
    write_json_atomic(root / "reports" / f"{trade_date}.json", report)
    return report


def load_baseline(data_dir: Path, trade_date: str) -> dict | None:
    path = Path(data_dir) / "options" / "snapshots" / f"{trade_date}.json.gz"
    if path.exists():
        return read_json(path)
    latest = read_option_latest(data_dir)
    return latest if latest and latest.get("trade_date") == trade_date else None
