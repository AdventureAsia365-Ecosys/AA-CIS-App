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
  * the S1 stamp written by the store step (final_contract.stamp_stored + final_contract_store.append_s1) is what
    the S1 read finds; the ledger rows of the object (and not those of another object) give its prompt and route
    versions;
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

from services.quality import final_contract as fc  # noqa: E402
from services.quality import final_contract_store as fcs  # noqa: E402
from services.quality import lineage  # noqa: E402
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
    record = fc.Contract(workflow=fc.S1, exit="done", verdict=fc.PUBLISH, text_sha="x").record()
    await fcs.append_s1(conn, str(s1), fc.stamp_stored(record, "y", actual="approved"))
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
        json.dumps({"final_contract": {"lineage": STAMP}}), fs)
    await conn.execute(
        "INSERT INTO gold_aa_internal.tenant_tour_versions (id, tenant_id, published_tour_id, version_number, "
        "rewritten_content, status, edit_source, rewrite_language, seo_mode, qa_status, qa_repair_count, "
        "qa_auto_passed) VALUES ($1, $2, $3, 2, '{}'::jsonb, 'approved', 'tenant_edit', 'en', 'off', 'passed', 0, "
        "false)", uuid.uuid4(), TENANT, uuid.uuid4())
    await _call(conn, "t2_generate", pv="v2", rv=77, created=now, job=job_t2)
    await _call(conn, "t2_judge", pv="v1", rv=77, created=now, job=job_t2)

    # ── T9: a stamped piece; another piece of the same job must not leak into it ─────────────────────────────────
    t9, other = uuid.uuid4(), uuid.uuid4()
    stamped = {"fs_id": str(fs), "writer_prompt_version": "t9abcd01", "final_contract": {"lineage": STAMP}}
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

    # ── coverage: the deltas of this transaction's rows ──────────────────────────────────────────────────────────
    after = await _coverage(conn)
    delta = {wf: {k: after[wf][k] - before[wf][k] for k in after[wf]} for wf in lineage.WORKFLOWS}
    assert delta["s1"] == {"objects": 2, "fs_id": 1, "prompt_version": 2, "policy_sha": 1, "route_config_version": 1}
    assert delta["t2"] == {"objects": 1, "fs_id": 1, "prompt_version": 1, "policy_sha": 1, "route_config_version": 1}
    assert delta["t9"] == {"objects": 2, "fs_id": 1, "prompt_version": 1, "policy_sha": 1, "route_config_version": 1}
    assert delta["eval"] == {"objects": 1, "fs_id": 0, "prompt_version": 1, "policy_sha": 1,
                             "route_config_version": 1}
