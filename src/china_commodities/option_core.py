"""Budgeted option sampling before any vendor quote request.

One active underlying, paired ATM strikes, at most 30 contracts and 10% of
the dated directory (a two-contract minimum for very small directories).
Publication and analytical readiness are intentionally separate.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import math
import re

from .collectors.ifind_option_adapter import (
    IFindOptionDataError, _exchange_quote_config, _iso_date, _realtime_quote_map,
    normalize_ifind_option_rows, option_contract_to_ifind_code,
)
from .collectors.akshare_adapter import collect_option_daily
from .collectors.ifind_http_adapter import IFindHTTPError
from .normalize import normalize_options
from .catalog import load_catalog
from .option_universe import collect_openctp_option_directories

CORE_INDICATORS = (
    "latest", "settlement", "preSettlement", "volume", "openInterest",
    "impliedVolatility", "delta", "gamma", "vega", "theta",
)
CORE_PROFILE = "core_v1"
SELECTOR_VERSION = 2


def series_type(row):
    """Retain the MS series marker, which is not part of the underlying code."""
    code = str(row.get("contract") or "").upper().replace("-", "").split(".")[0]
    match = re.fullmatch(r"[A-Z]+\d{3,4}(MS)?[CP]\d+(?:\.\d+)?", code)
    if not match:
        raise IFindOptionDataError(f"unknown option series identity: {code}")
    return "monthly_average" if match.group(1) else "standard"


def number(value):
    return (float(value) if isinstance(value, (int, float))
            and not isinstance(value, bool) and math.isfinite(value) else None)


def select_core_contracts(directory, futures, previous, trade_date):
    """Select using directory/local evidence only; never fetch a full quote chain."""
    contracts = [r.get("contract") for r in directory]
    if not all(contracts) or len(set(contracts)) != len(contracts):
        raise IFindOptionDataError("core directory has missing or duplicate contracts")
    groups = defaultdict(list)
    for row in directory:
        expiry = row.get("expiry_date")
        if expiry and expiry <= trade_date:
            continue
        if row.get("underlying_contract") and number(row.get("strike")):
            groups[(row["underlying_contract"], expiry, series_type(row))].append(row)
    budget = max(2, min(30, int(len(directory) * .10) // 2 * 2))
    old = {r["contract"]: r for r in previous}
    def activity(item):
        (underlying, _, kind), rows = item
        f = futures.get(underlying, {})
        return (
            kind == "standard",
            sum(number(r.get("open_interest")) or 0 for r in rows),
            number(f.get("open_interest")) or 0,
            sum(number(old.get(r["contract"], {}).get("open_interest")) or 0 for r in rows),
            sum(number(r.get("volume")) or 0 for r in rows),
        )
    for (underlying, expiry, kind), rows in sorted(groups.items(), key=activity, reverse=True):
        f = futures.get(underlying, {})
        forward = number(f.get("settle"))
        forward_date = f.get("source_trade_date")
        if not forward or forward <= 0:
            prior = next((r for r in previous if r.get("underlying_contract") == underlying
                          and (number(r.get("underlying_settle")) or 0) > 0), {})
            forward = number(prior.get("underlying_settle"))
            forward_date = prior.get("source_trade_date")
        if not forward or forward <= 0:
            continue
        sides = defaultdict(dict)
        for r in rows:
            if r.get("option_type") in {"C", "P"}:
                if r["option_type"] in sides[r["strike"]]:
                    raise IFindOptionDataError("ambiguous duplicate side within an option series")
                sides[r["strike"]][r["option_type"]] = r
        paired = [k for k, v in sides.items() if len(v) == 2]
        nearest = sorted(paired, key=lambda k: (abs(k-forward), k))
        strikes = nearest[:1]
        # Previous observed deltas guide selection only; fresh quotes determine readiness.
        for side, target in (("P", -.25), ("C", .25)):
            candidates = []
            for k in paired:
                prior = old.get(sides[k][side]["contract"], {})
                delta = number(((prior.get("greeks") or {}).get("selected") or {}).get("delta"))
                if delta is None:
                    delta = number(prior.get("delta"))
                if delta is not None and abs(delta-target) <= .05:
                    candidates.append((abs(delta-target), abs(k-forward), k))
            if candidates:
                k = min(candidates)[2]
                if k not in strikes and len(strikes) < budget//2:
                    strikes.append(k)
        strikes += [k for k in nearest if k not in strikes][:max(0, budget//2-len(strikes))]
        if not strikes:
            continue
        selected = [sides[k][side] for k in sorted(strikes) for side in ("C", "P")]
        return selected, {
            "profile": CORE_PROFILE, "directory_contract_count": len(directory),
            "selector_version": SELECTOR_VERSION, "series_type": kind,
            "contract_budget": budget, "selected_contract_count": len(selected),
            "underlying_contract": underlying, "expiry_date": expiry,
            "selection_forward": forward, "selection_forward_date": forward_date,
            "selection_reason": "standard_active_series_atm_then_observed_delta_wings_then_nearest_pairs",
            "full_chain": False,
        }
    raise IFindOptionDataError("core selection needs a known forward and paired active contracts")


class CoreOptionCollector:
    """Callable compatible with option_batch; reuse same-date successful quotes."""

    def __init__(self, *, futures_snapshot, previous_snapshot, force_refresh=False,
                 metadata_loader=None):
        self.futures = futures_snapshot or {}
        self.previous = previous_snapshot or {}
        self.force_refresh = force_refresh
        self.plans = {}
        self.metadata_loader = metadata_loader or collect_openctp_option_directories
        self.metadata_cache = {}

    def __call__(self, trade_date, *, client, universes, ak_module=None,
                 directory_records_by_product=None):
        universe = universes[0]
        exchange, product = universe.exchange.upper(), universe.product.upper()
        key = f"{exchange}:{product}"
        previous = [r for r in self.previous.get("records", [])
                    if r.get("exchange") == exchange and r.get("product") == product
                    and str(r.get("source_trade_date") or "") <= trade_date]
        same_day = self.previous.get("trade_date") == trade_date
        cached_plan = next((p for p in self.previous.get("collection_plans", [])
                            if p.get("product_key") == key and p.get("profile") == CORE_PROFILE), None) if same_day else None
        futures = {r["contract"]: r for r in self.futures.get("futures_contracts", [])
                   if r.get("exchange") == exchange and r.get("source_date_match") is True
                   and r.get("source_trade_date") == trade_date}
        if (cached_plan and cached_plan.get("selected_directory")
                and cached_plan.get("selector_version") == SELECTOR_VERSION):
            selected = cached_plan["selected_directory"]
            plan = dict(cached_plan)
        else:
            supplied = (directory_records_by_product or {}).get((exchange, product))
            directory = supplied if supplied is not None else normalize_options(
                collect_option_daily(trade_date, exchange, universe.symbol, ak_module=ak_module),
                exchange, product, universe.symbol, trade_date,
            )
            if not directory:
                raise IFindOptionDataError(f"empty option directory for {key}")
            directory = [dict(r) for r in directory]
            metadata_error = None
            if any(not r.get("expiry_date") for r in directory) and self.metadata_loader:
                if trade_date not in self.metadata_cache:
                    try:
                        self.metadata_cache[trade_date] = (self.metadata_loader(
                            trade_date, load_catalog().options), None)
                    except Exception as exc:
                        self.metadata_cache[trade_date] = ({}, str(exc)[:300])
                metadata, metadata_error = self.metadata_cache[trade_date]
                matches = {r["contract"].upper(): r for r in metadata.get((exchange, product), [])}
                for r in directory:
                    match = matches.get(r["contract"].upper(), {})
                    if match.get("expiry_date") and not r.get("expiry_date"):
                        r.update(expiry_date=match["expiry_date"], expiry_source="openctp_current_directory")
            selected, plan = select_core_contracts(directory, futures, previous, trade_date)
            plan["metadata_error"] = metadata_error
            plan["unresolved_expiry_count"] = sum(not r.get("expiry_date") for r in selected)
            plan["selected_directory"] = selected
        plan = dict(plan, product_key=key, trade_date=trade_date)
        by_code = {option_contract_to_ifind_code(r["contract"], exchange): r for r in selected}
        if len(by_code) != len(selected):
            raise IFindOptionDataError(f"duplicate selected contract for {key}")
        reusable = {r["ifind_code"]: dict(r) for r in previous
                    if same_day and not self.force_refresh
                    and r.get("source_trade_date") == trade_date
                    and r.get("source_date_match") is True and r.get("ifind_code") in by_code}
        for row in reusable.values():
            identity = by_code[row["ifind_code"]]
            row.update(collection_scope=CORE_PROFILE, analytics_scope="sampled_strikes",
                       series_type=series_type(identity), expiry_date=identity.get("expiry_date"))
        requested = sorted(set(by_code) - set(reusable))
        request_error = None
        try:
            quotes = _realtime_quote_map(requested, client=client, indicators=CORE_INDICATORS,
                                         batch_size=universe.quote_batch_size) if requested else {}
        except (IFindHTTPError, IFindOptionDataError) as exc:
            if not reusable:
                raise
            request_error = str(exc)[:400]
            quotes = {}
        records = list(reusable.values())
        rejected = []
        for code in requested:
            quote = quotes.get(code, {})
            source_date = _iso_date(quote.get("time"))
            if source_date != trade_date or not any(quote.get(k) is not None for k in CORE_INDICATORS):
                rejected.append({"contract": code, "source_date": source_date,
                                 "reason": "source_date_mismatch" if source_date != trade_date else "empty_quote"})
                continue
            try:
                row = normalize_ifind_option_rows([dict(quote, _source_endpoint="core+real_time_quotation")],
                                                  trade_date=trade_date, config=_exchange_quote_config(universe))[0]
            except (IFindOptionDataError, IndexError, ValueError):
                rejected.append({"contract": code, "reason": "invalid_quote"})
                continue
            invalid_fields = []
            for field in ("close", "settle", "pre_settle", "volume", "open_interest",
                          "iv_percent", "delta", "gamma", "vega", "theta"):
                value = row.get(field)
                if value is None:
                    continue
                numeric = number(value)
                invalid = (numeric is None or (field == "delta" and abs(numeric) > 1)
                           or (field not in {"delta", "theta"} and numeric < 0)
                           or (field in {"close", "settle", "pre_settle", "iv_percent"} and numeric == 0))
                if invalid:
                    row[field] = None
                    invalid_fields.append(field)
                    for source in ("selected", "vendor", "model"):
                        values = (row.get("greeks") or {}).get(source)
                        if isinstance(values, dict):
                            values[field] = None
            row["invalid_fields"] = invalid_fields
            if not any(number(row.get(k)) is not None for k in (
                "close", "settle", "pre_settle", "volume", "open_interest",
                "iv_percent", "delta", "gamma", "vega", "theta",
            )):
                rejected.append({"contract": code, "reason": "no_numeric_quote"})
                continue
            directory_row = by_code[code]
            f = futures.get(row["underlying_contract"], {})
            settlement = number(f.get("settle"))
            row.update(underlying_settle=settlement if settlement and settlement > 0 else None,
                       expiry_date=directory_row.get("expiry_date"),
                       series_type=series_type(directory_row),
                       underlying_price_basis="same_date_futures_settlement",
                       universe_source=directory_row.get("universe_source", "exchange_eod_via_akshare"),
                       collection_scope=CORE_PROFILE, analytics_scope="sampled_strikes")
            records.append(row)
        plan.update(requested_contract_count=len(requested), reused_contract_count=len(reusable),
                    returned_contract_count=len(records), rejected_contracts=rejected,
                    request_error=request_error,
                    selected_quote_coverage=len(records)/len(selected), indicators=list(CORE_INDICATORS))
        self.plans[key] = plan
        return {
            "schema_version": 1, "trade_date": trade_date,
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "source_provider": "ifind_http", "universe_source": "core_directory_selection",
            "collection_scope": CORE_PROFILE, "records": records,
            "universe_contract_count": len(selected), "quote_contract_count": len(records),
            "quote_coverage_complete": len(records) == len(selected),
            "selection": plan,
        }
