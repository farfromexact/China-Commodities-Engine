"""Read-only checks that prevent repeat vendor requests for verified data."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .storage import read_json


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def verified_futures_available(
    root: str | Path,
    requested_date: str,
    *,
    provider: str = "ifind",
    allow_scoped: bool = False,
) -> bool:
    """Return true only for a same-date, published, verified futures snapshot."""

    target = Path(root)
    snapshot = _mapping(read_json(target / "latest.json", default={}))
    status = _mapping(read_json(target / "last_run_status.json", default={}))
    verified = (
        snapshot.get("scope_verified") is True
        if allow_scoped
        else snapshot.get("verified") is True
    )
    fresh_key = "scope_data_fresh" if allow_scoped else "data_fresh"
    snapshot_is_verified = bool(
        snapshot.get("trade_date") == requested_date
        and verified
        and snapshot.get("futures_contracts")
    )
    if not snapshot_is_verified:
        return False
    status_date = str(status.get("run_date") or "")
    # ``last_run_status`` intentionally describes the newest attempt.  A
    # later, unclosed-date failure must not invalidate a separately promoted
    # snapshot for the requested completed EOD date and cause a duplicate API
    # request on the next recovery run.
    if status_date and status_date != requested_date:
        return True
    return bool(
        status_date == requested_date
        and status.get("primary_provider") == provider
        and status.get(fresh_key) is True
        and not (status.get("validation_errors") or [])
    )


def verified_option_chain_available(
    data_dir: str | Path,
    requested_date: str,
) -> bool:
    """Check compact option status files without loading the large chain JSON."""

    root = Path(data_dir) / "options"
    latest = _mapping(read_json(root / "latest.json", default={}))
    status = _mapping(read_json(root / "last_run_status.json", default={}))
    quality_payload = _mapping(
        read_json(root / "quality_latest.json", default={})
    )
    quality = _mapping(quality_payload.get("quality"))
    coverage = _mapping(status.get("coverage"))
    latest_is_verified = bool(
        latest.get("trade_date") == requested_date
        and int(
            latest.get("record_count") or 0
        )
        > 0
        and quality_payload.get("trade_date") == requested_date
        and latest.get("source_provider") == "ifind_http"
        and _mapping(latest.get("coverage")).get("publish_eligible") is True
        and _mapping(latest.get("coverage")).get("scope_complete") is True
        and quality.get("full_chain_verified") is True
        and quality.get("full_product_scope_verified") is True
    )
    if not latest_is_verified:
        return False
    status_date = str(status.get("trade_date") or "")
    if status_date and status_date != requested_date:
        return True
    return bool(
        status_date == requested_date
        and status.get("source_provider") == "ifind_http"
        and status.get("data_fresh") is True
        and status.get("published") is True
        and not status.get("global_error")
        and coverage.get("publish_eligible") is True
        and coverage.get("scope_complete") is True
        and int(status.get("quote_contract_count") or 0) > 0
    )


def verified_night_session_available(
    data_dir: str | Path,
    trading_date: str,
) -> bool:
    """Return true for a usable timestamp-validated night snapshot.

    The collection is intentionally best-effort: a partial snapshot with at
    least one fresh night quote is reusable, while coverage diagnostics retain
    any individual contracts iFinD could not classify.
    """

    root = Path(data_dir) / "night_session"
    snapshot = _mapping(read_json(root / "latest.json", default={}))
    status = _mapping(read_json(root / "last_run_status.json", default={}))
    coverage = _mapping(status.get("coverage"))
    return bool(
        snapshot.get("trading_date") == trading_date
        and status.get("trading_date") == trading_date
        and snapshot.get("frequency") == "night_session_snapshot"
        and snapshot.get("intraday_used") is True
        and status.get("data_fresh") is True
        and status.get("validation_passed") is True
        and status.get("published") is True
        and int(coverage.get("night_session_contract_count") or 0) > 0
        and snapshot.get("records")
    )


def verified_foundation_available(
    data_dir: str | Path,
    domain: str,
    requested_date: str,
    *,
    provider: str | None = None,
) -> bool:
    """Return true for a same-date promoted Physical or External snapshot.

    When a provider is specified, an old snapshot from another collector is
    intentionally not reused after a source-policy migration.
    """

    if domain not in {"physical", "external"}:
        raise ValueError(f"unsupported foundation domain: {domain}")
    root = Path(data_dir) / domain
    snapshot = _mapping(read_json(root / "latest.json", default={}))
    status = _mapping(read_json(root / "last_run_status.json", default={}))
    normalized_provider = str(provider or "").strip().lower()
    if normalized_provider and (
        str(snapshot.get("provider") or "").strip().lower() != normalized_provider
        or str(status.get("provider") or "").strip().lower() != normalized_provider
    ):
        return False
    promoted_snapshot = bool(
        snapshot.get("requested_date") == requested_date
        and snapshot.get("series")
    )
    if not promoted_snapshot:
        return False
    status_date = str(status.get("requested_date") or "")
    if status_date and status_date != requested_date:
        return True
    return bool(
        status_date == requested_date
        and status.get("validation_passed") is True
        and status.get("published") is True
    )


__all__ = [
    "verified_foundation_available",
    "verified_futures_available",
    "verified_night_session_available",
    "verified_option_chain_available",
]
