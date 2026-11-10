"""AA-1063 — the Jev pre-filter (services/ground_truth/prefilter.py) and the golden_claim enqueue on real Postgres
(conftest replays every migration), as cis_app, with decide() mocked (no provider call).

A golden A1 item and a 5-claim label set: two disputed claims the rule settles (one with fact.misbinding on an OK lens
only, the S275 edge case), one held back by fact.misbinding on its violation lens, one with a violation majority, and
one audit draw. The enqueue is idempotent and leaves out a corrected claim; the pre-filter plans without a call, asks
in shadow without writing, and in enforce writes the 'prefilter:jev090+panel' rows, resolves their queue items
judge_right, never touches the audit draw, and a second run writes nothing. The labels it wrote have source
'prefilter' and are no release evidence.

Invented values only; everything runs in a transaction that is rolled back."""
import json
import os
import sys
from contextlib import asynccontextmanager
from datetime import date

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from services.ground_truth import claims as cl  # noqa: E402
from services.ground_truth import panel as pn  # noqa: E402
from services.ground_truth import prefilter as pf  # noqa: E402
from services.ground_truth import review as rv  # noqa: E402
from services.ground_truth import store  # noqa: E402
from shared.llm_client import decide as dec  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
SET = "ZZ-AA1063-PF"
SAMPLE = "zz-1063-pf1"
ACTOR = "admin:11111111-1111-1111-1111-111111111111"
OUTPUT = {"content_text": "Arrive in Velmora and rest at the Harbour Lodge. Walk 40 km to Ostrin Valley on day two. "
                          "Breakfast is served at the lodge each morning. The Kestrel Bridge was rebuilt in 1911. "
                          "Dinner is at the Ostrin Inn."}
TOUR = {"name": "Velmora Walk", "itinerary": ["Day 1: Arrive in Velmora. Harbour Lodge.",
                                              "Day 2: Walk 12 km to Ostrin Valley. Breakfast at the lodge."]}
VOTES = [
    (("ok", "ok", "violation"), ("fact.number", "fact.number", "fact.number")),          # 0: auto_ok
    (("ok", "ok", "violation"), ("fact.number", "fact.misbinding", "fact.number")),      # 1: misbinding on an OK lens
    (("ok", "ok", "violation"), ("fact.number", "fact.number", "fact.misbinding")),      # 2: misbinding, non-ok lens
    (("violation", "violation", "ok"), ("fact.number", "fact.number", "fact.number")),   # 3: majority violation
    (("ok", "ok", "ok"), ("fact.number", "fact.number", "fact.number")),                 # 4: agreed -> audit draw
]
P = [0.97, 0.95, 0.99, 0.98, 0.99]


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        yield c
    finally:
        await tr.rollback()
        await c.close()


class _Pool:
    """One connection as a pool (the test's transaction holds every write)."""

    def __init__(self, c):
        self.c = c

    @asynccontextmanager
    async def acquire(self):
        yield self.c


async def _seed(c) -> list[dict]:
    gset = await c.fetchval(
        "INSERT INTO shared.eval_golden_set (name, version, status, source_uri, source_sha256, seed, holdout_pct, "
        "tier_rules, shared_inputs, meta, label_history, item_count, imported_by) VALUES ('zz_aa1063_pf', '1.0.0', "
        "'active', 'file:zz-aa1063-pf.json', $1, 7, 0.300, '{}', '{}', '{}', '[]', 1, 'pytest') RETURNING set_id",
        "e" * 64)
    await c.execute(
        "INSERT INTO shared.eval_golden_item (set_id, sample_id, writer, channel, scope, tenant_id, split, trap, kind, "
        "tiers, input, control_output, post_edit, label, label_verdict, label_source, refs, input_sha256) VALUES "
        "($1, $2, 'a1', NULL, 'platform', NULL, 'tune', false, 'real', ARRAY[]::text[], $3::jsonb, $4::jsonb, false, "
        "'{\"verdict\": \"pass\"}', 'pass', 'agent_panel', '{}', $5)",
        gset, SAMPLE, json.dumps({"tour": TOUR}), json.dumps(OUTPUT), "f" * 64)
    lines = pn.export_records(set_id=SET, writer="a1", scope="platform", tenant_id=None, refs={"sample_id": SAMPLE},
                              output=OUTPUT, source_text=rv.source_text("a1", None, {"tour": TOUR}))
    assert len(lines) == len(VOTES)
    raw = [{"claim_hash": e["claim_hash"], "lens": ln, "class": cls, "label": lab, "reason": f"{ln}: invented",
            "labeller": "panel:zz"}
           for e, (labels, classes) in zip(lines, VOTES) for ln, lab, cls in zip(pn.LENSES, labels, classes)]
    js = pn.parse_judgements(raw, export_by_hash={e["claim_hash"]: e for e in lines})
    cons = pn.consolidate(lines, js, set_id=SET, set_version="1.0.0", policy_sha="d" * 64, audit_pct=1.0)
    await store.insert_rows(c, cons.rows, apply=True)
    return lines


async def _as_cis_app(c):
    await c.execute("GRANT USAGE ON SCHEMA shared, acp_shared TO cis_app")
    await c.execute("GRANT SELECT, INSERT, UPDATE ON shared.writing_memory_disagreement TO cis_app")
    await c.execute("GRANT SELECT, INSERT, DELETE ON shared.eval_claim_label TO cis_app")
    await c.execute("GRANT SELECT ON shared.eval_golden_item, shared.decision_log, shared.tenants, "
                    "shared.jev_tenant_allowlist TO cis_app")
    await c.execute("GRANT SELECT, INSERT ON shared.eval_claim_proposal, shared.eval_claim_relabel TO cis_app")
    await c.execute("GRANT SELECT, INSERT ON acp_shared.audit_log TO cis_app")
    await c.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA acp_shared TO cis_app")
    await c.execute("SET LOCAL ROLE cis_app")


@pytest.fixture
def jev(monkeypatch):
    by_text = {}
    asked = []

    async def decide(stage, subject, state, keys, *, tenant_id=None, pool=None, use_cache=True):
        assert stage == pf.STAGE and keys == [pf.QUESTION] and tenant_id is None
        asked.append(subject)
        p = by_text[state["sentence"]]
        return dec.Decision(verdicts={pf.QUESTION: dec.Verdict(pf.QUESTION, "enforce", "accept", probability=p)})

    monkeypatch.setattr(dec, "decide", decide)
    monkeypatch.setattr(dec, "_price_in_per_mtok", lambda: 0.042)
    monkeypatch.setattr(pf, "ENFORCE_WRITERS", ("a1",))       # the fixture is platform A1 text (no tenant needed)
    return {"by_text": by_text, "asked": asked}


@pytest.mark.asyncio
async def test_enqueue_is_idempotent_and_skips_a_corrected_claim(conn):
    lines = await _seed(conn)
    await _as_cis_app(conn)
    rows = await store.read_rows(conn, SET, "1.0.0")
    audit = lines[4]["claim_hash"]
    assert {c["claim_hash"]: c["human_label"] for c in store.queue_candidates(rows)}[audit] == "audit"
    await rv.relabel(conn, SET, "1.0.0", audit, label="violation", claim_class=None, reason="invented correction",
                     supersedes=None, actor=ACTOR)
    dry = await store.enqueue(conn, rows, today=date(2026, 11, 10), apply=False)
    assert dry["added"] == 4 and dry["candidates"] == 4 and dry["corrected"] == 1
    first = await store.enqueue(conn, rows, today=date(2026, 11, 10), apply=True)
    assert first["added"] == 4
    again = await store.enqueue(conn, await store.read_rows(conn, SET, "1.0.0"), today=date(2026, 11, 10),
                                apply=True, writers=["a1"])
    assert again["added"] == 0 and again["already_queued"] == 4 and again["remaining"] == 0


@pytest.mark.asyncio
async def test_prefilter_plan_shadow_then_enforce(conn, jev):
    lines = await _seed(conn)
    jev["by_text"].update({e["claim_text"]: p for e, p in zip(lines, P)})
    h = [e["claim_hash"] for e in lines]
    await _as_cis_app(conn)
    await store.enqueue(conn, await store.read_rows(conn, SET, "1.0.0"), today=date(2026, 11, 10), apply=True)
    pool = _Pool(conn)

    plan = await pf.run(pool, SET, "1.0.0", mode="shadow")
    assert plan["call"] == "plan" and jev["asked"] == [] and plan["verdicts"]["to_ask"] == 4
    assert plan["skipped"] == {"audit_draw": 1}

    shadow = await pf.run(pool, SET, "1.0.0", ask=True, mode="shadow", run_id="pf-zz-1")
    assert len(jev["asked"]) == 4 and shadow["decisions"] == {"auto_ok": 2, "review": 2}
    got = {i["claim_hash"]: i["why"] for i in shadow["items"]}
    assert got == {h[0]: "rule_met", h[1]: "rule_met", h[2]: "misbinding_on_non_ok_lens",
                   h[3]: "majority_violation"}
    assert not [r for r in await store.read_rows(conn, SET, "1.0.0") if r["labeller_kind"] == "tech_lead"]
    with pytest.raises(pf.PrefilterError):
        await pf.run(pool, SET, "1.0.0", apply=True, mode="shadow")

    done = await pf.run(pool, SET, "1.0.0", ask=True, apply=True, mode="enforce", run_id="pf-zz-2", actor=ACTOR)
    assert done["applied"] == {"written": 2, "resolved_items": 2, "decided_meanwhile": 0, "shadow_writer": 0}
    rows = await store.read_rows(conn, SET, "1.0.0")
    lead = {r["claim_hash"]: r for r in rows if r["labeller_kind"] == "tech_lead"}
    assert set(lead) == {h[0], h[1]}
    for r in lead.values():
        assert r["labeller"] == pf.LABELLER and r["label"] == "ok" and "pf-zz-2" in r["reason"]
        assert r["adjudication"] == "final" and r["lens"] is None
    items = {r["claim_hash"]: dict(r) for r in await conn.fetch(
        "SELECT l.claim_hash, d.status, d.resolution, d.resolved_by FROM shared.writing_memory_disagreement d "
        "JOIN shared.eval_claim_label l ON l.label_id = d.source_ref WHERE d.source = 'golden_claim' "
        "AND l.set_id = $1", SET)}
    for x in (h[0], h[1]):
        assert items[x]["status"] == "resolved" and items[x]["resolution"] == "judge_right"
        assert items[x]["resolved_by"] == pf.LABELLER
    for x in (h[2], h[3], h[4]):
        assert items[x]["status"] == "open"
    audit_rows = await conn.fetchval("SELECT count(*) AS n FROM acp_shared.audit_log WHERE action = $1 "
                                     "AND resource_id = 'pf-zz-2'", pf.AUDIT_ACTION)
    assert audit_rows == 1

    finals = pn.final_labels(rows)
    assert finals[h[0]].source == "prefilter" and finals[h[1]].source == "prefilter"
    kept = pn.release_evidence(finals, audit_ok=True)
    assert h[0] not in kept and h[1] not in kept
    # the queue view shows the machine decision on the resolved item
    page = await rv.list_items(conn, set_id=SET, set_version="1.0.0", state="resolved", with_context=False)
    assert {i["decision"]["by"] for i in page["items"]} == {pf.LABELLER}

    again = await pf.run(pool, SET, "1.0.0", ask=True, apply=True, mode="enforce", run_id="pf-zz-3", actor=ACTOR)
    assert again["applied"]["written"] == 0 and again["candidates"] == 3       # the two settled claims left


# ── the live T9 piece path (review fix: in S275 every eligible CL-1 1.1.0 claim took it) ───────────────────────────

T9_OUTPUT = {"content_text": "Zz Velmora harbour is quiet at dawn. The Ostrin Inn serves breakfast from six.",
             "seo_title": None, "meta_description": None, "slug": None}
T9_WI = {"seed": "Zz Velmora harbour seed line.", "facts": "Zz the Ostrin Inn opens at six.", "trip": "Zz Velmora Walk",
         "destination": "Zz Velmora", "cta": "Ask Zzbrand."}
T9_PAYLOAD = {"context": {"channel": "blog", "goal": {"name": "Zz trust"}, "chosen": {"name": "Zz dawn walk"},
                          "atom_text": "Zz old seed", "keyword": "zz velmora walk"}}


async def _seed_live_t9(c) -> tuple[str, list[dict]]:
    # the migration replay lacks columns Dev has on content_piece (added outside the numbered files; test_aa1040 too)
    await c.execute("""
        ALTER TABLE acp_shared.content_piece
            ADD COLUMN IF NOT EXISTS channel text, ADD COLUMN IF NOT EXISTS seo_title text,
            ADD COLUMN IF NOT EXISTS meta_description text, ADD COLUMN IF NOT EXISTS slug text,
            ADD COLUMN IF NOT EXISTS route_hub_name text, ADD COLUMN IF NOT EXISTS writer_input jsonb,
            ADD COLUMN IF NOT EXISTS job_id uuid""")
    tid = await c.fetchval(
        "INSERT INTO shared.tenants (name, slug, posts_per_week, kind) VALUES ('Zz AA-1063 demo', "
        "'zz-aa1063-demo', 3, 'demo') RETURNING tenant_id::text")
    await c.execute("INSERT INTO shared.jev_tenant_allowlist (tenant_id, reason, added_by) VALUES ($1::uuid, "
                    "'pytest', 'pytest')", tid)
    req = await c.fetchval("INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, channel, status) "
                           "VALUES ($1::uuid, 'zz-atom', 'blog', 'approved') RETURNING request_id", tid)
    job = await c.fetchval("INSERT INTO shared.job (kind, payload, status) VALUES ('zz_aa1063_t9', $1::jsonb, "
                           "'succeeded') RETURNING id", json.dumps(T9_PAYLOAD))
    piece = await c.fetchval(
        "INSERT INTO acp_shared.content_piece (tenant_id, angle_gate_request_id, attempt_number, content_text, status, "
        "gate_ledger, repair_log, channel, writer_input, job_id) VALUES ($1::uuid, $2, 1, $3, 'approved', '[]'::jsonb, "
        "'[]'::jsonb, 'blog', $4::jsonb, $5) RETURNING piece_id::text",
        tid, req, T9_OUTPUT["content_text"], json.dumps(T9_WI), job)
    lines = pn.export_records(set_id=SET, writer="t9_blog", scope="tenant", tenant_id=tid,
                              refs={"sample_id": "zz-1063-live1", "piece_id": piece}, output=T9_OUTPUT,
                              source_text="Zz the panel's source")
    assert lines and all(e["content_sha256"] == cl.content_sha(T9_OUTPUT) for e in lines)
    raw = [{"claim_hash": e["claim_hash"], "lens": ln, "class": "fact.number", "label": lab,
            "reason": f"{ln}: invented", "labeller": "panel:zz"}
           for e in lines for ln, lab in zip(pn.LENSES, ("ok", "ok", "violation"))]
    js = pn.parse_judgements(raw, export_by_hash={e["claim_hash"]: e for e in lines})
    cons = pn.consolidate(lines, js, set_id=SET, set_version="1.1.0", policy_sha="d" * 64, audit_pct=0.0)
    await store.insert_rows(c, cons.rows, apply=True)
    return tid, lines


@pytest.mark.asyncio
async def test_prefilter_rebuilds_a_live_piece_source_and_reads_back_an_earlier_answer(conn, jev):
    """No golden item: the source is the live piece's (job payload context, writer_input laid over it), the same
    bytes the AA-729 freeze builds, so an answer stored earlier under the S275 subject key is read back, not asked."""
    from services.writing_memory.eval_judge import panel_source_text

    tid, lines = await _seed_live_t9(conn)
    await conn.execute("GRANT SELECT ON acp_shared.content_piece, shared.job, acp_shared.angle_gate_request "
                       "TO cis_app")
    await conn.execute("GRANT INSERT ON shared.decision_log TO cis_app")         # Dev: decide() writes it as cis_app
    await conn.execute("GRANT USAGE ON ALL SEQUENCES IN SCHEMA shared TO cis_app")
    # Dev has CIS 244's cis_app policy on content_piece; the replay takes it off (conftest x1_244_ci_shape)
    await conn.execute("DROP POLICY IF EXISTS content_piece_cis_app_all ON acp_shared.content_piece")
    await conn.execute("CREATE POLICY content_piece_cis_app_all ON acp_shared.content_piece AS PERMISSIVE FOR ALL "
                       "TO cis_app USING (true) WITH CHECK (true)")
    await _as_cis_app(conn)
    pool = _Pool(conn)
    anchors = [r for r in await store.read_rows(conn, SET, "1.1.0") if r["labeller_kind"] == "agent_panel"
               and r["lens"] == pn.LENSES[0]]
    found, kinds = await rv.claim_sources(pool, anchors)
    assert kinds == {"live_sha": 1} and set(found) == {e["claim_hash"] for e in lines}
    text = found[lines[0]["claim_hash"]][0]
    assert "Zz Velmora harbour seed line." in text and "Zz old seed" not in text and "Zz dawn walk" in text
    piece = (await rv.live_pieces(conn, [anchors[0]["piece_id"]], [tid]))[anchors[0]["piece_id"]]
    ctx, how = await rv.live_t9_context(pool, piece, piece["writer_input"])
    assert how == "job_payload" and text == panel_source_text("t9_blog", {"context": ctx})

    first = lines[0]
    await conn.execute(
        "INSERT INTO shared.decision_log (stage, question_key, subject_key, tenant_id, mode, zone, probability, "
        "cached) VALUES ($1, $2, $3, $4::uuid, 'enforce', 'accept', 0.97, false)",
        pf.STAGE, pf.QUESTION, pf.subject_key(first["claim_hash"], text), tid)
    plan = await pf.run(pool, SET, "1.1.0", mode="shadow")
    assert plan["sources"] == {"live_sha": 1} and "no_source" not in plan["skipped"]
    assert plan["verdicts"]["known"] == 1 and plan["verdicts"]["to_ask"] == len(lines) - 1 and jev["asked"] == []
    got = {i["claim_hash"]: (i["decision"], i["p"]) for i in plan["items"]}
    assert got[first["claim_hash"]] == ("auto_ok", 0.97)
    # another tenant's claim on the same piece id is never given this tenant's source
    other = [{**a, "tenant_id": "33333333-3333-4333-8333-333333333333"} for a in anchors]
    assert await rv.claim_sources(pool, other) == ({}, {"unmatched": 1})
