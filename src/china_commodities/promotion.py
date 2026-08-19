"""Deterministic shadow-run promotion gates for new data modules."""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping


def update_shadow_state(
    previous: Mapping[str, Any] | None,
    *,
    requested_date: str,
    validation_passed: bool,
    required_pass_days: int = 5,
) -> dict[str, Any]:
    """Count unique validated run dates and never demote an activated module."""

    normalized_date = date.fromisoformat(requested_date).isoformat()
    if required_pass_days < 1:
        raise ValueError("required_pass_days must be positive")
    prior = previous or {}
    activated = prior.get("activated") is True
    pass_dates = sorted(
        {
            date.fromisoformat(str(value)).isoformat()
            for value in prior.get("consecutive_pass_dates") or []
        }
    )
    last_pass_date = pass_dates[-1] if pass_dates else None
    out_of_order = bool(last_pass_date and normalized_date < last_pass_date)
    same_day_prior_pass = normalized_date in pass_dates

    if not activated and not out_of_order:
        if validation_passed:
            if not same_day_prior_pass:
                pass_dates.append(normalized_date)
                pass_dates.sort()
        elif not same_day_prior_pass:
            pass_dates = []
        if len(pass_dates) >= required_pass_days:
            activated = True
    activation_date = prior.get("activation_date")
    if activated and activation_date is None:
        activation_date = normalized_date
    promotion_allowed = bool(activated and validation_passed and not out_of_order)
    return {
        "schema_version": 1,
        "required_pass_days": required_pass_days,
        "activated": activated,
        "activation_date": activation_date,
        "consecutive_pass_dates": pass_dates[-required_pass_days:],
        "consecutive_pass_count": min(len(pass_dates), required_pass_days),
        "last_attempt_date": normalized_date,
        "last_attempt_validation_passed": validation_passed,
        "out_of_order_attempt": out_of_order,
        "promotion_allowed": promotion_allowed,
    }


__all__ = ["update_shadow_state"]
