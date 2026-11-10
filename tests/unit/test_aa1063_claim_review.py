"""AA-1063 — the Writing Lab claim review without a DB: answers -> resolution and tech-lead label, the class pick,
corrections read as the newest final label (effective rows), set summaries, the browser entries and filters, proposal
checks, the golden-item context, and the routes (policy, actor required, payload refusals, report / finals with a
correction). Invented names and places only."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest

from services.ground_truth import claims as cl
from services.ground_truth import panel as pn
from services.ground_truth import review as rv
from services.ground_truth import store

SET = "ZZ-AA1063-U"
T0 = datetime(2026, 11, 10, 3, 0, tzinfo=timezone.utc)
OUTPUT = {"content_text": "Arrive in Velmora and rest at the Harbour Lodge. Walk 40 km to Ostrin Valley on day "
                          "two. Breakfast is served at the lodge each morning.", "gate_ledger": [{"gate": "x"}]}
TOUR = {"name": "Velmora Walk", "days": ["Day 1: Arrive in Velmora. Harbour Lodge.",
                                         "Day 2: Walk 12 km to Ostrin Valley."]}


def _rows(labels=None, state="disputed", lead=None):
    """Panel rows of one invented A1 claim (3 lenses) and an optional tech-lead row."""
    lines = pn.export_records(set_id=SET, writer="a1", scope="platform", tenant_id=None,
                              refs={"sample_id": "zz-1063"}, output=rv.panel_output(OUTPUT),
                              source_text=rv.source_text("a1", None, {"tour": TOUR}))
    e = lines[1]
    rows = []
    for i, (lens, lab) in enumerate(zip(pn.LENSES, labels or ["violation", "violation", "ok"])):
        rows.append({"label_id": f"00000000-0000-4000-8000-00000000000{i}", "created_at": T0, "set_id": SET,
                     "set_version": "1.0.0", "writer": "a1", "scope": "platform", "tenant_id": None,
                     "sample_id": "zz-1063", "piece_id": None, "tour_id": None, "version_id": None,
                     "content_sha256": e["content_sha256"], "field": e["field"], "span_start": e["span"][0],
                     "span_end": e["span"][1], "claim_text": e["claim_text"], "claim_hash": e["claim_hash"],
                     "claim_class": "fact.number" if lens != "skeptic" else "fact.name", "label": lab,
                     "labeller_kind": "agent_panel", "labeller": "panel:zz", "lens": lens,
                     "reason": f"{lens}: invented reason", "split": "train", "stratum": "representative",
                     "adjudication": state, "policy_sha": "c" * 64})
    if lead:
        rows.append({**rows[0], "label_id": "00000000-0000-4000-8000-0000000000aa", "created_at": T0 + timedelta(1),
                     "labeller_kind": "tech_lead", "labeller": "admin:lead", "lens": None, "label": lead,
                     "adjudication": "final", "reason": "queue: judge_right"})
    return rows


def _relabel(rows, label, *, rid="00000000-0000-4000-8000-0000000000b1", sup=None, days=2,
             cls="fact.number"):
    return {"relabel_id": rid, "created_at": T0 + timedelta(days), "set_id": SET, "set_version": "1.0.0",
            "claim_hash": rows[0]["claim_hash"], "anchor_label_id": rows[0]["label_id"], "claim_class": cls,
            "label": label, "reason": "invented correction", "labeller": "admin:lead", "supersedes": sup,
            "previous_label": "violation", "previous_class": "fact.number", "previous_source": "tech_lead"}


# ── answers ─────────────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("machine, answer, label, resolution", [
    ("violation", "wrong", "violation", "judge_right"),
    ("violation", "correct", "ok", "human_right"),
    ("violation", "unsure", "unsure", "unclear"),
    ("ok", "correct", "ok", "judge_right"),
    ("ok", "wrong", "violation", "human_right"),
    ("split", "correct", "ok", "unclear"),
    ("split", "wrong", "violation", "unclear"),
    ("unsure", "unsure", "unsure", "unclear"),
])
def test_answer_plan(machine, answer, label, resolution):
    assert rv.answer_plan(machine, answer) == (label, resolution)
    if machine in ("ok", "violation"):      # the same label pull_resolutions would write later
        assert store.resolution_label(machine, resolution) == label


def test_answer_plan_refuses_an_unknown_answer():
    with pytest.raises(rv.ReviewError) as e:
        rv.answer_plan("ok", "maybe")
    assert e.value.status == 422


def test_pick_class_prefers_the_lenses_that_gave_the_label():
    rows = _rows(["ok", "ok", "violation"])
    assert rv.pick_class(rows, "violation", rows[0]) == "fact.name"
    assert rv.pick_class(rows, "ok", rows[0]) == "fact.number"
    assert rv.pick_class(rows, "unsure", rows[0]) == rows[0]["claim_class"]


def test_lead_row_copies_the_claim_and_marks_it_final():
    rows = _rows()
    r = rv.lead_row(rows[0], label="ok", claim_class="fact.number", labeller="admin:x", reason="review:  correct ",
                    policy_sha=None)
    assert set(r) == set(store.COLUMNS)
    assert r["labeller_kind"] == "tech_lead" and r["lens"] is None and r["adjudication"] == "final"
    assert r["claim_hash"] == rows[0]["claim_hash"] and r["reason"] == "review: correct"
    assert r["policy_sha"] == "c" * 64


# ── corrections ─────────────────────────────────────────────────────────────────────────────────────────────────────


def test_a_correction_is_the_final_label():
    rows = _rows(lead="violation")
    h = rows[0]["claim_hash"]
    assert pn.final_labels(rv.effective_rows(rows, []))[h].label == "violation"
    one = _relabel(rows, "ok")
    f = pn.final_labels(rv.effective_rows(rows, [one]))[h]
    assert (f.label, f.source) == ("ok", "tech_lead")
    two = _relabel(rows, "unsure", rid="00000000-0000-4000-8000-0000000000c2", sup=one["relabel_id"], days=3)
    assert pn.final_labels(rv.effective_rows(rows, [two, one]))[h].label == "unsure"
    assert rv.chain_head([one, two])["relabel_id"] == two["relabel_id"]
    assert rv.chain_head([]) is None


def test_a_later_tech_lead_row_wins_over_an_older_correction():
    rows = _rows(state="agreed", labels=["ok", "ok", "ok"])
    one = _relabel(rows, "violation", days=1)
    late = {**rows[0], "label_id": "00000000-0000-4000-8000-0000000000dd", "created_at": T0 + timedelta(5),
            "labeller_kind": "tech_lead", "lens": None, "label": "ok", "adjudication": "final"}
    assert pn.final_labels(rv.effective_rows(rows + [late], [one]))[rows[0]["claim_hash"]].label == "ok"


def test_one_decision_row_names_the_label_and_who_when_why():
    # a correction, then a tech-lead row written later (e.g. by an older tool): the later row is the final label, and
    # the browser's who / when / why and the relabelled flag come from that same row, not from the correction
    rows = _rows(state="audit", labels=["ok", "ok", "ok"])
    one = _relabel(rows, "violation", days=1)
    late = {**rows[0], "label_id": "00000000-0000-4000-8000-0000000000dd", "created_at": T0 + timedelta(5),
            "labeller_kind": "tech_lead", "labeller": "admin:later", "lens": None, "label": "ok",
            "adjudication": "final", "reason": "review: correct"}
    top = rv.newest_lead(rows + [late], [one])
    assert top["label_id"] == late["label_id"] and not top.get("relabel")
    [e] = rv.claim_entries(rows + [late], [one])
    assert (e["label"], e["decided_by"], e["why"]) == ("ok", "admin:later", "review: correct")
    assert e["decided_at"].startswith("2026-11-15") and not e["relabelled"]
    assert e["head_relabel_id"] == one["relabel_id"]                  # the next correction still supersedes it
    assert [h["kind"] for h in e["history"]] == ["relabel", "tech_lead"]  # time order
    assert all("_d" not in h for h in e["history"])
    # the correction alone: it is the decision
    top = rv.newest_lead(rows, [one])
    assert top["relabel"] and top["label"] == "violation" and top["labeller"] == "admin:lead"
    [e] = rv.claim_entries(rows, [one])
    assert e["relabelled"] and e["decided_by"] == "admin:lead" and e["why"] == "invented correction"
    assert rv.newest_lead(rows, []) is None


def test_effective_rows_leave_the_stored_rows_alone_without_corrections():
    rows = _rows(lead="violation")
    assert rv.effective_rows(rows, []) == rows
    orphan = {**_relabel(rows, "ok"), "claim_hash": "f" * 64}
    assert rv.effective_rows(rows, [orphan]) == rows


def test_set_summary_counts_sources_corrections_and_audit():
    agreed = _rows(labels=["ok", "ok", "ok"], state="audit")
    s = rv.set_summary(agreed, [])
    assert s["claims"] == 1 and s["evidence_sources"] == {"tech_lead": 0, "panel_agreed": 1, "none": 0}
    assert s["panel_states"] == {"audit": 1} and s["relabels"] == 0
    flipped = rv.set_summary(agreed, [_relabel(agreed, "violation")])
    assert flipped["evidence_sources"]["tech_lead"] == 1 and flipped["audit"]["flipped"] == 1
    assert flipped["labels"] == {"violation": 1} and flipped["last_change"].startswith("2026-11-12")
    assert rv.set_summary(_rows(), [])["evidence_sources"]["none"] == 1


def test_claim_entries_say_who_decided_when_and_why():
    rows = _rows(lead="violation")
    [e] = rv.claim_entries(rows, [])
    assert (e["source"], e["decided_by"], e["label"]) == ("tech_lead", "admin:lead", "violation")
    assert e["why"] == "queue: judge_right" and not e["relabelled"] and e["head_relabel_id"] is None
    assert [p["lens"] for p in e["panel"]] == sorted(pn.LENSES) and len(e["history"]) == 1
    one = _relabel(rows, "ok")
    [e] = rv.claim_entries(rows, [one])
    assert e["relabelled"] and e["head_relabel_id"] == one["relabel_id"] and e["label"] == "ok"
    assert [h["kind"] for h in e["history"]] == ["tech_lead", "relabel"] and e["why"] == "invented correction"
    [agreed] = rv.claim_entries(_rows(labels=["ok", "ok", "ok"], state="agreed"), [])
    assert agreed["decided_by"] == "panel" and "source: invented reason" in agreed["why"]
    [none] = rv.claim_entries(_rows(), [])
    assert none["source"] == "none" and none["decided_by"] is None


def test_filter_claims():
    entries = rv.claim_entries(_rows(lead="violation"), [])
    assert rv.filter_claims(entries, source="tech_lead") == entries
    assert rv.filter_claims(entries, source="panel_agreed") == []
    assert rv.filter_claims(entries, label="violation", writer="a1", split="train") == entries
    assert rv.filter_claims(entries, relabelled=True) == []
    assert rv.filter_claims(entries, q="OSTRIN   valley") == entries
    assert rv.filter_claims(entries, q="Harbour") == []
    assert rv.filter_claims(entries, claim_class="fact.name") == []


# ── proposals ───────────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("line, why", [
    ("x", "not an object"),
    ({"label": "ok", "reason_en": "fine"}, "needs item_id or claim_hash"),
    ({"claim_hash": "abc", "label": "ok", "reason_en": "fine"}, "64-hex"),
    ({"item_id": "i", "label": "maybe", "reason_en": "fine"}, "label must be"),
    ({"item_id": "i", "label": "ok"}, "needs reason_en or reason_vi"),
    ({"item_id": "i", "label": "ok", "reason_vi": "ổn", "confidence": "very"}, "confidence"),
    ({"item_id": "i", "label": "ok", "reason_vi": "ổn", "notes": {"other": "x"}}, "notes"),
    ({"item_id": "i", "label": "ok", "reason_vi": "ổn", "notes": {"claim_vi": 3}}, "notes"),
])
def test_check_proposal_refusals(line, why):
    assert why in (rv.check_proposal(line) or "")


def test_proposal_values_are_trimmed():
    p = {"item_id": "i", "label": "violation", "reason_en": "  The source says  12 km. ", "reason_vi": None,
         "bad_part": "40 km", "confidence": "high", "notes": {"claim_vi": " Đi bộ 40 km ", "doubt_en": "  "}}
    assert rv.check_proposal(p) is None
    v = rv.proposal_values(p)
    assert v["reason_en"] == "The source says 12 km." and v["reason_vi"] is None
    assert v["notes"] == {"claim_vi": "Đi bộ 40 km"} and v["bad_part"] == "40 km"


def test_import_refuses_a_bad_file_before_any_db_call():
    class NoDb:
        async def fetch(self, *a):
            raise AssertionError("no DB call expected")

    with pytest.raises(rv.ReviewError, match="#1"):
        asyncio.run(rv.import_proposals(NoDb(), run_id="panel:zz", proposals=[{"label": "ok"}], apply=False,
                                        actor="admin:x"))
    with pytest.raises(rv.ReviewError, match="set_id and set_version"):
        asyncio.run(rv.import_proposals(NoDb(), run_id="panel:zz", apply=False, actor="admin:x", proposals=[
            {"claim_hash": "a" * 64, "label": "ok", "reason_en": "fine"}]))
    with pytest.raises(rv.ReviewError, match="run id"):
        asyncio.run(rv.import_proposals(NoDb(), run_id="bad run", proposals=[], apply=False, actor="admin:x"))


# ── context ─────────────────────────────────────────────────────────────────────────────────────────────────────────


def test_context_comes_from_the_golden_item_with_the_same_content_sha():
    rows = _rows()
    claim = {**rows[0]}
    same = {"sample_id": "zz-1063", "writer": "a1", "channel": None, "input": {"tour": TOUR},
            "control_output": OUTPUT}
    other = {**same, "control_output": {"content_text": "Another text about the Ostrin Valley entirely."}}
    assert rv.match_golden(claim, [other]) is None
    g = rv.match_golden(claim, [other, same])
    assert g is same
    ctx = rv.context_of(claim, g)
    assert ctx["found"] and ctx["before"].endswith("Harbour Lodge.") and ctx["after"].startswith("Breakfast")
    assert "Day 2: Walk 12 km to Ostrin Valley." in ctx["evidence"]
    assert "Velmora Walk" in ctx["source_text"] and "gate_ledger" not in ctx["source_text"]
    assert rv.context_of(claim, None) == {"found": False, "before": None, "after": None, "evidence": [],
                                          "source_text": None}


def test_context_reads_json_text_from_the_db():
    import json

    claim = {**_rows()[0]}
    g = {"writer": "a1", "channel": None, "input": json.dumps({"tour": TOUR}), "control_output": json.dumps(OUTPUT)}
    assert rv.match_golden(claim, [g]) is g and rv.context_of(claim, g)["found"]
    assert cl.content_sha(rv.panel_output(OUTPUT)) == claim["content_sha256"]


# ── routes ──────────────────────────────────────────────────────────────────────────────────────────────────────────

_SECRET = "aa1063-review-test-admin-secret"
BASE = "/admin/writing-memory/claim-labels"
ITEM = "00000000-0000-4000-8000-00000000a106"


class _Pool:
    @asynccontextmanager
    async def acquire(self):
        yield object()


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from api.main import app
    from api.routers import admin, admin_settings
    monkeypatch.setenv("ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(admin, "ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(app.state, "pool", _Pool(), raising=False)
    monkeypatch.setattr(app.state, "redis", None, raising=False)
    calls: list = []

    def fake(name, result):
        async def f(conn, *a, **kw):
            calls.append((name, a, kw))
            if isinstance(result, Exception):
                raise result
            return result
        monkeypatch.setattr(rv, name, f)

    c = TestClient(app, raise_server_exceptions=True)
    c.calls, c.fake = calls, fake
    return c


def _h(role="admin", user=True):
    h = {"X-Admin-Secret": _SECRET, "x-admin-role": role}
    if user:
        h["x-admin-user-id"] = "11111111-1111-1111-1111-111111111111"
    return h


WRITES = [("post", f"{BASE}/review/{ITEM}/answer", {"answer": "correct"}),
          ("post", f"{BASE}/review/accept-proposals", {"item_ids": [ITEM]}),
          ("post", f"{BASE}/proposals/import", {"run_id": "panel:zz", "proposals": [{"item_id": ITEM}]}),
          ("post", f"{BASE}/{SET}/1.0.0/claims/{'a' * 64}/relabel", {"label": "ok", "reason": "invented why"})]


@pytest.mark.parametrize("method, url, body", WRITES)
@pytest.mark.parametrize("role", ["content", "tenant_qa", "viewer"])
def test_writes_are_admin_only(client, method, url, body, role):
    assert getattr(client, method)(url, json=body, headers=_h(role)).status_code == 403


@pytest.mark.parametrize("method, url, body", WRITES)
def test_writes_need_a_signed_in_admin(client, method, url, body):
    r = getattr(client, method)(url, json=body, headers=_h(user=False))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "no_actor"


def test_answer_route(client):
    client.fake("answer_item", {"item_id": ITEM, "answer": "wrong", "label": "violation",
                                "resolution": "judge_right", "inserted": 1, "status": "resolved"})
    r = client.post(f"{BASE}/review/{ITEM}/answer", json={"answer": "wrong", "note": "invented"}, headers=_h())
    assert r.status_code == 200 and r.json()["resolution"] == "judge_right"
    name, args, kw = client.calls[-1]
    assert args == (ITEM,) and kw["actor"] == "admin:11111111-1111-1111-1111-111111111111"
    assert kw["answer"] == "wrong" and kw["proposal_id"] is None
    assert client.post(f"{BASE}/review/{ITEM}/answer", json={"answer": "maybe"}, headers=_h()).status_code == 422
    assert client.post(f"{BASE}/review/not-a-uuid/answer", json={"answer": "wrong"}, headers=_h()).status_code == 404
    client.fake("answer_item", rv.ReviewError(409, "resolved", "the item is already resolved"))
    r = client.post(f"{BASE}/review/{ITEM}/answer", json={"answer": "wrong"}, headers=_h())
    assert r.status_code == 409 and r.json()["detail"]["code"] == "resolved"


def test_review_list_and_context_routes(client):
    client.fake("list_items", {"items": [], "total": 0, "limit": 25, "offset": 0, "facets": []})
    r = client.get(f"{BASE}/review?set_id={SET}&set_version=1.1.0&state=all&proposal=none&writer=t9_blog",
                   headers=_h())
    assert r.status_code == 200 and r.json()["total"] == 0
    kw = client.calls[-1][2]
    assert (kw["set_id"], kw["set_version"], kw["state"], kw["proposal"], kw["writer"]) == (
        SET, "1.1.0", "all", "none", "t9_blog")
    assert kw["with_context"] is True
    client.get(f"{BASE}/review?context=false&limit=100", headers=_h())
    assert client.calls[-1][2]["with_context"] is False and client.calls[-1][2]["limit"] == 100
    assert client.get(f"{BASE}/review?state=closed", headers=_h()).status_code == 422
    assert client.get(f"{BASE}/review?set_version=one", headers=_h()).status_code == 422
    client.fake("item_context", {"item_id": ITEM, "found": False})
    assert client.get(f"{BASE}/review/{ITEM}/context", headers=_h()).json()["found"] is False
    assert client.get(f"{BASE}/review/{ITEM}/context", headers=_h("viewer")).status_code == 403


def test_review_routes_answer_503_before_the_migration(client):
    import asyncpg

    client.fake("list_items", asyncpg.UndefinedTableError("relation does not exist"))
    r = client.get(f"{BASE}/review", headers=_h())
    assert r.status_code == 503 and "CIS 394" in r.json()["detail"]["detail"]


def test_proposals_and_accept_routes(client):
    client.fake("import_proposals", {"run_id": "panel:zz", "dry_run": True, "lines": 1, "new": 1, "unchanged": 0,
                                     "not_queued": 0, "inserted": 0, "labels": {"ok": 1}})
    body = {"run_id": "panel:zz", "set_id": SET, "set_version": "1.1.0",
            "proposals": [{"claim_hash": "a" * 64, "label": "ok", "reason_vi": "ổn"}]}
    r = client.post(f"{BASE}/proposals/import", json=body, headers=_h())
    assert r.status_code == 200 and client.calls[-1][2]["apply"] is False
    client.post(f"{BASE}/proposals/import", json={**body, "dry_run": False}, headers=_h())
    assert client.calls[-1][2]["apply"] is True
    assert client.post(f"{BASE}/proposals/import", json={**body, "extra": 1}, headers=_h()).status_code == 422
    client.fake("accept_proposals", {"accepted": 1, "no_proposal": 0, "not_open": 0, "unknown": 0, "refused": [],
                                     "labels": {"ok": 1}})
    r = client.post(f"{BASE}/review/accept-proposals", json={"item_ids": [ITEM]}, headers=_h())
    assert r.status_code == 200 and client.calls[-1][1] == ([ITEM],)
    too_many = {"item_ids": [ITEM] * (rv.MAX_BULK + 1)}
    assert client.post(f"{BASE}/review/accept-proposals", json=too_many, headers=_h()).status_code == 422


def test_relabel_route(client):
    client.fake("relabel", {"relabel_id": "r", "claim_hash": "a" * 64, "label": "ok", "claim_class": "fact.number",
                            "previous_label": "violation", "previous_source": "tech_lead"})
    url = f"{BASE}/{SET}/1.0.0/claims/{'a' * 64}/relabel"
    r = client.post(url, json={"label": "ok", "reason": "invented why"}, headers=_h())
    assert r.status_code == 200
    args, kw = client.calls[-1][1:]
    assert args == (SET, "1.0.0", "a" * 64) and kw["supersedes"] is None and kw["label"] == "ok"
    assert client.post(url, json={"label": "fine", "reason": "invented"}, headers=_h()).status_code == 422
    assert client.post(url, json={"label": "ok", "reason": "x"}, headers=_h()).status_code == 422
    assert client.post(f"{BASE}/{SET}/1.0.0/claims/xyz/relabel", json={"label": "ok", "reason": "invented"},
                       headers=_h()).status_code == 404
    assert client.post(f"{BASE}/bad%20set/1.0.0/claims/{'a' * 64}/relabel", json={"label": "ok", "reason": "abc"},
                       headers=_h()).status_code == 404
    client.fake("relabel", rv.ReviewError(409, "stale", "reload"))
    r = client.post(url, json={"label": "ok", "reason": "invented", "supersedes": ITEM}, headers=_h())
    assert r.status_code == 409 and client.calls[-1][2]["supersedes"] == ITEM


def test_sets_claims_report_and_finals_read_the_correction(client, monkeypatch):
    rows = _rows(lead="violation")
    one = _relabel(rows, "ok")

    async def read_rows(conn, s, v):
        return rows

    async def read_relabels_soft(conn, s, v):
        return [one]

    monkeypatch.setattr(store, "read_rows", read_rows)
    monkeypatch.setattr(rv, "read_relabels_soft", read_relabels_soft)
    rep = client.get(f"{BASE}/{SET}/1.0.0/report", headers=_h()).json()
    assert rep["rows"] == 4 and rep["relabels"] == 1 and rep["by_labeller_kind"] == {"agent_panel": 3,
                                                                                     "tech_lead": 1}
    assert rep["labels_by_class"] == {"fact.number": {"ok": 1}}
    assert rep["evidence_sources"] == {"tech_lead": 1, "panel_agreed": 0, "none": 0}
    fin = client.get(f"{BASE}/{SET}/1.0.0/finals", headers=_h()).json()["finals"]
    assert [f["label"] for f in fin] == ["ok"]
    page = client.get(f"{BASE}/{SET}/1.0.0/claims?relabelled=true", headers=_h()).json()
    assert page["total"] == 1 and page["claims"][0]["head_relabel_id"] == one["relabel_id"]
    assert page["summary"]["relabels"] == 1 and "fact.number" in page["classes"]
    assert client.get(f"{BASE}/{SET}/1.0.0/claims?source=none", headers=_h()).json()["total"] == 0
    assert client.get(f"{BASE}/{SET}/1.0.0/claims", headers=_h("viewer")).status_code == 403
    client.fake("list_sets", [{"set_id": SET, "set_version": "1.0.0"}])
    assert client.get(f"{BASE}/sets", headers=_h()).json() == {"sets": [{"set_id": SET, "set_version": "1.0.0"}]}
