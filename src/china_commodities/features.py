"""Deterministic commodity curve, option and anomaly-screening features."""

from __future__ import annotations

import math
import re
from datetime import date
from typing import Any, Iterable

import pandas as pd

from .catalog import ProductCatalog, UNKNOWN_SECTOR


def contract_month(contract: str, trade_date: str) -> date | None:
    """Resolve YYMM and CZCE YMM contract suffixes without creating a continuous series."""
    match = re.match(r"^[A-Za-z]+(\d{3,4})", str(contract))
    if not match:
        return None
    digits = match.group(1)
    trade = date.fromisoformat(trade_date)
    if len(digits) == 4:
        year = 2000 + int(digits[:2])
        month = int(digits[2:])
    else:
        year_digit = int(digits[0])
        month = int(digits[1:])
        decade = trade.year - trade.year % 10
        candidates = [decade - 10 + year_digit, decade + year_digit, decade + 10 + year_digit]
        floor = trade.year - 1
        future_candidates = [candidate for candidate in candidates if candidate >= floor]
        year = min(future_candidates, key=lambda candidate: abs(candidate - trade.year))
    if not 1 <= month <= 12:
        return None
    return date(year, month, 1)


def _finite(value: Any) -> float | None:
    number = pd.to_numeric(value, errors="coerce")
    return float(number) if pd.notna(number) and math.isfinite(float(number)) else None


def _contract_view(row: pd.Series, trade_date: str) -> dict[str, Any]:
    month = contract_month(str(row["contract"]), trade_date)
    return {
        "contract": str(row["contract"]),
        "contract_month": month.isoformat() if month else None,
        "close": _finite(row.get("close")),
        "settle": _finite(row.get("settle")),
        "pre_settle": _finite(row.get("pre_settle")),
        "close_return_pct": _finite(row.get("close_return_pct")),
        "settle_return_pct": _finite(row.get("settle_return_pct")),
        "volume": _finite(row.get("volume")),
        "open_interest": _finite(row.get("open_interest")),
        "turnover": _finite(row.get("turnover")),
    }


def _curve_pair(
    near: dict[str, Any] | None, deferred: dict[str, Any] | None
) -> dict[str, Any]:
    output = {
        "near_minus_deferred": None,
        "near_minus_deferred_pct": None,
        "annualized_near_deferred_pct": None,
        "curve_shape": None,
        "price_quality": None,
        "calendar_days": None,
    }
    if not near or not deferred:
        return output
    # A close-only fallback is useful as a quote, but it is not an official
    # settlement curve and must not enter curve evidence or scoring.
    near_price = near.get("settle")
    deferred_price = deferred.get("settle")
    if not near_price or not deferred_price:
        output["price_quality"] = "settlement_unavailable"
        return output
    output["price_quality"] = "official_settlement"
    spread = near_price - deferred_price
    output["near_minus_deferred"] = spread
    output["near_minus_deferred_pct"] = spread / deferred_price * 100.0
    output["curve_shape"] = "backwardation" if spread > 0 else "contango" if spread < 0 else "flat"
    near_month = near.get("contract_month")
    deferred_month = deferred.get("contract_month")
    if near_month and deferred_month:
        days = (date.fromisoformat(deferred_month) - date.fromisoformat(near_month)).days
        if days > 0:
            output["calendar_days"] = days
            output["annualized_near_deferred_pct"] = spread / deferred_price * 365.0 / days * 100.0
    return output


def build_curve_features(
    futures_records: list[dict[str, Any]], catalog: ProductCatalog
) -> list[dict[str, Any]]:
    """Build explicit main/sub-main and near/next-liquid views for every product."""
    if not futures_records:
        return []
    frame = pd.DataFrame(futures_records)
    output: list[dict[str, Any]] = []
    for (exchange, product), group in frame.groupby(["exchange", "product"], sort=True):
        trade_date = str(group["trade_date"].iloc[0])
        ranked = group.copy()
        ranked["_oi"] = pd.to_numeric(ranked["open_interest"], errors="coerce").fillna(0)
        ranked["_volume"] = pd.to_numeric(ranked["volume"], errors="coerce").fillna(0)
        ranked.sort_values(["_oi", "_volume", "contract"], ascending=[False, False, True], inplace=True)
        main = _contract_view(ranked.iloc[0], trade_date) if not ranked.empty else None
        secondary = _contract_view(ranked.iloc[1], trade_date) if len(ranked) > 1 else None

        liquid = group.copy()
        liquid["_oi"] = pd.to_numeric(liquid["open_interest"], errors="coerce").fillna(0)
        liquid["_volume"] = pd.to_numeric(liquid["volume"], errors="coerce").fillna(0)
        liquid["_month"] = liquid["contract"].map(
            lambda contract: contract_month(str(contract), trade_date)
        )
        liquid = liquid[
            liquid["_month"].notna()
            & liquid["_oi"].gt(0)
            & liquid["_volume"].gt(0)
        ].sort_values(["_month", "_oi"], ascending=[True, False])
        near = _contract_view(liquid.iloc[0], trade_date) if not liquid.empty else None
        deferred = _contract_view(liquid.iloc[1], trade_date) if len(liquid) > 1 else None
        main_curve = _curve_pair(main, secondary)
        near_curve = _curve_pair(near, deferred)
        output.append(
            {
                "trade_date": trade_date,
                "exchange": exchange,
                "product": product,
                "product_name": catalog.name_for(product),
                "sector": catalog.sector_for(product),
                "contract_count": int(len(group)),
                "main_contract": main,
                "secondary_contract": secondary,
                "nearest_liquid_contract": near,
                "next_liquid_contract": deferred,
                "main_secondary_curve": main_curve,
                "near_next_curve": near_curve,
            }
        )
    return output


def summarize_options(option_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not option_records:
        return []
    frame = pd.DataFrame(option_records)
    output: list[dict[str, Any]] = []
    for keys, group in frame.groupby(
        ["trade_date", "exchange", "product", "source_symbol"], sort=True
    ):
        trade_date, exchange, product, source_symbol = keys
        volume = pd.to_numeric(group["volume"], errors="coerce").fillna(0)
        oi = pd.to_numeric(group["open_interest"], errors="coerce").fillna(0)
        calls = group["option_type"].eq("C")
        puts = group["option_type"].eq("P")
        call_volume = float(volume[calls].sum())
        put_volume = float(volume[puts].sum())
        call_oi = float(oi[calls].sum())
        put_oi = float(oi[puts].sum())
        iv = pd.to_numeric(group["iv_percent"], errors="coerce")
        valid_iv = iv.where(iv.gt(0)).dropna()
        weighted_iv = None
        if not valid_iv.empty:
            weights = volume.loc[valid_iv.index]
            weighted_iv = (
                float((valid_iv * weights).sum() / weights.sum())
                if weights.sum() > 0
                else float(valid_iv.median())
            )
        output.append(
            {
                "trade_date": trade_date,
                "exchange": exchange,
                "product": product,
                "source_symbol": source_symbol,
                "contract_count": int(len(group)),
                "total_volume": float(volume.sum()),
                "total_open_interest": float(oi.sum()),
                "put_call_volume_ratio": put_volume / call_volume if call_volume > 0 else None,
                "put_call_open_interest_ratio": put_oi / call_oi if call_oi > 0 else None,
                "median_iv_percent": float(valid_iv.median()) if not valid_iv.empty else None,
                "volume_weighted_iv_percent": weighted_iv,
                "iv_coverage": float(valid_iv.count() / len(group)) if len(group) else 0.0,
                "dealer_gamma_known": False,
            }
        )
    return output


def _index_by_product(records: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(record.get("product", "")).upper(): record
        for record in records
        if record.get("product")
    }


def enrich_and_score_curves(
    curves: list[dict[str, Any]],
    warehouse_records: list[dict[str, Any]],
    basis_records: list[dict[str, Any]],
    option_summaries: list[dict[str, Any]],
    member_ranking_summaries: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Attach optional evidence and calculate a market-anomaly score, not a trade score."""
    warehouse = _index_by_product(warehouse_records)
    basis = _index_by_product(basis_records)
    options = _index_by_product(option_summaries)
    ranking_records = member_ranking_summaries or []
    rankings_by_contract = {
        (record.get("product"), record.get("contract")): record
        for record in ranking_records
        if record.get("contract") and record.get("ranking_reconciled") is True
    }
    rankings_by_product = {
        record.get("product"): record
        for record in ranking_records
        if not record.get("contract") and record.get("ranking_reconciled") is True
    }
    rows: list[dict[str, Any]] = []
    for curve in curves:
        product = curve["product"]
        main = curve.get("main_contract") or {}
        curve_signal = curve.get("near_next_curve") or {}
        row = dict(curve)
        row["warehouse"] = warehouse.get(product)
        row["basis"] = basis.get(product)
        row["commodity_options"] = options.get(product)
        main_contract = (row.get("main_contract") or {}).get("contract")
        row["member_ranking"] = rankings_by_contract.get(
            (product, main_contract)
        ) or rankings_by_product.get(product)
        row["_abs_return"] = abs(main.get("close_return_pct") or 0.0)
        row["_volume"] = main.get("volume") or 0.0
        row["_oi"] = main.get("open_interest") or 0.0
        row["_abs_curve"] = abs(curve_signal.get("near_minus_deferred_pct") or 0.0)
        row["liquidity_eligible"] = bool(row["_volume"] > 0 and row["_oi"] > 0)
        evidence = {
            "curve": {
                "available": bool(curve_signal.get("curve_shape")),
                "quality": curve_signal.get("price_quality"),
            },
            "basis": {
                "available": bool(row["basis"]),
                "quality": (
                    (row["basis"] or {}).get("basis_quality", "proxy_unmatched")
                    if row["basis"]
                    else None
                ),
            },
            "warehouse": {
                "available": bool(row["warehouse"]),
                "quality": (
                    "official_as_published_unit_unstandardized"
                    if row["warehouse"]
                    else None
                ),
            },
            "options": {
                "available": bool(row["commodity_options"]),
                "quality": "product_aggregate_only" if row["commodity_options"] else None,
            },
        }
        row["evidence"] = evidence
        row["evidence_count"] = sum(
            item["available"] for item in evidence.values()
        )
        row["available_evidence_layers"] = [
            name for name, item in evidence.items() if item["available"]
        ]
        row["missing_evidence_layers"] = [
            name
            for name in (
                "curve",
                "basis",
                "warehouse",
                "options",
                "physical_supply_demand",
                "onshore_offshore_parity",
            )
            if name not in row["available_evidence_layers"]
        ]
        rows.append(row)

    if not rows:
        return []
    frame = pd.DataFrame(
        [
            {
                "_abs_return": row["_abs_return"],
                "_volume": row["_volume"],
                "_oi": row["_oi"],
                "_abs_curve": row["_abs_curve"],
            }
            for row in rows
        ]
    )
    ranks = frame.rank(pct=True, method="average")
    for index, row in enumerate(rows):
        score = (
            ranks.loc[index, "_abs_return"] * 40.0
            + ranks.loc[index, "_volume"] * 20.0
            + ranks.loc[index, "_oi"] * 15.0
            + ranks.loc[index, "_abs_curve"] * 25.0
        )
        row["cross_sectional_activity_score"] = round(float(score), 2)
        for internal in ("_abs_return", "_volume", "_oi", "_abs_curve"):
            row.pop(internal, None)
    sorted_rows = sorted(
        rows,
        key=lambda row: (-row["cross_sectional_activity_score"], row["product"]),
    )
    for score_rank, row in enumerate(sorted_rows, start=1):
        row["score_rank"] = score_rank
    return sorted_rows


def select_candidates(
    scored_curves: list[dict[str, Any]], limit: int = 12
) -> list[dict[str, Any]]:
    """Keep sector breadth while never promoting an illiquid contract."""
    eligible = [row for row in scored_curves if row.get("liquidity_eligible")]
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    sector_representatives: set[tuple[str, str]] = set()
    sectors = [
        "黑色与建材",
        "有色与贵金属",
        "能源与化工",
        "新能源与材料",
        "农产品、油脂油料、饲料和畜牧",
        "软商品与特色农产品",
    ]
    for sector in sectors:
        match = next((row for row in eligible if row.get("sector") == sector), None)
        if match:
            selected.append(match)
            key = (match["exchange"], match["product"])
            seen.add(key)
            sector_representatives.add(key)
    for row in eligible:
        key = (row["exchange"], row["product"])
        if key not in seen:
            selected.append(row)
            seen.add(key)
        if len(selected) >= limit:
            break
    output: list[dict[str, Any]] = []
    for display_order, row in enumerate(selected[:limit], start=1):
        main = row.get("main_contract") or {}
        key = (row["exchange"], row["product"])
        output.append(
            {
                "display_order": display_order,
                "score_rank": row["score_rank"],
                "sector_representative": key in sector_representatives,
                "trade_date": row["trade_date"],
                "exchange": row["exchange"],
                "product": row["product"],
                "product_name": row["product_name"],
                "sector": row.get("sector", UNKNOWN_SECTOR),
                "concrete_contract": main.get("contract"),
                "close_return_pct": main.get("close_return_pct"),
                "volume": main.get("volume"),
                "open_interest": main.get("open_interest"),
                "curve_shape": (row.get("near_next_curve") or {}).get("curve_shape"),
                "cross_sectional_activity_score": row[
                    "cross_sectional_activity_score"
                ],
                "evidence": row["evidence"],
                "evidence_count": row["evidence_count"],
                "available_evidence_layers": row["available_evidence_layers"],
                "missing_evidence_layers": row["missing_evidence_layers"],
                "is_trade_recommendation": False,
            }
        )
    return output
