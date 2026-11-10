"""AA-1063 (S276 lane l8) — the source of a T2 claim in the Writing Lab review (services/ground_truth/review.py) on
real Postgres (conftest replays every migration), read as cis_app.

The S276 gap, rebuilt with invented data: a round-2 addendum T2 version of one agency (sample id "zz-t2-001", never
imported as a golden item) and a base golden item of the SAME sample id and tenant that is another version of another
tour. Its claims are queued. The review now resolves them from their own version (t2_sha): the source is the Master
as the T2 writer reads it (`_PT_SQL` + `source_tour_dict`), the context is cut from the version's object, the raw
facts come from the version's FactSheet of the same tenant; the pre-filter's plan counts the kind; another tenant's
claim on the same version id gets nothing; an A1 golden claim keeps its golden source.

Triggers are off for the transaction (session_replication_role = replica: the FKs to tenants, batches and tours are
not the point). ``gold_aa_internal.tenant_tour_versions`` comes from migration 001, which the replay skips: a
stand-in with the columns read here is created when it is missing. Invented values only; rolled back."""
import json
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone

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

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
SET, VERSION_LABEL = "t2@99", "1.0.0"
SAMPLE = "zz-t2-001"
PLATFORM = "00000000-0000-0000-0000-000000000001"
AGENCY = "00000000-0000-0000-0000-00000a106301"
OTHER = "00000000-0000-0000-0000-00000a106302"
T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
T2_OUT = {"name": "Zz Quellmoor Lakes by Bike", "subtitle": "Zz two days around Quellmoor",
          "summary": "Ride from Varnhold to the Quellmoor shore. Stay one night at the Tessaly Lodge.",
          "highlights": ["Zz ride the Quellmoor shore path", "Zz a night at the Tessaly Lodge"],
          "itineraries": "Day 1 — Varnhold to Quellmoor. Day 2 — Quellmoor to Varnhold.",
          "seo_title": "Zz Quellmoor bike tour", "seo_meta": "Zz two days by bike", "trip_type": "cycling"}
FACTS = "Zz FACTS: 2 days; from Varnhold; Quellmoor shore; Tessaly Lodge."


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        await c.execute("SET LOCAL session_replication_role = replica")
        yield c
    finally:
        await tr.rollback()
        await c.close()


class _Pool:
    """One connection as a pool (the test's transaction holds every write)."""

    def __init__(self, c):
        self.c = c

    def acquire(self):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def one():
            yield self.c

        return one()


async def _schema(c):
    if not await c.fetchval("SELECT to_regclass('gold_aa_internal.tenant_tour_versions') IS NOT NULL AS ok"):
        await c.execute(
            "CREATE TABLE gold_aa_internal.tenant_tour_versions (id uuid PRIMARY KEY, tenant_id uuid NOT NULL, "
            "published_tour_id uuid NOT NULL, version_number int NOT NULL, rewritten_content jsonb NOT NULL, "
            "status text NOT NULL, edit_source text NOT NULL, rewrite_language text NOT NULL, seo_mode text NOT NULL, "
            "created_at timestamptz NOT NULL DEFAULT now(), qa_status text NOT NULL, "
            "qa_repair_count smallint NOT NULL, qa_auto_passed boolean NOT NULL, job_id uuid, generation_meta jsonb, "
            "judge_breakdown jsonb, fs_id uuid)")
    await c.execute("ALTER TABLE gold_aa_internal.tenant_tour_versions ADD COLUMN IF NOT EXISTS fs_id uuid")


async def _tour(c, name: str) -> tuple[str, str]:
    """A raw tour, its Master version and its published Master; (tour_id, published_tour_id)."""
    tour = str(uuid.uuid4())
    await c.execute(
        "INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, src_summary, "
        "src_itineraries, country, duration, inclusions, source_status, source_group_id, source_version) "
        "VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, 'Vietnam', '2 days', 'Zz bike hire', 'active', $7, 1)",
        tour, PLATFORM, uuid.uuid4(), f"{name} (raw)", "Zz raw: Varnhold to the Quellmoor shore.",
        "Day 1: Varnhold to Quellmoor. Day 2: back to Varnhold.", uuid.uuid4())
    gc = await c.fetchval(
        "INSERT INTO silver_aa_internal.generated_content (tour_id, tenant_id, version_num, status, retry_count, "
        "metadata, aa_name, created_at) VALUES ($1::uuid, $2::uuid, 1, 'approved'::content_status_enum, 0, "
        "'{}'::jsonb, $3, $4) RETURNING id", tour, PLATFORM, name, T0 - timedelta(days=5))
    pt = await c.fetchval(
        "INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, tenant_id, aa_name, aa_subtitle, "
        "aa_summary, aa_description, aa_highlights, aa_itineraries, seo_title, seo_meta, master_status) VALUES "
        "($1::uuid, $2, $3::uuid, $4, 'Zz lakes by bike', 'Ride from Varnhold to the Quellmoor shore.', "
        "'A night at the Tessaly Lodge on the lake.', '[\"Zz shore path\"]'::jsonb, "
        "'Day 1 — Varnhold to Quellmoor', 'Zz ride', 'Zz meta', 'active') RETURNING id::text",
        tour, gc, PLATFORM, name)
    return tour, pt


async def _seed(c) -> dict:
    await _schema(c)
    await c.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week, kind) VALUES ($1::uuid, "
                    "'Zz AA-1063 T2 demo', 'zz-aa1063-t2-demo', 3, 'demo')", AGENCY)
    await c.execute("INSERT INTO shared.jev_tenant_allowlist (tenant_id, reason, added_by) VALUES ($1::uuid, "
                    "'pytest', 'pytest')", AGENCY)
    tour, pt = await _tour(c, "Zz Quellmoor Lakes Ride")
    _other_tour, other_pt = await _tour(c, "Zz Ostrava Hills Walk")
    fs = await c.fetchval(
        "INSERT INTO acp_shared.fact_sheet (tenant_id, workflow, builder, schema_version, builder_version, fs_sha, "
        "tour_id, line_count, lines, render_text) VALUES ($1::uuid, 't2', 'tour', 'factsheet/1', '1', $2, $3::uuid, "
        "0, '[]'::jsonb, $4) RETURNING fs_id::text", AGENCY, "a" * 64, tour, FACTS)
    version, base_version = str(uuid.uuid4()), str(uuid.uuid4())
    for vid, ptid, out, fs_id in ((version, pt, T2_OUT, fs), (base_version, other_pt, {"name": "Zz Ostrava"}, None)):
        await c.execute(
            "INSERT INTO gold_aa_internal.tenant_tour_versions (id, tenant_id, published_tour_id, version_number, "
            "rewritten_content, status, edit_source, rewrite_language, seo_mode, created_at, qa_status, "
            "qa_repair_count, qa_auto_passed, fs_id) VALUES ($1::uuid, $2::uuid, $3::uuid, 1, $4::jsonb, 'approved', "
            "'ai_generated', 'EN-US', 'off', $5, 'passed', 0, true, $6::uuid)",
            vid, AGENCY, ptid, json.dumps({**out, "flags": []}), T0, fs_id)
    # the base fixture's item of the same sample id: the other version (another tour), same tenant
    gset = await c.fetchval(
        "INSERT INTO shared.eval_golden_set (name, version, status, source_uri, source_sha256, seed, holdout_pct, "
        "tier_rules, shared_inputs, meta, label_history, item_count, imported_by) VALUES ('zz_aa1063_t2', '1.3.0', "
        "'active', 'file:zz-aa1063-t2.json', $1, 7, 0.300, '{}', '{}', '{}', '[]', 1, 'pytest') RETURNING set_id",
        "b" * 64)
    await c.execute(
        "INSERT INTO shared.eval_golden_item (set_id, sample_id, writer, channel, scope, tenant_id, split, trap, kind, "
        "tiers, input, control_output, post_edit, label, label_verdict, label_source, refs, input_sha256) VALUES "
        "($1, $2, 't2', NULL, 'tenant', $3::uuid, 'tune', false, 'real', ARRAY[]::text[], $4::jsonb, $5::jsonb, "
        "false, '{\"verdict\": \"pass\"}', 'pass', 'agent_panel', $6::jsonb, $7)",
        gset, SAMPLE, AGENCY, json.dumps({"tour": {"name": "Zz Ostrava Hills Walk", "summary": "Zz another tour"}}),
        json.dumps({"name": "Zz Ostrava"}), json.dumps({"version_id": base_version, "tenant_id": AGENCY}), "c" * 64)
    from services.jobs.t2_rewrite_job import _PT_SQL, source_tour_dict

    panel_src = rv.source_text("t2", None, {"tour": dict(source_tour_dict(await c.fetchrow(_PT_SQL, pt)))})
    lines = pn.export_records(set_id=SET, writer="t2", scope="tenant", tenant_id=AGENCY,
                              refs={"sample_id": SAMPLE, "version_id": version, "tour_id": tour},
                              output=T2_OUT, source_text=panel_src)
    raw = [{"claim_hash": e["claim_hash"], "lens": ln, "class": "fact.number", "label": lab,
            "reason": f"{ln}: invented reason", "labeller": "panel:zz"}
           for e in lines for ln, lab in zip(pn.LENSES, ("ok", "ok", "violation"))]
    js = pn.parse_judgements(raw, export_by_hash={e["claim_hash"]: e for e in lines})
    cons = pn.consolidate(lines, js, set_id=SET, set_version=VERSION_LABEL, policy_sha="d" * 64, audit_pct=0.0)
    await store.insert_rows(c, cons.rows, apply=True)
    out = await store.enqueue(c, await store.read_rows(c, SET, VERSION_LABEL), today=date(2026, 11, 10), apply=True)
    assert out["added"] == len(lines), out
    return {"lines": lines, "version": version, "pt": pt, "panel_src": panel_src}


async def _as_cis_app(c):
    tables = ("gold_aa_internal.tenant_tour_versions", "gold_aa_internal.published_tours",
              "silver_aa_internal.generated_content", "silver_aa_internal.raw_tours", "acp_shared.fact_sheet")
    await c.execute("GRANT USAGE ON SCHEMA shared, acp_shared, gold_aa_internal, silver_aa_internal TO cis_app")
    await c.execute("GRANT SELECT, INSERT, UPDATE ON shared.writing_memory_disagreement TO cis_app")
    await c.execute("GRANT SELECT ON shared.eval_claim_label, shared.eval_golden_item, shared.eval_claim_proposal, "
                    "shared.eval_claim_relabel, shared.decision_log, shared.tenants, shared.jev_tenant_allowlist, "
                    "acp_shared.content_piece TO cis_app")
    await c.execute(f"GRANT SELECT ON {', '.join(tables)} TO cis_app")
    # Dev has CIS 244's cis_app policies on these tables; the replay takes them off (conftest x1_244_ci_shape)
    for t in tables:
        schema, name = t.split(".")
        rls = await c.fetchval("SELECT relrowsecurity AS ok FROM pg_class WHERE oid = $1::regclass", t)
        has = await c.fetchval("SELECT count(*) AS n FROM pg_policies WHERE schemaname = $1 AND tablename = $2 AND "
                               "'cis_app' = ANY(roles)", schema, name)
        if rls and not has:
            await c.execute(f"CREATE POLICY zz_aa1063_cis_app ON {t} AS PERMISSIVE FOR SELECT TO cis_app USING (true)")
    await c.execute("SET LOCAL ROLE cis_app")


@pytest.mark.asyncio
async def test_a_t2_addendum_claim_resolves_its_version_and_master(conn):
    seed = await _seed(conn)
    lines, version = seed["lines"], seed["version"]
    await _as_cis_app(conn)
    pool = _Pool(conn)

    page = await rv.list_items(conn, set_id=SET, set_version=VERSION_LABEL, state="open", pool=pool)
    assert page["total"] == len(lines)
    by = {i["claim_hash"]: i for i in page["items"]}
    assert all(i["context"]["found"] for i in page["items"])
    summary = next(e for e in lines if e["field"] == "summary")
    item = by[summary["claim_hash"]]
    assert item["context"]["before"] == summary["context"]["before"]
    assert item["context"]["after"] == summary["context"]["after"]

    full = await rv.item_context(conn, item["item_id"], pool=pool)
    assert full["found"] and full["source_kind"] == "t2_sha"
    assert full["source_text"] == seed["panel_src"] == summary["source_text"]     # the bytes the panel read
    assert "Zz another tour" not in full["source_text"]                           # not the colliding golden item
    assert full["evidence"] == summary["evidence"]
    assert full["master_changed"] is False                                        # Master written before the version
    assert (full["raw_facts"], full["raw_facts_kind"]) == (FACTS, "stored")

    # the pre-filter's plan (no Jev call) counts the kind instead of unmatched / a wrong golden_id
    plan = await pf.run(pool, SET, VERSION_LABEL, mode="shadow", writers=["t2"])
    assert plan["sources"] == {"t2_sha": 1} and "no_source" not in plan["skipped"]

    # another tenant's claim on the same version id gets nothing
    anchors = [r for r in await store.read_rows(conn, SET, VERSION_LABEL) if r["labeller_kind"] == "agent_panel"]
    assert {a["version_id"] for a in anchors} == {version}
    other = [{**a, "tenant_id": OTHER} for a in anchors]
    assert await rv.claim_sources(pool, other) == ({}, {"unmatched": 1})


@pytest.mark.asyncio
async def test_an_edited_version_and_a_regenerated_master_are_named(conn):
    seed = await _seed(conn)
    await conn.execute("UPDATE gold_aa_internal.tenant_tour_versions SET rewritten_content = $1::jsonb, fs_id = NULL "
                       "WHERE id = $2::uuid", json.dumps({**T2_OUT, "summary": "Zz edited by the agency."}),
                       seed["version"])
    await conn.execute("UPDATE silver_aa_internal.generated_content gc SET edited_at = $1 FROM "
                       "gold_aa_internal.published_tours pt WHERE pt.generated_content_id = gc.id AND pt.id = $2::uuid",
                       T0 + timedelta(days=1), seed["pt"])
    await _as_cis_app(conn)
    page = await rv.list_items(conn, set_id=SET, set_version=VERSION_LABEL, state="open", pool=_Pool(conn))
    full = await rv.item_context(conn, page["items"][0]["item_id"])               # no pool: the caller's connection
    assert full["found"] and full["source_kind"] == "t2_id" and full["master_changed"] is True
    assert full["raw_facts_kind"] == "rebuilt" and "Varnhold" in full["raw_facts"]
    assert cl.content_sha(T2_OUT) == page["items"][0]["content_sha256"]
