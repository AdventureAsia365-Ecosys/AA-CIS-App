"""AA-1087 (S278 lane l18): the Dev-only CIS fixtures (services/tenant_account/e2e_fixtures.py) and the saga's
register-under-an-id rule (services/tenant_account/provision.py), without a database.

The CRM agency ids match AA-Partner's seed (UUIDv5 of the same namespace); a create names its tenant_id only on Dev for
a demo tenant; registering a CRM agency is a DRY RUN until --apply, enqueues the saga, and refuses an id CIS holds as
a real tenant; the subject gate fixture works only on a synthetic tenant, never writes in a DRY RUN, and refuses when
the gate passes its text; the CLI refuses outside Dev before connecting. The saga's database step runs on Postgres in
tests/integration/test_aa1087_e2e_fixtures_cis.py.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from services.acp_shared.subject_gate import GateResult
from services.tenant_account import e2e_fixtures as fx
from services.tenant_account import provision as pv

TID = "7d4c6f0e-1b2a-4c3d-9e8f-0a1b2c3d4e5f"
SYNTH = "5e1b7a2c-3d4f-4a6b-8c9d-0e1f2a3b4c5d"
REQUEST = "2b3c4d5e-6f70-4812-9a3b-4c5d6e7f8091"


def _create(**kw):
    body = {"request_key": "zz-l18-key-1", "mode": "create", "name": "Lantern Cove Travel", "slug": "lantern-cove",
            "plan_tier": "growth", "posts_per_week": 1, "kind": "demo"}
    body.update(kw)
    return body


class FakeConn:
    """fetchrow / fetchval answered by the first matching SQL fragment; records every call."""

    def __init__(self, answers: dict[str, object]):
        self.answers = answers
        self.calls: list[tuple[str, tuple]] = []

    def _answer(self, sql: str):
        for fragment, value in self.answers.items():
            if fragment in sql:
                return value
        return None

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return self._answer(sql)

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        return self._answer(sql)

    @asynccontextmanager
    async def transaction(self):
        yield


# ── the saga rule ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_register_id_round_trips_on_dev(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    req = pv.parse(_create(tenant_id=TID.upper()))
    assert req.register_id == TID
    assert req.to_payload()["tenant_id"] == TID
    assert pv.parse(json.loads(json.dumps(req.to_payload()))) == req


@pytest.mark.parametrize("env,extra,code", [
    ("prod", {}, "register_id_dev_demo_only"),
    ("test", {}, "register_id_dev_demo_only"),
    ("dev", {"kind": "agency", "pilot_weeks": 2}, "register_id_dev_demo_only"),
    ("dev", {"tenant_id": "not-a-uuid"}, "invalid_tenant"),
])
def test_register_id_is_refused(monkeypatch, env, extra, code):
    monkeypatch.setenv("ENVIRONMENT", env)
    body = _create(tenant_id=TID)
    body.update(extra)
    with pytest.raises(pv.ProvisionError) as err:
        pv.parse(body)
    assert err.value.code == code


def test_a_create_without_tenant_id_is_unchanged(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    req = pv.parse(_create())
    assert req.register_id is None and "tenant_id" not in req.to_payload()


@pytest.mark.asyncio
async def test_create_with_register_id_inserts_under_that_id(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    req = pv.parse(_create(tenant_id=TID))
    conn = FakeConn({"FROM shared.plan p": {"plan_key": "content.growth", "membership_plan_id": None, "rpm": 300},
                     "INSERT INTO shared.tenants (tenant_id,": TID})
    conn.execute = AsyncMock()
    tenant_id, _, _ = await pv._create(conn, req, "job:x", [])
    assert tenant_id == TID
    insert = [args for sql, args in conn.calls if "INSERT INTO shared.tenants" in sql]
    assert len(insert) == 1 and insert[0][-1] == TID


@pytest.mark.asyncio
async def test_create_with_a_held_id_is_tenant_exists(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    req = pv.parse(_create(tenant_id=TID))
    conn = FakeConn({"FROM shared.plan p": {"plan_key": "content.growth", "membership_plan_id": None, "rpm": 300},
                     "SELECT 1 FROM shared.tenants WHERE tenant_id": 1})
    with pytest.raises(pv.ProvisionError) as err:
        await pv._create(conn, req, "job:x", [])
    assert err.value.code == "tenant_exists"
    assert not any("INSERT" in sql for sql, _ in conn.calls)


# ── the CRM agencies ──────────────────────────────────────────────────────────────────────────────────────────────

def test_crm_agency_ids_match_the_crm_seed():
    # The Dev id of the CRM's sen-vang agency (AA-Partner api/app/seed/catalogue.py, S258 evidence).
    assert fx.CRM_AGENCIES["sen-vang"].tenant_id == "bab16039-a064-517f-a99f-172b729ad3c4"
    assert {a.slug for a in fx.CRM_AGENCIES.values()} == {"crm-sen-vang", "crm-southern-cross", "crm-harbour-lights"}
    assert all(len(a.request_key) <= 64 for a in fx.CRM_AGENCIES.values())


@pytest.mark.asyncio
async def test_register_dry_run_then_apply(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    conn = FakeConn({})
    dry = await fx.register_crm_agency(conn, None, "sen-vang", apply=False)
    assert dry["state"] == "would_enqueue" and dry["request"]["tenant_id"] == fx.CRM_AGENCIES["sen-vang"].tenant_id
    assert dry["request"]["kind"] == "demo"
    enqueue = AsyncMock(return_value=("job-1", True))
    monkeypatch.setattr(fx.tenant_provision_job, "enqueue_request", enqueue)
    out = await fx.register_crm_agency(conn, "pool", "sen-vang", apply=True)
    assert out["state"] == "enqueued" and out["job_id"] == "job-1"
    assert enqueue.await_args.args[1].register_id == fx.CRM_AGENCIES["sen-vang"].tenant_id


@pytest.mark.asyncio
@pytest.mark.parametrize("synthetic,state", [(True, "registered"), (False, None)])
async def test_register_when_cis_already_holds_the_id(monkeypatch, synthetic, state):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    conn = FakeConn({"FROM shared.tenants WHERE tenant_id = $1::uuid": {"tenant_id": TID},
                     "SELECT is_canary IS TRUE": synthetic})
    if synthetic:
        assert (await fx.register_crm_agency(conn, None, "sen-vang", apply=True))["state"] == state
    else:
        with pytest.raises(fx.Refused, match="not as a synthetic"):
            await fx.register_crm_agency(conn, None, "sen-vang", apply=True)


@pytest.mark.asyncio
async def test_register_unknown_key():
    with pytest.raises(fx.Refused, match="unknown CRM agency"):
        await fx.register_crm_agency(FakeConn({}), None, "nope", apply=False)


# ── the subject gate flag ─────────────────────────────────────────────────────────────────────────────────────────

def _gate_conn(synthetic=True, existing=None):
    return FakeConn({
        "SELECT is_canary IS TRUE": synthetic,
        "cp.content_summary = $2": existing,
        "SELECT flag_id::text": "flag-1",
        "ORDER BY cp.created_at DESC LIMIT 1": {"request_id": REQUEST, "channel": "facebook"},
        "INSERT INTO acp_shared.content_piece": "piece-1",
    })


@pytest.mark.asyncio
async def test_flag_refused_on_a_real_tenant():
    with pytest.raises(fx.Refused, match="not synthetic"):
        await fx.subject_gate_flag(_gate_conn(synthetic=False), tenant=TID, run="s278a", apply=True)


@pytest.mark.asyncio
async def test_flag_dry_run_writes_nothing():
    conn = _gate_conn()
    out = await fx.subject_gate_flag(conn, tenant=SYNTH, run="s278a", apply=False)
    assert out["state"] == "would_create" and out["request_id"] == REQUEST
    assert not any("INSERT" in sql for sql, _ in conn.calls)


@pytest.mark.asyncio
async def test_flag_apply_runs_the_gate(monkeypatch):
    run = AsyncMock(return_value=GateResult(False, ["off_catalog_link"], "msg", {}))
    monkeypatch.setattr(fx.subject_gate, "run", run)
    out = await fx.subject_gate_flag(_gate_conn(), tenant=SYNTH, run="s278a", apply=True)
    assert out["state"] == "created" and out["flag_id"] == "flag-1" and out["codes"] == ["off_catalog_link"]
    piece = run.await_args.kwargs["piece"]
    assert piece["piece_id"] == "piece-1" and piece["request_id"] == REQUEST and piece["tenant_id"] == SYNTH
    assert "lantern-cove.invalid" in piece["content_text"]
    assert out["content_sha256"] == fx.subject_gate.content_sha256(fx.fixture_text("s278a"))


@pytest.mark.asyncio
async def test_flag_refused_when_the_gate_passes(monkeypatch):
    monkeypatch.setattr(fx.subject_gate, "run", AsyncMock(return_value=GateResult(True)))
    with pytest.raises(fx.Refused, match="gate passed"):
        await fx.subject_gate_flag(_gate_conn(), tenant=SYNTH, run="s278a", apply=True)


@pytest.mark.asyncio
async def test_flag_of_the_same_run_is_returned():
    existing = {"piece_id": "piece-0", "content_text": fx.fixture_text("s278a")}
    out = await fx.subject_gate_flag(_gate_conn(existing=existing), tenant=SYNTH, run="s278a", apply=True)
    assert out["state"] == "exists" and out["piece_id"] == "piece-0" and out["flag_id"] == "flag-1"


@pytest.mark.asyncio
async def test_flag_without_a_synthetic_tenant():
    conn = FakeConn({})
    with pytest.raises(fx.Refused, match="no synthetic tenant"):
        await fx.subject_gate_flag(conn, tenant=None, run="s278a", apply=False)


# ── the CLI ──────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("env", [None, "prod", "test", "Dev"])
def test_cli_refuses_outside_dev(monkeypatch, capsys, env):
    if env is None:
        monkeypatch.delenv("ENVIRONMENT", raising=False)
    else:
        monkeypatch.setenv("ENVIRONMENT", env)
    monkeypatch.setattr(fx.asyncpg, "create_pool", AsyncMock(side_effect=AssertionError("must not connect")))
    assert fx.main(["subject-gate-flag", "--apply"]) == 2
    assert "Dev-only" in json.loads(capsys.readouterr().out)["error"]


def test_cli_needs_a_dsn(monkeypatch, capsys):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert fx.main(["register-crm-agency", "--key", "sen-vang"]) == 2


def test_default_run_and_text():
    at = datetime(2026, 11, 12, 7, 5, tzinfo=timezone.utc)
    assert fx.default_run(at) == at.strftime("%Y%m%d%H%M") and fx.RUN_RE.match(fx.default_run(at))
    assert "https://booking.lantern-cove.invalid/tours/dawn-s278a" in fx.fixture_text("s278a")


# ── AA-1086 G1: the pilot rehearsal agency ────────────────────────────────────────────────────────────────────────

def test_rehearsal_agency_id_is_the_crm_seed_key():
    # AA-Partner api/app/seed/catalogue.py agency_id("pilot-rehearsal-01"): the same namespace and name form
    import uuid as _uuid

    assert fx.REHEARSAL.tenant_id == str(_uuid.uuid5(fx.CRM_SYNTHETIC_NAMESPACE,
                                                     "aa-crm-synthetic-agency:pilot-rehearsal-01"))
    assert fx.REHEARSAL_SLUG == "pilot-rehearsal-01"
    assert len(fx.rehearsal_request_key("a" * 31)) <= 64


@pytest.mark.asyncio
async def test_rehearsal_dry_run_then_apply(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    conn = FakeConn({})
    dry = await fx.rehearsal_tenant(conn, None, "run-a", apply=False)
    assert dry["state"] == "would_enqueue" and dry["tenant_id"] == fx.REHEARSAL.tenant_id
    req = dry["request"]
    assert (req["kind"], req["slug"], req["plan_tier"], req["posts_per_week"]) == ("demo", "pilot-rehearsal-01",
                                                                                  "growth", 2)
    assert req["tenant_id"] == fx.REHEARSAL.tenant_id and "pilot_weeks" not in req
    assert {a["product"]: (a["plan_key"], a["seats"]) for a in req["add_ons"]} == {
        "crm": ("crm.core", 3), "tripplanner": ("tripplanner.hosted", None)}
    assert req["request_key"] == "aa1086-pilot-rehearsal-01-run-a"
    enqueue = AsyncMock(return_value=("job-9", True))
    monkeypatch.setattr(fx.tenant_provision_job, "enqueue_request", enqueue)
    out = await fx.rehearsal_tenant(conn, "pool", "run-a", apply=True)
    assert out["state"] == "enqueued" and out["job_id"] == "job-9"
    assert enqueue.await_args.args[1].register_id == fx.REHEARSAL.tenant_id
    assert {a.product for a in enqueue.await_args.args[1].add_ons} == {"crm", "tripplanner"}


@pytest.mark.asyncio
async def test_rehearsal_dry_run_reports_an_earlier_job(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    conn = FakeConn({"FROM shared.job WHERE kind": {"id": "job-1", "status": "failed"}})
    assert (await fx.rehearsal_tenant(conn, None, "run-a", apply=False))["state"] == "job_failed"


@pytest.mark.asyncio
async def test_rehearsal_is_idempotent_when_the_tenant_exists(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    conn = FakeConn({"FROM shared.tenants WHERE tenant_id = $1::uuid": {"tenant_id": fx.REHEARSAL.tenant_id},
                     "SELECT is_canary IS TRUE": True})
    conn.fetch = AsyncMock(return_value=[{"product": "content", "plan_key": "content.growth", "status": "active"}])
    enqueue = AsyncMock()
    monkeypatch.setattr(fx.tenant_provision_job, "enqueue_request", enqueue)
    out = await fx.rehearsal_tenant(conn, "pool", "run-b", apply=True)
    assert out["state"] == "exists" and out["subscriptions"][0]["product"] == "content"
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("answers,match", [
    ({"FROM shared.tenants WHERE tenant_id = $1::uuid": {"tenant_id": "x"}, "SELECT is_canary IS TRUE": False},
     "not as a synthetic"),
    ({"WHERE slug = $1": "1b2c3d4e-5f60-4718-8a9b-0c1d2e3f4a5b"}, "held by another tenant"),
])
async def test_rehearsal_refuses_a_real_or_clashing_tenant(monkeypatch, answers, match):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    with pytest.raises(fx.Refused, match=match):
        await fx.rehearsal_tenant(FakeConn(answers), "pool", "run-c", apply=True)


def test_rehearsal_cli_needs_a_run(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    with pytest.raises(SystemExit):
        fx.main(["rehearsal-tenant"])
    assert fx.main(["rehearsal-tenant", "--run", "run-a"]) == 2       # refused outside Dev before connecting
