"""AA-839 — erasure inventory, planner, payload rules and the admin route (no DB).

The DB behaviour (counts, apply in one transaction, verification, catalog completeness on the replayed
migrations) is in tests/integration/test_aa839_erasure_job.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from services.erasure import inventory as inv
from services.erasure import plan as pl
from services.jobs import erasure_job as ej
from shared.jobs.registry import NonRetryable, get_kind

TID = "11111111-2222-4333-8444-555555555555"
H = "a" * 64
FP = "b" * 64
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
ON = {ej.APPLY_FLAG: "true"}


# ── inventory ───────────────────────────────────────────────────────────────────────────────────

def test_inventory_is_well_formed():
    names = [t.table for t in inv.TENANT_TABLES]
    assert len(names) == len(set(names))
    assert {t.action for t in inv.TENANT_TABLES} <= {"delete", "detach", "merge", "keep"}
    for t in inv.TENANT_TABLES:
        assert t.table.split(".")[0] in inv.CIS_SCHEMAS, t.table
        if t.action == "merge":
            assert t.table in inv.MERGE_SQL
        if t.where:
            assert "{t}" in t.where and "$1" in t.where
    assert inv.TENANT_TABLE_BY_NAME["shared.tenants"].action == "delete"
    assert inv.TENANT_TABLE_BY_NAME["acp_shared.audit_log"].action == "keep"
    # a tenant-actor usage row cannot lose its tenant (CHECK chk_tenant_api_usage_actor): never detach it
    assert inv.TENANT_TABLE_BY_NAME["shared.tenant_api_usage"].action == "delete"


def test_job_rows_of_the_tenant_exclude_live_and_erasure_jobs():
    where = inv.TENANT_TABLE_BY_NAME["shared.job"].where
    assert "'queued', 'running'" in where and "data_erasure" in where


def test_traveller_scans_never_delete_staff_or_audit_rows():
    by = {(s.table, s.column): s.action for s in inv.TRAVELLER_EXACT + inv.TRAVELLER_TEXT}
    assert by[("shared.admin_users", "t.email")] == "manual"
    assert by[("shared.alert_recipient", "t.email")] == "manual"
    assert by[("acp_shared.audit_log", "t.details::text")] == "manual"
    for s in inv.TRAVELLER_TEXT:
        assert (s.action == "redact") == bool(s.redact_set)


# ── planner (pure) ──────────────────────────────────────────────────────────────────────────────

def E(con, child, parent, ccols=("x_id",), pcols=("id",), nullable=False, deltype="a"):
    return pl.Edge(con, child, parent, tuple(ccols), tuple(pcols), nullable, deltype)


def _registry(*tables):
    return inv.Registry(tenant_tables=tuple(tables), merge_sql={})


def _catalog(tables, tenant_cols, edges):
    return pl.Catalog(set(tables), dict(tenant_cols), list(edges))


def test_plan_finds_cascade_children_and_orders_children_first():
    reg = _registry(inv.TenantTable("shared.tenants", "delete", "acct"),
                    inv.TenantTable("silver_aa_internal.raw_tours", "delete", "tours"))
    cat = _catalog(
        ["shared.tenants", "silver_aa_internal.raw_tours", "acp_contract.tour_atoms", "acp_contract.atom_embedding"],
        {"shared.tenants": "uuid", "silver_aa_internal.raw_tours": "uuid"},
        [E("raw_tenant_fk", "silver_aa_internal.raw_tours", "shared.tenants", ("tenant_id",), ("tenant_id",)),
         E("atoms_tour_fk", "acp_contract.tour_atoms", "silver_aa_internal.raw_tours", ("tour_id",), ("tour_id",)),
         E("emb_atom_fk", "acp_contract.atom_embedding", "acp_contract.tour_atoms", ("atom_id",), ("atom_id",))])
    plan = pl.plan_tenant(reg, cat)
    assert plan.nodes["acp_contract.tour_atoms"].action == "cascade"
    assert plan.nodes["acp_contract.atom_embedding"].action == "cascade"
    assert plan.order.index("acp_contract.atom_embedding") < plan.order.index("acp_contract.tour_atoms")
    assert plan.order.index("acp_contract.tour_atoms") < plan.order.index("silver_aa_internal.raw_tours")
    assert plan.order[-1] == "shared.tenants"
    sql = pl.predicate(plan.nodes, "acp_contract.atom_embedding", "t")
    assert sql.count(" IN (SELECT ") == 2 and '"silver_aa_internal"."raw_tours"' in sql and "$1::uuid" in sql
    # tenant_id -> tenant_id between two default predicates selects the same rows: no cross-tenant query
    assert not [c for c in plan.checks if c.table == "silver_aa_internal.raw_tours"]
    assert plan.unregistered == [] and plan.missing == []


def test_plan_flags_other_tenants_rows_kept_rows_and_other_owners():
    reg = _registry(inv.TenantTable("silver_aa_internal.raw_tours", "delete", "tours"),
                    inv.TenantTable("gold_aa_internal.published_tours", "delete", "pub"),
                    inv.TenantTable("acp_shared.audit_log", "keep", "chain"),
                    inv.TenantTable("shared.llm_call_log", "detach", "cost"))
    cat = _catalog(
        ["silver_aa_internal.raw_tours", "gold_aa_internal.published_tours", "acp_shared.audit_log",
         "shared.llm_call_log"],
        {t: "uuid" for t in ("silver_aa_internal.raw_tours", "gold_aa_internal.published_tours",
                              "acp_shared.audit_log", "shared.llm_call_log")},
        [E("pub_tour_fk", "gold_aa_internal.published_tours", "silver_aa_internal.raw_tours",
           ("tour_id",), ("tour_id",)),
         E("audit_tour_fk", "acp_shared.audit_log", "silver_aa_internal.raw_tours", ("tour_id",), ("tour_id",)),
         E("log_tour_fk", "shared.llm_call_log", "silver_aa_internal.raw_tours", ("tour_id",), ("tour_id",),
           nullable=True, deltype="n"),
         E("tp_tour_fk", "tripplanner.tour_day", "silver_aa_internal.raw_tours", ("tour_id",), ("tour_id",))])
    plan = pl.plan_tenant(reg, cat)
    kinds = {(c.kind, c.table) for c in plan.checks}
    assert ("cross_tenant", "gold_aa_internal.published_tours") in kinds
    assert ("kept_row_reference", "acp_shared.audit_log") in kinds
    assert ("foreign_owner", "tripplanner.tour_day") in kinds
    assert not any(t == "shared.llm_call_log" for _, t in kinds)          # ON DELETE SET NULL is fine
    cross = next(c for c in plan.checks if c.kind == "cross_tenant")
    assert "AND NOT" in cross.sql and cross.sql.startswith("SELECT count(*)")


def test_plan_lists_unregistered_and_missing_tables():
    reg = _registry(inv.TenantTable("shared.tenants", "delete", "acct"),
                    inv.TenantTable("gold_aa_internal.tenant_tour_versions", "delete", "absent here"))
    cat = _catalog(["shared.tenants", "shared.new_tenant_table"],
                   {"shared.tenants": "uuid", "shared.new_tenant_table": "uuid"}, [])
    plan = pl.plan_tenant(reg, cat)
    assert plan.unregistered == ["shared.new_tenant_table"]
    assert plan.missing == ["gold_aa_internal.tenant_tour_versions"]
    assert any(c.kind == "unregistered" and c.sql is None for c in plan.checks)


def test_text_tenant_id_compares_as_text_and_uuid_as_uuid():
    reg = _registry(inv.TenantTable("acp_deliver.tenant_tour_pages", "delete", "pages"),
                    inv.TenantTable("shared.tenants", "delete", "acct"))
    cat = _catalog(["acp_deliver.tenant_tour_pages", "shared.tenants"],
                   {"acp_deliver.tenant_tour_pages": "text", "shared.tenants": "uuid"}, [])
    plan = pl.plan_tenant(reg, cat)
    assert plan.nodes["acp_deliver.tenant_tour_pages"].template == "{t}.tenant_id::text = $1::text"
    assert plan.nodes["shared.tenants"].template == "{t}.tenant_id = $1::uuid"


def test_fk_cycle_is_broken_by_nulling_a_nullable_column_first():
    reg = _registry(inv.TenantTable("acp_shared.quarter_plan", "delete", "plans"))
    cat = _catalog(
        ["acp_shared.quarter_plan", "acp_shared.quarter_plan_version"], {"acp_shared.quarter_plan": "uuid"},
        [E("qpv_plan_fk", "acp_shared.quarter_plan_version", "acp_shared.quarter_plan", ("plan_id",), ("plan_id",)),
         E("qp_current_fk", "acp_shared.quarter_plan", "acp_shared.quarter_plan_version", ("current_version_id",),
           ("version_id",), nullable=True)])
    plan = pl.plan_tenant(reg, cat)
    assert [nf.edge.conname for nf in plan.null_first] == ["qp_current_fk"]
    assert plan.order == ["acp_shared.quarter_plan_version", "acp_shared.quarter_plan"]
    assert not [c for c in plan.checks if c.kind == "unsupported_cycle"]


def test_fk_cycle_without_a_nullable_column_blocks():
    reg = _registry(inv.TenantTable("acp_shared.a", "delete", "a"), inv.TenantTable("acp_shared.b", "delete", "b"))
    cat = _catalog(["acp_shared.a", "acp_shared.b"], {"acp_shared.a": "uuid", "acp_shared.b": "uuid"},
                   [E("a_b", "acp_shared.a", "acp_shared.b"), E("b_a", "acp_shared.b", "acp_shared.a")])
    plan = pl.plan_tenant(reg, cat)
    assert any(c.kind == "unsupported_cycle" for c in plan.checks)


def test_identifiers_are_quoted():
    assert pl.qname('shared.we"ird') == '"shared"."we""ird"'
    assert pl.qcol('a"b') == '"a""b"'


def test_fingerprint_is_order_free_and_count_sensitive():
    a = [{"table": "x", "action": "delete", "rows": 2}, {"table": "y", "action": "detach", "rows": 1}]
    assert pl.fingerprint("tenant", a) == pl.fingerprint("tenant", list(reversed(a)))
    assert pl.fingerprint("tenant", a) != pl.fingerprint("traveller", a)
    b = [dict(a[0], rows=3), a[1]]
    assert pl.fingerprint("tenant", a) != pl.fingerprint("tenant", b)


def test_traveller_predicates_hash_the_value_never_take_an_address():
    exact = pl.traveller_where(inv.TRAVELLER_EXACT[0])
    assert "sha256" in exact and "lower(btrim(t.email))" in exact and "$1" in exact
    text = pl.traveller_where(inv.TRAVELLER_TEXT[0])
    assert "regexp_matches" in text and "(m.tok)[1]" in text and "LIKE '%@%'" in text


def test_affected_reads_asyncpg_status():
    assert pl._affected("DELETE 3") == 3 and pl._affected("UPDATE 0") == 0 and pl._affected("INSERT 0 4") == 4


# ── payload and apply rules ─────────────────────────────────────────────────────────────────────

def test_validate_payload_defaults_to_dry_run_and_normalises():
    p = ej.validate_payload({"subject": "tenant", "tenant": TID.upper()})
    assert p == {"subject": "tenant", "tenant": TID, "mode": "dry_run"}
    p = ej.validate_payload({"subject": "traveller", "email_sha256": H.upper()})
    assert p["email_sha256"] == H and p["mode"] == "dry_run"


@pytest.mark.parametrize("payload", [
    {"subject": "customer", "tenant": TID},
    {"subject": "tenant", "tenant": "not-a-uuid"},
    {"subject": "tenant", "tenant": inv.PLATFORM_TENANT_ID},
    {"subject": "tenant", "tenant": TID, "email_sha256": H},
    {"subject": "traveller", "email_sha256": "someone@example.com"},
    {"subject": "traveller", "email_sha256": "abc"},
    {"subject": "traveller", "email_sha256": H, "tenant": TID},
    {"subject": "tenant", "tenant": TID, "mode": "live"},
    {"subject": "tenant", "tenant": TID, "mode": "apply"},
    {"subject": "tenant", "tenant": TID, "mode": "apply", "dry_run_job_id": TID, "approved_fingerprint": "x",
     "approved_by": "n", "snapshot_id": "s"},
])
def test_validate_payload_refuses(payload):
    with pytest.raises(NonRetryable):
        ej.validate_payload(payload)


def _apply_payload(**kw):
    p = {"subject": "tenant", "tenant": TID, "mode": "apply", "dry_run_job_id": TID, "approved_fingerprint": FP,
         "approved_by": "Nghiep", "snapshot_id": "dsar-20261023-pre"}
    p.update(kw)
    return ej.validate_payload(p)


def _dry_job(**kw):
    j = {"kind": ej.KIND, "status": "succeeded", "payload": {"subject": "tenant", "tenant": TID, "mode": "dry_run"},
         "result": {"mode": "dry_run", "fingerprint": FP, "blocked": False}, "finished_at": NOW - timedelta(hours=1)}
    j.update(kw)
    return j


OFFBOARDED = {"cancelled_at": NOW - timedelta(days=40)}


def test_apply_allowed_only_when_everything_holds():
    assert ej.apply_refusals(_apply_payload(), _dry_job(), OFFBOARDED, NOW, ON) == []


def test_apply_is_off_by_default():
    out = ej.apply_refusals(_apply_payload(), _dry_job(), OFFBOARDED, NOW, {})
    assert len(out) == 1 and ej.APPLY_FLAG in out[0]
    assert ej.apply_enabled({}) is False and ej.apply_enabled({ej.APPLY_FLAG: "1"}) is False


@pytest.mark.parametrize("dry,tenant,needle", [
    (None, OFFBOARDED, "not a data_erasure job"),
    (_dry_job(status="failed"), OFFBOARDED, "not a succeeded"),
    (_dry_job(result={"mode": "apply", "fingerprint": FP}), OFFBOARDED, "not a succeeded"),
    (_dry_job(payload={"subject": "tenant", "tenant": "99999999-2222-4333-8444-555555555555"}), OFFBOARDED,
     "another subject"),
    (_dry_job(finished_at=NOW - timedelta(hours=30)), OFFBOARDED, "older than"),
    (_dry_job(result={"mode": "dry_run", "fingerprint": "c" * 64}), OFFBOARDED, "differs"),
    (_dry_job(result={"mode": "dry_run", "fingerprint": FP, "blocked": True}), OFFBOARDED, "blockers"),
    (_dry_job(), None, "does not exist"),
    (_dry_job(), {"cancelled_at": None}, "not offboarded"),
    (_dry_job(), {"cancelled_at": NOW - timedelta(days=3)}, "less than 30 days"),
])
def test_apply_refusals(dry, tenant, needle):
    out = ej.apply_refusals(_apply_payload(), dry, tenant, NOW, ON)
    assert any(needle in r for r in out), out


def test_retention_window_is_config_with_a_safe_default():
    assert ej.tenant_retention_days({}) == 30
    assert ej.tenant_retention_days({"ERASURE_TENANT_RETENTION_DAYS": "0"}) == 0
    assert ej.tenant_retention_days({"ERASURE_TENANT_RETENTION_DAYS": "junk"}) == 30
    env = dict(ON, ERASURE_TENANT_RETENTION_DAYS="0")
    assert ej.apply_refusals(_apply_payload(), _dry_job(), {"cancelled_at": NOW - timedelta(days=3)}, NOW, env) == []


def test_kind_is_registered_background_without_retries():
    k = get_kind(ej.KIND)
    assert k is not None and k.max_attempts == 1 and k.concurrency == 1 and k.background


def test_redacted_job_keeps_counts_only():
    job = {"kind": ej.KIND, "payload": {"subject": "traveller", "email_sha256": H, "approved_by": "N"},
           "result": {"subject": "traveller", "mode": "dry_run", "fingerprint": FP, "totals": {"delete": 0},
                      "blocked": False, "rows": [{"table": "x", "ids": ["1"]}]}}
    ej.redact_job(job)
    assert job["payload"]["email_sha256"] == "redacted" and job["payload"]["approved_by"] == "redacted"
    assert "rows" not in job["result"] and job["result"]["redacted"] is True and job["result"]["totals"]


def test_checklist_names_the_other_owners():
    owners = {i["owner"] for i in ej.checklist("traveller", None)}
    assert {"AA-Booking", "AA-TripPlanner-Web", "Cognito"} <= owners
    items = ej.checklist("tenant", TID)
    assert any(TID in i["item"] for i in items)


# ── admin route ─────────────────────────────────────────────────────────────────────────────────

def _req(identity=None):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=None)),
                           scope={"state": {"staff_identity": identity}} if identity else {"state": {}})


@pytest.fixture
def enqueued(monkeypatch):
    from api.routers import admin_job_runner as r
    from shared.jobs import registry
    monkeypatch.setattr(r, "verify_admin_secret", lambda s: None)
    seen = {}

    async def fake_enqueue(pool, kind, payload, **kw):
        seen.update(kind=kind, payload=payload, **kw)
        return "job-1", True
    monkeypatch.setattr(registry, "enqueue", fake_enqueue)
    return seen


@pytest.mark.asyncio
async def test_route_enqueues_a_dry_run(enqueued, monkeypatch):
    from api.routers import admin_job_runner as r
    monkeypatch.delenv(ej.APPLY_FLAG, raising=False)
    out = await r.erasure_run(r.ErasureRunRequest(subject="tenant", tenant_id=TID), _req(), "s", "u1")
    assert out["mode"] == "dry_run" and enqueued["kind"] == ej.KIND
    assert enqueued["payload"] == {"subject": "tenant", "tenant": TID, "mode": "dry_run"}
    out = await r.erasure_run(r.ErasureRunRequest(subject="traveller", email_sha256=H), _req(), "s", None)
    assert enqueued["payload"] == {"subject": "traveller", "email_sha256": H, "mode": "dry_run"}


@pytest.mark.asyncio
async def test_route_refuses_an_address_and_misplaced_approval(enqueued):
    from fastapi import HTTPException
    from api.routers import admin_job_runner as r
    for body in (dict(subject="traveller", email_sha256="someone@example.com"),
                 dict(subject="tenant", tenant_id=TID, approved_fingerprint=FP),
                 dict(subject="tenant", tenant_id=inv.PLATFORM_TENANT_ID)):
        with pytest.raises(HTTPException) as e:
            await r.erasure_run(r.ErasureRunRequest(**body), _req(), "s", None)
        assert e.value.status_code == 422, body
    assert "payload" not in enqueued


@pytest.mark.asyncio
async def test_route_apply_needs_the_flag_and_cognito(enqueued, monkeypatch):
    from fastapi import HTTPException
    from api.routers import admin_job_runner as r
    body = r.ErasureRunRequest(subject="tenant", tenant_id=TID, dry_run=False, dry_run_job_id=TID,
                               approved_fingerprint=FP, approved_by="Nghiep", snapshot_id="dsar-20261023-pre")
    monkeypatch.delenv(ej.APPLY_FLAG, raising=False)
    with pytest.raises(HTTPException) as e:
        await r.erasure_run(body, _req(), "s", None)
    assert e.value.status_code == 403 and ej.APPLY_FLAG in e.value.detail
    monkeypatch.setenv(ej.APPLY_FLAG, "true")
    with pytest.raises(HTTPException) as e:
        await r.erasure_run(body, _req(), "s", None)
    assert e.value.status_code == 403 and "Cognito" in e.value.detail
    who = SimpleNamespace(auth="cognito", admin_id="adm-1")
    out = await r.erasure_run(body, _req(who), "s", None)
    assert out["mode"] == "apply" and enqueued["payload"]["actor"] == "admin:adm-1"
    assert enqueued["created_by"] == "admin:adm-1"


def test_policy_entries_are_admin_only():
    from api.core.staff_authz import POLICY, ROLE_PERMS, TENANT_READ, TENANT_WRITE
    assert POLICY[("POST", "/admin/job-runner/erasure/run")] == TENANT_WRITE
    assert POLICY[("GET", "/admin/job-runner/erasure")] == TENANT_READ
    assert [r for r, perms in ROLE_PERMS.items() if TENANT_WRITE in perms] == ["admin"]


@pytest.mark.asyncio
async def test_status_route_lists_the_inventory(monkeypatch):
    from api.routers import admin_job_runner as r
    monkeypatch.delenv(ej.APPLY_FLAG, raising=False)
    out = await r.erasure_status()
    assert out["apply_enabled"] is False and out["tenant_retention_days"] == 30
    assert {t["table"] for t in out["tenant_tables"]} == {t.table for t in inv.TENANT_TABLES}
