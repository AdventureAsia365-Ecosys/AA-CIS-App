"""AA-1063 — CIS 394 (394_eval_claim_proposal_relabel.sql: shared.eval_claim_proposal, shared.eval_claim_relabel) and
the Writing Lab claim review (services/ground_truth/review.py) on real Postgres (conftest replays every migration).

The migration: both tables, RLS ENABLE + FORCE with the X1 parity policies, the pii:free_text tags, cis_app's grants,
the append-only trigger (UPDATE refused even with an UPDATE grant), the FK cascades and the registration. The review
as cis_app: a golden item and a 3-claim label set (a majority dispute, a split panel, an audit draw), the queue, the
proposal import (DRY RUN, apply, unchanged, not queued), the page with its context from the golden item, one answer
per kind (majority: judge_right; split: tech-lead row + unclear), accept every proposal, pull_resolutions writing
nothing afterwards, the correction chain (stale click refused), the set summary, and no access for the tenant pool.

Invented values only; everything runs in a transaction that is rolled back."""
import os
import sys
from datetime import date

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from services.ground_truth import panel as pn  # noqa: E402
from services.ground_truth import review as rv  # noqa: E402
from services.ground_truth import store  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
SET = "ZZ-AA1063"
SAMPLE = "zz-1063-a1"
ACTOR = "admin:11111111-1111-1111-1111-111111111111"
OUTPUT = {"content_text": "Arrive in Velmora and rest at the Harbour Lodge. Walk 40 km to Ostrin Valley on day "
                          "two. Breakfast is served at the lodge each morning."}
TOUR = {"name": "Velmora Walk", "itinerary": ["Day 1: Arrive in Velmora. Harbour Lodge.",
                                              "Day 2: Walk 12 km to Ostrin Valley. Breakfast at the lodge."]}
TABLES = ("shared.eval_claim_proposal", "shared.eval_claim_relabel")


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


async def _seed(c) -> list[dict]:
    """A golden A1 item and the panel rows of its first three claims, queued (as the owner)."""
    gset = await c.fetchval(
        "INSERT INTO shared.eval_golden_set (name, version, status, source_uri, source_sha256, seed, holdout_pct, "
        "tier_rules, shared_inputs, meta, label_history, item_count, imported_by) VALUES ('zz_aa1063', '1.0.0', "
        "'active', 'file:zz-aa1063.json', $1, 7, 0.300, '{}', '{}', '{}', '[]', 1, 'pytest') RETURNING set_id",
        "b" * 64)
    import json

    await c.execute(
        "INSERT INTO shared.eval_golden_item (set_id, sample_id, writer, channel, scope, tenant_id, split, trap, kind, "
        "tiers, input, control_output, post_edit, label, label_verdict, label_source, refs, input_sha256) VALUES "
        "($1, $2, 'a1', NULL, 'platform', NULL, 'tune', false, 'real', ARRAY[]::text[], $3::jsonb, $4::jsonb, false, "
        "'{\"verdict\": \"pass\"}', 'pass', 'agent_panel', '{}', $5)",
        gset, SAMPLE, json.dumps({"tour": TOUR}), json.dumps(OUTPUT), "c" * 64)
    lines = pn.export_records(set_id=SET, writer="a1", scope="platform", tenant_id=None, refs={"sample_id": SAMPLE},
                              output=OUTPUT, source_text=rv.source_text("a1", None, {"tour": TOUR}))[:3]
    votes = [["violation", "ok", "unsure"], ["violation", "violation", "ok"], ["ok", "ok", "ok"]]
    raw = [{"claim_hash": e["claim_hash"], "lens": lens, "class": "fact.number", "label": lab,
            "reason": f"{lens}: invented reason", "labeller": "panel:zz"}
           for e, vs in zip(lines, votes) for lens, lab in zip(pn.LENSES, vs)]
    js = pn.parse_judgements(raw, export_by_hash={e["claim_hash"]: e for e in lines})
    cons = pn.consolidate(lines, js, set_id=SET, set_version="1.0.0", policy_sha="d" * 64)
    await store.insert_rows(c, cons.rows, apply=True)
    rows = await store.read_rows(c, SET, "1.0.0")
    out = await store.enqueue(c, rows, today=date(2026, 11, 10), apply=True)
    assert out["added"] == 3, out
    return lines


async def _as_cis_app(c):
    await c.execute("GRANT USAGE ON SCHEMA shared, acp_shared TO cis_app")
    await c.execute("GRANT SELECT, INSERT, UPDATE ON shared.writing_memory_disagreement TO cis_app")
    await c.execute("GRANT SELECT, INSERT, DELETE ON shared.eval_claim_label TO cis_app")
    await c.execute("GRANT SELECT ON shared.eval_golden_item TO cis_app")
    # UPDATE too: the append-only trigger must refuse it even when a grant allows it (cis_owner's default privileges)
    await c.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON shared.eval_claim_proposal, shared.eval_claim_relabel "
                    "TO cis_app")
    await c.execute("GRANT SELECT, INSERT ON acp_shared.audit_log TO cis_app")
    await c.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA acp_shared TO cis_app")
    await c.execute("SET LOCAL ROLE cis_app")


# ── the migration ───────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_migration_394_shape(conn):
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '394'") == 1
    for t in TABLES:
        rel = await conn.fetchrow("SELECT c.relrowsecurity AS rls, c.relforcerowsecurity AS force FROM pg_class c "
                                  "WHERE c.oid = $1::regclass", t)
        assert rel["rls"] and rel["force"], t
        name = t.split(".")[1]
        pols = {r["policyname"]: list(r["roles"]) for r in await conn.fetch(
            "SELECT policyname, roles::text[] AS roles FROM pg_policies WHERE schemaname = 'shared' AND tablename = $1",
            name)}
        assert pols == {f"{name}_cis_app_all": ["cis_app"], f"{name}_cis_owner_all": ["cis_owner"]}, pols
        privs = {r["p"] for r in await conn.fetch(
            "SELECT privilege_type AS p FROM information_schema.role_table_grants WHERE grantee = 'cis_app' AND "
            "table_schema = 'shared' AND table_name = $1", name)}
        assert {"SELECT", "INSERT", "DELETE"} <= privs, privs
        assert not {r["p"] for r in await conn.fetch(
            "SELECT privilege_type AS p FROM information_schema.role_table_grants WHERE grantee = 'aa_app_user' AND "
            "table_schema = 'shared' AND table_name = $1", name)}
        trig = await conn.fetchval("SELECT count(*) AS n FROM pg_trigger WHERE tgrelid = $1::regclass AND "
                                   "NOT tgisinternal AND tgname LIKE '%append_only'", t)
        assert trig == 1
    tags = {r["col"]: r["c"] for r in await conn.fetch(
        "SELECT a.attrelid::regclass::text || '.' || a.attname AS col, col_description(a.attrelid, a.attnum) AS c "
        "FROM pg_attribute a WHERE a.attrelid IN ('shared.eval_claim_proposal'::regclass, "
        "'shared.eval_claim_relabel'::regclass) AND a.attnum > 0")}
    for col in ("shared.eval_claim_proposal.reason_en", "shared.eval_claim_proposal.reason_vi",
                "shared.eval_claim_proposal.bad_part", "shared.eval_claim_proposal.notes",
                "shared.eval_claim_relabel.reason"):
        assert (tags.get(col) or "").startswith("pii:free_text"), col


# ── the review as cis_app ───────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_review_proposals_answers_corrections(conn):
    lines = await _seed(conn)
    # h[0]: "Walk 40 km ..." (majority violation), h[1]: "Arrive in Velmora ..." (split panel), h[2]: audit draw
    h = [lines[1]["claim_hash"], lines[0]["claim_hash"], lines[2]["claim_hash"]]
    await _as_cis_app(conn)

    # proposals: DRY RUN, apply, unchanged, not queued
    props = [{"claim_hash": h[0], "label": "violation", "reason_en": "The source says 12 km, not 40 km.",
              "reason_vi": "Nguồn ghi 12 km.", "bad_part": "40 km", "confidence": "high",
              "notes": {"claim_vi": "Đi bộ 40 km."}},
             {"claim_hash": h[1], "label": "ok", "reason_en": "Velmora and the lodge are in the source."},
             {"claim_hash": "e" * 64, "label": "ok", "reason_en": "Not queued."}]
    dry = await rv.import_proposals(conn, run_id="panel:zz-1", proposals=props, apply=False, actor=ACTOR,
                                    set_id=SET, set_version="1.0.0")
    assert (dry["new"], dry["not_queued"], dry["inserted"]) == (2, 1, 0)
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.eval_claim_proposal") == 0
    done = await rv.import_proposals(conn, run_id="panel:zz-1", proposals=props, apply=True, actor=ACTOR,
                                     set_id=SET, set_version="1.0.0")
    assert done["inserted"] == 2 and done["labels"] == {"violation": 1, "ok": 1}
    again = await rv.import_proposals(conn, run_id="panel:zz-1", proposals=props, apply=True, actor=ACTOR,
                                      set_id=SET, set_version="1.0.0")
    assert again["inserted"] == 0 and again["unchanged"] == 2

    # the page: three items, proposals, context from the golden item
    page = await rv.list_items(conn, set_id=SET, set_version="1.0.0", state="open")
    assert page["total"] == 3
    by = {i["claim_hash"]: i for i in page["items"]}
    assert by[h[0]]["machine_verdict"] == "violation" and by[h[1]]["machine_verdict"] == "split"
    assert by[h[2]]["human_label"] == "audit" and by[h[2]]["proposal"] is None
    p0 = by[h[0]]["proposal"]
    assert p0["bad_part"] == "40 km" and p0["notes"] == {"claim_vi": "Đi bộ 40 km."} and p0["confidence"] == "high"
    assert [p["lens"] for p in by[h[0]]["panel"]] == sorted(pn.LENSES) and by[h[0]]["decision"] is None
    ctx = by[h[0]]["context"]
    assert ctx["found"] and ctx["before"].endswith("Harbour Lodge.") and ctx["after"].startswith("Breakfast")
    assert any("Ostrin Valley" in e for e in ctx["evidence"])
    assert by[h[1]]["context"]["found"] and by[h[1]]["context"]["before"] == ""
    assert sum(f["n"] for f in page["facets"] if f["set_id"] == SET) == 3
    none = await rv.list_items(conn, set_id=SET, set_version="1.0.0", proposal="none")
    assert [i["claim_hash"] for i in none["items"]] == [h[2]]
    assert (await rv.list_items(conn, set_id=SET, writer="t2"))["total"] == 0
    full = await rv.item_context(conn, by[h[0]]["item_id"])
    assert full["found"] and "Velmora Walk" in full["source_text"]

    # majority dispute answered "wrong" with its proposal: judge_right, a tech-lead violation row
    a0 = await rv.answer_item(conn, by[h[0]]["item_id"], answer="wrong", actor=ACTOR, proposal_id=p0["proposal_id"])
    assert (a0["label"], a0["resolution"], a0["inserted"]) == ("violation", "judge_right", 1)
    with pytest.raises(rv.ReviewError) as e:
        await rv.answer_item(conn, by[h[0]]["item_id"], answer="wrong", actor=ACTOR)
    assert e.value.status == 409
    # split panel answered "correct": a tech-lead ok row, resolved unclear
    a1 = await rv.answer_item(conn, by[h[1]]["item_id"], answer="correct", actor=ACTOR, note="invented note")
    assert (a1["label"], a1["resolution"], a1["inserted"]) == ("ok", "unclear", 1)
    # accept every proposal: the audit item has none, then gets one
    acc = await rv.accept_proposals(conn, [by[h[2]]["item_id"]], actor=ACTOR)
    assert acc["no_proposal"] == 1 and acc["accepted"] == 0
    await rv.import_proposals(conn, run_id="panel:zz-2", apply=True, actor=ACTOR, proposals=[
        {"item_id": by[h[2]]["item_id"], "label": "ok", "reason_vi": "Khớp nguồn."}])
    acc = await rv.accept_proposals(conn, [by[h[2]]["item_id"], by[h[0]]["item_id"]], actor=ACTOR)
    assert (acc["accepted"], acc["not_open"], acc["labels"]) == (1, 1, {"ok": 1})

    rows = await store.read_rows(conn, SET, "1.0.0")
    finals = pn.final_labels(rows)
    assert {k: (f.label, f.source) for k, f in finals.items()} == {
        h[0]: ("violation", "tech_lead"), h[1]: ("ok", "tech_lead"), h[2]: ("ok", "tech_lead")}
    lead1 = next(r for r in rows if r["claim_hash"] == h[1] and r["labeller_kind"] == "tech_lead")
    assert lead1["labeller"] == ACTOR and lead1["reason"].startswith("review: correct")
    pull = await store.pull_resolutions(conn, SET, "1.0.0", apply=True)
    assert pull["resolved_items"] == 3 and pull["inserted"] == 0
    assert (await rv.list_items(conn, set_id=SET, state="open"))["total"] == 0
    resolved = await rv.list_items(conn, set_id=SET, state="resolved")
    assert all(i["decision"] for i in resolved["items"])

    # corrections: a chain, a stale click refused, the newest wins
    r1 = await rv.relabel(conn, SET, "1.0.0", h[0], label="ok", claim_class=None, reason="Re-read: the source "
                          "says 40 km after all.", supersedes=None, actor=ACTOR)
    assert (r1["previous_label"], r1["previous_source"]) == ("violation", "tech_lead")
    with pytest.raises(rv.ReviewError) as e:
        await rv.relabel(conn, SET, "1.0.0", h[0], label="unsure", claim_class=None, reason="stale page",
                         supersedes=None, actor=ACTOR)
    assert e.value.code == "stale"
    with pytest.raises(rv.ReviewError) as e:
        await rv.relabel(conn, SET, "1.0.0", h[0], label="ok", claim_class=None, reason="no change",
                         supersedes=r1["relabel_id"], actor=ACTOR)
    assert e.value.code == "unchanged"
    r2 = await rv.relabel(conn, SET, "1.0.0", h[0], label="unsure", claim_class="fact.number",
                          reason="Not sure any more.", supersedes=r1["relabel_id"], actor=ACTOR)
    assert r2["previous_source"] == "relabel"
    relabels = await rv.read_relabels(conn, SET, "1.0.0")
    assert len(relabels) == 2
    eff = pn.final_labels(rv.effective_rows(await store.read_rows(conn, SET, "1.0.0"), relabels))
    assert eff[h[0]].label == "unsure"
    [entry] = [x for x in rv.claim_entries(await store.read_rows(conn, SET, "1.0.0"), relabels) if x["claim_hash"]
               == h[0]]
    assert [x["kind"] for x in entry["history"]] == ["tech_lead", "relabel", "relabel"]
    assert entry["head_relabel_id"] == r2["relabel_id"]
    with pytest.raises(rv.ReviewError) as e:
        await rv.relabel(conn, SET, "1.0.0", "f" * 64, label="ok", claim_class=None, reason="unknown", supersedes=None,
                         actor=ACTOR)
    assert e.value.status == 404

    # append only, even with an UPDATE grant
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match="append-only"):
        async with conn.transaction():
            await conn.execute("UPDATE shared.eval_claim_relabel SET reason = 'changed' WHERE relabel_id = $1::uuid",
                               r1["relabel_id"])
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match="append-only"):
        async with conn.transaction():
            await conn.execute("UPDATE shared.eval_claim_proposal SET label = 'ok'")

    # the set browser
    sets = {(s["set_id"], s["set_version"]): s for s in await rv.list_sets(conn)}
    s = sets[(SET, "1.0.0")]
    assert s["claims"] == 3 and s["relabels"] == 2 and s["queue"] == {"open": 0, "resolved": 3}
    assert s["evidence_sources"] == {"tech_lead": 3, "panel_agreed": 0, "none": 0}

    # audit rows: ids and labels, no text
    n = await conn.fetchval("SELECT count(*) AS n FROM acp_shared.audit_log WHERE action = ANY($1::text[])",
                            [rv.AUDIT_ANSWER, rv.AUDIT_PROPOSALS, rv.AUDIT_RELABEL])
    assert n == 3 + 2 + 2
    details = await conn.fetchval("SELECT string_agg(details::text, ' ') AS d FROM acp_shared.audit_log WHERE "
                                  "action = ANY($1::text[])", [rv.AUDIT_ANSWER, rv.AUDIT_RELABEL])
    assert "Velmora" not in details and "40 km after all" not in details


@pytest.mark.asyncio
async def test_a_correction_and_the_queue_keep_one_decision(conn):
    """A correction of an audit-drawn claim whose queue item is still open: an answer that disagrees is refused, one
    that agrees resolves the item without a new row, pull_resolutions leaves it alone, and the page, the browser and the
    next correction all read the same decision. A tech-lead row stored after a correction (an older tool) is the
    decision everywhere, previous_source included."""
    lines = await _seed(conn)
    h0, h2 = lines[1]["claim_hash"], lines[2]["claim_hash"]     # majority dispute, audit draw (panel says ok)
    await _as_cis_app(conn)
    r1 = await rv.relabel(conn, SET, "1.0.0", h2, label="violation", claim_class=None, reason="Invented: re-read.",
                          supersedes=None, actor=ACTOR)
    assert r1["previous_source"] == "panel_agreed"
    page = await rv.list_items(conn, set_id=SET, set_version="1.0.0", state="open", with_context=False)
    item = next(i for i in page["items"] if i["claim_hash"] == h2)
    assert item["decision"]["label"] == "violation" and item["decision"]["relabel"]
    assert item["decision"]["by"] == ACTOR and item["decision"]["reason"] == "Invented: re-read."
    with pytest.raises(rv.ReviewError) as e:
        await rv.answer_item(conn, item["item_id"], answer="correct", actor=ACTOR)
    assert (e.value.status, e.value.code) == (409, "relabelled")
    assert await conn.fetchval("SELECT status FROM shared.writing_memory_disagreement WHERE item_id = $1::uuid",
                               item["item_id"]) == "open"
    a = await rv.answer_item(conn, item["item_id"], answer="wrong", actor=ACTOR)
    assert (a["label"], a["inserted"], a["status"]) == ("violation", 0, "resolved")
    rows = await store.read_rows(conn, SET, "1.0.0")
    assert not [r for r in rows if r["claim_hash"] == h2 and r["labeller_kind"] == "tech_lead"]
    # a resolved item of a corrected claim: pull_resolutions writes nothing over the correction
    pull = await store.pull_resolutions(conn, SET, "1.0.0", apply=True)
    assert pull["inserted"] == 0
    relabels = await rv.read_relabels(conn, SET, "1.0.0")
    [e2] = [x for x in rv.claim_entries(await store.read_rows(conn, SET, "1.0.0"), relabels) if x["claim_hash"] == h2]
    assert (e2["label"], e2["decided_by"], e2["why"], e2["relabelled"]) == (
        "violation", ACTOR, "Invented: re-read.", True)

    # h0: a correction, then a tech-lead row stored later (written as the owner, as an older tool would)
    c0 = await rv.relabel(conn, SET, "1.0.0", h0, label="ok", claim_class=None, reason="Invented: first fix.",
                          supersedes=None, actor=ACTOR)
    await conn.execute("RESET ROLE")
    anchor = next(r for r in await store.read_rows(conn, SET, "1.0.0") if r["claim_hash"] == h0)
    cols = [c for c in store.COLUMNS if c not in ("labeller_kind", "labeller", "lens", "label", "adjudication",
                                                  "reason")]
    await conn.execute(
        f"INSERT INTO shared.eval_claim_label ({', '.join(cols)}, labeller_kind, labeller, lens, label, adjudication, "
        f"reason, created_at) SELECT {', '.join(cols)}, 'tech_lead', 'admin:older-tool', NULL, 'unsure', 'final', "
        "'invented later row', clock_timestamp() + interval '1 hour' FROM shared.eval_claim_label "
        "WHERE label_id = $1::uuid", anchor["label_id"])
    await conn.execute("SET LOCAL ROLE cis_app")
    relabels = await rv.read_relabels(conn, SET, "1.0.0")
    [e0] = [x for x in rv.claim_entries(await store.read_rows(conn, SET, "1.0.0"), relabels) if x["claim_hash"] == h0]
    assert (e0["label"], e0["decided_by"], e0["why"], e0["relabelled"]) == (
        "unsure", "admin:older-tool", "invented later row", False)
    assert e0["head_relabel_id"] == c0["relabel_id"]
    c1 = await rv.relabel(conn, SET, "1.0.0", h0, label="violation", claim_class=None, reason="Invented: second fix.",
                          supersedes=c0["relabel_id"], actor=ACTOR)
    assert (c1["previous_label"], c1["previous_source"]) == ("unsure", "tech_lead")


@pytest.mark.asyncio
async def test_cascades_and_no_tenant_pool_access(conn):
    lines = await _seed(conn)
    item = await conn.fetchval("SELECT d.item_id FROM shared.writing_memory_disagreement d JOIN "
                               "shared.eval_claim_label l ON l.label_id = d.source_ref WHERE l.set_id = $1 AND "
                               "l.claim_hash = $2", SET, lines[1]["claim_hash"])
    await conn.execute("INSERT INTO shared.eval_claim_proposal (item_id, run_id, label, reason_en, imported_by) "
                       "VALUES ($1, 'panel:zz', 'ok', 'Invented.', 'admin:pytest')", item)
    anchor = await conn.fetchval("SELECT label_id FROM shared.eval_claim_label WHERE set_id = $1 AND claim_hash = $2 "
                                 "AND lens = 'source'", SET, lines[2]["claim_hash"])
    await conn.execute(
        "INSERT INTO shared.eval_claim_relabel (set_id, set_version, claim_hash, anchor_label_id, claim_class, label, "
        "reason, labeller, previous_label, previous_class, previous_source) VALUES ($1, '1.0.0', $2, $3, "
        "'fact.number', 'violation', 'Invented.', 'admin:pytest', 'ok', 'fact.number', 'panel_agreed')",
        SET, lines[2]["claim_hash"], anchor)
    with pytest.raises(asyncpg.UniqueViolationError):   # one first correction per claim (NULLS NOT DISTINCT)
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO shared.eval_claim_relabel (set_id, set_version, claim_hash, anchor_label_id, "
                "claim_class, label, reason, labeller, previous_label, previous_class, previous_source) VALUES "
                "($1, '1.0.0', $2, $3, 'fact.number', 'ok', 'Invented.', 'admin:pytest', 'ok', 'fact.number', "
                "'panel_agreed')", SET, lines[2]["claim_hash"], anchor)
    with pytest.raises(asyncpg.CheckViolationError):
        async with conn.transaction():
            await conn.execute("INSERT INTO shared.eval_claim_proposal (item_id, run_id, label, imported_by) "
                               "VALUES ($1, 'panel:zz-3', 'ok', 'admin:pytest')", item)   # no reason
    await conn.execute("DELETE FROM shared.writing_memory_disagreement WHERE item_id = $1", item)
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.eval_claim_proposal WHERE item_id = $1", item) == 0
    await conn.execute("DELETE FROM shared.writing_memory_disagreement WHERE source_ref IN (SELECT label_id FROM "
                       "shared.eval_claim_label WHERE set_id = $1)", SET)
    await conn.execute("DELETE FROM shared.eval_claim_label WHERE label_id = $1", anchor)
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.eval_claim_relabel WHERE set_id = $1", SET) == 0
    await conn.execute("GRANT USAGE ON SCHEMA shared TO aa_app_user")
    await conn.execute("SET LOCAL ROLE aa_app_user")
    for t in TABLES:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with conn.transaction():
                await conn.fetchval(f"SELECT count(*) AS n FROM {t}")
