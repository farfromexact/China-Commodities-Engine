"""Small serializable models shared by collection and publication."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


ModuleState = Literal["ok", "empty", "error", "skipped"]


@dataclass
class ModuleStatus:
    dataset: str
    scope: str
    state: ModuleState
    trade_date: str
    source_function: str
    records: int = 0
    error: str | None = None
    upstream_source: str | None = None
    is_fresh: bool = False
    is_proxy: bool = False
    is_fallback: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PipelineResult:
    trade_date: str
    generated_at: str
    akshare_version: str
    scope_id: str = "full-market"
    included_exchanges: list[str] = field(default_factory=list)
    excluded_exchanges: list[str] = field(default_factory=list)
    statuses: list[ModuleStatus] = field(default_factory=list)
    futures_records: list[dict[str, Any]] = field(default_factory=list)
    contract_metadata: list[dict[str, Any]] = field(default_factory=list)
    warehouse_records: list[dict[str, Any]] = field(default_factory=list)
    basis_records: list[dict[str, Any]] = field(default_factory=list)
    option_records: list[dict[str, Any]] = field(default_factory=list)
    member_ranking_summaries: list[dict[str, Any]] = field(default_factory=list)
    curves: list[dict[str, Any]] = field(default_factory=list)
    option_summaries: list[dict[str, Any]] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    scope_verified: bool = False
    scope_official_complete: bool = False
    verified: bool = False
    official_complete: bool = False
    validation_errors: list[str] = field(default_factory=list)

    def status_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "run_date": self.trade_date,
            "generated_at": self.generated_at,
            "akshare_version": self.akshare_version,
            "data_fresh": self.verified,
            "official_complete": self.official_complete,
            "scope_data_fresh": self.scope_verified,
            "scope_official_complete": self.scope_official_complete,
            "coverage_scope": {
                "scope_id": self.scope_id,
                "included_exchanges": self.included_exchanges,
                "excluded_exchanges": self.excluded_exchanges,
                "is_full_market": not self.excluded_exchanges,
            },
            "validation_errors": self.validation_errors,
            "modules": [status.to_dict() for status in self.statuses],
        }
