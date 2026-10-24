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
from tests.unit.test_aa871_request_id_json_logs import json_lines, sentry_events  # noqa: F401 (fixtures)

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
        for tpl, needs in t.where_any:
            assert "{t}" in tpl and "$1" in tpl and all(n.split(".")[0] in inv.CIS_SCHEMAS for n in needs)
        assert not (t.where and t.where_any), t.table
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
    assert pl.fingerprint("tenant", TID, a) == pl.fingerprint("tenant", TID, list(reversed(a)))
    assert pl.fingerprint("tenant", TID, a) != pl.fingerprint("traveller", TID, a)
    b = [dict(a[0], rows=3), a[1]]
    assert pl.fingerprint("tenant", TID, a) != pl.fingerprint("tenant", TID, b)


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
    j["result"] = {"fingerprint_version": pl.FINGERPRINT_VERSION, **j["result"]}
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


# ── AA-839 review round ─────────────────────────────────────────────────────────────────────────

def test_review_tenant_data_keyed_without_tenant_id_is_registered():
    """owner_scope atoms, pipeline_jobs.tenant and the soft-keyed A3 fingerprints are in the inventory."""
    by = inv.TENANT_TABLE_BY_NAME
    atoms = by["acp_contract.tour_atoms"]
    assert atoms.action == "delete" and atoms.no_tenant_column
    assert [tpl for tpl, _ in atoms.where_any][0] == "{t}.owner_scope = $1::text"
    assert by["shared.pipeline_jobs"].where_any[0][0] == "{t}.tenant = $1::text"
    assert "tenant_tour_version_id" in by["acp_contract.atomize_day_fingerprint"].where_any[0][0]
    assert ("silver_aa_internal.raw_sources", "s3_path") in inv.TENANT_S3_REF_COLUMNS
    assert any("retention archives" in i.item for i in inv.TENANT_CHECKLIST)


def _review_catalog(with_ttv=True):
    tables = ["shared.tenants", "silver_aa_internal.raw_tours", "acp_contract.tour_atoms",
              "acp_contract.atom_embedding", "acp_contract.atomize_day_fingerprint",
              "silver_aa_internal.generated_content"] + (["gold_aa_internal.tenant_tour_versions"] if with_ttv else [])
    cols = {t: "uuid" for t in ("shared.tenants", "silver_aa_internal.raw_tours",
                                "silver_aa_internal.generated_content")}
    if with_ttv:
        cols["gold_aa_internal.tenant_tour_versions"] = "uuid"
    edges = [E("raw_tenant_fk", "silver_aa_internal.raw_tours", "shared.tenants", ("tenant_id",), ("tenant_id",)),
             E("atoms_tour_fk", "acp_contract.tour_atoms", "silver_aa_internal.raw_tours", ("tour_id",), ("tour_id",)),
             E("emb_atom_fk", "acp_contract.atom_embedding", "acp_contract.tour_atoms", ("atom_id",), ("atom_id",))]
    if with_ttv:
        edges += [E("ttv_tenant_fk", "gold_aa_internal.tenant_tour_versions", "shared.tenants", ("tenant_id",),
                    ("tenant_id",)),
                  E("ttv_parent_fk", "gold_aa_internal.tenant_tour_versions", "gold_aa_internal.tenant_tour_versions",
                    ("parent_version_id",), ("id",), nullable=True)]
    keys = {t: ("tenant_id",) for t in cols}
    keys.update({"acp_contract.tour_atoms": ("owner_scope",),
                 "acp_contract.atomize_day_fingerprint": ("tenant_tour_version_id",)})
    return pl.Catalog(set(tables), cols, edges, keys)


def _review_registry(*names):
    return _registry(*[inv.TENANT_TABLE_BY_NAME[n] for n in names])


REVIEW_TABLES = ("shared.tenants", "silver_aa_internal.raw_tours", "acp_contract.tour_atoms",
                 "acp_contract.atomize_day_fingerprint", "silver_aa_internal.generated_content",
                 "gold_aa_internal.tenant_tour_versions")


def test_review_a_tenant_key_other_than_tenant_id_must_be_registered():
    plan = pl.plan_tenant(_review_registry("shared.tenants", "silver_aa_internal.raw_tours"), _review_catalog())
    assert {"acp_contract.tour_atoms", "acp_contract.atomize_day_fingerprint"} <= set(plan.unregistered)
    # an unregistered tenant-key table is never turned into a cascade node (it blocks instead)
    assert "acp_contract.tour_atoms" not in plan.nodes


def test_review_owner_scope_atoms_are_one_node_with_the_tour_path():
    plan = pl.plan_tenant(_review_registry(*REVIEW_TABLES), _review_catalog())
    atoms = plan.nodes["acp_contract.tour_atoms"]
    assert atoms.action == "delete" and atoms.template.startswith("({t}.owner_scope = $1::text) OR (")
    assert "silver_aa_internal.raw_tours" in atoms.soft_parents
    # children of the atoms still cascade from the registered node
    assert plan.nodes["acp_contract.atom_embedding"].action == "cascade"
    pred = pl.predicate(plan.nodes, "acp_contract.atom_embedding", "t")
    assert "owner_scope = $1::text" in pred and "t_rt" not in pred      # the inner alias follows the outer one
    assert plan.order.index("acp_contract.atom_embedding") < plan.order.index("acp_contract.tour_atoms") \
        < plan.order.index("silver_aa_internal.raw_tours")
    assert plan.unregistered == []


def test_review_soft_key_rows_go_before_the_tables_their_predicate_reads():
    plan = pl.plan_tenant(_review_registry(*REVIEW_TABLES), _review_catalog())
    fp = "acp_contract.atomize_day_fingerprint"
    assert set(plan.nodes[fp].soft_parents) == {"gold_aa_internal.tenant_tour_versions",
                                                "silver_aa_internal.generated_content"}
    assert plan.order.index(fp) < plan.order.index("gold_aa_internal.tenant_tour_versions")
    assert plan.order.index(fp) < plan.order.index("silver_aa_internal.generated_content")
    assert not [c for c in plan.checks if c.kind == "unsupported_cycle"]


def test_review_a_branch_whose_table_is_absent_is_left_out():
    plan = pl.plan_tenant(_review_registry(*REVIEW_TABLES[:-1]), _review_catalog(with_ttv=False))
    fp = plan.nodes["acp_contract.atomize_day_fingerprint"]
    assert "tenant_tour_versions" not in fp.template and "generated_content" in fp.template
    assert fp.soft_parents == ("silver_aa_internal.generated_content",)
    only = inv.TenantTable("acp_contract.atomize_day_fingerprint", "delete", "x", no_tenant_column=True,
                           where_any=(("{t}.a IN (SELECT 1 FROM gold_aa_internal.tenant_tour_versions WHERE $1)",
                                       ("gold_aa_internal.tenant_tour_versions",)),))
    plan = pl.plan_tenant(_registry(only), _review_catalog(with_ttv=False))
    assert plan.missing == ["acp_contract.atomize_day_fingerprint"]


def test_review_self_referencing_fk_gets_a_blocker_check():
    plan = pl.plan_tenant(_review_registry(*REVIEW_TABLES), _review_catalog())
    (check,) = [c for c in plan.checks if c.kind == "self_reference"]
    assert check.table == "gold_aa_internal.tenant_tour_versions"
    assert '(c."parent_version_id") IN (SELECT p."id" FROM "gold_aa_internal"."tenant_tour_versions" p' in check.sql
    assert "AND NOT coalesce((c.tenant_id = $1::uuid), false)" in check.sql      # NULL predicate = outside
    # a cascade node with a self FK is checked too (shared.job-like parent_job_id on a reached table)
    reg = _registry(inv.TenantTable("acp_shared.quarter_plan", "delete", "plans"))
    cat = _catalog(["acp_shared.quarter_plan", "acp_shared.qpv"], {"acp_shared.quarter_plan": "uuid"},
                   [E("qpv_plan", "acp_shared.qpv", "acp_shared.quarter_plan", ("plan_id",), ("plan_id",)),
                    E("qpv_self", "acp_shared.qpv", "acp_shared.qpv", ("prev_id",), ("version_id",), deltype="c")])
    plan = pl.plan_tenant(reg, cat)
    assert [c.table for c in plan.checks if c.kind == "self_reference"] == ["acp_shared.qpv"]


class _FakeTx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.conn.savepoints += 1

    async def __aexit__(self, et, e, tb):
        return False


class _FakeConn:
    """fetchval answers by SQL; a check whose SQL contains 'boom' raises, as a missing privilege would."""

    def __init__(self):
        self.savepoints = 0
        self.calls = []

    def transaction(self):
        return _FakeTx(self)

    async def fetchval(self, sql, *args):
        self.calls.append(sql)
        if "boom" in sql:
            raise PermissionError("permission denied")
        if "has_table_privilege" in sql:
            return True
        return 0


@pytest.mark.asyncio
async def test_review_every_guarded_check_runs_in_a_savepoint():
    plan = pl.TenantPlan(nodes={}, checks=[pl.Check("foreign_owner", "booking.x", "d", "SELECT boom"),
                                           pl.Check("cross_tenant", "acp_shared.y", "d", "SELECT count(*) y")],
                         order=[], null_first=[], unregistered=[], missing=[])
    conn = _FakeConn()
    out = await pl.count_tenant(conn, plan, TID)
    assert conn.savepoints == 2
    assert [b["kind"] for b in out["blockers"]] == ["foreign_owner"] and "not readable" in out["blockers"][0]["detail"]
    assert out["checks_passed"][0]["table"] == "acp_shared.y"          # the check after the failure still ran


def test_review_s3_ref_sql_lists_keys_outside_the_tenant_prefixes():
    count_sql, keys_sql = ej._s3_ref_sql("gold_aa_internal.content_exports", "s3_path")
    assert "regexp_replace(t.\"s3_path\", '^s3://[^/]+/', '')" in count_sql
    assert "starts_with(" in count_sql and "unnest($2::text[])" in count_sql
    assert keys_sql.endswith(f"LIMIT {inv.MAX_S3_REF_KEYS + 1}") and "AND NOT EXISTS" in keys_sql


def test_review_apply_refuses_a_used_dry_run():
    used = _dry_job(result={"mode": "dry_run", "fingerprint": FP, "blocked": False, "used_by_apply": "job-9"})
    out = ej.apply_refusals(_apply_payload(), used, OFFBOARDED, NOW, ON)
    assert any("already used by apply job job-9" in r for r in out)


def test_review_tenant_refusals_are_shared_with_the_locked_recheck():
    assert ej.tenant_refusals(OFFBOARDED, NOW, ON) == []
    assert "not offboarded" in ej.tenant_refusals({"cancelled_at": None, "is_active": True}, NOW, ON)[0]
    assert "does not exist" in ej.tenant_refusals(None, NOW, ON)[0]


def test_review_verify_is_a_traveller_dry_run_only():
    assert ej.validate_payload({"subject": "traveller", "email_sha256": H, "verify": True})["verify"] is True
    for p in ({"subject": "tenant", "tenant": TID, "verify": True},
              {"subject": "traveller", "email_sha256": H, "verify": "yes"},
              {"subject": "traveller", "email_sha256": H, "verify": True, "mode": "apply", "dry_run_job_id": TID,
               "approved_fingerprint": FP, "approved_by": "n", "snapshot_id": "s"}):
        with pytest.raises(NonRetryable):
            ej.validate_payload(p)


def test_review_kind_refuses_retry_of_an_apply_and_strips_on_terminal():
    k = get_kind(ej.KIND)
    assert k.on_terminal is ej.on_terminal and k.retry_refusal is ej.retry_refusal
    assert "never retried" in ej.retry_refusal({"payload": {"mode": "apply"}})
    assert ej.retry_refusal({"payload": {"mode": "dry_run"}}) is None


class _Pool:
    def __init__(self):
        self.executed = []

    def acquire(self):
        pool = self

        class _A:
            async def __aenter__(self):
                return pool

            async def __aexit__(self, *a):
                return False
        return _A()

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


@pytest.mark.asyncio
async def test_review_on_terminal_strips_a_traveller_hash_only():
    pool = _Pool()
    await ej.on_terminal(pool, {"id": TID, "status": "failed", "payload": {"subject": "traveller",
                                                                           "email_sha256": H}})
    ((sql, args),) = pool.executed
    assert "jsonb_set(payload, '{email_sha256}'" in sql and args[0] == [TID] and args[1] == "stripped-on-failed"
    pool = _Pool()
    await ej.on_terminal(pool, {"id": TID, "status": "failed", "payload": {"subject": "tenant", "tenant": TID}})
    await ej.on_terminal(pool, {"id": TID, "status": "cancelled",
                                "payload": {"subject": "traveller", "email_sha256": ej.STRIPPED}})
    assert pool.executed == []


@pytest.mark.asyncio
async def test_review_retry_route_refuses_an_erasure_apply(monkeypatch):
    from fastapi import HTTPException
    from api.routers import admin_job_runner as r
    monkeypatch.setattr(r, "verify_admin_secret", lambda s: None)
    retried = []

    async def fake_retry(pool, job_id):
        retried.append(job_id)
        return True
    rows = {"a": {"id": "a", "kind": ej.KIND, "status": "failed", "payload": {"subject": "tenant", "mode": "apply"}},
            "d": {"id": "d", "kind": ej.KIND, "status": "failed", "payload": {"subject": "tenant", "mode": "dry_run"}}}

    async def fake_get(pool, job_id):
        return rows.get(job_id)
    monkeypatch.setattr(r.queue, "retry", fake_retry)
    monkeypatch.setattr(r.queue, "get", fake_get)
    with pytest.raises(HTTPException) as e:
        await r.retry_job("a", _req(), "s", None)
    assert e.value.status_code == 409 and "never retried" in e.value.detail and retried == []
    assert (await r.retry_job("d", _req(), "s", None))["status"] == "queued" and retried == ["d"]


def test_review_sentry_drops_frame_locals_of_erasure_events():
    from shared import sentry_init

    def ev(kind, module="shared.jobs.worker"):
        return {"tags": {"job_kind": kind}, "exception": {"values": [{"stacktrace": {"frames": [
            {"module": module, "vars": {"payload": {"email_sha256": H}}}]}}]}}
    out = sentry_init.before_send(ev(ej.KIND), {})
    assert "vars" not in out["exception"]["values"][0]["stacktrace"]["frames"][0]
    out = sentry_init.before_send(ev("other", module="services.erasure.plan"), {})
    assert "vars" not in out["exception"]["values"][0]["stacktrace"]["frames"][0]
    out = sentry_init.before_send(ev("other"), {})
    assert "vars" not in out["exception"]["values"][0]["stacktrace"]["frames"][0]      # AA-941: no kind keeps locals


def test_review_failed_erasure_job_reaches_sentry_without_the_hash(json_lines, sentry_events,  # noqa: F811
                                                                   monkeypatch):
    """End to end through the worker and a real Sentry client: the traceback is there, the frame locals are not."""
    import asyncio
    import json as _json
    from unittest.mock import AsyncMock, MagicMock

    from shared.jobs import queue, registry
    from shared.jobs import worker as worker_mod

    async def handler(ctx):
        email_sha256 = ctx.payload["email_sha256"]                  # a local holding the hash
        raise RuntimeError(f"refused ({len(email_sha256)})")

    monkeypatch.setitem(registry._KINDS, ej.KIND, registry.JobKind(ej.KIND, handler))
    monkeypatch.setattr(queue, "fail", AsyncMock(return_value="failed"))
    monkeypatch.setattr(worker_mod, "run_terminal_hook", AsyncMock())
    w = worker_mod.Worker(MagicMock(), worker_id="w-aa839")
    job = queue.Job("33333333-3333-3333-3333-333333333333", ej.KIND,
                    {"subject": "traveller", "email_sha256": H, "mode": "apply"}, "running", 1, 1, {}, 0.0)
    asyncio.run(w._execute(job))
    (event,) = [e for e in sentry_events if e["logentry"]["message"] == "job_failed"]
    assert event["exception"]["values"][-1]["type"] == "RuntimeError"
    assert H not in _json.dumps(event, default=str)


# ── fingerprint v2: bound to the subject (AA-839 review: v1 hashed the subject kind and the counts only) ────────

TID2 = "99999999-2222-4333-8444-555555555555"
SALT = "0" * 32
ROWS = [{"table": "shared.tenants", "action": "delete", "rows": 1},
        {"table": "acp_shared.audit_log", "action": "keep", "rows": 3}]


def test_fp_same_counts_for_another_tenant_is_another_fingerprint():
    assert pl.fingerprint("tenant", TID, ROWS) != pl.fingerprint("tenant", TID2, ROWS)
    assert pl.fingerprint("tenant", TID, ROWS) == pl.fingerprint("tenant", TID, list(reversed(ROWS)))
    with pytest.raises(ValueError):
        pl.fingerprint("tenant", "", ROWS)


def test_fp_traveller_is_bound_to_the_hash_and_the_dry_run_salt():
    a = pl.fingerprint("traveller", pl.traveller_key(H, SALT), ROWS)
    assert a != pl.fingerprint("traveller", pl.traveller_key("c" * 64, SALT), ROWS)     # another traveller
    assert a != pl.fingerprint("traveller", pl.traveller_key(H, "1" * 32), ROWS)       # another DRY RUN
    assert H not in pl.traveller_key(H, SALT)


def test_fp_subject_key_is_the_tenant_id_or_the_salted_traveller_key():
    assert ej.subject_key({"subject": "tenant", "tenant": TID}, None) == TID
    assert ej.subject_key({"subject": "traveller", "email_sha256": H}, SALT) == pl.traveller_key(H, SALT)


def test_fp_apply_with_a_fingerprint_from_another_subject_is_refused():
    fp_a, fp_b = pl.fingerprint("tenant", TID, ROWS), pl.fingerprint("tenant", TID2, ROWS)
    dry_a = _dry_job(result={"mode": "dry_run", "fingerprint": fp_a, "blocked": False})
    dry_b = _dry_job(payload={"subject": "tenant", "tenant": TID2, "mode": "dry_run"},
                     result={"mode": "dry_run", "fingerprint": fp_b, "blocked": False})
    # tenant A's DRY RUN and approval used for tenant B: refused on the subject (as before)
    out = ej.apply_refusals(_apply_payload(tenant=TID2, approved_fingerprint=fp_a), dry_a, OFFBOARDED, NOW, ON)
    assert any("another subject" in r for r in out), out
    # tenant B's own DRY RUN with the same counts, but the approval names tenant A's fingerprint: v1 matched here
    out = ej.apply_refusals(_apply_payload(tenant=TID2, approved_fingerprint=fp_a), dry_b, OFFBOARDED, NOW, ON)
    assert out == ["approved_fingerprint differs from the DRY RUN's fingerprint"]
    assert ej.apply_refusals(_apply_payload(tenant=TID2, approved_fingerprint=fp_b), dry_b, OFFBOARDED, NOW, ON) == []


def test_fp_a_v1_dry_run_cannot_be_applied():
    import hashlib
    import json
    v1 = hashlib.sha256(json.dumps({"subject": "tenant", "rows": sorted(
        (r["table"], r["action"], r["rows"]) for r in ROWS)}).encode()).hexdigest()
    dry = _dry_job(result={"mode": "dry_run", "fingerprint": v1, "blocked": False})
    del dry["result"]["fingerprint_version"]
    out = ej.apply_refusals(_apply_payload(approved_fingerprint=v1), dry, OFFBOARDED, NOW, ON)
    assert any("predates the subject-bound fingerprint" in r for r in out), out


def test_fp_a_traveller_dry_run_without_its_salt_cannot_be_applied():
    payload = ej.validate_payload({"subject": "traveller", "email_sha256": H, "mode": "apply", "dry_run_job_id": TID,
                                   "approved_fingerprint": FP, "approved_by": "N", "snapshot_id": "s"})
    tp = {"subject": "traveller", "email_sha256": H, "mode": "dry_run"}
    dry = _dry_job(payload=tp, result={"mode": "dry_run", "fingerprint": FP, "blocked": False})
    out = ej.apply_refusals(payload, dry, None, NOW, ON)
    assert any("no subject salt" in r for r in out), out
    dry = _dry_job(payload=tp, result={"mode": "dry_run", "fingerprint": FP, "blocked": False, "subject_salt": SALT})
    assert ej.apply_refusals(payload, dry, None, NOW, ON) == []


@pytest.mark.asyncio
async def test_fp_strip_hash_drops_the_salt_too():
    pool = _Pool()
    await ej._strip_hash(pool, [TID], H, ej.STRIPPED)
    ((sql, args),) = pool.executed
    assert args[2] == H
    # the key is removed only from an object result (a scalar or array result would make `-` remove an element
    # or raise), and only object payloads are rewritten
    assert "CASE WHEN jsonb_typeof(result) = 'object' THEN result - 'subject_salt' ELSE result END" in sql
    assert "jsonb_typeof(payload) = 'object'" in sql
