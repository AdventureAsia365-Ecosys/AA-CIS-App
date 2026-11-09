"""AA-1050 W4 (root ADR 0020 D6 / D7) — the claim instrument in shadow: no DB, no model, no paid call.

Covers: the switch off = no read and no call; the enforce map warns for every class (and enforce needs both the
override and a passing record); the shadow never writes a piece or a version (only its own table); a miss, a
refusal, an unusable reply, a split vote, a non-pinned answer and a caller error are unverified, never a pass; the
spend guard stops before a call that could pass the cap and closes on an unreadable ledger; the cache serves a
verdict with no call; tenant-authored spans never count (D6); the ledger row carries the shadow flag and the billing
keys; the baselines and the agreement; the admin summary route (policy, shape, 503 before CIS 391).

Invented names and places only.
"""
from __future__ import annotations

import asyncio
import json
import math
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from services.ground_truth import r1
from services.quality import claim_shadow as cs

PIECE = "00000000-0000-4000-8000-0000000a1050"
VERSION = "00000000-0000-4000-8000-0000000a1051"
TENANT = "00000000-0000-4000-8000-0000000a1052"
JOB = "00000000-0000-4000-8000-0000000a1053"
SOURCE = "Day 1: Arrive in Velmora. Harbour Lodge. Dinner included.\nDay 2: Walk to Ostrin Valley with a guide."
BODY = ("Arrive in Velmora and settle in at the Harbour Lodge. A private chef cooks a lobster dinner on the pier. "
        "Walk to Ostrin Valley with a guide on day two. Book with Juniper Trails today.")
FLAGGED = "A private chef cooks a lobster dinner on the pier."
CTA = "Book with Juniper Trails today."
INST = cs.DEFAULT_INSTRUMENT


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in (cs.SWITCH_ENV, cs.INSTRUMENT_ENV, cs.CAP_ENV, cs.WINDOW_ENV):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(cs, "CALIBRATED", {})
    monkeypatch.setattr(cs, "ENFORCE_OVERRIDES", {})


def _obj(**over) -> cs.ShadowObject:
    kw = dict(surface="t9_blog", fields=(("content_text", BODY),), source_text=SOURCE, baseline="pass",
              baseline_status="approved", tenant_id=TENANT, piece_id=PIECE, job_id=JOB, tenant_spans=(CTA,))
    kw.update(over)
    return cs.ShadowObject(**kw)


def _reply(*quotes, cls="fact.inclusion", model="gpt-6-luna-openai", conn="openai-direct", cost=0.001, stop="stop"):
    content = json.dumps({"claims": [{"quote": q, "class": cls, "why": "not in the source"} for q in quotes]})
    return SimpleNamespace(content=content, model_used=model, connection_key=conn, cost_usd=cost, attempt_no=1,
                           stop_reason=stop, route_position=0, provider="openai", input_tokens=900,
                           output_tokens=300, satellite_account=None, fallback_used=False)


class _Caller:
    """A scripted gateway: one reply (or exception) per call, in order; counts calls."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = 0

    def __call__(self, inst, system, user, schema):
        self.calls += 1
        assert system == r1.EXTRACTION_SYSTEM and schema == r1.EXTRACTION_SCHEMA
        r = self.replies.pop(0) if self.replies else _reply()
        if isinstance(r, Exception):
            raise r
        return r


class _Conn:
    def __init__(self, *, cached=None, spend=0.0, missing_table=False):
        self.cached, self.spend, self.missing_table = cached, spend, missing_table
        self.sql: list[tuple[str, tuple]] = []
        self.inserted: list[tuple] = []

    async def fetchrow(self, sql, *args):
        self.sql.append((sql, args))
        if "FROM shared.claim_shadow_verdict" in sql and "cache_key = $1" in sql:
            return self.cached
        raise AssertionError(f"unexpected fetchrow: {sql[:80]}")

    async def fetchval(self, sql, *args):
        self.sql.append((sql, args))
        if "FROM shared.llm_call_log" in sql:
            return self.spend
        if sql.lstrip().startswith("INSERT INTO shared.claim_shadow_verdict"):
            self.inserted.append(args)
            return "00000000-0000-4000-8000-00000000f001"
        raise AssertionError(f"unexpected fetchval: {sql[:80]}")

    async def fetch(self, sql, *args):
        self.sql.append((sql, args))
        return []


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


class _NoPool:
    """Any use fails the test: the switch off must not even read."""

    def acquire(self):
        raise AssertionError("the shadow touched the pool while switched off")


def _run(obj, caller, *, spend=0.0, cap=0.30, run_cap=None, logged=None, attempts=1):
    guard = cs.SpendGuard(cap, run_cap=run_cap)

    async def probe():
        return spend

    async def log(resp, signal, parsed, error):
        if logged is not None:
            logged.append((signal, parsed, error))

    return asyncio.run(cs.run_object(obj, INST, guard, probe=probe, caller=caller, log=log, run_kind="live",
                                     run_id=JOB, key="k" * 64, attempts=attempts)), guard


# ── the switch ──────────────────────────────────────────────────────────────────────────────────────────────────────


def test_switch_off_by_default_makes_no_read_and_no_call(monkeypatch):
    assert cs.enabled() is False
    caller = _Caller()
    monkeypatch.setattr(cs, "gateway_call", caller)
    kw = dict(piece_id=PIECE, tenant_id=TENANT, job_id=JOB, channel="blog", content_text=BODY, seo_title=None,
              meta_description=None, source_text=SOURCE, cta=CTA, gate_ledger=[], status="approved")
    assert asyncio.run(cs.after_t9_piece(_NoPool(), **kw)) is None
    assert asyncio.run(cs.after_t2_version(_NoPool(), version_id=VERSION, tenant_id=TENANT, job_id=JOB,
                                           tour_dict={}, brand_rules={})) is None
    assert caller.calls == 0
    for value in ("off", "1", "true", "ON "):
        monkeypatch.setenv(cs.SWITCH_ENV, value)
        assert cs.enabled() is (value.strip().lower() == "on")


def test_switch_on_skips_an_offline_replay_and_a_piece_without_content(monkeypatch):
    from services.acp_content_writing import piece_store

    monkeypatch.setenv(cs.SWITCH_ENV, "on")
    kw = dict(piece_id=PIECE, tenant_id=TENANT, job_id=JOB, channel="blog", content_text=BODY, seo_title=None,
              meta_description=None, source_text=SOURCE, cta=CTA, gate_ledger=[], status="failed")
    assert asyncio.run(cs.after_t9_piece(_NoPool(), **kw)) is None           # failed: nothing to shadow
    monkeypatch.setattr(piece_store, "active", lambda: object())
    assert asyncio.run(cs.after_t9_piece(_NoPool(), **{**kw, "status": "approved"})) is None


def test_the_hooks_never_raise(monkeypatch):
    monkeypatch.setenv(cs.SWITCH_ENV, "on")

    class _Boom:
        @asynccontextmanager
        async def acquire(self):
            raise RuntimeError("database down")
            yield  # pragma: no cover

    kw = dict(piece_id=PIECE, tenant_id=TENANT, job_id=JOB, channel="blog", content_text=BODY, seo_title=None,
              meta_description=None, source_text=SOURCE, cta=CTA, gate_ledger=[], status="held")
    assert asyncio.run(cs.after_t9_piece(_Boom(), **kw)) is None
    assert asyncio.run(cs.after_t2_version(_Boom(), version_id=VERSION, tenant_id=TENANT, job_id=JOB, tour_dict={},
                                           brand_rules={})) is None


# ── the enforce map ─────────────────────────────────────────────────────────────────────────────────────────────────


def test_every_class_warns_and_no_override_is_shipped():
    assert cs.ENFORCE_OVERRIDES == {} and cs.CALIBRATED == {}
    for writer in cs.SURFACES:
        modes = cs.enforce_map(writer)
        assert modes and set(modes.values()) == {cs.WARN}


def test_the_shipped_tables_are_empty_in_the_module_source():
    """A later flip edits these two literals in a reviewed PR with its W2 record; W4 shadow ships none."""
    import inspect

    src = inspect.getsource(cs)
    assert "CALIBRATED: dict[str, frozenset[str]] = {}" in src
    assert "ENFORCE_OVERRIDES: dict[str, str] = {}" in src


def test_enforce_needs_both_the_override_and_a_passing_record(monkeypatch):
    monkeypatch.setattr(cs, "ENFORCE_OVERRIDES", {"t9_blog:fact.inclusion": cs.ENFORCE})
    assert cs.mode_for("t9_blog", "fact.inclusion", INST) == cs.WARN            # override alone: still warn
    monkeypatch.setattr(cs, "CALIBRATED", {INST.id: frozenset({"t9_blog:fact.inclusion"})})
    assert cs.mode_for("t9_blog", "fact.inclusion", INST) == cs.ENFORCE
    other = r1.Instrument("llama4-maverick", "acc2", cs.MODE)
    assert cs.mode_for("t9_blog", "fact.inclusion", other) == cs.WARN           # another instrument: no record
    assert cs.mode_for("t2", "fact.inclusion", INST) == cs.WARN                 # another writer


def test_warn_map_records_warn_and_writes_only_its_own_table(monkeypatch):
    """The warn map never changes a status: an unsupported verdict is a 'warn' action, and the only statements are
    the cache read, the ledger reads and the verdict INSERT (never a piece or a version)."""
    conn = _Conn()
    caller = _Caller(*[_reply(FLAGGED)] * cs.VOTES)
    logged = []

    async def fake_log(pool, resp, signal, parsed, error, *, obj):
        logged.append(signal)

    monkeypatch.setattr(cs, "_log", fake_log)
    out = asyncio.run(cs.shadow_object(_Pool(conn), _obj(), run_kind="live", run_id=JOB, caller=caller, attempts=1))
    assert out["verdict"] == "unsupported" and out["reason"] == "unsupported:fact.inclusion"
    assert out["shadow_action"] == "warn" and out["agreement"] == "disagree" and out["cached"] is False
    assert caller.calls == cs.VOTES and len(logged) == cs.VOTES
    for sql, _args in conn.sql:
        s = " ".join(sql.split()).lower()
        assert "content_piece" not in s and "tenant_tour_versions" not in s
        assert not s.startswith(("update", "delete"))
    (args,) = conn.inserted
    assert args[0] == "live" and args[2] == "t9_blog" and args[4] == PIECE and args[5] is None
    assert args[12] == "unsupported" and args[14] == "warn" and args[17] == "disagree"
    details = json.loads(args[-1])
    assert "lobster" not in args[-1] and "Velmora" not in args[-1]                  # no text in the row
    (flagged,) = details["fields"][0]["flagged"]
    assert flagged["votes"] == [1, 1, 1] and flagged["mode"] == "warn" and flagged["cut_eligible"] is False


def test_an_enforce_class_would_hold_only_from_pinned_votes(monkeypatch):
    monkeypatch.setattr(cs, "ENFORCE_OVERRIDES", {"t9_blog:fact.inclusion": cs.ENFORCE})
    monkeypatch.setattr(cs, "CALIBRATED", {INST.id: frozenset({"t9_blog:fact.inclusion"})})
    out, _ = _run(_obj(), _Caller(*[_reply(FLAGGED)] * 3))
    assert out["shadow_action"] == "hold" and out["pinned"] is True
    off_pin = [_reply(FLAGGED, model="llama4-maverick", conn="acc2")] * 3
    out, _ = _run(_obj(), _Caller(*off_pin))
    assert out["verdict"] == "unverified" and out["reason"] == "unverified:identity"
    assert out["shadow_action"] == "review" and out["pinned"] is False          # a miss in an enforce writer


# ── unverified, never a pass ───────────────────────────────────────────────────────────────────────────────────────


def test_unparsable_replies_are_a_miss_not_a_pass():
    bad = SimpleNamespace(**{**vars(_reply()), "content": "not json"})
    logged = []
    out, _ = _run(_obj(), _Caller(bad, bad, bad), logged=logged)
    assert out["verdict"] == "unverified" and out["reason"] == "unverified:miss"
    assert [e is not None for _s, _p, e in logged] == [True, True, True]           # billed_unusable rows


def test_a_refusal_is_unverified():
    refused = _reply(stop="refusal")
    out, _ = _run(_obj(), _Caller(refused, refused, refused))
    assert out["verdict"] == "unverified" and out["reason"] == "unverified:refused"
    empty_refusal = SimpleNamespace(**{**vars(_reply()), "content": "", "stop_reason": "content_filter"})
    out, _ = _run(_obj(), _Caller(empty_refusal, empty_refusal, empty_refusal))
    assert out["reason"] == "unverified:refused"


def test_a_split_vote_is_unverified():
    bad = SimpleNamespace(**{**vars(_reply()), "content": "not json"})
    out, _ = _run(_obj(), _Caller(_reply(FLAGGED), _reply(), bad))
    assert out["verdict"] == "unverified"
    assert out["details"]["fields"][0]["flagged"][0]["votes"] == [1, 0, None]


def test_two_of_three_clear_is_supported_and_two_of_three_flags_is_unsupported():
    bad = SimpleNamespace(**{**vars(_reply()), "content": "not json"})
    out, _ = _run(_obj(), _Caller(_reply(), _reply(), bad))
    assert out["verdict"] == "supported" and out["reason"] == "supported:clear"
    out, _ = _run(_obj(), _Caller(_reply(FLAGGED), bad, _reply(FLAGGED)))
    assert out["verdict"] == "unsupported"


def test_a_quote_not_in_the_text_flags_nothing():
    out, _ = _run(_obj(), _Caller(*[_reply("A lobster dinner on a different pier entirely.")] * 3))
    assert out["verdict"] == "supported"
    assert out["details"]["fields"][0]["unmapped_quotes"] == 0                       # unconfirmed: dropped before


def test_a_caller_error_is_charged_its_worst_case_and_stops_the_run():
    out, guard = _run(_obj(), _Caller(_reply(), RuntimeError("route exhausted")))
    assert out["verdict"] == "unverified" and out["reason"] == "unverified:error" and out["stopped"] == "error"
    worst = r1.worst_call_cost(INST, cs.user_prompts(_obj(), INST)[0][2], 1)
    assert guard.calls == 2 and math.isclose(guard.charged, 0.001 + worst)


# ── the spend guard ────────────────────────────────────────────────────────────────────────────────────────────────


def test_spend_guard_stops_before_a_call_that_could_pass_the_cap():
    caller = _Caller()
    out, guard = _run(_obj(), caller, spend=0.2999)
    assert caller.calls == 0 and out["verdict"] == "unverified" and out["reason"] == "unverified:budget"
    assert out["stopped"] == "budget" and out["calls"] == 0 and guard.charged == 0


def test_spend_guard_counts_this_run_and_the_run_cap():
    prompt = cs.user_prompts(_obj(), INST)[0][2]
    worst = r1.worst_call_cost(INST, prompt, 1)
    caller = _Caller(_reply(cost=worst * 0.8), _reply())
    out, guard = _run(_obj(), caller, cap=0.30, run_cap=worst * 1.5)
    assert caller.calls == 1 and out["stopped"] == "budget"           # 0.8 w billed + the next worst case w > 1.5 w
    assert math.isclose(guard.charged, worst * 0.8)


def test_spend_guard_closes_on_an_unreadable_ledger():
    g = cs.SpendGuard(0.30)
    assert g.allows(0.0001) is False                                               # no reading yet
    g.observe(0.01)
    assert g.allows(0.0001) is True
    g.observe(float("nan"))
    assert g.allows(0.0001) is False
    g2 = cs.SpendGuard(0.30)
    g2.observe(None)
    assert g2.allows(0.0001) is False
    with pytest.raises(ValueError):
        cs.SpendGuard(cs.HARD_CAP_USD + 0.01)
    g3 = cs.SpendGuard(0.30)
    g3.observe(0.10)
    g3.charge(float("nan"), 0.05)                                                  # a non-finite cost = the worst case
    assert math.isclose(g3.spent, 0.15)
    g3.observe(0.12)                                                               # the ledger lags: base + charges win
    assert math.isclose(g3.spent, 0.15)


def test_cap_and_window_come_from_the_env_within_bounds(monkeypatch):
    assert cs.cap_usd() == cs.DEFAULT_CAP_USD and cs.window_days() == cs.DEFAULT_WINDOW_DAYS
    monkeypatch.setenv(cs.CAP_ENV, "0.1")
    assert cs.cap_usd() == 0.1
    for bad in ("5", "-1", "nan", "x"):
        monkeypatch.setenv(cs.CAP_ENV, bad)
        assert cs.cap_usd() == cs.DEFAULT_CAP_USD


def test_the_instrument_env_must_name_a_priced_extraction_instrument(monkeypatch):
    monkeypatch.setenv(cs.INSTRUMENT_ENV, "llama4-maverick@acc2:extraction")
    assert cs.instrument().id == "llama4-maverick@acc2:extraction"
    for bad in ("llama4-maverick@acc2:entailment", "unknown-model@acc2:extraction", "garbage"):
        monkeypatch.setenv(cs.INSTRUMENT_ENV, bad)
        assert cs.instrument() == cs.DEFAULT_INSTRUMENT


# ── cache first ────────────────────────────────────────────────────────────────────────────────────────────────────


def test_a_cache_hit_makes_no_call_and_writes_a_zero_cost_row():
    hit = {"verdict": "supported", "reason": "supported:clear", "shadow_action": "none",
           "details": json.dumps({"fields": []}), "pinned": True, "answered_model": "gpt-6-luna-openai",
           "max_rung": 0}
    conn = _Conn(cached=hit)
    caller = _Caller()
    out = asyncio.run(cs.shadow_object(_Pool(conn), _obj(), run_kind="replay", run_id="replay-zz", caller=caller))
    assert caller.calls == 0 and out["cached"] is True and out["cost_usd"] == 0.0 and out["calls"] == 0
    (args,) = conn.inserted
    assert args[11] is True and args[19] == 0 and args[20] == 0                    # cached, calls, cost


def test_the_cache_key_moves_with_the_text_the_source_and_the_instrument():
    base = cs.cache_key(INST, cs.user_prompts(_obj(), INST))
    assert base == cs.cache_key(INST, cs.user_prompts(_obj(piece_id=VERSION), INST))     # same input, other piece
    assert base != cs.cache_key(INST, cs.user_prompts(_obj(source_text=SOURCE + " Lunch."), INST))
    assert base != cs.cache_key(INST, cs.user_prompts(_obj(fields=(("content_text", BODY + " More."),)), INST))
    other = r1.Instrument("llama4-maverick", "acc2", cs.MODE)
    assert base != cs.cache_key(other, cs.user_prompts(_obj(), other))


# ── tenant spans (D6) ──────────────────────────────────────────────────────────────────────────────────────────────


def test_a_tenant_authored_span_never_counts():
    out, _ = _run(_obj(), _Caller(*[_reply(CTA)] * 3))
    assert out["verdict"] == "supported"
    (entry,) = out["details"]["fields"][0]["flagged"]
    assert entry["tenant_span"] is True and "mode" not in entry


# ── objects, baselines, agreement, ledger ──────────────────────────────────────────────────────────────────────────


def test_baselines():
    assert cs.baseline_t9([]) == ("none", {})
    ok = {"gate": "F9_cta_fact", "passed": True, "violations": []}
    assert cs.baseline_t9([ok])[0] == "pass"
    assert cs.baseline_t9([{**ok, "passed": False, "blocking": True, "violations": ["x"]}])[0] == "flag"
    assert cs.baseline_t9([{**ok, "passed": False, "blocking": False}])[0] == "warn"
    assert cs.baseline_t9([{**ok, "passed": False, "degraded": "f9_judge_unavailable"}])[0] == "unavailable"
    assert cs.baseline_t2(None) == ("none", {})
    assert cs.baseline_t2({"brand_claims": {"unsupported": [{"kind": "age"}]}}) == ("flag", {"unsupported": 1})
    assert cs.baseline_t2({"brand_claims": {"unsupported": []}})[0] == "pass"


@pytest.mark.parametrize("baseline, verdict, want", [
    ("flag", "unsupported", "agree"), ("pass", "supported", "agree"), ("warn", "supported", "agree"),
    ("pass", "unsupported", "disagree"), ("flag", "supported", "disagree"), ("flag", "unverified", "unverified"),
    ("unavailable", "supported", "no_baseline"), ("none", "unsupported", "no_baseline"),
])
def test_agreement(baseline, verdict, want):
    assert cs.agreement(baseline, verdict) == want


def test_t9_and_t2_objects():
    o = cs.t9_object(piece_id=PIECE, tenant_id=TENANT, job_id="not-a-uuid", channel="linkedin",
                     content_text="Walk to Ostrin Valley [F:2] with a guide on day two.", seo_title=" ",
                     meta_description=None, source_text=SOURCE, cta=CTA, gate_ledger=[], status="held")
    assert o.surface == "t9_social" and o.job_id is None and o.baseline == "none" and o.baseline_status == "held"
    assert o.fields == (("content_text", "Walk to Ostrin Valley with a guide on day two."),)
    t2 = cs.t2_object(version_id=VERSION, tenant_id=TENANT, job_id=JOB,
                      rewritten={"name": "Velmora Coast", "summary": "A walk to Ostrin Valley with a guide.",
                                 "highlights": ["Harbour Lodge stay", "Valley walk"], "seo_title": "x"},
                      status="ai_generated", judge_breakdown={"brand_claims": {"unsupported": []}},
                      tour_dict={"name": "Velmora Coast", "summary": SOURCE},
                      brand_rules={"default_cta": CTA, "brand_name": "Juniper Trails"})
    assert [f for f, _t in t2.fields] == ["summary", "highlights"] and t2.object_id == VERSION
    assert t2.tenant_spans == (CTA, "Juniper Trails") and "Velmora" in t2.source_text


def test_the_ledger_row_is_a_shadow_row_with_the_billing_keys(monkeypatch):
    from shared.llm_client import call_log

    rows = []

    async def fake_record(pool, **kw):
        rows.append(kw)

    monkeypatch.setattr(call_log, "record_call_with_pool", fake_record)
    resp = _reply(FLAGGED)
    asyncio.run(cs._log(object(), resp, {"shadow": True, "surface": "t9_blog"}, {"claims": []}, None, obj=_obj()))
    (row,) = rows
    assert row["stage"] == cs.SHADOW_STAGE and row["role"] == "judge" and row["quality_signal"]["shadow"] is True
    assert row["tenant_id"] == TENANT and row["content_piece_id"] == PIECE
    assert set(call_log.billed_fields(resp)) <= set(row)


# ── the admin summary route ────────────────────────────────────────────────────────────────────────────────────────

_SECRET = "aa1050-shadow-test-admin-secret"


class _SummaryConn:
    def __init__(self, missing=False):
        self.missing = missing

    async def fetch(self, sql, *args):
        if self.missing:
            import asyncpg

            raise asyncpg.UndefinedTableError('relation "shared.claim_shadow_verdict" does not exist')
        if "GROUP BY surface" in sql:
            return [{"surface": "t9_blog", "baseline": "pass", "verdict": "unsupported", "agreement": "disagree",
                     "cached": False, "pinned": True, "shadow_action": "warn", "n": 3, "cost": 0.012},
                    {"surface": "t9_blog", "baseline": "flag", "verdict": "unsupported", "agreement": "agree",
                     "cached": True, "pinned": True, "shadow_action": "warn", "n": 2, "cost": 0.0}]
        if "GROUP BY reason" in sql:
            return [{"reason": "unsupported:fact.inclusion", "n": 5}]
        from datetime import datetime, timezone

        assert args[3] == "aa1050" and args[4] == 30
        return [{"verdict_id": "00000000-0000-4000-8000-00000000f001", "created_at": datetime.now(timezone.utc),
                 "run_kind": "live", "surface": "t9_blog", "piece_id": PIECE, "version_id": None,
                 "baseline": "pass", "baseline_status": "approved", "verdict": "unsupported",
                 "reason": "unsupported:fact.inclusion", "instrument_id": INST.id}]

    async def fetchval(self, sql, *args):
        return 0.012


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from api.main import app
    from api.routers import admin, admin_settings
    monkeypatch.setenv("ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(admin, "ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(app.state, "redis", None, raising=False)

    def make(conn):
        monkeypatch.setattr(app.state, "pool", _Pool(conn), raising=False)
        return TestClient(app, raise_server_exceptions=True)
    return make


def _h(role="admin"):
    return {"X-Admin-Secret": _SECRET, "x-admin-role": role, "x-admin-user-id": "11111111-1111-1111-1111-111111111111"}


URL = "/admin/writing-memory/claim-shadow/summary"


def test_route_summary_counts_and_ids_only(client):
    r = client(_SummaryConn()).get(URL, headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["switch"] == "off" and body["default_mode"] == "warn" and body["enforce_classes"] == []
    assert body["by_surface"]["t9_blog"] == {"agree": 2, "disagree": 3, "unverified": 0, "no_baseline": 0}
    assert body["rows"] == 5 and body["cached_share"] == 0.4 and body["non_pinned_holds"] == 0
    assert body["disagreements"]["total"] == 3
    (s,) = body["disagreements"]["sample"]
    assert s["piece_id"] == PIECE and "tenant_id" not in s and "content_text" not in json.dumps(body)


def test_route_summary_answers_503_before_the_table(client):
    r = client(_SummaryConn(missing=True)).get(URL, headers=_h())
    assert r.status_code == 503 and r.json()["detail"]["code"] == "unavailable"


def test_route_summary_refuses_bad_params_and_unknown_roles(client):
    c = client(_SummaryConn())
    assert c.get(URL + "?run_kind=other", headers=_h()).status_code == 422
    assert c.get(URL + "?seed=a%20b", headers=_h()).status_code == 422
    assert c.get(URL, headers=_h("viewer")).status_code == 403
