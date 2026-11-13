"""AA-825 — the retention policies (data standards DM-7 retention table, windows approved in E3).

One TableSpec per job kind. A spec has one or more parts (shared.job keeps succeeded and failed rows
for different windows); each part is "rows of this table whose retention column is older than the
window and that match `where`". Rows referenced by a foreign key are never candidates: the runner
adds a NOT EXISTS per referencing column it finds in pg_constraint (shared.job is referenced by
acp_shared.content_piece, gold_aa_internal.tenant_tour_versions and its own parent_job_id).

Changing a window is a DM-7.5 decision (Nghiệp approves) and makes the next scheduled run a DRY RUN
again (the job compares windows with the last live run).

S279 thresholds L1: the windows of llm_call_log, tenant_api_usage and shared.job are settings (Platform › Settings,
cis.retention.*; WINDOW_SETTINGS). SPECS holds their compiled defaults (the migration seed); `current()` /
`current_specs()` apply the values in force, and the job and the status page read those.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional

from shared import settings
from shared.llm_client.decide import CACHE_MAX_AGE_DAYS

# The archive bucket is the bronze bucket: shared.aws_client.bronze.bronze_bucket() (AA-902).
ARCHIVE_PREFIX = "archive"

ROLLUP_LLM_DAILY = "llm_call_daily"
ROLLUP_TENANT_USAGE_MONTHLY = "tenant_api_usage_monthly"


@dataclass(frozen=True)
class Part:
    name: str
    window_days: int
    archive: bool
    where: str = ""              # extra predicate on alias `t`, no bind parameters


@dataclass(frozen=True)
class TableSpec:
    kind: str                    # job kind name
    table: str                   # schema-qualified table
    archive_name: str            # S3 prefix segment: archive/<archive_name>/yyyy/mm/
    ts_col: str                  # retention column (indexed, with id, for the keyset)
    id_type: str                 # SQL type of `id` ('uuid' | 'bigint')
    parts: tuple[Part, ...]
    rollup: Optional[str] = None

    def windows(self) -> dict[str, int]:
        return {p.name: p.window_days for p in self.parts}


# Jev verdict cache (shared/llm_client/decide.py): a verdict older than CACHE_MAX_AGE_DAYS is never
# reused, so decision_log rows past it are history only. Rows with a human outcome (AA-756 truth
# labels, read up to 365 days by /admin/decisions/outcomes/summary) are kept.
DECISION_LOG_WINDOW_DAYS = max(180, CACHE_MAX_AGE_DAYS)

# AA-969 (root ADR 0019 d16): clean-up jobs skip the pilot tenant (shared.tenants.is_pilot, CIS 354). Added to every
# part of a table that holds tenant rows. The windows (90 / 180 days) are longer than the pilot (at most 8 weeks), so
# this only matters for a manual_override run (21 days); after the pilot (the marker cleared) the rows age out as usual.
NOT_PILOT = "NOT EXISTS (SELECT 1 FROM shared.tenants p WHERE p.tenant_id = t.tenant_id AND p.is_pilot)"

# (kind, part) -> the setting that holds the part's window (days).
WINDOW_SETTINGS: dict[tuple[str, str], str] = {
    ("retention_llm_call_log", "raw"): "cis.retention.llm_call_log_days",
    ("retention_tenant_api_usage", "raw"): "cis.retention.tenant_api_usage_days",
    ("retention_job", "succeeded"): "cis.retention.job_succeeded_days",
    ("retention_job", "failed"): "cis.retention.job_failed_days",
}


def _seed(kind: str, part: str) -> int:
    """The compiled default of a window setting (the migration seed)."""
    return int(settings.definition(WINDOW_SETTINGS[(kind, part)]).default_value)


SPECS: dict[str, TableSpec] = {s.kind: s for s in (
    TableSpec(
        kind="retention_llm_call_log", table="shared.llm_call_log", archive_name="llm_call_log",
        ts_col="created_at", id_type="uuid",
        parts=(Part("raw", _seed("retention_llm_call_log", "raw"), archive=True, where=NOT_PILOT),),
        rollup=ROLLUP_LLM_DAILY,
    ),
    TableSpec(
        kind="retention_tenant_api_usage", table="shared.tenant_api_usage",
        archive_name="tenant_api_usage", ts_col="called_at", id_type="uuid",
        parts=(Part("raw", _seed("retention_tenant_api_usage", "raw"), archive=True, where=NOT_PILOT),),
        rollup=ROLLUP_TENANT_USAGE_MONTHLY,
    ),
    TableSpec(
        kind="retention_job", table="shared.job", archive_name="job",
        ts_col="finished_at", id_type="uuid",
        parts=(
            Part("succeeded", _seed("retention_job", "succeeded"), archive=False, where="t.status = 'succeeded'"),
            # stopped_budget is a failure for retention purposes (it did not finish its work).
            Part("failed", _seed("retention_job", "failed"), archive=True,
                 where="t.status IN ('failed', 'cancelled', 'stopped_budget')"),
        ),
    ),
    TableSpec(
        kind="retention_decision_log", table="shared.decision_log", archive_name="decision_log",
        ts_col="created_at", id_type="bigint",
        parts=(Part("unlabelled", DECISION_LOG_WINDOW_DAYS, archive=True, where="t.outcome IS NULL"),),
    ),
    TableSpec(
        # Readers take the newest fetch per (account, service, day) (cost_explorer._RANGE_ROWS_SQL),
        # so a row with a strictly newer fetch of the same key is never read again. Fetches from the
        # last day are left alone so the page and an in-flight refresh see the same rows.
        kind="retention_cost_explorer_snapshot", table="shared.cost_explorer_snapshot",
        archive_name="cost_explorer_snapshot", ts_col="fetched_at", id_type="uuid",
        parts=(Part("superseded", 1, archive=False, where=(
            "EXISTS (SELECT 1 FROM shared.cost_explorer_snapshot n "
            "WHERE n.account_id = t.account_id AND n.service = t.service "
            "AND n.period_start = t.period_start AND n.fetched_at > t.fetched_at)")),),
    ),
)}


def current(spec: TableSpec) -> TableSpec:
    """``spec`` with the windows in force (the settings of WINDOW_SETTINGS; other parts unchanged)."""
    parts = tuple(replace(p, window_days=int(settings.get(WINDOW_SETTINGS[(spec.kind, p.name)])))
                  if (spec.kind, p.name) in WINDOW_SETTINGS else p for p in spec.parts)
    return replace(spec, parts=parts)


def current_specs() -> dict[str, TableSpec]:
    return {kind: current(s) for kind, s in SPECS.items()}


def spec_for(kind: str) -> TableSpec:
    try:
        return current(SPECS[kind])
    except KeyError:
        raise ValueError(f"unknown retention kind {kind!r}; one of {sorted(SPECS)}")
