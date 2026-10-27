"""AA-919 WM-18 — the API half of the release double check (services/writing_memory/releases.evidence_problem: the same
cases as CIS 287's CHECK, trigger and guard 3), the insertion path's resolver and stamp, the release-0 verdicts
(services/writing_memory/release0.verdicts) and the release routes (role, actor, error mapping). No DB.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.writing_memory import insertion, release0
from services.writing_memory import releases as rel

NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)
ADMIN = "11111111-1111-1111-1111-111111111111"


def _run(kind, verdict="pass", writer="t9_social", finished=True, details=None):
    return {"pipeline": "writing_memory", "writer": writer, "run_kind": kind, "verdict": verdict,
            "finished_at": NOW if finished else None, "details": details or {}}


def _release(cls="empty", status="approved", **kw):
    r = {"writer": "t9_social", "scope": "platform", "tenant_id": None, "content_class": cls, "status": status,
         "train_kind": rel.train_kind_for(cls, None), "approved_by": ADMIN, "approved_at": NOW,
         "r0_run_id": "r0", "eval_run_id": "ev", "shadow_run_id": "sh", "rule_ids": [], "retired_reason": None}
    r.update(kw)
    return r


R0_OK = _run("r0_replay", details={"inputs": 20, "byte_diffs": 0})
BASE = _run("baseline_import", "baseline")
R2 = _run("r2")
SHADOW = _run("shadow")


def _rule(kind="writer_instruction", scope="platform", tenant=None, status="approved", writer="t9_social"):
    return {"rule_id": str(uuid.uuid4()), "kind": kind, "scope": scope, "tenant_id": tenant, "writer": writer,
            "status": status}


def _p(release, runs=None, rules=(), **kw):
    p = rel.evidence_problem(release, runs if runs is not None else {"r0": R0_OK, "eval": BASE, "shadow": SHADOW},
                             list(rules), **kw)
    return p[0] if p else None


# ── the evidence mirror: guard 3's cases, one by one ──────────────────────────────────────────────────────────────

def test_release0_activates_on_r0_and_the_baseline():
    assert _p(_release()) is None


@pytest.mark.parametrize("details,code", [
    ({"inputs": 20, "byte_diffs": 1}, "r0_not_byte_identical"),
    ({"inputs": 19, "byte_diffs": 0}, "r0_not_byte_identical"),
    ({"inputs": 20}, "r0_not_byte_identical"),                       # no byte_diffs = not proven
    ({"inputs": "20", "byte_diffs": "0"}, "r0_not_byte_identical"),   # strings are not numbers (the trigger agrees)
])
def test_release0_needs_zero_byte_diffs_over_20_inputs(details, code):
    assert _p(_release(), {"r0": _run("r0_replay", details=details), "eval": BASE}) == code


def test_activation_without_r0_or_eval_is_refused():
    assert _p(_release(r0_run_id=None)) == "r0_missing"
    assert _p(_release(eval_run_id=None)) == "eval_missing"
    assert _p(_release(), {"r0": None, "eval": BASE}) == "r0_missing"


@pytest.mark.parametrize("run,code", [
    (_run("r0_replay", "fail", details={"inputs": 20, "byte_diffs": 0}), "r0_not_passed"),
    (_run("r0_replay", details={"inputs": 20, "byte_diffs": 0}, finished=False), "r0_unfinished"),
    (_run("r0_replay", writer="a1", details={"inputs": 20, "byte_diffs": 0}), "r0_wrong_writer"),
    (_run("r2", details={"inputs": 20, "byte_diffs": 0}), "r0_not_passed"),
])
def test_r0_run_must_be_a_finished_passing_replay_of_the_writer(run, code):
    assert _p(_release(), {"r0": run, "eval": BASE}) == code


def test_rules_release_needs_eval_and_shadow():
    rules = [_rule()]
    ok = _release("rules", rule_ids=[rules[0]["rule_id"]], r0_run_id=None)
    assert _p(ok, {"eval": R2, "shadow": SHADOW}, rules) is None
    assert _p({**ok, "shadow_run_id": None}, {"eval": R2, "shadow": None}, rules) == "shadow_missing"
    assert _p(ok, {"eval": R2, "shadow": R2}, rules) == "shadow_not_passed"           # an r2 is not a shadow
    assert _p(ok, {"eval": BASE, "shadow": SHADOW}, rules) == "eval_not_passed"       # baseline: empty class only


def test_lexicon_only_needs_r0_only_and_lexicon_rules():
    lex = [_rule("lexicon")]
    ok = _release("lexicon_only", rule_ids=[lex[0]["rule_id"]], eval_run_id=None, shadow_run_id=None)
    assert _p(ok, {"r0": _run("r0_replay")}, lex) is None
    wi = [_rule("writer_instruction")]
    assert _p({**ok, "rule_ids": [wi[0]["rule_id"]]}, {"r0": _run("r0_replay")}, wi) == "class_mismatch"


def test_tenant_release_never_holds_another_tenants_rule():
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    rule_b = _rule(scope="tenant", tenant=b)
    rel_a = _release("rules", scope="tenant", tenant_id=a, rule_ids=[rule_b["rule_id"]])
    assert _p(rel_a, {"eval": R2, "shadow": SHADOW}, [rule_b]) == "rule_isolation"
    plat = _release("rules", rule_ids=[rule_b["rule_id"]])
    assert _p(plat, {"eval": R2, "shadow": SHADOW}, [rule_b]) == "rule_isolation"
    other_writer = _rule(writer="t2")
    assert _p(_release("rules", rule_ids=[other_writer["rule_id"]]), {"eval": R2, "shadow": SHADOW},
              [other_writer]) == "rule_isolation"


def test_first_activation_needs_approved_rules_rollback_does_not():
    r = _rule(status="expired")
    release = _release("rules", rule_ids=[r["rule_id"]])
    assert _p(release, {"eval": R2, "shadow": SHADOW}, [r]) == "rules_not_approved"
    back = {**release, "status": "retired", "retired_reason": "superseded"}
    assert _p(back, {"eval": R2, "shadow": SHADOW}, [r], first_activation=False) is None


def test_a_rolled_back_release_is_never_reactivated():
    back = _release(status="retired", retired_reason="rollback: bad tone")
    assert _p(back, first_activation=False) == "rolled_back"
    assert rel.transition_problem("active", "draft")[0] == "bad_transition"
    assert rel.transition_problem("retired", "active", "superseded") is None


def test_unapproved_or_closed_month_is_refused():
    assert _p(_release(status="evaluated")) == "not_approved"
    assert _p(_release(approved_by=None)) == "not_approved"
    rules = [_rule()]
    reg = _release("rules", rule_ids=[rules[0]["rule_id"]])
    runs = {"eval": R2, "shadow": SHADOW}
    assert _p(reg, runs, rules, closed_month=date(2026, 10, 1), today=date(2026, 10, 26)) == "train_closed"
    assert _p(reg, runs, rules, closed_month=date(2026, 9, 1), today=date(2026, 10, 26)) is None


def test_content_classes_and_items_hash():
    assert rel.content_class_of(set()) == "empty"
    assert rel.content_class_of({"lexicon"}) == "lexicon_only"
    assert rel.content_class_of({"repair_hint"}) == "repair_hint_only"
    assert rel.content_class_of({"lexicon", "repair_hint"}) == "rules"
    assert rel.train_kind_for("rules", "emergency") == "emergency"
    assert rel.train_kind_for("empty", "regular") == "baseline"
    a = [{"rule_id": "b", "rule_text": "x", "replacement": None}, {"rule_id": "a", "rule_text": "y",
                                                                   "replacement": "z"}]
    assert rel.items_sha256(a) == rel.items_sha256(list(reversed(a)))
    assert len(rel.items_sha256([])) == 64


def test_only_empty_releases_are_wired_until_wm23():
    assert rel.wiring_problem(_release()) is None
    assert rel.wiring_problem(_release("rules"))[0] == "content_not_wired"


def test_approval_is_a_humans():
    assert rel.is_human(ADMIN)
    assert not rel.is_human("unknown") and not rel.is_human("job:writing_memory_mine") and not rel.is_human(None)


def test_reverify_reasons():
    stored = {"golden_set": "aa729@1.1.0", "gate_taxonomy": "qtax-v1",
              "writer_route": {"model_id": "a"}, "judge_route": None}
    now = {"golden_set": "aa729@1.2.0", "gate_taxonomy": "qtax-v2", "writer_route": {"model_id": "b"},
           "judge_route": None}
    assert rel.reverify_reasons(stored, now) == ["golden_set", "model_route", "gate_taxonomy"]
    assert rel.reverify_reasons(stored, stored) == []
    assert rel.reverify_reasons({}, now) == []          # a key the release never recorded is not a change


# ── the insertion path: resolve fails open, filters the tenant explicitly; the stamp ─────────────────────────────────

class _DB:
    def __init__(self, rows=None, exc=None):
        self.rows, self.exc, self.calls = rows or [], exc, []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if self.exc:
            raise self.exc
        return self.rows


def _row(scope, no=0, cls="empty", enabled=True, rid=None):
    return {"memory_enabled": enabled, "release_id": rid or str(uuid.uuid4()), "scope": scope, "release_no": no,
            "content_class": cls, "rule_ids": []}


def test_resolve_fails_open_and_respects_the_switch():
    assert asyncio.run(insertion.resolve(_DB(exc=RuntimeError("relation does not exist")), "t2", "x")) is None
    assert asyncio.run(insertion.resolve(_DB([_row("platform", enabled=False)]), "t2", None)) is None
    assert asyncio.run(insertion.resolve(_DB([]), "a1")) is None
    assert asyncio.run(insertion.resolve(_DB(), "nope")) is None
    none_active = [{"memory_enabled": True, "release_id": None, "scope": None, "release_no": None,
                    "content_class": None, "rule_ids": None}]
    assert asyncio.run(insertion.resolve(_DB(none_active), "t9_blog", "t")) is None


def test_resolve_filters_the_tenant_explicitly_and_a1_never_reads_one():
    tenant = str(uuid.uuid4())
    db = _DB([_row("platform"), _row("tenant", 1)])
    view = asyncio.run(insertion.resolve(db, "t9_social", tenant))
    assert view["platform"]["release_no"] == 0 and view["tenant"]["release_no"] == 1
    sql, args = db.calls[0]
    assert "r.tenant_id = $2::uuid" in sql and args == ("t9_social", tenant)
    db = _DB([_row("platform")])
    asyncio.run(insertion.resolve(db, "a1", tenant))
    assert db.calls[0][1] == ("a1", None)
    # a tenant row without a tenant asked for is never used (defence in depth)
    view = asyncio.run(insertion.resolve(_DB([_row("platform"), _row("tenant", 3)]), "t2", None))
    assert view["tenant"] is None


def test_stamp_shape_and_empty_slots():
    view = {"writer": "t2", "platform": {"release_id": "p", "release_no": 0, "content_class": "empty",
                                         "rule_ids": []}, "tenant": None}
    assert insertion.stamp(view) == {"writer": "t2", "platform": {"release_id": "p", "release_no": 0},
                                     "tenant": None, "ignored": False}
    assert insertion.stamp(None) is None
    assert insertion.t2_block(view) == "" and insertion.t9_tenant_lines(view) == []
    loaded = {**view, "platform": {**view["platform"], "content_class": "rules", "rule_ids": ["r"]}}
    assert insertion.t2_block(loaded) == "" and insertion.stamp(loaded)["ignored"] is True
    assert insertion.writer_for_channel("blog") == "t9_blog" and insertion.writer_for_channel("x") == "t9_social"


def test_t9_writer_input_records_the_release_only_with_memory():
    from services.acp_content_writing.service import writer_input_snapshot
    view = {"writer": "t9_social", "platform": {"release_id": "p", "release_no": 0, "content_class": "empty",
                                                "rule_ids": []}, "tenant": None}
    assert "writing_release" not in writer_input_snapshot({})
    assert writer_input_snapshot({"writing_memory": view})["writing_release"]["platform"] == {
        "release_id": "p", "release_no": 0}


def test_a1_metadata_records_the_release_only_when_stamped():
    from api.routers.admin_pipeline import _build_generated_metadata
    kw = dict(brand_rule_id=None, brand_name="", seo_mode="x", model_used="m", llm_cost_usd=0, dataforseo_used=False)
    assert "writing_release" not in _build_generated_metadata({"writer_version": "v2"}, **kw)
    stamp = {"writer": "a1", "platform": {"release_id": "p", "release_no": 0}, "tenant": None, "ignored": False}
    assert _build_generated_metadata({"writing_release": stamp}, **kw)["writing_release"] == stamp


# ── release-0 verdicts (re-derived from the rows) ─────────────────────────────────────────────────────────────────

def _report(rows, pre_key="s3:pre.json", v2=True):
    return {"schema": release0.SCHEMA, "phase": "post", "pre_key": pre_key, "a1_v2_enabled_here": v2, "rows": rows}


def _rows(writer, n=20, diff_at=None, stored=None, pre=True):
    out = []
    for i in range(n):
        r = {"writer": writer, "input_id": f"{writer}-{i}", "source": "x", "system_sha256": "s" * 64,
             "user_sha256": f"u{i}", "none_vs_r0_equal": i != diff_at}
        if pre:
            r.update(pre_system_sha256="s" * 64, pre_user_sha256=f"u{i}")
        if stored is not None:
            r.update(stored_hash="abcd1234", stored_hash_match=stored)
        out.append(r)
    return out


def test_release0_verdicts():
    rows = _rows("a1", stored=True) + _rows("t2") + _rows("t9_blog", stored=True) + _rows("t9_social", stored=False)
    v = release0.verdicts(_report(rows))
    assert {w: v[w]["verdict"] for w in v} == {"a1": "pass", "t2": "pass", "t9_blog": "pass", "t9_social": "fail"}
    assert "no stored production prompt hash is reproduced" in v["t9_social"]["reasons"]
    v = release0.verdicts(_report(_rows("t2", diff_at=3) + _rows("a1", n=19)))
    assert v["t2"]["details"]["byte_diffs"] == 1 and v["t2"]["verdict"] == "fail"
    assert v["a1"]["verdict"] == "fail"
    nopre = release0.verdicts(_report(_rows("t2", pre=False), pre_key=None))
    assert nopre["t2"]["verdict"] == "fail"
    assert release0.verdicts(_report(_rows("t2", pre=False), pre_key=None), allow_no_pre=True)["t2"]["verdict"] == "pass"
    assert release0.verdicts(_report(_rows("a1"), v2=False))["a1"]["verdict"] == "fail"
    assert release0.verdicts(_report(_rows("a1"), v2=False), a1_v2_on_worker=True)["a1"]["verdict"] == "pass"
    with pytest.raises(ValueError):
        release0.verdicts({"schema": release0.SCHEMA, "phase": "pre", "rows": []})


def test_release0_merge_pre_marks_vs_pre_diffs():
    post = _report([{"writer": "t2", "input_id": "a", "system_sha256": "s", "user_sha256": "u1",
                     "none_vs_r0_equal": True}])
    pre = {"rows": [{"writer": "t2", "input_id": "a", "system_sha256": "s", "user_sha256": "OLD"}]}
    v = release0.verdicts(release0.merge_pre(post, pre), min_inputs=1)
    assert v["t2"]["details"]["byte_diffs_vs_pre"] == 1 and v["t2"]["verdict"] == "fail"


# ── the routes: role, actor, error mapping ──────────────────────────────────────────────────────────────────────────

_SECRET = "aa919-test-admin-secret"


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from api.main import app
    from api.routers import admin, admin_settings
    monkeypatch.setenv("ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(admin, "ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", _SECRET)
    conn = MagicMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acq)
    monkeypatch.setattr(app.state, "pool", pool, raising=False)
    monkeypatch.setattr(app.state, "redis", None, raising=False)
    return TestClient(app, raise_server_exceptions=True)


def _h(role="admin", user=ADMIN):
    h = {"X-Admin-Secret": _SECRET, "x-admin-role": role}
    if user:
        h["x-admin-user-id"] = user
    return h


RID = "22222222-2222-2222-2222-222222222222"


def test_activate_maps_the_mirror_and_the_db_refusals(client, monkeypatch):
    calls = []

    async def refuse(conn, rid, *, actor, **kw):
        calls.append((rid, actor))
        raise rel.ReleaseError(409, "r0_not_byte_identical", "1 byte diff")

    monkeypatch.setattr(rel, "activate", refuse)
    r = client.post(f"/admin/writing-memory/releases/{RID}/activate", headers=_h())
    assert r.status_code == 409 and r.json()["detail"]["code"] == "r0_not_byte_identical"
    assert calls == [(RID, f"admin:{ADMIN}")]

    async def db(conn, rid, *, actor, **kw):
        raise rel.DBRefused(409, "db_refused", "CheckViolationError: writing_memory_release_evidence_check")

    monkeypatch.setattr(rel, "activate", db)
    r = client.post(f"/admin/writing-memory/releases/{RID}/activate", headers=_h())
    assert r.status_code == 409 and r.json()["detail"]["code"] == "db_refused"


def test_release_writes_need_an_admin_id_and_ai_admin(client, monkeypatch):
    monkeypatch.setattr(rel, "activate", AsyncMock(side_effect=AssertionError("must not be reached")))
    assert client.post(f"/admin/writing-memory/releases/{RID}/activate", headers=_h(user=None)).status_code == 403
    for role in ("reviewer", "content"):
        r = client.post(f"/admin/writing-memory/releases/{RID}/activate", headers=_h(role=role))
        assert r.status_code == 403, role
    assert client.post(f"/admin/writing-memory/releases/{RID}/rollback", json={"reason": "x"},
                       headers=_h()).status_code == 422
    assert client.post("/admin/writing-memory/releases", json={"writer": "zz"}, headers=_h()).status_code == 422
    assert client.post(f"/admin/writing-memory/releases/not-a-uuid/activate", headers=_h()).status_code == 404


def test_rollback_and_switch_pass_through(client, monkeypatch):
    async def back(conn, rid, *, actor, reason):
        raise rel.ReleaseError(409, "no_previous", "release 0")

    monkeypatch.setattr(rel, "rollback", back)
    r = client.post(f"/admin/writing-memory/releases/{RID}/rollback", json={"reason": "bad tone"}, headers=_h())
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_previous"

    async def switch(conn, writer, enabled, *, actor):
        assert (writer, enabled, actor) == ("t2", True, f"admin:{ADMIN}")
        raise rel.ReleaseError(409, "no_active_release", "release 0 first")

    monkeypatch.setattr(rel, "set_memory_enabled", switch)
    r = client.patch("/admin/writing-memory/writers/t2", json={"memory_enabled": True}, headers=_h())
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_active_release"
    assert client.patch("/admin/writing-memory/writers/zz", json={"memory_enabled": True},
                        headers=_h()).status_code == 404
    assert client.patch("/admin/writing-memory/writers/t2", json={}, headers=_h()).status_code == 422


def test_unavailable_before_the_migration(client, monkeypatch):
    import asyncpg

    async def missing(conn, **kw):
        raise asyncpg.UndefinedTableError("relation \"shared.writing_memory_release\" does not exist")

    monkeypatch.setattr(rel, "list_releases", missing)
    r = client.get("/admin/writing-memory/releases", headers=_h())
    assert r.status_code == 503 and r.json()["detail"]["code"] == "unavailable"


def test_policy_entries():
    from api.core.staff_authz import AI_ADMIN, POLICY, TENANT_READ
    base = "/admin/writing-memory"
    for path in ("/releases", "/releases/active", "/releases/{release_id}", "/writers"):
        assert POLICY[("GET", base + path)] == TENANT_READ
    for path in ("/releases", "/releases/reverify-scan", "/releases/{release_id}/evidence",
                 "/releases/{release_id}/approve", "/releases/{release_id}/activate",
                 "/releases/{release_id}/rollback", "/releases/{release_id}/reject"):
        assert POLICY[("POST", base + path)] == AI_ADMIN
    assert POLICY[("PATCH", base + "/writers/{writer}")] == AI_ADMIN


# ── WM-22 Releases tab: the list's evidence verdicts and the drawer's run summary ─────────────────────────────────

def test_run_summary_keeps_counts_and_hashes_only():
    details = {"inputs": 25, "byte_diffs": 0, "byte_diffs_vs_pre": 0, "compared_with_pre": 25, "stored_hashes": 25,
               "stored_hash_match": 25, "prompt_sha256": "a" * 64, "git_sha": "dddb7cff", "reasons": ["2 byte diffs"],
               "results": {"pipeline:pass": 41, "pipeline:fail": 9, "bad": "x"}, "report_key": "scripts/r.json",
               "flags": ["x"], "sources": {"job": 25}, "pre_key": "k", "rows": [{"text": "tenant words"}],
               "source": "fixture.pipeline_verdict", "skipped_post_edit": True}
    s = rel.run_summary(details)
    assert s == {"inputs": 25, "byte_diffs": 0, "byte_diffs_vs_pre": 0, "compared_with_pre": 25,
                 "stored_hashes": 25, "stored_hash_match": 25, "prompt_sha256": "a" * 64, "git_sha": "dddb7cff",
                 "source": "fixture.pipeline_verdict", "reasons": ["2 byte diffs"],
                 "results": {"pipeline:pass": 41, "pipeline:fail": 9}}
    assert rel.run_summary('{"inputs": 3}') == {"inputs": 3}       # a JSON string column
    assert rel.run_summary(None) == {} and rel.run_summary("not json") == {}


def test_evidence_of_marks_missing_and_unnamed_runs():
    r = _release(r0_run_id="a", eval_run_id="b", shadow_run_id=None)
    v = {"a": {"run_kind": "r0_replay", "verdict": "pass", "finished": True}}
    ev = rel.evidence_of(r, v)
    assert ev["r0"] == v["a"] and ev["shadow"] is None
    assert ev["eval"]["missing"] is True and ev["eval"]["verdict"] is None


def test_run_verdicts_one_query_and_none_for_no_ids():
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[{"id": "a", "run_kind": "r0_replay", "verdict": "pass", "finished": True}])
    out = asyncio.run(rel.run_verdicts(conn, ["a", None, "a"]))
    assert out == {"a": {"run_kind": "r0_replay", "verdict": "pass", "finished": True}}
    assert conn.fetch.await_count == 1 and conn.fetch.await_args.args[1] == ["a"]
    assert asyncio.run(rel.run_verdicts(conn, [None])) == {} and conn.fetch.await_count == 1


def test_list_route_adds_evidence_and_detail_route_adds_run_summary(client, monkeypatch):
    row = {"release_id": RID, "writer": "a1", "scope": "platform", "tenant_id": None, "release_no": 0,
           "status": "active", "content_class": "empty", "rule_ids": [], "r0_run_id": "r0id", "eval_run_id": "evid",
           "shadow_run_id": None, "created_at": NOW, "fingerprint": {"prompt_sha256": "f" * 64}}

    async def listing(conn, **kw):
        return [row], 1

    async def verdicts(conn, ids):
        assert sorted(i for i in ids if i) == ["evid", "r0id"]
        return {"r0id": {"run_kind": "r0_replay", "verdict": "pass", "finished": True}}

    monkeypatch.setattr(rel, "list_releases", listing)
    monkeypatch.setattr(rel, "run_verdicts", verdicts)
    body = client.get("/admin/writing-memory/releases", headers=_h()).json()
    assert body["total"] == 1 and body["items"][0]["created_at"] == NOW.isoformat()
    ev = body["items"][0]["evidence"]
    assert ev["r0"]["verdict"] == "pass" and ev["eval"]["missing"] is True and ev["shadow"] is None

    async def get(conn, rid, **kw):
        return dict(row)

    async def runs(conn, release):
        return {"r0": {"id": "r0id", "run_kind": "r0_replay", "verdict": "pass", "finished_at": NOW,
                       "details": {"inputs": 25, "byte_diffs": 0, "rows": [{"text": "tenant words"}]}},
                "eval": None, "shadow": None}

    monkeypatch.setattr(rel, "get_release", get)
    monkeypatch.setattr(rel, "runs_of", runs)
    d = client.get(f"/admin/writing-memory/releases/{RID}", headers=_h()).json()
    assert d["runs"]["r0"]["summary"] == {"inputs": 25, "byte_diffs": 0}
    assert "details" not in d["runs"]["r0"] and d["runs"]["eval"] is None and d["activation"] is None
    assert client.get("/admin/writing-memory/releases", headers=_h(role="viewer")).status_code == 403
