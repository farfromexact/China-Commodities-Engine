"""Validated, pinned source definitions for the data-foundation modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import json
from pathlib import Path
from typing import Any, Iterable


CORE_PHYSICAL_PRODUCTS: tuple[str, ...] = (
    "I",
    "JM",
    "J",
    "RB",
    "HC",
    "FG",
    "SA",
    "SC",
    "FU",
    "LU",
    "MA",
    "TA",
    "PX",
    "CU",
    "AL",
    "LC",
    "SI",
    "M",
    "Y",
    "P",
)

EXTERNAL_TARGETS: tuple[str, ...] = (
    "WTI",
    "BRENT",
    "DUBAI_OMAN",
    "SINGAPORE_HSFO",
    "SINGAPORE_VLSFO",
    "LME_CU",
    "LME_AL",
    "LME_ZN",
    "LME_NI",
    "COMEX_AU",
    "COMEX_AG",
    "SGX_IRON_ORE",
    "CBOT_SOYBEAN",
    "CBOT_SOYMEAL",
    "CBOT_SOYBEAN_OIL",
    "CBOT_CORN",
    "CBOT_WHEAT",
    "BMD_PALM",
    "ICE_SUGAR",
    "ICE_COTTON",
    "USDCNH",
    "DXY",
)

MAPPING_STATES = frozenset({"verified", "unavailable"})
SERIES_USAGES = frozenset({"physical_evidence", "context_only", "parity_leg"})


class SourceRegistryError(ValueError):
    """Raised when a pinned source registry is ambiguous or incomplete."""


@dataclass(frozen=True)
class SeriesDefinition:
    series_key: str
    indicator_id: str
    name: str
    unit: str
    frequency: str
    max_lag_days: int
    original_source: str
    usage: str
    domain: str
    source_endpoint: str = "edb_service"
    report_id: str | None = None
    metric: str | None = None
    product: str | None = None
    exchange: str | None = None
    target: str | None = None
    contract_kind: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CoverageTarget:
    key: str
    domain: str
    mapping_status: str
    permission_status: str
    missing_reason: str | None
    series: tuple[SeriesDefinition, ...]
    exchange: str | None = None


@dataclass(frozen=True)
class SourceRegistry:
    schema_version: int
    timezone: str
    vendor: str
    mapping_verified_on: str
    mapping_evidence: str
    physical_products: tuple[CoverageTarget, ...]
    external_targets: tuple[CoverageTarget, ...]
    parities: tuple[dict[str, Any], ...]

    def series(self, domain: str | None = None) -> tuple[SeriesDefinition, ...]:
        targets: Iterable[CoverageTarget]
        if domain == "physical":
            targets = self.physical_products
        elif domain == "external":
            targets = self.external_targets
        elif domain is None:
            targets = (*self.physical_products, *self.external_targets)
        else:
            raise ValueError(f"unsupported registry domain: {domain}")
        return tuple(series for target in targets for series in target.series)

    def audit(self) -> dict[str, Any]:
        def summarize(targets: tuple[CoverageTarget, ...]) -> dict[str, Any]:
            return {
                "target_count": len(targets),
                "verified_target_count": sum(
                    target.mapping_status == "verified" for target in targets
                ),
                "unavailable_target_count": sum(
                    target.mapping_status == "unavailable" for target in targets
                ),
                "pinned_series_count": sum(len(target.series) for target in targets),
                "permission_status_counts": {
                    status: sum(target.permission_status == status for target in targets)
                    for status in sorted({target.permission_status for target in targets})
                },
                "unavailable_targets": [
                    target.key
                    for target in targets
                    if target.mapping_status == "unavailable"
                ],
            }

        def matrix(targets: tuple[CoverageTarget, ...]) -> list[dict[str, Any]]:
            return [
                {
                    "key": target.key,
                    "exchange": target.exchange,
                    "mapping_status": target.mapping_status,
                    "permission_status": target.permission_status,
                    "missing_reason": target.missing_reason,
                    "series": [
                        {
                            "series_key": series.series_key,
                            "indicator_id": series.indicator_id,
                            "report_id": series.report_id,
                            "source_endpoint": series.source_endpoint,
                            "unit": series.unit,
                            "frequency": series.frequency,
                            "original_source": series.original_source,
                            "max_lag_days": series.max_lag_days,
                            "usage": series.usage,
                            "contract_kind": series.contract_kind,
                            "current_canary_date": series.metadata.get(
                                "current_canary_date"
                            ),
                            "current_canary_status": series.metadata.get(
                                "current_canary_status"
                            ),
                        }
                        for series in target.series
                    ],
                }
                for target in targets
            ]

        return {
            "schema_version": self.schema_version,
            "timezone": self.timezone,
            "vendor": self.vendor,
            "mapping_verified_on": self.mapping_verified_on,
            "mapping_evidence": self.mapping_evidence,
            "physical": summarize(self.physical_products),
            "external": summarize(self.external_targets),
            "physical_matrix": matrix(self.physical_products),
            "external_matrix": matrix(self.external_targets),
            "parity_count": len(self.parities),
            "production_uses_natural_language_search": False,
        }


def default_registry_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "data_foundation.json"


def _required_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise SourceRegistryError(f"source registry field {field_name} is required")
    return text


def _series_definition(
    raw: dict[str, Any],
    *,
    domain: str,
    product: str | None = None,
    exchange: str | None = None,
    target: str | None = None,
) -> SeriesDefinition:
    max_lag_days = raw.get("max_lag_days")
    if not isinstance(max_lag_days, int) or isinstance(max_lag_days, bool) or max_lag_days < 0:
        raise SourceRegistryError("series max_lag_days must be a non-negative integer")
    usage = _required_text(raw.get("usage"), "usage")
    if usage not in SERIES_USAGES:
        raise SourceRegistryError(f"unsupported series usage: {usage}")
    excluded = {
        "series_key",
        "indicator_id",
        "name",
        "unit",
        "frequency",
        "max_lag_days",
        "original_source",
        "usage",
        "source_endpoint",
        "report_id",
        "metric",
        "contract_kind",
    }
    return SeriesDefinition(
        series_key=_required_text(raw.get("series_key"), "series_key"),
        indicator_id=_required_text(raw.get("indicator_id"), "indicator_id"),
        name=_required_text(raw.get("name"), "name"),
        unit=_required_text(raw.get("unit"), "unit"),
        frequency=_required_text(raw.get("frequency"), "frequency"),
        max_lag_days=max_lag_days,
        original_source=_required_text(raw.get("original_source"), "original_source"),
        usage=usage,
        domain=domain,
        source_endpoint=str(raw.get("source_endpoint") or "edb_service"),
        report_id=(str(raw["report_id"]) if raw.get("report_id") else None),
        metric=(str(raw.get("metric")).strip() if raw.get("metric") else None),
        product=product,
        exchange=exchange,
        target=target,
        contract_kind=(
            str(raw.get("contract_kind")).strip()
            if raw.get("contract_kind")
            else None
        ),
        metadata={key: value for key, value in raw.items() if key not in excluded},
    )


def _validate_mapping(
    mapping_status: Any,
    missing_reason: Any,
    series: tuple[SeriesDefinition, ...],
    key: str,
) -> tuple[str, str | None]:
    status = str(mapping_status or "").strip().lower()
    if status not in MAPPING_STATES:
        raise SourceRegistryError(f"{key} has unsupported mapping_status: {status}")
    reason = str(missing_reason or "").strip() or None
    if status == "verified" and not series:
        raise SourceRegistryError(f"{key} is verified but has no pinned series")
    if status == "unavailable" and (series or not reason):
        raise SourceRegistryError(
            f"{key} unavailable mapping must have no series and an explicit reason"
        )
    return status, reason


def _permission_status(mapping_status: str, reason: str | None) -> str:
    if mapping_status == "verified":
        return "verified"
    text = str(reason or "").lower()
    if "permission" in text or "权限" in text:
        return "no_permission"
    if "returned no" in text or "no data" in text or "无数据" in text:
        return "no_data"
    return "not_verified"


def load_source_registry(path: str | Path | None = None) -> SourceRegistry:
    registry_path = Path(path) if path is not None else default_registry_path()
    try:
        raw = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceRegistryError(f"could not read source registry: {registry_path}") from exc
    if raw.get("schema_version") != 1:
        raise SourceRegistryError("unsupported source registry schema_version")
    mapping_verified_on = _required_text(
        raw.get("mapping_verified_on"), "mapping_verified_on"
    )
    try:
        mapping_verified_on = date.fromisoformat(mapping_verified_on).isoformat()
    except ValueError as exc:
        raise SourceRegistryError("mapping_verified_on must be an ISO date") from exc

    physical: list[CoverageTarget] = []
    physical_keys: list[str] = []
    for item in raw.get("physical_products") or []:
        if not isinstance(item, dict):
            raise SourceRegistryError("physical product entries must be objects")
        product = _required_text(item.get("product"), "product").upper()
        exchange = _required_text(item.get("exchange"), "exchange").upper()
        series = tuple(
            _series_definition(
                value,
                domain="physical",
                product=product,
                exchange=exchange,
            )
            for value in (item.get("series") or [])
        )
        status, reason = _validate_mapping(
            item.get("mapping_status"), item.get("missing_reason"), series, product
        )
        physical.append(
            CoverageTarget(
                key=product,
                domain="physical",
                mapping_status=status,
                permission_status=_permission_status(status, reason),
                missing_reason=reason,
                series=series,
                exchange=exchange,
            )
        )
        physical_keys.append(product)
    if tuple(physical_keys) != CORE_PHYSICAL_PRODUCTS:
        raise SourceRegistryError(
            "physical product scope/order must equal the approved 20-product list"
        )

    external: list[CoverageTarget] = []
    external_keys: list[str] = []
    for item in raw.get("external_series") or []:
        if not isinstance(item, dict):
            raise SourceRegistryError("external entries must be objects")
        target = _required_text(item.get("target"), "target").upper()
        values = ()
        if item.get("mapping_status") == "verified":
            values = (
                _series_definition(item, domain="external", target=target),
            )
        status, reason = _validate_mapping(
            item.get("mapping_status"), item.get("missing_reason"), values, target
        )
        external.append(
            CoverageTarget(
                key=target,
                domain="external",
                mapping_status=status,
                permission_status=_permission_status(status, reason),
                missing_reason=reason,
                series=values,
            )
        )
        external_keys.append(target)
    if tuple(external_keys) != EXTERNAL_TARGETS:
        raise SourceRegistryError(
            "external target scope/order must equal the approved target list"
        )

    all_series = [
        series.series_key
        for target in (*physical, *external)
        for series in target.series
    ]
    if len(all_series) != len(set(all_series)):
        raise SourceRegistryError("source registry contains duplicate series_key values")
    indicator_domains = [
        (series.domain, series.indicator_id)
        for target in (*physical, *external)
        for series in target.series
    ]
    if len(indicator_domains) != len(set(indicator_domains)):
        raise SourceRegistryError(
            "source registry contains duplicate indicator IDs within a domain"
        )

    parities = tuple(raw.get("parities") or [])
    if any(
        not isinstance(item, dict)
        or not str(item.get("parity_key") or "").strip()
        or item.get("status") not in {"verified", "unavailable"}
        for item in parities
    ):
        raise SourceRegistryError("parity registry entries are invalid")
    return SourceRegistry(
        schema_version=1,
        timezone=_required_text(raw.get("timezone"), "timezone"),
        vendor=_required_text(raw.get("vendor"), "vendor"),
        mapping_verified_on=mapping_verified_on,
        mapping_evidence=_required_text(raw.get("mapping_evidence"), "mapping_evidence"),
        physical_products=tuple(physical),
        external_targets=tuple(external),
        parities=parities,
    )


__all__ = [
    "CORE_PHYSICAL_PRODUCTS",
    "EXTERNAL_TARGETS",
    "CoverageTarget",
    "SeriesDefinition",
    "SourceRegistry",
    "SourceRegistryError",
    "default_registry_path",
    "load_source_registry",
]
