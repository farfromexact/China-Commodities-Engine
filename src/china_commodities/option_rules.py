"""Versioned exchange rules used only for commodity-option static metadata."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class OptionRulesError(ValueError):
    """Raised when the versioned option-rule registry is invalid."""


def default_option_rules_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "option_rules.json"


def load_option_rules(path: str | Path | None = None) -> dict[str, Any]:
    rules_path = Path(path) if path is not None else default_option_rules_path()
    try:
        payload = json.loads(rules_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OptionRulesError(f"could not read option rules: {rules_path}") from exc
    if payload.get("schema_version") != 1:
        raise OptionRulesError("unsupported option-rules schema_version")
    defaults = payload.get("exchange_defaults")
    if not isinstance(defaults, dict):
        raise OptionRulesError("option rules require exchange_defaults")
    for exchange in ("SHFE", "INE", "DCE", "CZCE", "GFEX"):
        rule = defaults.get(exchange)
        if not isinstance(rule, dict):
            raise OptionRulesError(f"missing option rule for {exchange}")
        if str(rule.get("exercise_style") or "").lower() not in {
            "american",
            "european",
        }:
            raise OptionRulesError(f"invalid exercise style for {exchange}")
        if not str(rule.get("source_url") or "").startswith("https://"):
            raise OptionRulesError(f"missing official source URL for {exchange}")
    return payload


def option_rule_for(
    exchange: str,
    product: str,
    *,
    rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = rules or load_option_rules()
    exchange_key = str(exchange).upper()
    product_key = str(product).upper()
    override = (payload.get("product_overrides") or {}).get(
        f"{exchange_key}:{product_key}"
    )
    raw = override or (payload.get("exchange_defaults") or {}).get(exchange_key)
    if not isinstance(raw, dict):
        raise OptionRulesError(
            f"no option rule for {exchange_key}:{product_key}"
        )
    return {
        "exercise_style": str(raw["exercise_style"]).lower(),
        "source_url": str(raw["source_url"]),
        "rules_as_of_date": payload.get("as_of_date"),
    }


__all__ = [
    "OptionRulesError",
    "default_option_rules_path",
    "load_option_rules",
    "option_rule_for",
]
