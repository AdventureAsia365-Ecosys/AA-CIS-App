"""AA-1024 D2 (S263 gateway plan, OPS-04) — model lifecycle as data: the pure core (shared/llm_client/
model_lifecycle.py), the Bedrock list that fails soft, the job llm_model_lifecycle_sync (dry run, live write, bells,
schedule), its sampler part and admin route, and the OPS-12 digest lifecycle section. No AWS: every Bedrock client is
a fake. Model ids are invented."""
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.exceptions import ClientError

from services.jobs import llm_model_lifecycle_job as job
from shared.jobs.registry import NonRetryable, get_kind
from shared.llm_client import model_lifecycle as ml
from shared.llm_client.connections import BUILTIN_CONNECTIONS, ProviderConnection

T0 = datetime(2026, 11, 3, 4, 30, tzinfo=timezone.utc)
TODAY = T0.date()


def _summary(model_id, status="ACTIVE", eol=None, legacy=None):
    lc = {"status": status}
    if eol:
        lc["endOfLifeTime"] = datetime.fromisoformat(eol + "T00:00:00+00:00")
    if legacy:
        lc["legacyTime"] = datetime.fromisoformat(legacy + "T00:00:00+00:00")
    return {"modelId": model_id, "modelLifecycle": lc}


def _listing(key, *summaries):
    return ml.Listing(key, ml.ACCESS_OK, {s["modelId"]: ml.summarize(s) for s in summaries})


REFUSED = ml.Listing("acc3", ml.ACCESS_REFUSED, error_class="access_denied", error="AccessDeniedException: no")


# ── pure core ──────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("wire, expected", [
    ("global.zeta.writer-large-v2", "zeta.writer-large-v2"),
    ("us.zeta.writer-small-v1:0", "zeta.writer-small-v1:0"),
    ("us-gov.zeta.writer-small-v1:0", "zeta.writer-small-v1:0"),
    ("zeta.embed-v9:0", "zeta.embed-v9:0"),
    ("arn:aws:bedrock:us-west-1::foundation-model/zeta.embed-v9:0", "zeta.embed-v9:0"),
    ("arn:aws:bedrock:us-west-1:123456789012:inference-profile/global.zeta.writer-large-v2", "zeta.writer-large-v2"),
    ("arn:aws:bedrock:us-west-1:123456789012:application-inference-profile/abc123", None),
    ("", None), (None, None),
])
def test_the_foundation_model_id_of_a_wire_id(wire, expected):
    assert ml.foundation_model_id(wire) == expected


def test_summarize_keeps_the_status_and_the_dates():
    s = ml.summarize(_summary("zeta.a", "LEGACY", eol="2027-01-15", legacy="2026-10-01"))
    assert s == {"model_id": "zeta.a", "status": "legacy", "eol_date": date(2027, 1, 15),
                 "legacy_date": date(2026, 10, 1), "extended_access_date": None}


def test_match_exact_then_one_versioned_id_never_an_ambiguous_one():
    listed = {"zeta.a-v1:0": 1, "zeta.b-v1:0": 2, "zeta.b-v1:0:200k": 3, "zeta.c": 4}
    assert ml.match("zeta.c", listed) == 4
    assert ml.match("zeta.a-v1", listed) == 1
    assert ml.match("zeta.b-v1:0", listed) == 2          # exact wins over the context variant
    assert ml.match("zeta.b-v1", listed) is None          # two ids share the base: no guess
    assert ml.match("zeta.gone", listed) is None


def test_a_model_without_a_bedrock_deployment_is_untracked():
    lc = ml.model_lifecycle("vendor-x", [], {}, None, checked_at=T0)
    assert lc.status == ml.UNTRACKED and lc.eol_date is None and "no Bedrock deployment" in lc.detail["reason"]


def test_the_worst_answered_status_and_the_earliest_dates_win():
    listings = {"acc2": _listing("acc2", _summary("zeta.a", "ACTIVE", eol="2027-06-01")),
                "acc3": _listing("acc3", _summary("zeta.a", "LEGACY", eol="2027-02-01", legacy="2026-10-01"))}
    lc = ml.model_lifecycle("a", [("acc2", "zeta.a"), ("acc3", "global.zeta.a")], listings, None, checked_at=T0)
    assert lc.status == ml.LEGACY and lc.eol_date == date(2027, 2, 1) and lc.legacy_date == date(2026, 10, 1)
    assert lc.detail["connections"]["acc3"]["model_id"] == "zeta.a"
    assert lc.detail["source"] == "bedrock:ListFoundationModels"


def test_an_answered_account_without_the_model_marks_it_unlisted():
    listings = {"acc2": _listing("acc2", _summary("zeta.other"))}
    lc = ml.model_lifecycle("a", [("acc2", "zeta.a")], listings, None, checked_at=T0)
    assert lc.status == ml.UNLISTED and lc.detail["connections"]["acc2"]["status"] == "unlisted"


def test_a_status_bedrock_adds_later_counts_as_legacy():
    listings = {"acc2": _listing("acc2", _summary("zeta.a", "SUNSETTING"))}
    lc = ml.model_lifecycle("a", [("acc2", "zeta.a")], listings, None, checked_at=T0)
    assert lc.status == ml.LEGACY and lc.detail["connections"]["acc2"]["listed_status"] == "sunsetting"


def test_a_refusing_account_keeps_the_earlier_answered_status():
    prev = {"status": "active", "eol_date": date(2027, 3, 1), "legacy_date": None}
    lc = ml.model_lifecycle("a", [("acc3", "global.zeta.a")], {"acc3": REFUSED}, prev, checked_at=T0)
    assert lc.status == ml.ACTIVE and lc.eol_date == date(2027, 3, 1) and lc.kept
    assert lc.detail["kept_from_previous"] is True
    assert lc.detail["connections"]["acc3"] == {"access": "refused", "error_class": "access_denied",
                                                "wire_id": "global.zeta.a"}


def test_a_refusing_account_with_no_earlier_status_is_unknown_never_null():
    lc = ml.model_lifecycle("a", [("acc3", "global.zeta.a")], {"acc3": REFUSED}, {"status": None}, checked_at=T0)
    assert lc.status == ml.UNKNOWN and not lc.kept
    # an earlier "unknown" or "untracked" is not an answer to keep either
    lc = ml.model_lifecycle("a", [("acc3", "x")], {"acc3": REFUSED}, {"status": "unknown"}, checked_at=T0)
    assert lc.status == ml.UNKNOWN


def test_deployment_pairs_use_bedrock_rows_then_the_catalog_profiles():
    deps = [{"model_key": "a", "connection_key": "acc2", "wire_id": "zeta.a", "enabled": False},
            {"model_key": "a", "connection_key": "openai-direct", "wire_id": "w", "enabled": True},
            {"model_key": "b", "connection_key": "acc3", "wire_id": "global.zeta.b", "enabled": True}]
    keys = {"acc2", "acc3"}
    assert ml.deployment_pairs({"model_key": "a", "provider": "bedrock"}, deps, keys) == [("acc2", "zeta.a")]
    assert ml.deployment_pairs({"model_key": "c", "provider": "bedrock",
                                "bedrock_profile_ids": '{"acc3": "global.zeta.c", "acc9": "x"}'}, deps, keys) == [
        ("acc3", "global.zeta.c")]
    assert ml.deployment_pairs({"model_key": "d", "provider": "openai",
                                "bedrock_profile_ids": {"acc3": "x"}}, deps, keys) == []


def test_route_keys_expand_aliases_fallbacks_and_shadows_of_active_routes():
    routes = [{"stage": "s_write", "model_id": "alias-w", "fallback_model_ids": ["b"], "shadow_model_id": "c",
               "is_active": True},
              {"stage": "s_judge", "model_id": "b", "fallback_model_ids": [], "shadow_model_id": None,
               "is_active": True},
              {"stage": "s_off", "model_id": "z", "fallback_model_ids": [], "shadow_model_id": None,
               "is_active": False}]
    aliases = [{"alias_key": "alias-w", "position": 2, "model_key": "a2"},
               {"alias_key": "alias-w", "position": 1, "model_key": "a1"}]
    assert ml.route_keys(routes, aliases) == {"a1": ["s_write"], "a2": ["s_write"], "b": ["s_judge", "s_write"],
                                              "c": ["s_write"]}


@pytest.mark.parametrize("status, eol, warned", [
    ("active", date(2026, 12, 1), True),      # 28 days
    ("active", date(2027, 2, 1), True),       # exactly 90 days
    ("active", date(2027, 2, 2), False),      # 91 days
    ("active", None, False),
    ("active", date(2026, 10, 1), True),      # past
    ("legacy", None, True),
    ("unlisted", None, True),
    ("unknown", None, False),
    ("untracked", None, False),
])
def test_the_warning_window_is_ninety_days(status, eol, warned):
    w = ml.warning("a", status, eol, TODAY, ["s_write"])
    assert (w is not None) is warned
    if w:
        assert "a" in w["message"] and "s_write" in w["message"]


def test_warnings_cover_only_routed_models_soonest_first():
    rows = [{"model_key": "a", "lifecycle_status": "active", "eol_date": date(2027, 1, 10)},
            {"model_key": "b", "lifecycle_status": "legacy", "eol_date": None},
            {"model_key": "c", "lifecycle_status": "active", "eol_date": date(2026, 11, 20)},
            {"model_key": "not-routed", "lifecycle_status": "legacy", "eol_date": date(2026, 11, 5)}]
    out = ml.warnings(rows, {"a": ["s1"], "b": ["s2"], "c": ["s3"]}, TODAY)
    assert [w["model_key"] for w in out] == ["c", "a", "b"]


# ── the Bedrock list fails soft ─────────────────────────────────────────────────────────────────────────────────

class _Client:
    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.calls = answer, error, 0

    def list_foundation_models(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.answer


def test_an_answering_account_lists_its_models():
    client = _Client({"modelSummaries": [_summary("zeta.a", eol="2027-01-01"), {"modelId": None}]})
    out = ml.list_connection(BUILTIN_CONNECTIONS["acc2"], client_factory=lambda c: client)
    assert out.access == "ok" and list(out.models) == ["zeta.a"] and client.calls == 1


def test_a_refusing_account_is_recorded_masked_not_raised():
    err = ClientError({"Error": {"Code": "AccessDeniedException",
                                 "Message": "User: arn:aws:sts::123456789012:assumed-role/r/s is not authorized"}},
                      "ListFoundationModels")
    out = ml.list_connection(BUILTIN_CONNECTIONS["acc3"], client_factory=lambda c: _Client(error=err))
    assert out.access == "refused" and out.error_class == "access_denied" and out.models == {}
    assert "123456789012" not in out.error and "arn:aws:<redacted>" in out.error


def test_an_assume_role_failure_or_other_error_never_raises():
    out = ml.list_connection(BUILTIN_CONNECTIONS["acc3"],
                             client_factory=lambda c: (_ for _ in ()).throw(RuntimeError("socket closed")))
    assert out.access in ("error", "refused") and out.models == {}


def test_a_disabled_or_unconfigured_connection_is_not_called():
    called = []
    off = ProviderConnection(connection_key="acc9", kind="bedrock", auth_mode="task_role", label="x",
                             data_scope="aws_only", enabled=False)
    assert ml.list_connection(off, client_factory=called.append).access == "disabled"
    half = ProviderConnection(connection_key="acc8", kind="bedrock", auth_mode="assume_role", label="x",
                              data_scope="aws_only", enabled=True)          # no role, ExternalId, region, account
    out = ml.list_connection(half, client_factory=called.append)
    assert out.access == "unconfigured" and "assume_role needs" in out.error
    assert called == []


# ── the job ─────────────────────────────────────────────────────────────────────────────────────────────────────

def _state(ready=True):
    return {
        "connections": {"acc2": BUILTIN_CONNECTIONS["acc2"], "acc3": BUILTIN_CONNECTIONS["acc3"]},
        "deployments": [{"model_key": "writer-a", "connection_key": "acc3", "wire_id": "global.zeta.writer-a",
                         "enabled": True},
                        {"model_key": "judge-b", "connection_key": "acc2", "wire_id": "us.zeta.judge-b-v1:0",
                         "enabled": True},
                        {"model_key": "vendor-c", "connection_key": "openai-direct", "wire_id": "vendor-c-1",
                         "enabled": True}],
        "catalog": [{"model_key": "writer-a", "provider": "bedrock", "bedrock_profile_ids": {},
                     "enabled": True, "lifecycle_status": None, "eol_date": None, "legacy_date": None},
                    {"model_key": "judge-b", "provider": "bedrock", "bedrock_profile_ids": {}, "enabled": True,
                     "lifecycle_status": None, "eol_date": None, "legacy_date": None},
                    {"model_key": "vendor-c", "provider": "openai", "bedrock_profile_ids": {}, "enabled": True,
                     "lifecycle_status": None, "eol_date": None, "legacy_date": None}],
        "routes": [{"stage": "s_write", "model_id": "writer-a", "fallback_model_ids": ["vendor-c"],
                    "shadow_model_id": None, "is_active": True},
                   {"stage": "s_judge", "model_id": "judge-b", "fallback_model_ids": [], "shadow_model_id": None,
                    "is_active": True}],
        "aliases": [], "ready": ready}


def _listings():
    return {"acc2": _listing("acc2", _summary("zeta.judge-b-v1:0", "LEGACY", eol="2026-12-15")), "acc3": REFUSED}


class _Pool:
    """acquire() -> a fake connection recording executemany / execute and answering fetchval."""

    def __init__(self, recent_bell=False):
        self.db = MagicMock()
        self.db.executemany = AsyncMock()
        self.db.execute = AsyncMock()
        self.db.fetchval = AsyncMock(return_value=recent_bell)

    def acquire(self):
        pool = self

        class _Cm:
            async def __aenter__(self):
                return pool.db

            async def __aexit__(self, *a):
                return False
        return _Cm()


def _ctx(payload, pool):
    ctx = MagicMock()
    ctx.payload, ctx.job_id, ctx.pool = payload, "j1", pool
    return ctx


def test_the_kind_is_background_gate_exempt_and_retried_once():
    from shared.jobs.worker import load_kinds
    load_kinds()
    k = get_kind(job.KIND)
    assert k.max_attempts == 2 and k.background and k.deploy_gate_exempt and not k.deploy_blocking
    assert k.concurrency == 1


def test_the_switch_is_on_by_default(monkeypatch):
    monkeypatch.delenv(job.SWITCH_ENV, raising=False)
    assert job.schedule_enabled() is True
    monkeypatch.setenv(job.SWITCH_ENV, "off")
    assert job.schedule_enabled() is False


def test_every_catalog_row_gets_a_status_and_routed_models_none_null():
    rows = job.plan_rows(_state(), _listings(), now=T0)
    assert {r.model_key: r.status for r in rows} == {"writer-a": "unknown", "judge-b": "legacy",
                                                      "vendor-c": "untracked"}
    summary = job.summarize_run(_state(), _listings(), rows, TODAY)
    assert summary["routed"] == 3 and summary["routed_without_status"] == []
    assert summary["routed_unknown"] == ["writer-a"]
    assert [w["model_key"] for w in summary["warnings"]] == ["judge-b"]
    assert {c["connection_key"]: c["access"] for c in summary["connections"]} == {"acc2": "ok", "acc3": "refused"}


@pytest.mark.asyncio
async def test_a_dry_run_writes_nothing(monkeypatch):
    monkeypatch.setattr(job, "read_state", AsyncMock(return_value=_state()))
    monkeypatch.setattr(job, "list_all", AsyncMock(return_value=_listings()))
    pool = _Pool()
    out = await job.run(_ctx({"trigger": "manual", "dry_run": True}, pool))
    assert out["dry_run"] is True and out["updated"] == 0 and out["bells"] == 0
    assert {w["model_key"]: w["status"] for w in out["would_write"]}["judge-b"] == "legacy"
    pool.db.executemany.assert_not_awaited()
    pool.db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_live_run_writes_every_row_and_rings_one_bell_per_warned_routed_model(monkeypatch):
    monkeypatch.setattr(job, "read_state", AsyncMock(return_value=_state()))
    monkeypatch.setattr(job, "list_all", AsyncMock(return_value=_listings()))
    pool = _Pool()
    out = await job.run(_ctx({"trigger": "schedule"}, pool))
    assert out["dry_run"] is False and out["updated"] == 3 and out["bells"] == 1
    sql, args = pool.db.executemany.await_args.args
    assert "UPDATE shared.llm_model_catalog" in sql and "lifecycle_status = $2" in sql
    by_key = {a[0]: a for a in args}
    assert by_key["judge-b"][1:4] == ("legacy", date(2026, 12, 15), None)
    assert '"access": "refused"' in by_key["writer-a"][4]
    bell = pool.db.execute.await_args.args
    assert bell[2] == job.BELL_EVENT and bell[3] == "llm_model:judge-b" and "s_judge" in bell[4]
    assert out["connections"][1]["error_class"] == "access_denied"        # acc3 refused, the run still succeeded


@pytest.mark.asyncio
async def test_a_bell_is_not_repeated_within_seven_days(monkeypatch):
    monkeypatch.setattr(job, "read_state", AsyncMock(return_value=_state()))
    monkeypatch.setattr(job, "list_all", AsyncMock(return_value=_listings()))
    pool = _Pool(recent_bell=True)
    out = await job.run(_ctx({"trigger": "schedule"}, pool))
    assert out["bells"] == 0
    pool.db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_run_before_migration_352_is_refused_or_skipped(monkeypatch):
    monkeypatch.setattr(job, "read_state", AsyncMock(return_value=_state(ready=False)))
    lister = AsyncMock()
    monkeypatch.setattr(job, "list_all", lister)
    with pytest.raises(NonRetryable, match="352"):
        await job.run(_ctx({"trigger": "manual"}, _Pool()))
    out = await job.run(_ctx({"trigger": "schedule"}, _Pool()))          # the nightly run is a no-op, not a failure
    assert out["skipped"].startswith("CIS migration 352") and out["updated"] == 0
    lister.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_all_runs_each_bedrock_connection_once():
    seen = []

    def lister(conn):
        seen.append(conn.connection_key)
        return ml.Listing(conn.connection_key, "ok")
    out = await job.list_all(_state()["connections"], lister=lister)
    assert sorted(out) == ["acc2", "acc3"] and sorted(seen) == ["acc2", "acc3"]


@pytest.mark.asyncio
async def test_the_nightly_enqueue_needs_the_switch_and_the_hour(monkeypatch):
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=False)
    enq = AsyncMock(return_value=("job-1", True))
    monkeypatch.setattr(job, "enqueue", enq)
    monkeypatch.setenv(job.SWITCH_ENV, "off")
    assert await job.enqueue_nightly(pool, now=T0) == (None, False, TODAY)
    monkeypatch.delenv(job.SWITCH_ENV)
    assert (await job.enqueue_nightly(pool, now=T0.replace(hour=job.SCHEDULE_HOUR_UTC - 1)))[1] is False
    assert await job.enqueue_nightly(pool, now=T0) == ("job-1", True, TODAY)
    assert enq.await_args.kwargs["idempotency_key"] == "llm_model_lifecycle_sync:2026-11-03"
    pool.fetchval = AsyncMock(return_value=True)                   # today's run exists
    assert (await job.enqueue_nightly(pool, now=T0))[1] is False


@pytest.mark.asyncio
async def test_the_sampler_part_queues_tonights_run(monkeypatch):
    from api.core import ops_metrics_sampler as sampler
    monkeypatch.delenv(job.SWITCH_ENV, raising=False)
    nightly = AsyncMock(return_value=("job-2", True, TODAY))
    monkeypatch.setattr(job, "enqueue_nightly", nightly)
    out: dict = {}
    await sampler._llm_model_lifecycle_part(MagicMock(), {job.KIND: object()}, T0, out)
    assert out == {"llm_model_lifecycle_sync": {"job_id": "job-2", "day": "2026-11-03"}}
    out = {}
    await sampler._llm_model_lifecycle_part(MagicMock(), {}, T0, out)       # kind not registered: nothing
    assert out == {}


@pytest.mark.asyncio
async def test_the_admin_route_queues_a_live_run_by_default(monkeypatch):
    from api.routers import admin_job_runner as r
    monkeypatch.setattr(r, "verify_admin_secret", lambda s: None)
    enq = AsyncMock(return_value=("job-9", True))
    monkeypatch.setattr(job, "enqueue", enq)
    out = await r.llm_model_lifecycle_run(r.LlmModelLifecycleRunRequest(), MagicMock(), x_admin_secret="s",
                                          x_admin_user_id="u1")
    assert out["job_id"] == "job-9" and out["kind"] == job.KIND and out["dry_run"] is False
    assert enq.await_args.args[2] == {"trigger": "manual", "dry_run": False, "requested_by": "admin:u1"}


# ── OPS-12 digest: the lifecycle section ────────────────────────────────────────────────────────────────────────

def _lc(models, warnings=(), last_run=None):
    return {"ready": True, "models": models, "warnings": list(warnings), "last_run": last_run}


def test_lifecycle_attention_lines():
    from services.reports import digest
    w = ml.warning("judge-b", "legacy", date(2026, 12, 15), TODAY, ["s_judge"])
    lc = _lc([{"model_key": "judge-b", "stages": ["s_judge"], "status": "legacy", "eol_date": "2026-12-15"},
              {"model_key": "writer-a", "stages": ["s_write"], "status": "unknown", "eol_date": None},
              {"model_key": "new-d", "stages": ["s_new"], "status": None, "eol_date": None}], [w])
    items = digest.lifecycle_attention(lc)
    assert items[0].startswith("Routed model judge-b is end of life 2026-12-15 (in 42 days)")
    assert any("no lifecycle status (new-d)" in i for i in items)
    assert any("Lifecycle unknown for 1 routed model(s) (writer-a)" in i for i in items)
    assert digest.lifecycle_attention(None) == [] and digest.lifecycle_attention({"ready": False}) == []


def test_the_lifecycle_section_renders_and_an_old_digest_renders_as_before():
    from services.reports import digest
    run = {"job_id": "j", "status": "succeeded", "finished_at": "2026-11-03T04:01:00+00:00",
           "connections": [{"connection_key": "acc2", "access": "ok", "error_class": None},
                           {"connection_key": "acc3", "access": "refused", "error_class": "access_denied"}]}
    md = "\n".join(digest.render_lifecycle(_lc(
        [{"model_key": "judge-b", "stages": ["s_judge"], "status": "active", "eol_date": "2027-08-01"}],
        last_run=run)))
    assert "## Model lifecycle" in md and "acc3 refused (access_denied)" in md
    assert "| judge-b | active | 2027-08-01 | s_judge |" in md
    assert digest.render_lifecycle(None) == []
    assert "migration 352 not applied" in "\n".join(digest.render_lifecycle({"ready": False}))


@pytest.mark.asyncio
async def test_load_lifecycle_reads_routes_aliases_rows_and_the_last_run():
    from services.reports import digest
    conn = MagicMock()

    async def fetch(sql, *a):
        if "lifecycle_checked_at" in sql:
            return [{"model_key": "judge-b", "lifecycle_status": "active", "eol_date": date(2027, 1, 20),
                     "lifecycle_checked_at": T0}]
        if "llm_role_config" in sql:
            return [{"stage": "s_judge", "model_id": "judge-b", "fallback_model_ids": [], "shadow_model_id": None,
                     "is_active": True}]
        return []
    conn.fetch = fetch
    conn.fetchrow = AsyncMock(return_value={"id": "j", "status": "succeeded", "finished_at": T0,
                                            "result": '{"connections": [{"connection_key": "acc2", "access": "ok"}]}'})
    out = await digest.load_lifecycle(conn, T0)
    assert out["ready"] and out["models"] == [{"model_key": "judge-b", "stages": ["s_judge"], "status": "active",
                                               "eol_date": "2027-01-20", "checked_at": T0.isoformat()}]
    assert [w["model_key"] for w in out["warnings"]] == ["judge-b"]        # 78 days left
    assert out["last_run"]["connections"] == [{"connection_key": "acc2", "access": "ok", "error_class": None}]
