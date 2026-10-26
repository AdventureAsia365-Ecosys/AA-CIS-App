"""AA-992 — response of GET /admin/data-coverage (documented in docs/api/openapi.json). Counts only: no id, no name,
no sample anywhere (tests/unit/test_aa992_data_coverage.py walks every key)."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CoverageRow(_Model):
    id: str
    label: str
    unit: str
    kind: Literal["must", "backlog", "info"]
    polarity: Literal["coverage", "defect", "threshold", "equal"]
    value: Optional[int]
    denominator: Optional[int]
    denominator_label: Optional[str]
    pct: Optional[float]
    missing: Optional[int]
    expected_gap: Optional[int]
    expected_note: Optional[str]
    limit: Optional[int]
    status: Literal["ok", "gap", "info", "na"]
    reason: Optional[str]
    issue: Optional[str]
    drill: Optional[str]
    source: str
    timing_ms: int
    as_of: Optional[str]


class CoverageGroup(_Model):
    id: str
    label: str
    note: Optional[str]
    rows: list[CoverageRow]


class SourceStatus(_Model):
    status: Literal["ok", "error", "timeout", "unavailable"]
    reason: Optional[str]
    timing_ms: int
    tier: Literal["fast", "slow", "crm"]
    reads: str


class CoverageSummary(_Model):
    rows: int
    gaps: int
    backlog: int
    na: int


class DataCoverageResponse(_Model):
    as_of: str
    generated_ms: int
    cached: bool
    stale: bool
    live_tours: Optional[int]
    summary: CoverageSummary
    groups: list[CoverageGroup]
    sources: dict[str, SourceStatus]
