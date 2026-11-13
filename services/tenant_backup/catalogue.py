"""AA-1093 part 3 — which CIS tables a per-tenant backup holds, and which rows of each.

The table catalogue is the erasure planner's (services/erasure/inventory.py + plan.py), on purpose: the erasure must
know every table that holds a tenant's rows, and `plan.unregistered` already blocks an erasure (and fails CI,
tests/integration/test_aa839_erasure_job.py) when a table with a tenant key has no inventory entry. A backup built
from the same plan cannot miss a table the erasure would delete; a table the erasure does not know is reported here
as `unregistered` and makes the backup incomplete.

Per plan node (an inventory entry, or a table reached only through foreign keys = `cascade`):
  export   the rows the erasure predicate selects for the tenant (`plan.predicate`), so only the tenant's own rows:
           its raw tours, the atoms it owns or that sit on its own tours, its published rows, never the shared AA
           catalogue (that belongs to the AA internal tenant, which a backup refuses: PITR restores it)
  excluded not in the backup, with a reason (EXCLUDED, or every FK parent of a cascade table is excluded):
           platform ledgers the erasure detaches or merges (cost history stays on the platform), the append-only
           audit chain (never erased, and a hash chain cannot be re-inserted), job history, the model reply cache
           and the per-request usage metering.

Pure (unit-tested): `backup_plan(plan)` -> BackupPlan.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from services.erasure import inventory as inv
from services.erasure import plan as pl

FORMAT = "cis-tenant-backup/1"

# Inventory tables a backup leaves out on purpose, with the reason the manifest prints. Every key must be an
# inventory entry (tests/unit/test_aa1093_tenant_backup.py), so a renamed table cannot leave a stale exclusion.
EXCLUDED: dict[str, str] = {
    "acp_shared.audit_log": "audit_chain: append-only hash chain, never erased; it cannot be re-inserted",
    "shared.job": "job_history: operational queue rows (the backup job itself is one)",
    "shared.pipeline_jobs": "job_history: retired run-tour jobs",
    "shared.llm_cassette": "model_reply_cache: raw model replies kept for replays, not tenant state",
    "shared.tenant_api_usage": "usage_ledger: per-request metering (the monthly rollup is backed up)",
}
# Actions whose rows stay on the platform after an erasure: cost history, not tenant state.
PLATFORM_LEDGER_ACTIONS = {"detach": "platform_ledger: cost history the erasure detaches from the tenant",
                           "merge": "platform_ledger: usage rollup the erasure merges into the platform row",
                           "keep": "audit_chain: kept as is by every erasure"}


class BackupRefused(Exception):
    """A backup that must not run (the code says why)."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True)
class BackupTable:
    table: str                        # schema.table
    action: str                       # the erasure action of the node (delete | cascade)
    where: str                        # WHERE over alias t, $1 = the tenant id as text


@dataclass
class BackupPlan:
    tables: list[BackupTable]                         # exported, in restore order (parents first)
    excluded: list[dict] = field(default_factory=list)  # {table, reason}
    unregistered: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def restore_order(self) -> list[str]:
        return [t.table for t in self.tables]


def check_tenant_id(tenant_id: str) -> str:
    """The tenant id, normalised; refuses the AA internal tenant (it owns the shared catalogue)."""
    import uuid

    try:
        tid = str(uuid.UUID(str(tenant_id)))
    except (TypeError, ValueError):
        raise BackupRefused("bad_tenant_id", "a tenant id is a uuid") from None
    if tid == inv.PLATFORM_TENANT_ID:
        raise BackupRefused("platform_tenant", "the AA internal tenant owns the shared catalogue; PITR restores it")
    return tid


def _reason(node: pl.Node, exported: set[str]) -> str | None:
    """None = export the node; else why it stays out."""
    if node.table in EXCLUDED:
        return EXCLUDED[node.table]
    if node.action in PLATFORM_LEDGER_ACTIONS:
        return PLATFORM_LEDGER_ACTIONS[node.action]
    if node.action == "cascade":
        parents = {e.parent for e in node.via}
        if parents and not parents & exported:
            return "child_of_excluded: reached only through " + ", ".join(sorted(parents))
    return None


def backup_plan(plan: pl.TenantPlan) -> BackupPlan:
    """The exported tables (restore order: the erasure's delete order reversed, so parents come first), the excluded
    ones with their reason, and the planner's unregistered / missing lists."""
    excluded: dict[str, str] = {}
    exported: set[str] = set()
    # parents before children, so a cascade node sees whether a parent is exported
    order = list(reversed(plan.order)) + sorted(tb for tb in plan.nodes if tb not in plan.order)
    changed = True
    while changed:                     # a cascade chain may need several passes (child before its parent in order)
        changed = False
        for tb in order:
            if tb in exported or tb in excluded:
                continue
            node = plan.nodes[tb]
            why = _reason(node, exported)
            if why is None:
                exported.add(tb)
                changed = True
            elif not why.startswith("child_of_excluded") or all(
                    e.parent in excluded for e in node.via):
                excluded[tb] = why
                changed = True
    for tb in order:                   # anything still undecided hangs off another undecided node: leave it out
        if tb not in exported and tb not in excluded:
            excluded[tb] = "child_of_excluded: reached only through tables left out of the backup"
    tables = [BackupTable(tb, plan.nodes[tb].action, pl.predicate(plan.nodes, tb, "t"))
              for tb in order if tb in exported]
    return BackupPlan(tables=tables,
                      excluded=[{"table": tb, "reason": excluded[tb]} for tb in sorted(excluded)],
                      unregistered=list(plan.unregistered), missing=list(plan.missing))


def object_prefix(tenant_id: str, backup_id: str) -> str:
    """Under the gold bucket's `exports/` prefix, which its lifecycle rule expires after 7 days."""
    return f"exports/tenants/{tenant_id}/cis/{backup_id}/"


def table_file(table: str) -> str:
    return f"tables/{table}.jsonl.gz"
