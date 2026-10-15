"""AA-825 — the retention policies (data standards DM-7 retention table, windows approved in E3).

One TableSpec per job kind. A spec has one or more parts (shared.job keeps succeeded and failed rows
for different windows); each part is "rows of this table whose retention column is older than the
window and that match `where`". Rows referenced by a foreign key are never candidates: the runner
adds a NOT EXISTS per referencing column it finds in pg_constraint (shared.job is referenced by
acp_shared.content_piece, gold_aa_internal.tenant_tour_versions and its own parent_job_id).

Changing a window is a DM-7.5 decision (Nghiệp approves) and makes the next scheduled run a DRY RUN
again (the job compares windows with the last live run).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from shared.llm_client.decide import CACHE_MAX_AGE_DAYS

ARCHIVE_BUCKET = "aa-cis-bronze-005097885195"
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

SPECS: dict[str, TableSpec] = {s.kind: s for s in (
    TableSpec(
        kind="retention_llm_call_log", table="shared.llm_call_log", archive_name="llm_call_log",
        ts_col="created_at", id_type="uuid",
        parts=(Part("raw", 180, archive=True),),
        rollup=ROLLUP_LLM_DAILY,
    ),
    TableSpec(
        kind="retention_tenant_api_usage", table="shared.tenant_api_usage",
        archive_name="tenant_api_usage", ts_col="called_at", id_type="uuid",
        parts=(Part("raw", 90, archive=True),),
        rollup=ROLLUP_TENANT_USAGE_MONTHLY,
    ),
    TableSpec(
        kind="retention_job", table="shared.job", archive_name="job",
        ts_col="finished_at", id_type="uuid",
        parts=(
            Part("succeeded", 30, archive=False, where="t.status = 'succeeded'"),
            # stopped_budget is a failure for retention purposes (it did not finish its work).
            Part("failed", 90, archive=True,
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


def spec_for(kind: str) -> TableSpec:
    try:
        return SPECS[kind]
    except KeyError:
        raise ValueError(f"unknown retention kind {kind!r}; one of {sorted(SPECS)}")
