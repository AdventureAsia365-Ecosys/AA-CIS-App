"""AA-1063 — the Jev pre-filter of the claim adjudication (services/ground_truth/prefilter.py) without a DB or a
provider: the rule (with the S275 edge case: fact.misbinding counts only on a lens that scored the claim NOT ok), the
switch (default shadow), the tenant gate, the S275 subject key, the 'prefilter' source of a machine-rule label (never
release evidence, never an audit adjudication), and `run` on a fake pool with decide() mocked: plan asks nothing,
shadow asks and writes nothing, apply is refused in shadow, enforce writes only the measured writers' auto_ok claims,
answers already in decision_log are read back, never asked again. Invented names and places only."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from services.ground_truth import panel as pn
from services.ground_truth import prefilter as pf
from services.ground_truth import review as rv
from services.ground_truth import store
from shared.llm_client import decide as dec

SET = "ZZ-AA1063-P"
OUTPUT = {"content_text": "Arrive in Velmora and rest at the Harbour Lodge. Walk 40 km to Ostrin Valley on day two. "
                          "Breakfast is served at the lodge each morning. The Kestrel Bridge was rebuilt in 1911. "
                          "Dinner is at the Ostrin Inn."}
TOUR = {"name": "Velmora Walk", "itinerary": ["Day 1: Arrive in Velmora. Harbour Lodge.",
                                              "Day 2: Walk 12 km to Ostrin Valley. Breakfast at the lodge."]}
# per claim (in text order): (lens labels, lens classes)
VOTES = [
    (("ok", "ok", "violation"), ("fact.number", "fact.number", "fact.number")),          # 0: auto_ok
    (("ok", "ok", "violation"), ("fact.number", "fact.misbinding", "fact.number")),      # 1: misbinding on an OK lens
    (("ok", "ok", "violation"), ("fact.number", "fact.number", "fact.misbinding")),      # 2: misbinding, non-ok lens
    (("violation", "violation", "ok"), ("fact.number", "fact.number", "fact.number")),   # 3: majority violation
    (("ok", "ok", "ok"), ("fact.number", "fact.number", "fact.number")),                 # 4: agreed -> audit draw
]
P = [0.97, 0.95, 0.99, 0.98, 0.99]


def lens(label, cls="fact.number"):
    return {"label": label, "claim_class": cls}


# ── the rule ────────────────────────────────────────────────────────────────────────────────────────────────────────


def test_rule_accepts_only_when_every_condition_holds():
    ok3 = [lens("ok"), lens("ok"), lens("violation")]
    assert pf.rule(0.90, ok3) == ("auto_ok", "rule_met")
    assert pf.rule(0.97, ok3) == ("auto_ok", "rule_met")
    assert pf.rule(0.8999, ok3) == ("review", "p_below_floor")
    assert pf.rule(None, ok3) == ("review", "no_verdict")
    assert pf.rule(0.99, [lens("violation"), lens("violation"), lens("ok")]) == ("review", "majority_violation")
    assert pf.rule(0.99, [lens("ok"), lens("violation"), lens("unsure")]) == ("review", "majority_split")
    assert pf.rule(0.99, [lens("unsure"), lens("unsure"), lens("ok")]) == ("review", "majority_unsure")


def test_misbinding_counts_only_on_a_lens_that_scored_the_claim_not_ok():
    """S275 back-test: the flag over EVERY lens kept 94 claims back for nothing; only a non-ok lens counts."""
    on_ok_lens = [lens("ok", pf.MISBINDING), lens("ok"), lens("violation", "fact.number")]
    assert not pf.non_ok_misbinding(on_ok_lens)
    assert pf.rule(0.95, on_ok_lens) == ("auto_ok", "rule_met")
    on_violation = [lens("ok"), lens("ok"), lens("violation", pf.MISBINDING)]
    assert pf.non_ok_misbinding(on_violation)
    assert pf.rule(0.99, on_violation) == ("review", "misbinding_on_non_ok_lens")
    on_unsure = [lens("ok"), lens("ok"), lens("unsure", pf.MISBINDING)]
    assert pf.rule(0.99, on_unsure) == ("review", "misbinding_on_non_ok_lens")


def test_switch_is_shadow_unless_exactly_enforce():
    assert pf.switch_mode({}) == "shadow"
    assert pf.switch_mode({pf.MODE_ENV: "enforce"}) == "enforce"
    assert pf.switch_mode({pf.MODE_ENV: " Enforce "}) == "enforce"
    for v in ("on", "true", "1", "shadow", "off", ""):
        assert pf.switch_mode({pf.MODE_ENV: v}) == "shadow", v


def test_tenant_gate():
    demo = {"kind": "demo", "is_pilot": False, "offboarded": False, "listed": True}
    assert pf.tenant_gate("platform", None, None) is None
    assert pf.tenant_gate("tenant", "t1", demo) is None
    assert pf.tenant_gate("tenant", "t1", {**demo, "listed": False}) == "tenant_not_allowlisted"
    assert pf.tenant_gate("tenant", "t1", {**demo, "kind": "agency"}) == "tenant_not_demo"
    assert pf.tenant_gate("tenant", "t1", {**demo, "is_pilot": True}) == "tenant_pilot"
    assert pf.tenant_gate("tenant", "t1", {**demo, "offboarded": True}) == "tenant_offboarded"
    assert pf.tenant_gate("tenant", "t1", None) == "tenant_unknown"
    assert pf.tenant_gate("tenant", None, demo) == "tenant_unknown"


def test_subject_key_keeps_the_s275_shape_and_reason_names_the_run():
    h = "ab" * 32
    key = pf.subject_key(h, "Day 1: Velmora.")
    assert key.startswith(f"aa1063:panel:{h[:24]}:") and len(key.rsplit(":", 1)[1]) == 10
    assert pf.subject_key(h, "Day 1: Velmora.") == key and pf.subject_key(h, "other") != key
    why = pf.reason_text("pf-run-7", 0.934)
    assert "pf-run-7" in why and "p=0.93" in why and pf.MISBINDING in why and len(why) <= pn.REASON_MAX
    assert pf.LABELLER == "prefilter:jev090+panel" and pf.STAGE.startswith("adhoc_")


# ── a machine-rule label is never release evidence ──────────────────────────────────────────────────────────────────


def _rows():
    lines = pn.export_records(set_id=SET, writer="a1", scope="platform", tenant_id=None,
                              refs={"sample_id": "zz-1063-p1"}, output=OUTPUT,
                              source_text=rv.source_text("a1", None, {"tour": TOUR}))
    assert len(lines) == len(VOTES)
    raw = [{"claim_hash": e["claim_hash"], "lens": ln, "class": c, "label": lab, "reason": f"{ln}: invented reason",
            "labeller": "panel:zz"}
           for e, (labels, classes) in zip(lines, VOTES) for ln, lab, c in zip(pn.LENSES, labels, classes)]
    js = pn.parse_judgements(raw, export_by_hash={e["claim_hash"]: e for e in lines})
    cons = pn.consolidate(lines, js, set_id=SET, set_version="1.0.0", policy_sha="d" * 64, audit_pct=1.0)
    t = datetime(2026, 11, 10, tzinfo=timezone.utc)
    rows = [{**r, "label_id": f"00000000-0000-4000-8000-{i:012d}", "created_at": t} for i, r in enumerate(cons.rows)]
    return lines, rows


def _lead(row, *, labeller, label="ok", minute=1):
    out = rv.lead_row(row, label=label, claim_class="fact.number", labeller=labeller, reason="invented",
                      policy_sha=None)
    return {**out, "label_id": f"00000000-0000-4000-9000-{minute:012d}",
            "created_at": datetime(2026, 11, 10, 0, minute, tzinfo=timezone.utc)}


def test_prefilter_label_has_its_own_source_and_never_counts_as_release_evidence():
    lines, rows = _rows()
    h0, h4 = lines[0]["claim_hash"], lines[4]["claim_hash"]
    anchor0 = next(r for r in rows if r["claim_hash"] == h0)
    anchor4 = next(r for r in rows if r["claim_hash"] == h4)
    with_pf = rows + [_lead(anchor0, labeller=pf.LABELLER), _lead(anchor4, labeller=pf.LABELLER, minute=2)]
    finals = pn.final_labels(with_pf)
    assert finals[h0].source == "prefilter" and finals[h0].label == "ok"
    assert h0 not in pn.release_evidence(finals) and h0 not in pn.release_evidence(finals, audit_ok=True)
    assert h4 not in pn.release_evidence(finals, audit_ok=True)          # its machine row hides the panel label too
    # a machine row is not an audit adjudication: the panel error rate reads human rows only
    assert pn.agreement_report(with_pf)["audit"]["audited"] == 0
    # a human decision after it wins and counts again
    human = with_pf + [_lead(anchor0, labeller="admin:11111111-1111-1111-1111-111111111111", minute=5)]
    assert pn.final_labels(human)[h0].source == "tech_lead" and h0 in pn.release_evidence(pn.final_labels(human))
    summary = rv.set_summary(with_pf, [])
    assert summary["evidence_sources"]["prefilter"] == 2 and set(summary["evidence_sources"]) == set(rv.SOURCES)
    entry = next(e for e in rv.claim_entries(with_pf, []) if e["claim_hash"] == h0)
    assert entry["source"] == "prefilter" and entry["decided_by"] == pf.LABELLER
    # a claim with a machine row is no adjudication candidate (it left the queue)
    assert h0 not in {c["claim_hash"] for c in store.queue_candidates(with_pf)}


def test_queue_candidates_skip_claims():
    lines, rows = _rows()
    every = {c["claim_hash"] for c in store.queue_candidates(rows)}
    h0 = lines[0]["claim_hash"]
    assert h0 in every
    assert {c["claim_hash"] for c in store.queue_candidates(rows, skip_claims={h0})} == every - {h0}


# ── run on a fake pool ──────────────────────────────────────────────────────────────────────────────────────────────


class FakeConn:
    def __init__(self, *, queued=None, done=None):
        self.queued, self.done, self.calls = queued or [], done or [], []

    async def fetch(self, sql, *args):
        self.calls.append(sql)
        if sql is pf.QUEUED_SQL:
            return [q for q in self.queued if q["source_ref"] in args[0]]
        if sql is pf.DONE_SQL:
            return [d for d in self.done if d["subject_key"] in args[2]]
        if sql is pf.TENANT_SQL:
            return []
        raise AssertionError(f"unexpected SQL: {sql[:80]}")

    async def execute(self, sql, *args):
        self.calls.append(sql)
        return "INSERT 0 1"

    @asynccontextmanager
    async def transaction(self):
        yield


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


@pytest.fixture
def world(monkeypatch):
    lines, rows = _rows()
    by_text = {e["claim_text"]: p for e, p in zip(lines, P)}
    asked = []

    async def read_rows(conn, set_id, set_version):
        assert (set_id, set_version) == (SET, "1.0.0")
        return rows

    async def relabelled(conn, set_id, set_version):
        return set()

    async def sources(conn, anchors):
        return {a["claim_hash"]: "Day 1: Arrive in Velmora. Harbour Lodge.\nDay 2: Walk 12 km." for a in anchors}

    async def decide(stage, subject, state, keys, *, tenant_id=None, pool=None, use_cache=True):
        assert stage == pf.STAGE and keys == [pf.QUESTION] and use_cache is False
        assert set(state) == {"source", "sentence"}
        asked.append(subject)
        p = by_text[state["sentence"]]
        return dec.Decision(verdicts={pf.QUESTION: dec.Verdict(pf.QUESTION, "enforce", "accept", probability=p)},
                            cost_usd=0.0001)

    monkeypatch.setattr(store, "read_rows", read_rows)
    monkeypatch.setattr(store, "relabelled_claims", relabelled)
    monkeypatch.setattr(pf, "_sources", sources)
    monkeypatch.setattr(dec, "decide", decide)
    monkeypatch.setattr(dec, "_price_in_per_mtok", lambda: 0.042)
    return {"lines": lines, "rows": rows, "asked": asked}


def _run(conn, **kw):
    return asyncio.run(pf.run(FakePool(conn), SET, "1.0.0", **kw))


def test_plan_asks_nothing_and_counts(world):
    out = _run(FakeConn(), mode="shadow")
    assert out["call"] == "plan" and world["asked"] == [] and out["switch"] == "shadow"
    assert out["candidates"] == 5 and out["skipped"] == {"audit_draw": 1}
    assert out["verdicts"]["to_ask"] == 4 and out["verdicts"]["asked"] == 0
    assert out["decisions"] == {"auto_ok": 0, "review": 4} and out["review_reasons"] == {"no_verdict": 4}
    assert out["usd"]["estimated_to_ask"] > 0 and out["applied"] is None
    assert "Velmora" not in str(out)


def test_shadow_asks_jev_and_reports_without_writing(world):
    conn = FakeConn()
    out = _run(conn, ask=True, mode="shadow")
    assert out["call"] == "shadow" and len(world["asked"]) == 4
    assert out["verdicts"]["answered"] == 4 and out["usd"]["actual_this_call"] == pytest.approx(0.0004)
    h = [e["claim_hash"] for e in world["lines"]]
    got = {i["claim_hash"]: (i["decision"], i["why"]) for i in out["items"]}
    assert got[h[0]] == ("auto_ok", "rule_met")
    assert got[h[1]] == ("auto_ok", "rule_met")                              # misbinding on an ok lens only
    assert got[h[2]] == ("review", "misbinding_on_non_ok_lens")
    assert got[h[3]] == ("review", "majority_violation")
    assert h[4] not in got                                                    # the audit draw is never asked
    assert out["decisions"] == {"auto_ok": 2, "review": 2}
    # a1 is not a measured writer: in shadow and enforce alike its auto_ok claims are never written
    assert out["auto_ok_writable"] == 0 and out["auto_ok_shadow_writer"] == 2
    assert not any("INSERT" in c for c in conn.calls)


def test_apply_is_refused_while_the_switch_is_shadow(world, monkeypatch):
    monkeypatch.delenv(pf.MODE_ENV, raising=False)
    with pytest.raises(pf.PrefilterError) as e:
        _run(FakeConn(), apply=True)
    assert e.value.status == 409 and e.value.code == "switched_off" and world["asked"] == []


def test_enforce_applies_only_the_measured_writers(world, monkeypatch):
    applied = []

    async def fake_apply(pool, entries, *, run_id):
        applied.extend(entries)
        return {"written": len(entries), "resolved_items": 0, "decided_meanwhile": 0}

    monkeypatch.setattr(pf, "_apply", fake_apply)
    out = _run(FakeConn(), ask=True, apply=True, mode="enforce", run_id="pf-test-1")
    assert applied == [] and out["applied"]["written"] == 0 and out["applied"]["shadow_writer"] == 2
    monkeypatch.setattr(pf, "ENFORCE_WRITERS", ("a1",))
    out = _run(FakeConn(), ask=True, apply=True, mode="enforce", run_id="pf-test-2")
    h = [e["claim_hash"] for e in world["lines"]]
    assert sorted(e["claim_hash"] for e in applied) == sorted([h[0], h[1]]) and out["applied"]["written"] == 2


def test_known_answers_are_read_back_never_asked_again(world):
    h0 = world["lines"][0]["claim_hash"]
    src = "Day 1: Arrive in Velmora. Harbour Lodge.\nDay 2: Walk 12 km."
    conn = FakeConn(done=[{"subject_key": pf.subject_key(h0, src), "p": 0.96}])
    out = _run(conn, ask=True, mode="shadow")
    assert out["verdicts"]["known"] == 1 and len(world["asked"]) == 3
    assert pf.subject_key(h0, src) not in world["asked"]


def test_limit_and_resolved_items(world):
    rows = world["rows"]
    h1 = world["lines"][1]["claim_hash"]
    ref = next(c["source_ref"] for c in store.queue_candidates(rows) if c["claim_hash"] == h1)
    conn = FakeConn(queued=[{"item_id": "i-1", "source_ref": ref, "status": "resolved"}])
    out = _run(conn, ask=True, limit=1, mode="shadow")
    assert out["skipped"].get("queue_resolved") == 1
    assert out["verdicts"]["asked"] == 1 and out["verdicts"]["stopped"] == "limit 1"
    with pytest.raises(pf.PrefilterError):
        _run(FakeConn(), limit=0, mode="shadow")
    with pytest.raises(pf.PrefilterError):
        _run(FakeConn(), max_usd=1.0, mode="shadow")


# ── the route ───────────────────────────────────────────────────────────────────────────────────────────────────────

_SECRET = "aa1063-prefilter-test-admin-secret"
URL = f"/admin/writing-memory/claim-labels/{SET}/1.0.0/prefilter"


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from api.main import app
    from api.routers import admin, admin_settings
    monkeypatch.setenv("ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(admin, "ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(app.state, "pool", FakePool(FakeConn()), raising=False)
    monkeypatch.setattr(app.state, "redis", None, raising=False)
    return TestClient(app, raise_server_exceptions=True)


def _h(role="admin", user="11111111-1111-1111-1111-111111111111"):
    h = {"X-Admin-Secret": _SECRET, "x-admin-role": role}
    if user:
        h["x-admin-user-id"] = user
    return h


def test_route_passes_the_body_and_maps_refusals(client, monkeypatch):
    seen = {}

    async def fake_run(pool, set_id, set_version, **kw):
        seen.update(kw, set=(set_id, set_version))
        if kw["apply"]:
            raise pf.PrefilterError(409, "switched_off", "shadow")
        return {"set": f"{set_id}@{set_version}", "call": "shadow", "switch": "shadow", "candidates": 0,
                "decisions": {"auto_ok": 0, "review": 0}, "verdicts": {"asked": 0},
                "usd": {"actual_this_call": 0.0}, "applied": None}

    monkeypatch.setattr(pf, "run", fake_run)
    r = client.post(URL, json={"ask": True, "writers": ["t2"], "limit": 20, "run_id": "pf-t2-1"}, headers=_h())
    assert r.status_code == 200, r.text
    assert seen["set"] == (SET, "1.0.0") and seen["ask"] and seen["writers"] == ["t2"] and seen["limit"] == 20
    assert seen["actor"] == "admin:11111111-1111-1111-1111-111111111111" and seen["max_usd"] == 0.05
    r = client.post(URL, json={"apply": True}, headers=_h())
    assert r.status_code == 409 and r.json()["detail"]["code"] == "switched_off"
    assert client.post(URL, json={}, headers=_h(user=None)).status_code == 403          # a signed-in admin only
    assert client.post(URL, json={}, headers=_h("viewer")).status_code == 403
    for bad in ({"limit": 0}, {"limit": 101}, {"max_usd": 0.6}, {"writers": ["s1"]}, {"run_id": "bad id"},
                {"extra": 1}):
        assert client.post(URL, json=bad, headers=_h()).status_code == 422, bad


# ── the source and the write, with the DB helpers faked ─────────────────────────────────────────────────────────────


def test_sources_come_from_the_matching_golden_item():
    import json

    lines, rows = _rows()
    anchor = next(r for r in rows if r["claim_hash"] == lines[0]["claim_hash"])
    other = {**anchor, "claim_hash": "c" * 64, "sample_id": "zz-none"}

    class Conn:
        async def fetch(self, sql, *args):
            assert "shared.eval_golden_item" in sql
            return [{"sample_id": "zz-1063-p1", "writer": "a1", "channel": None, "input": json.dumps({"tour": TOUR}),
                     "control_output": json.dumps(OUTPUT)}]

    got = asyncio.run(pf._sources(Conn(), [anchor, other]))
    assert set(got) == {anchor["claim_hash"]} and "Ostrin Valley" in got[anchor["claim_hash"]]


def test_apply_writes_the_label_and_resolves_open_items(monkeypatch):
    from services.writing_memory import disagreements as dis

    lines, rows = _rows()
    h0, h1 = lines[0]["claim_hash"], lines[1]["claim_hash"]
    panel = {h: [r for r in rows if r["claim_hash"] == h] for h in (h0, h1)}
    lead_for = {h1: [_lead(panel[h1][0], labeller="admin:someone")]}       # decided by a human meanwhile
    inserted, resolved = [], []

    async def lock(conn, set_id, set_version, claim_hash):
        return [{"item_id": f"item-{claim_hash[:6]}", "status": "open"}]

    async def claim_rows(conn, keys):
        [(_s, _v, h)] = keys
        return panel[h] + lead_for.get(h, [])

    async def read_relabels(conn, set_id, set_version, claim_hash=None):
        return []

    async def insert_rows(conn, new_rows, *, apply):
        inserted.extend(new_rows)
        return {"inserted": len(new_rows)}

    async def resolve(conn, item_id, *, resolution, actor, note=None):
        resolved.append((item_id, resolution, actor))
        return {"status": "resolved"}

    monkeypatch.setattr(rv, "lock_claim_items", lock)
    monkeypatch.setattr(rv, "claim_rows", claim_rows)
    monkeypatch.setattr(rv, "read_relabels", read_relabels)
    monkeypatch.setattr(store, "insert_rows", insert_rows)
    monkeypatch.setattr(dis, "resolve", resolve)
    entries = [{"claim_hash": h, "set_id": SET, "set_version": "1.0.0", "p": 0.97} for h in (h0, h1)]
    out = asyncio.run(pf._apply(FakePool(FakeConn()), entries, run_id="pf-unit-1"))
    assert out == {"written": 1, "resolved_items": 1, "decided_meanwhile": 1}
    [row] = inserted
    assert row["claim_hash"] == h0 and row["labeller"] == pf.LABELLER and row["label"] == "ok"
    assert row["labeller_kind"] == "tech_lead" and row["adjudication"] == "final" and "pf-unit-1" in row["reason"]
    assert resolved == [(f"item-{h0[:6]}", "judge_right", pf.LABELLER)]
