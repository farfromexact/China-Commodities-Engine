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
    verified: bool = False
    official_complete: bool = False
    validation_errors: list[str] = field(default_factory=list)

    def status_dict(self) -> dict[str, Any]:
        return {
            "run_date": self.trade_date,
            "generated_at": self.generated_at,
            "akshare_version": self.akshare_version,
            "data_fresh": self.verified,
            "official_complete": self.official_complete,
            "validation_errors": self.validation_errors,
            "modules": [status.to_dict() for status in self.statuses],
        }
