"""AA-1052 W6 — the lineage SQL (services/quality/lineage.py) on real Postgres.

One transaction, rolled back. The rows are invented (no text from the DB, audits or prompts). Triggers are off for
the transaction (session_replication_role = replica: the FKs to tenants, tours, requests and golden items are not the
point here, and the ledger rows keep the route version they are inserted with); CHECK constraints still apply.
``gold_aa_internal.tenant_tour_versions`` is created by migration 001, which the replay skips: a stand-in with the
columns lineage reads is created when it is missing.
Coverage is read before and after the inserts, so rows another test left in this database do not matter.

What it proves, per workflow (S1, T2, T9, eval):
  * OBJECT_SQL, LEDGER_SQL and COVERAGE_SQL run on the replayed schema (types, JSON operators, the S1 CASE join);
  * a stamped object comes back complete, an object written before the stamp names what it lacks;
  * the S1 writer-time stamp (``metadata.lineage``, written by the version INSERT from the writer exit's record) is
    what the S1 read finds; the ledger rows of the object (and not those of another object) give its prompt and
    route versions;
  * a LATER exit under another policy and route version (S1 approve through final_contract_store.record_s1_exit's
    append, T2 / T9 admin sentence apply through the router's own UPDATE SQL) replaces the final contract record but
    not the writer's lineage: the read still names the writer's policy_sha and route version;
  * coverage counts each field once per object.
"""
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from api.routers import admin_content_quality as acq  # noqa: E402
from services.quality import final_contract as fc  # noqa: E402
from services.quality import final_contract_store as fcs  # noqa: E402
from services.quality import lineage, writing_policy  # noqa: E402
from shared.llm_client import route_settings  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
TENANT = uuid.UUID("00000000-0000-0000-0000-0000000a1052")
POLICY = "739223d6cc24f1e5"
STAMP = {"v": lineage.LINEAGE_VERSION, "policy_sha": POLICY, "route_config_version": 77}


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        await c.execute("SET LOCAL session_replication_role = replica")
        await _schema(c)
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _schema(conn):
    if not await conn.fetchval("SELECT to_regclass('gold_aa_internal.tenant_tour_versions') IS NOT NULL AS ok"):
        await conn.execute(
            "CREATE TABLE gold_aa_internal.tenant_tour_versions (id uuid PRIMARY KEY, tenant_id uuid NOT NULL, "
            "published_tour_id uuid NOT NULL, version_number int NOT NULL, rewritten_content jsonb NOT NULL, "
            "status text NOT NULL, edit_source text NOT NULL, rewrite_language text NOT NULL, seo_mode text NOT NULL, "
            "created_at timestamptz NOT NULL DEFAULT now(), qa_status text NOT NULL, qa_repair_count smallint NOT NULL, "
            "qa_auto_passed boolean NOT NULL, job_id uuid, generation_meta jsonb, judge_breakdown jsonb, fs_id uuid)")
    for col in ("job_id uuid", "generation_meta jsonb", "judge_breakdown jsonb", "fs_id uuid"):
        await conn.execute(f"ALTER TABLE gold_aa_internal.tenant_tour_versions ADD COLUMN IF NOT EXISTS {col}")
    await conn.execute("ALTER TABLE silver_aa_internal.generated_content ADD COLUMN IF NOT EXISTS fs_id uuid")
    for col in ("job_id uuid", "writer_input jsonb"):
        await conn.execute(f"ALTER TABLE acp_shared.content_piece ADD COLUMN IF NOT EXISTS {col}")


async def _call(conn, stage, *, pv, rv, created, job=None, piece=None, tour=None, signal=None, model="zz-model"):
    await conn.execute(
        "INSERT INTO shared.llm_call_log (stage, role, model, cost_usd, quality_signal, created_at, status, job_id, "
        "content_piece_id, tour_id, prompt_version, route_config_version) "
        "VALUES ($1, 'writer', $2, 0.01, $3::jsonb, $4, 'ok', $5, $6, $7, $8, $9)",
        stage, model, json.dumps(signal or {}), created, job, piece, tour, pv, rv)


async def _coverage(conn) -> dict:
    return {wf: dict(await conn.fetchrow(lineage.COVERAGE_SQL[wf], 1)) for wf in lineage.WORKFLOWS}


async def _read(conn, wf, oid):
    row = await conn.fetchrow(lineage.OBJECT_SQL[wf], str(oid))
    assert row is not None, (wf, oid)
    ledger = await conn.fetch(lineage.LEDGER_SQL[wf], *lineage.ledger_args(wf, row)) \
        if lineage.can_read_ledger(wf, row) else []
    return lineage.assemble(wf, row, ledger)


@pytest.mark.asyncio
async def test_lineage_of_every_workflow(conn, monkeypatch):
    now = await conn.fetchval("SELECT now() AS ok")
    before = await _coverage(conn)
    fs, job_t2, job_t9, job_s1 = (uuid.uuid4() for _ in range(4))

    # ── S1: one version stamped by the real store step (job marker), one written before the stamp (no job) ──────
    monkeypatch.setattr(route_settings, "_state", ("model_first", 77))
    s1_tour, s1_old_tour = uuid.uuid4(), uuid.uuid4()
    s1 = await conn.fetchval(
        "INSERT INTO silver_aa_internal.generated_content (tour_id, tenant_id, version_num, status, retry_count, "
        "metadata, fs_id, aa_name) VALUES ($1, $2, 1, 'approved'::content_status_enum, 0, $3::jsonb, $4, "
        "'Invented Ridge Walk') RETURNING id",
        s1_tour, TENANT, json.dumps({"prompt_version": "s1abcd01", "s1_job_id": str(job_s1)}), fs)
    record = fc.stamp_stored(fc.Contract(workflow=fc.S1, exit="done", verdict=fc.PUBLISH, text_sha="x").record(),
                             "y", actual="approved")
    # the version INSERT writes the writer exit's record and, next to it, the writer-time lineage (admin_pipeline)
    await conn.execute(
        "UPDATE silver_aa_internal.generated_content SET metadata = metadata || $2::jsonb WHERE id = $1",
        s1, json.dumps({lineage.KEY: lineage.writer_lineage(record)}))
    await fcs.append_s1(conn, str(s1), record)
    s1_old = await conn.fetchval(
        "INSERT INTO silver_aa_internal.generated_content (tour_id, tenant_id, version_num, status, retry_count, "
        "metadata) VALUES ($1, $2, 1, 'hitl'::content_status_enum, 0, '{\"prompt_version\": \"s1abcd00\"}'::jsonb) "
        "RETURNING id", s1_old_tour, TENANT)
    await _call(conn, "s1_generate", pv="v1", rv=77, created=now, job=job_s1, tour=s1_tour)
    await _call(conn, "s1_generate", pv="v1", rv=70, created=now - timedelta(minutes=30), tour=s1_old_tour)
    await _call(conn, "s1_generate", pv="v1", rv=60, created=now - timedelta(hours=5), tour=s1_old_tour)  # too old
    await _call(conn, "t2_generate", pv="v9", rv=70, created=now, tour=s1_old_tour)       # not an S1 stage

    # ── T2: a stamped version with its job, a tenant edit (not counted) ─────────────────────────────────────────
    t2 = uuid.uuid4()
    await conn.execute(
        "INSERT INTO gold_aa_internal.tenant_tour_versions (id, tenant_id, published_tour_id, version_number, "
        "rewritten_content, status, edit_source, rewrite_language, seo_mode, qa_status, qa_repair_count, "
        "qa_auto_passed, job_id, generation_meta, judge_breakdown, fs_id) "
        "VALUES ($1, $2, $3, 1, '{}'::jsonb, 'ai_generated', 'ai_generated', 'en', 'off', 'passed', 0, false, $4, "
        "$5::jsonb, $6::jsonb, $7)",
        t2, TENANT, uuid.uuid4(), job_t2, json.dumps({"writer_prompt_version": "t2abcd01"}),
        json.dumps({lineage.KEY: STAMP, "final_contract": {"exit": "done", "lineage": STAMP}}), fs)
    await conn.execute(
        "INSERT INTO gold_aa_internal.tenant_tour_versions (id, tenant_id, published_tour_id, version_number, "
        "rewritten_content, status, edit_source, rewrite_language, seo_mode, qa_status, qa_repair_count, "
        "qa_auto_passed) VALUES ($1, $2, $3, 2, '{}'::jsonb, 'approved', 'tenant_edit', 'en', 'off', 'passed', 0, "
        "false)", uuid.uuid4(), TENANT, uuid.uuid4())
    await _call(conn, "t2_generate", pv="v2", rv=77, created=now, job=job_t2)
    await _call(conn, "t2_judge", pv="v1", rv=77, created=now, job=job_t2)

    # ── T9: a stamped piece; another piece of the same job must not leak into it ─────────────────────────────────
    t9, other = uuid.uuid4(), uuid.uuid4()
    stamped = {"fs_id": str(fs), "writer_prompt_version": "t9abcd01", lineage.KEY: STAMP,
               "final_contract": {"exit": "done", "lineage": STAMP}}
    for pid, wi in ((t9, stamped), (other, {})):
        await conn.execute(
            "INSERT INTO acp_shared.content_piece (piece_id, tenant_id, angle_gate_request_id, attempt_number, "
            "content_text, status, gate_ledger, repair_log, job_id, writer_input) "
            "VALUES ($1, $2, $3, 1, 'invented text', 'approved', '[]'::jsonb, '[]'::jsonb, $4, $5::jsonb)",
            pid, TENANT, uuid.uuid4(), job_t9, json.dumps(wi))
    await _call(conn, "t9_write", pv="v4", rv=77, created=now, job=job_t9, piece=t9)
    await _call(conn, "t9_repair", pv="v2", rv=77, created=now, job=job_t9, piece=other)
    await _call(conn, "t8_angle_gen", pv="v1", rv=76, created=now, job=job_t9)          # the job's own call

    # ── eval: one judged item of an r2 run with a fingerprint ────────────────────────────────────────────────────
    run = await conn.fetchval(
        "INSERT INTO shared.prompt_eval_runs (pipeline, prompt_version, tour_count, cost_usd, details, triggered_by, "
        "run_kind, writer, fingerprint) VALUES ('writing_memory', 'zz1052', 0, 0, '{}'::jsonb, 'pytest', 'r2', "
        "'t9_blog', $1::jsonb) RETURNING id",
        json.dumps({"config_fp": {"sha": "f" * 64, "parts": {"policy": POLICY}, "route_config_version": 75}}))
    item = uuid.uuid4()
    result = await conn.fetchval(
        "INSERT INTO shared.eval_item_result (run_id, item_id, writer, scope, arm, mode, phase, verdict, "
        "judge_version, cost_usd, details) VALUES ($1, $2, 't9_blog', 'platform', 'candidate', 'score_only', "
        "'tune', 'pass', 'judge-v7', 0, '{}'::jsonb) RETURNING result_id", run, item)
    await _call(conn, "writing_memory_eval", pv="v2", rv=75, created=now,
                signal={"run_id": str(run), "item_id": str(item)})
    await _call(conn, "writing_memory_eval", pv="v2", rv=75, created=now,
                signal={"run_id": str(run), "item_id": str(uuid.uuid4())}, model="zz-other-item")

    # ── reads ────────────────────────────────────────────────────────────────────────────────────────────────────
    got = await _read(conn, "s1", s1)
    assert got["complete"], got
    assert got["route_config_version"] == {"stamped": 77, "ledger": [77]}
    assert got["prompt_version"] == {"writer": "s1abcd01", "stages": {"s1_generate": ["v1"]}}
    assert got["fs_id"] == str(fs) and got["policy_sha"] == lineage.stamp()["policy_sha"]
    s1_writer_policy = got["policy_sha"]

    old = await _read(conn, "s1", s1_old)
    assert old["missing"] == ["policy_sha", "fs_id"]
    assert old["route_config_version"] == {"stamped": None, "ledger": [70]}     # tour window, S1 stages only

    got = await _read(conn, "t2", t2)
    assert got["complete"] and got["prompt_version"]["stages"] == {"t2_generate": ["v2"], "t2_judge": ["v1"]}
    assert got["policy_sha"] == POLICY and got["route_config_version"]["stamped"] == 77

    got = await _read(conn, "t9", t9)
    assert got["complete"] and set(got["prompt_version"]["stages"]) == {"t9_write", "t8_angle_gen"}
    assert got["route_config_version"]["ledger"] == [76, 77]
    assert (await _read(conn, "t9", other))["missing"] == ["policy_sha", "fs_id"]

    got = await _read(conn, "eval", result)
    assert got["complete"] and got["prompt_version"] == {"writer": "judge-v7",
                                                         "stages": {"writing_memory_eval": ["v2"]}}
    assert got["run"]["config_fp"] == "f" * 64 and got["route_config_version"] == {"stamped": 75, "ledger": [75]}
    assert [c["model"] for c in got["calls"]] == ["zz-model"]

    # ── a later exit under policy B and route version 99 keeps the writer's lineage (S1 approve, T2 / T9 apply) ──
    monkeypatch.setattr(writing_policy, "policy_sha", lambda: "policy-b-later")
    monkeypatch.setattr(route_settings, "_state", ("model_first", 99))
    approve = fc.stamp_stored(fc.Contract(workflow=fc.S1, exit="approve", verdict=fc.PUBLISH, text_sha="y")
                              .record(), "y", actual="approved")
    assert approve["lineage"] == {"v": lineage.LINEAGE_VERSION, "policy_sha": "policy-b-later",
                                  "route_config_version": None}     # a later exit stamps no route version
    await fcs.append_s1(conn, str(s1), approve)
    meta = json.loads(await conn.fetchval(
        "SELECT metadata FROM silver_aa_internal.generated_content WHERE id = $1", s1))
    assert meta["final_contract"]["exit"] == "approve"
    assert [e.get("lineage", {}).get("policy_sha") for e in meta["final_contract_log"]] == [
        s1_writer_policy, "policy-b-later"]                           # each exit's log entry keeps its own stamp
    got = await _read(conn, "s1", s1)
    assert got["policy_sha"] == s1_writer_policy and got["route_config_version"]["stamped"] == 77

    apply_t2 = fc.stamp_stored(fc.Contract(workflow=fc.T2, exit="admin_sentence_apply", verdict=fc.AI_GENERATED,
                                           text_sha="z").record(), "z")
    assert await conn.execute(acq._VERSION_UPDATE_SQL, t2, TENANT, json.dumps({"name": "Invented"}),
                              ["ai_generated"], json.dumps(apply_t2)) == "UPDATE 1"
    got = await _read(conn, "t2", t2)
    assert got["policy_sha"] == POLICY and got["route_config_version"]["stamped"] == 77

    apply_t9 = fc.stamp_stored(fc.Contract(workflow=fc.T9, exit="admin_sentence_apply", verdict=fc.APPROVED,
                                           text_sha="w").record(), "w")
    await conn.execute(acq._PIECE_UPDATE_SQL, t9, "invented edited text", json.dumps(apply_t9))
    wi = json.loads(await conn.fetchval("SELECT writer_input FROM acp_shared.content_piece WHERE piece_id = $1", t9))
    assert wi["final_contract"]["exit"] == "admin_sentence_apply"
    got = await _read(conn, "t9", t9)
    assert got["policy_sha"] == POLICY and got["route_config_version"]["stamped"] == 77

    # ── coverage: the deltas of this transaction's rows ──────────────────────────────────────────────────────────
    after = await _coverage(conn)
    delta = {wf: {k: after[wf][k] - before[wf][k] for k in after[wf]} for wf in lineage.WORKFLOWS}
    assert delta["s1"] == {"objects": 2, "fs_id": 1, "prompt_version": 2, "policy_sha": 1, "route_config_version": 1}
    assert delta["t2"] == {"objects": 1, "fs_id": 1, "prompt_version": 1, "policy_sha": 1, "route_config_version": 1}
    assert delta["t9"] == {"objects": 2, "fs_id": 1, "prompt_version": 1, "policy_sha": 1, "route_config_version": 1}
    assert delta["eval"] == {"objects": 1, "fs_id": 0, "prompt_version": 1, "policy_sha": 1,
                             "route_config_version": 1}
