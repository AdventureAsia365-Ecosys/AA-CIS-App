"""AA-1026 (S267) — stage coverage (shared/llm_client/stage_coverage.py): every active stage has a model that answers
now, and the ops check that bells / mails a stage without one (shared/ops_checks.py, once per stage per day). Pure
functions and mocks: no database, no provider call."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared import ops_checks
from shared.llm_client import stage_coverage as sc
from shared.llm_client.breaker import Health

NOW = datetime(2026, 11, 4, 9, 0, tzinfo=timezone.utc)
H1 = timedelta(hours=1)


def _route(stage, model, fallbacks=(), role="validate", scopes=None, active=True):
    return {"stage": stage, "role": role, "model_id": model, "fallback_model_ids": list(fallbacks),
            "allowed_scopes": scopes, "is_active": active}


def _conn(key, enabled=True, scope="aws_only", roles=None, state="ok"):
    return {"connection_key": key, "kind": "bedrock", "enabled": enabled, "data_scope": scope,
            "served_roles": roles, "access_state": state}


def _dep(model, conn, enabled=True, priority=10, state=None, probed=None):
    d = {"model_key": model, "connection_key": conn, "priority": priority, "enabled": enabled}
    if state is not None:
        d.update(access_state=state, last_probe_at=probed)
    return d


def _ok(conn, model, at=NOW - H1):
    return Health(conn, model, last_ok_at=at)


def _refused(conn, model, at=NOW - H1, cls="access_denied", open_for=timedelta(minutes=5)):
    return Health(conn, model, last_error_at=at, last_error_class=cls, consecutive_failures=1,
                  circuit_open_until=at + open_for, open_streak=1, opened_at=at)


CONNS = [_conn("acc3"), _conn("acc2"), _conn("vendor", scope="vendor_direct"), _conn("off", enabled=False),
         _conn("embed-only", roles=["embed"])]
CATALOG = [{"model_key": k, "enabled": True} for k in ("m-a", "m-b", "m-c", "m-d", "m-e")] + [
    {"model_key": "m-off", "enabled": False}]


def _report(routes, deps, health, conns=CONNS, aliases=(), now=NOW):
    return sc.evaluate(routes, aliases, CATALOG, deps, conns, health, now)


def _stage(report, name):
    return next(s for s in report["stages"] if s["stage"] == name)


def test_a_stage_is_live_on_its_first_proven_model_in_route_order():
    rep = _report([_route("s1", "m-a", ["m-b"])], [_dep("m-a", "acc3"), _dep("m-b", "acc2")],
                  [_refused("acc3", "m-a"), _ok("acc2", "m-b")])
    st = _stage(rep, "s1")
    assert st["status"] == "live" and (st["live_model"], st["live_connection"]) == ("m-b", "acc2")
    assert [(m["model_key"], m["state"]) for m in st["models"]] == [("m-a", "refused"), ("m-b", "live")]
    assert rep["ok"] is True and rep["uncovered"] == []


def test_a_probe_answer_on_the_deployment_row_is_proof_and_a_newer_refusal_is_not():
    deps = [_dep("m-a", "acc3", state="ok", probed=NOW - H1), _dep("m-b", "acc3", state="quota_exhausted",
                                                                    probed=NOW - H1)]
    rep = _report([_route("s1", "m-a"), _route("s2", "m-b")], deps, [_ok("acc3", "m-b", at=NOW - 3 * H1)])
    assert _stage(rep, "s1")["status"] == "live"
    s2 = _stage(rep, "s2")
    assert s2["status"] == "none" and s2["models"][0]["state"] == "refused"
    assert rep["uncovered"] == ["s2"]


def test_an_answer_newer_than_the_refusal_wins():
    h = Health("acc3", "m-a", last_ok_at=NOW - H1, last_error_at=NOW - 2 * H1, last_error_class="access_denied")
    assert _stage(_report([_route("s1", "m-a")], [_dep("m-a", "acc3")], [h]), "s1")["status"] == "live"


def test_old_proof_is_unproven_and_leaves_the_stage_uncovered():
    rep = _report([_route("s1", "m-a")], [_dep("m-a", "acc3")],
                  [_ok("acc3", "m-a", at=NOW - sc.PROOF_MAX_AGE - H1)])
    st = _stage(rep, "s1")
    assert st["status"] == "none" and st["unproven"] == 1 and st["models"][0]["state"] == "unproven"
    rep = _report([_route("s2", "m-c")], [_dep("m-c", "acc3")], [])
    assert _stage(rep, "s2")["models"][0]["why"] == "no recent answer on record"


def test_an_open_circuit_is_not_live_but_a_throttle_is_not_a_refusal():
    h = Health("acc3", "m-a", last_ok_at=NOW - 2 * H1, last_error_at=NOW - timedelta(seconds=10),
               last_error_class="throttled", consecutive_failures=3,
               circuit_open_until=NOW + timedelta(seconds=20), open_streak=1)
    st = _stage(_report([_route("s1", "m-a")], [_dep("m-a", "acc3")], [h]), "s1")
    assert st["models"][0]["state"] == "circuit_open" and st["status"] == "none"


def test_a_tripped_connection_circuit_stops_every_model_of_it():
    # two refused models trip acc3's connection circuit: a third, proven earlier, is skipped by the gateway now
    health = [_refused("acc3", "m-b", at=NOW - timedelta(minutes=1)), _refused("acc3", "m-c", at=NOW - timedelta(
        minutes=1)), _ok("acc3", "m-a", at=NOW - H1)]
    st = _stage(_report([_route("s1", "m-a")], [_dep("m-a", "acc3")], health), "s1")
    assert st["models"][0]["state"] == "circuit_open" and st["status"] == "none"


def test_account_states_of_the_connection():
    conns = [_conn("acc3", state="quota_exhausted"), _conn("acc2", state="access_denied")]
    rep = _report([_route("s1", "m-a"), _route("s2", "m-b"), _route("s3", "m-c")],
                  [_dep("m-a", "acc3"), _dep("m-b", "acc2"), _dep("m-c", "acc2")],
                  [_ok("acc3", "m-a"), _ok("acc2", "m-c")], conns=conns)
    # a quota of 0 on other models of the account stamps the connection: this model's own recent answer wins
    assert _stage(rep, "s1")["status"] == "live"
    assert _stage(rep, "s2")["models"][0]["why"] == "connection access_denied"   # no answer of its own on record
    assert _stage(rep, "s3")["status"] == "live"                                 # it answered: one model refused


def test_a_quota_exhausted_connection_refuses_a_deployment_without_recent_evidence_of_its_own():
    conns = [_conn("acc3", state="quota_exhausted")]
    deps = [_dep("m-a", "acc3"), _dep("m-b", "acc3"), _dep("m-c", "acc3", state="ok", probed=NOW - H1)]
    health = [_ok("acc3", "m-b", at=NOW - sc.PROOF_MAX_AGE - H1)]
    rep = _report([_route("s1", "m-a"), _route("s2", "m-b"), _route("s3", "m-c")], deps, health, conns=conns)
    assert _stage(rep, "s1")["models"][0] == {"model_key": "m-a", "connection_key": "acc3", "state": "refused",
                                              "why": "connection quota_exhausted"}     # no answer on record
    assert _stage(rep, "s2")["models"][0]["why"] == "connection quota_exhausted"     # its answer is too old
    assert _stage(rep, "s3")["status"] == "live"                                     # the probe's 'ok' counts


def test_a_quota_exhausted_connection_still_loses_to_the_deployments_own_newer_refusal():
    conns = [_conn("acc3", state="quota_exhausted")]
    h = Health("acc3", "m-a", last_ok_at=NOW - 2 * H1, last_error_at=NOW - H1, last_error_class="quota_exhausted")
    st = _stage(_report([_route("s1", "m-a")], [_dep("m-a", "acc3")], [h], conns=conns), "s1")
    assert st["models"][0]["why"] == "refused (quota_exhausted)" and st["status"] == "none"


def test_an_unconfigured_connection_refuses_every_model_even_one_that_answered():
    conns = [_conn("acc3", state="unconfigured")]
    st = _stage(_report([_route("s1", "m-a")], [_dep("m-a", "acc3")], [_ok("acc3", "m-a")], conns=conns), "s1")
    assert st["models"][0]["why"] == "connection unconfigured" and st["status"] == "none"


def test_switched_off_role_and_scope_rules():
    routes = [_route("s1", "m-off", ["m-a", "m-b", "m-c", "m-d"], role="writer")]
    deps = [_dep("m-off", "acc3"), _dep("m-a", "off"), _dep("m-b", "embed-only"), _dep("m-c", "vendor"),
            _dep("m-d", "acc3", enabled=False)]
    health = [_ok(c, m) for m, c in (("m-off", "acc3"), ("m-a", "off"), ("m-b", "embed-only"), ("m-c", "vendor"),
                                     ("m-d", "acc3"))]
    st = _stage(_report(routes, deps, health), "s1")
    assert [m["state"] for m in st["models"]] == ["off", "off", "role_skip", "scope_skip", "off"]
    assert st["status"] == "none"


def test_without_a_stage_policy_the_primary_may_be_vendor_direct_but_not_a_fallback():
    deps = [_dep("m-c", "vendor"), _dep("m-a", "vendor")]
    health = [_ok("vendor", "m-c"), _ok("vendor", "m-a")]
    st = _stage(_report([_route("s1", "m-c", ["m-a"])], deps, health), "s1")
    assert [m["state"] for m in st["models"]] == ["live", "scope_skip"]
    st = _stage(_report([_route("s2", "m-a", ["m-c"], scopes=["aws_only", "vendor_direct"])], deps, health), "s2")
    assert [m["state"] for m in st["models"]] == ["live", "live"]


def test_aliases_expand_inactive_stages_are_skipped_and_a_model_without_deployment_says_so():
    aliases = [{"alias_key": "tier", "position": 1, "model_key": "m-e"}]
    routes = [_route("s1", "tier", ["m-a"]), _route("s2", "m-a", active=False)]
    rep = _report(routes, [_dep("m-a", "acc2")], [_ok("acc2", "m-a")], aliases=aliases)
    st = _stage(rep, "s1")
    assert st["models"][0] == {"model_key": "m-e", "connection_key": None, "state": "off", "why": "no deployment"}
    assert st["live_model"] == "m-a" and rep["stages_checked"] == 1 and rep["disabled"] == ["s2"]


def test_summary_and_a_report_that_is_not_ready():
    rep = _report([_route("s1", "m-a"), _route("s2", "m-b")], [_dep("m-a", "acc3")], [_ok("acc3", "m-a")])
    assert sc.summary(rep) == {"ready": True, "ok": False, "stages_checked": 2, "uncovered": ["s2"],
                               "live": {"s1": "m-a@acc3"}}
    assert sc.summary({"ready": False}) == {"ready": False}


@pytest.mark.asyncio
async def test_read_falls_back_without_the_probe_state_and_says_not_ready_without_tables():
    import asyncpg

    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=[
        [_route("s1", "m-a")], [], [{"model_key": "m-a", "enabled": True}],
        asyncpg.exceptions.UndefinedColumnError("access_state"), [_dep("m-a", "acc3")],
        [_conn("acc3")], []])
    rep = await sc.read(conn, now=NOW)
    assert rep["ready"] is True and rep["uncovered"] == ["s1"]
    sqls = [c.args[0] for c in conn.fetch.await_args_list]
    assert sqls[3] == sc.DEPLOYMENTS_V2_SQL and sqls[4] == sc.DEPLOYMENTS_SQL
    assert conn.fetch.await_args_list[5].args[1] == list(sc.LLM_KINDS)
    conn.fetch = AsyncMock(side_effect=asyncpg.exceptions.UndefinedTableError("llm_role_config"))
    assert await sc.read(conn, now=NOW) == {"ready": False}


# ── the ops check ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_check_for_knows_the_fixed_checks_and_one_per_stage():
    assert ops_checks.check_for("jobs_starved") is ops_checks.CHECKS["jobs_starved"]
    chk = ops_checks.check_for("llm_stage_coverage.s1_generate")
    assert chk.bell_every == timedelta(hours=24) and chk.page == "/admin/ai-gateway?tab=routing"
    for bad in ("llm_stage_coverage.", "nope"):
        with pytest.raises(KeyError):
            ops_checks.check_for(bad)


def test_one_finding_per_uncovered_stage():
    rep = _report([_route("s1", "m-a"), _route("s2", "m-b", ["m-c"])], [_dep("m-a", "acc3"), _dep("m-b", "acc3")],
                  [_ok("acc3", "m-a"), _refused("acc3", "m-b")])
    found = ops_checks.coverage_findings(rep)
    assert [f.check for f in found] == ["llm_stage_coverage.s2"]
    f = found[0]
    assert "m-b@acc3 refused" in f.summary and "m-c@- off" in f.summary
    assert f.details["stage"] == "s2" and len(f.details["models"]) == 2
    assert ops_checks.coverage_findings({"ready": False}) == []
    subject, text = ops_checks.render(f, environment="dev")
    assert subject == "[AA-CIS dev] Ops check: LLM stage has no live model" and "tab=routing" in text


class _CM:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *exc):
        return False


def _pool(last=None, mailed_today=False):
    conn = MagicMock()
    conn.transaction = MagicMock(return_value=_CM())
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"last": last, "mailed_today": mailed_today})
    conn.fetchval = AsyncMock(return_value=7)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_CM(conn))
    pool.execute = AsyncMock()
    pool.conn = conn
    return pool


def test_a_stage_bells_at_most_once_a_day(monkeypatch):
    monkeypatch.setenv("ALERTS_TOPIC_ARN", "topic-placeholder")
    finding = ops_checks.Finding("llm_stage_coverage.s2", "stage s2 (validate) has no model that answers", {})
    sns = MagicMock()
    pool = _pool(last=NOW - timedelta(hours=7))           # past the 6 h of the fixed checks, inside 24 h
    assert asyncio.run(ops_checks.raise_alert(pool, finding, sns, now=NOW)) == {"bell": False}
    pool = _pool(last=NOW - timedelta(hours=25))
    assert asyncio.run(ops_checks.raise_alert(pool, finding, sns, now=NOW)) == {"bell": True, "email": "sent"}
    sql, *args = pool.conn.fetchval.await_args.args
    assert args[1] == "platform.ops_check.llm_stage_coverage.s2" and args[2] == "llm_stage_coverage.s2"
    assert json.loads(args[3])["title"] == "LLM stage has no live model"


def test_stage_coverage_findings_reads_through_the_pool(monkeypatch):
    rep = _report([_route("s9", "m-a")], [], [])
    read = AsyncMock(return_value=rep)
    monkeypatch.setattr(sc, "read", read)
    pool = object()
    found = asyncio.run(ops_checks.stage_coverage_findings(pool, now=NOW))
    assert [f.check for f in found] == ["llm_stage_coverage.s9"] and read.await_args.args[0] is pool


def test_the_sampler_adds_coverage_findings_and_survives_a_failure(monkeypatch):
    from api.core import ops_metrics_sampler as sampler

    monkeypatch.setattr(ops_checks, "app_spend_findings", AsyncMock(return_value=[]))
    monkeypatch.setattr(ops_checks, "flush_events", AsyncMock(return_value={}))
    alert = AsyncMock(return_value={"llm_stage_coverage.s9": {"bell": True, "email": "sent"}})
    monkeypatch.setattr(ops_checks, "alert_all", alert)
    monkeypatch.setattr(ops_checks, "stage_coverage_findings", AsyncMock(return_value=[
        ops_checks.Finding("llm_stage_coverage.s9", "x", {"stage": "s9"})]))
    out: dict = {}
    asyncio.run(sampler._ops_checks_part(object(), None, out))
    assert [f.check for f in alert.await_args.args[1]] == ["llm_stage_coverage.s9"]
    assert out["stage_coverage_uncovered"] == ["s9"]
    monkeypatch.setattr(ops_checks, "stage_coverage_findings", AsyncMock(side_effect=RuntimeError("db down")))
    out = {}
    asyncio.run(sampler._ops_checks_part(object(), None, out))
    assert "db down" in out["stage_coverage_error"] and alert.await_args.args[1] == []
