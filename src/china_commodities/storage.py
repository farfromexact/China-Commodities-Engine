"""Atomic JSON publication for verified and compact commodity artifacts."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

from .features import contract_month
from .models import PipelineResult


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if hasattr(value, "item"):
        return json_safe(value.item())
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(json_safe(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _snapshot_payload(result: PipelineResult) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "trade_date": result.trade_date,
        "generated_at": result.generated_at,
        "verified": result.verified,
        "official_complete": result.official_complete,
        "source": {
            "provider": "akshare",
            "akshare_version": result.akshare_version,
            "modules": [status.to_dict() for status in result.statuses],
        },
        "futures_contracts": result.futures_records,
        "commodity_curves": result.curves,
        "warehouse_inventory": result.warehouse_records,
        "proxy_basis": result.basis_records,
        "commodity_options": result.option_summaries,
        "member_rankings": result.member_ranking_summaries,
        "heatmap_candidates": result.candidates,
        "limitations": {
            "continuous_contracts_are_research_only": True,
            "basis_may_be_proxy": True,
            "warehouse_is_not_social_inventory": True,
            "dealer_gamma_direction_known": False,
            "seasonality_is_standalone_signal": False,
        },
    }


def _radar_payload(result: PipelineResult) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "trade_date": result.trade_date,
        "generated_at": result.generated_at,
        "verified": result.verified,
        "official_complete": result.official_complete,
        "commodity_regime": None,
        "heatmap": result.candidates,
        "curve_basis": [
            {
                "exchange": curve["exchange"],
                "product": curve["product"],
                "product_name": curve["product_name"],
                "sector": curve["sector"],
                "main_contract": curve["main_contract"],
                "near_next_curve": curve["near_next_curve"],
                "basis": curve.get("basis"),
                "anomaly_score": curve.get("anomaly_score"),
            }
            for curve in result.curves
        ],
        "warehouse_inventory": result.warehouse_records,
        "commodity_options": result.option_summaries,
        "member_rankings": result.member_ranking_summaries,
        "session_freshness": [status.to_dict() for status in result.statuses],
        "no_trade_reason": "This engine identifies anomalies; it does not issue trades.",
    }


def _history_record(result: PipelineResult) -> dict[str, Any]:
    return {
        "trade_date": result.trade_date,
        "generated_at": result.generated_at,
        "products": [
            {
                "exchange": curve["exchange"],
                "product": curve["product"],
                "sector": curve["sector"],
                "main_contract": (curve.get("main_contract") or {}).get("contract"),
                "main_close": (curve.get("main_contract") or {}).get("close"),
                "main_close_return_pct": (curve.get("main_contract") or {}).get(
                    "close_return_pct"
                ),
                "main_volume": (curve.get("main_contract") or {}).get("volume"),
                "main_open_interest": (curve.get("main_contract") or {}).get(
                    "open_interest"
                ),
                "near_contract": (curve.get("nearest_liquid_contract") or {}).get(
                    "contract"
                ),
                "next_contract": (curve.get("next_liquid_contract") or {}).get(
                    "contract"
                ),
                "near_minus_next_pct": (curve.get("near_next_curve") or {}).get(
                    "near_minus_deferred_pct"
                ),
                "anomaly_score": curve.get("anomaly_score"),
            }
            for curve in result.curves
        ],
    }


def _contract_meta(result: PipelineResult) -> dict[str, Any]:
    official = {
        (record["exchange"], record["contract"]): record
        for record in result.contract_metadata
    }
    contracts = []
    for record in result.futures_records:
        expiry = contract_month(record["contract"], result.trade_date)
        source = official.get((record["exchange"], record["contract"]), {})
        contracts.append({
            "exchange": record["exchange"],
            "product": record["product"],
            "contract": record["contract"],
            "contract_month": expiry.isoformat() if expiry else None,
            "multiplier": source.get("multiplier"),
            "tick_size": source.get("tick_size"),
            "tick_value": source.get("tick_value"),
            "margin_rate_percent": source.get("margin_rate_percent"),
            "price_limit_percent": source.get("price_limit_percent"),
            "list_date": source.get("list_date"),
            "last_trading_day": source.get("last_trading_day"),
            "last_delivery_day": source.get("last_delivery_day"),
            "metadata_status": source.get("metadata_status", "observed_contract_only"),
        })
    covered = sum(item["metadata_status"] == "official_partial" for item in contracts)
    return {
        "schema_version": 1,
        "trade_date": result.trade_date,
        "generated_at": result.generated_at,
        "contracts": contracts,
        "official_contract_coverage": covered / len(contracts) if contracts else 0.0,
        "warning": "Null trading parameters were not published by the verified exchange interface and must not be inferred.",
    }


def publish_status(result: PipelineResult, data_dir: Path) -> None:
    write_json_atomic(data_dir / "last_run_status.json", result.status_dict())


def publish_raw_options(result: PipelineResult, data_dir: Path) -> Path | None:
    if not result.option_records:
        return None
    path = data_dir / "raw" / result.trade_date / "commodity_options.json"
    write_json_atomic(
        path,
        {
            "schema_version": 1,
            "trade_date": result.trade_date,
            "generated_at": result.generated_at,
            "records": result.option_records,
        },
    )
    return path


def publish_verified(result: PipelineResult, data_dir: Path, history_limit: int = 252) -> None:
    if not result.verified:
        raise ValueError("refusing to publish an unverified commodity snapshot")
    snapshot = _snapshot_payload(result)
    write_json_atomic(data_dir / "latest.json", snapshot)
    write_json_atomic(data_dir / "radar_latest.json", _radar_payload(result))
    write_json_atomic(data_dir / "contract_meta.json", _contract_meta(result))
    write_json_atomic(data_dir / "snapshots" / f"{result.trade_date}.json", snapshot)

    history_path = data_dir / "radar_history.json"
    current = read_json(history_path, default={"schema_version": 1, "records": []})
    records = [
        record
        for record in current.get("records", [])
        if record.get("trade_date") != result.trade_date
    ]
    records.append(_history_record(result))
    records.sort(key=lambda record: record["trade_date"])
    write_json_atomic(
        history_path,
        {"schema_version": 1, "records": records[-history_limit:]},
    )
