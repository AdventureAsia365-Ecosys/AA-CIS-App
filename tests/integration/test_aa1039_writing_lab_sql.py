"""AA-1039 R6 slice 3 — services/writing_memory/lab.py's SQL on real Postgres (every migration applied by conftest; one
transaction, rolled back). The unit tests use a fake connection, so they cannot see a column that does not exist, a
type error or a GROUP BY mistake. Here the stage read runs against real rows of the learning tables:

  * samples / labels count only the asked writer's items of ACTIVE golden sets, per split, kind, verdict and source;
  * the writing signals are counted per fact status / reason / action, with the 30-day window;
  * A1's experiments include the legacy S1 regression runs (writer NULL); T2's do not;
  * the model shadow reads only the writer's prompt stages, inside 30 days;
  * the prompt versions come from the register (CIS 325 seeds it), filtered to the writer's stages;
  * the gate section reads without error and names a state;
  * the gate state reads the ACTIVE release's newest evaluation: a newer failed r1 of a candidate release leaves it
    passing (shown apart as the newest evaluation of any release), a failed reverify of the active release is a
    regression.
Invented names and values only.
"""
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from services.writing_memory import lab  # noqa: E402
from services.writing_memory import releases as rel  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
TAG = f"zz1039-{uuid.uuid4().hex[:6]}"
ADMIN = "11111111-1111-1111-1111-111111111111"


class _OneConnPool:
    """A pool shim over the test's single connection, so every section sees the rows of the open transaction."""

    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        @asynccontextmanager
        async def _cm():
            yield self.conn
        return _cm()


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


async def _golden(conn, *, status="active"):
    set_id = await conn.fetchval(
        "INSERT INTO shared.eval_golden_set (name, version, status, source_uri, source_sha256, seed, holdout_pct, "
        "tier_rules, shared_inputs, meta, label_history, item_count, imported_by, superseded_at) "
        "VALUES ($1, '1.0.0', $2, 'file:zz-lab.json', $3, 7, 0.300, '{}', '{}', '{}', '[]', 0, 'pytest', "
        "CASE WHEN $2 = 'superseded' THEN now() END) RETURNING set_id",
        f"zzlab_{uuid.uuid4().hex[:8]}", status, "b" * 64)
    return set_id


async def _item(conn, set_id, n, *, writer="a1", split="tune", kind="real", verdict="pass", source="agent_panel"):
    trap = split == "trap"
    scope, tenant = ("platform", None) if writer == "a1" else ("tenant", str(uuid.uuid4()))
    channel = None if writer in ("a1", "t2") else ("blog" if writer == "t9_blog" else "facebook")
    await conn.execute(
        "INSERT INTO shared.eval_golden_item (set_id, sample_id, writer, channel, scope, tenant_id, split, trap, kind, "
        "tiers, country, days, input, post_edit, label, label_verdict, label_source, injection_spec, refs, "
        "input_sha256, pick_reason) "
        "VALUES ($1, $2, $3, $4, $5, $6::uuid, $7, $8, $9, ARRAY['water'], 'Zedland', 5, '{\"t\": \"x\"}', false, "
        "'{\"v\": 1}', $10, $11, CASE WHEN $8 THEN '{\"k\": 1}'::jsonb END, '{}', $12, 'pytest')",
        set_id, f"{TAG}-{writer.replace('_', '')}-{n}", writer, channel, scope, tenant, split, trap, kind, verdict,
        source, f"{n:064x}")


async def _signal(conn, *, writer="a1", fact="pass", reason="ai_phrasing", action="field_edit", days_ago=0):
    await conn.execute(
        "INSERT INTO shared.writing_signal (writer, scope, action, actor_type, fact_status, reason_code, source_table, "
        "source_id, created_at) VALUES ($1, 'platform', $2, 'admin', $3, $4, 'pytest', $5, "
        "now() - make_interval(days => $6))",
        writer, action, fact, reason, f"{TAG}-{uuid.uuid4().hex[:6]}", days_ago)


async def _run(conn, *, pipeline="writing_memory", writer="a1", kind="r2", verdict="pass", details=None,
               release=None, golden=None):
    return await conn.fetchval(
        "INSERT INTO shared.prompt_eval_runs (pipeline, prompt_version, tour_count, cost_usd, details, triggered_by, "
        "run_kind, writer, finished_at, stop_reason, verdict, release_id, golden_set_id) "
        "VALUES ($1, $2, 3, 0.25, $6::jsonb, 'pytest', $3, $4, clock_timestamp(), 'completed', $5, $7::uuid, $8) "
        "RETURNING id::text",
        pipeline, TAG, kind, writer, verdict, json.dumps(details or {}), release, golden)


async def _live_release0(conn, writer):
    """Release 0 of the writer, activated the way the release machinery does it (its evidence: an r0 replay with no
    byte diff and the baseline import), with the writer's memory switched on. Returns (release, baseline run id)."""
    golden = await _golden(conn)
    r0 = await _run(conn, writer=writer, kind="r0_replay", details={"inputs": 20, "byte_diffs": 0})
    base = await _run(conn, writer=writer, kind="baseline_import", verdict="baseline", golden=golden)
    d = await rel.create_draft(conn, writer=writer, created_by=f"admin:{ADMIN}")
    await rel.attach_evidence(conn, d["release_id"], r0_run_id=r0, eval_run_id=base, actor=f"admin:{ADMIN}")
    await rel.approve(conn, d["release_id"], approved_by=ADMIN, approval_ref="pytest")
    live = await rel.activate(conn, d["release_id"], actor=f"admin:{ADMIN}")
    await rel.set_memory_enabled(conn, writer, True, actor=f"admin:{ADMIN}")
    return live, base


async def _view(conn, writer):
    return await lab.stage_view(_OneConnPool(conn), writer)


@pytest.mark.asyncio
async def test_samples_and_labels_count_only_the_writers_active_items(conn):
    active, old = await _golden(conn), await _golden(conn, status="superseded")
    await _item(conn, active, 1, split="tune")
    await _item(conn, active, 2, split="holdout", verdict="fail", source="human_label")
    await _item(conn, active, 3, split="trap", kind="synthetic", verdict="fail", source="synthetic")
    await _item(conn, old, 4)                                   # superseded set: not counted
    await _item(conn, active, 5, writer="t2")                   # another writer: not counted
    before = await _view(conn, "a1")
    assert before["sections"]["samples"] == {"ready": True, "error": None}
    mine = [i for i in before["samples"]["items"] if i["sample_id"].startswith(TAG)]
    assert sorted(i["sample_id"] for i in mine) == [f"{TAG}-a1-1", f"{TAG}-a1-2", f"{TAG}-a1-3"]
    assert {"input", "label", "control_output"}.isdisjoint(mine[0])
    sets = {s["set_id"]: s for s in before["samples"]["golden_sets"]}
    assert sets[str(active)]["items"] == 3 and str(old) not in sets
    # counts include rows other tests may have left in active sets; ours add exactly these
    base = await conn.fetchval("SELECT count(*) FROM shared.eval_golden_item i JOIN shared.eval_golden_set s "
                               "ON s.set_id = i.set_id AND s.status = 'active' WHERE i.writer = 'a1'")
    assert before["samples"]["total"] == base
    assert before["samples"]["by_split"].get("trap", 0) >= 1 and before["samples"]["by_kind"].get("synthetic", 0) >= 1
    assert before["labels"]["golden"]["by_source"].get("human_label", 0) >= 1


@pytest.mark.asyncio
async def test_signals_are_counted_with_the_30_day_window(conn):
    first = (await _view(conn, "a1"))["labels"]["signals"]
    await _signal(conn, fact="grey", reason="voice_off_brand")
    await _signal(conn, fact="pass", reason="voice_off_brand", days_ago=45)
    await _signal(conn, writer="t9_blog")                       # another writer
    after = (await _view(conn, "a1"))["labels"]["signals"]
    assert after["total"] == first["total"] + 2
    assert after["last_30d"] == first["last_30d"] + 1
    assert after["by_reason_code"]["voice_off_brand"] == first["by_reason_code"].get("voice_off_brand", 0) + 2
    assert after["by_fact_status"]["grey"] == first["by_fact_status"].get("grey", 0) + 1


@pytest.mark.asyncio
async def test_experiments_read_the_writers_runs_and_a1_legacy_s1_runs(conn):
    wm = await _run(conn)
    legacy = await conn.fetchval(
        "INSERT INTO shared.prompt_eval_runs (pipeline, prompt_version, tour_count, triggered_by, run_kind) "
        "VALUES ('s1_old', $1, 4, 'pytest', 'regression') RETURNING id::text", TAG)
    other = await _run(conn, writer="t2", verdict="fail")
    await conn.execute(
        "INSERT INTO shared.llm_shadow_log (stage, primary_model, shadow_model, shadow_error, primary_cost_usd, "
        "shadow_cost_usd, shadow_latency_ms, created_at) VALUES "
        "('s1_generate', 'zz-live', 'zz-shadow', NULL, 0.01, 0.02, 800, now()), "
        "('s1_generate', 'zz-live', 'zz-shadow', 'timeout', 0.01, 0, 1200, now()), "
        "('s1_generate', 'zz-live', 'zz-shadow', NULL, 0.01, 0.02, 900, now() - interval '40 days'), "
        "('t2_generate', 'zz-live', 'zz-shadow', NULL, 0.01, 0.02, 700, now())")
    a1 = await _view(conn, "a1")
    assert a1["sections"]["experiments"] == {"ready": True, "error": None}
    ids = {r["id"] for r in a1["experiments"]["eval_runs"]}
    assert {wm, legacy} <= ids and other not in ids
    shadow = [r for r in a1["experiments"]["model_shadow"] if r["primary_model"] == "zz-live"]
    assert len(shadow) == 1 and shadow[0]["stage"] == "s1_generate"
    assert shadow[0]["calls"] == 2 and shadow[0]["errors"] == 1 and shadow[0]["shadow_p50_ms"] == 1000
    t2 = await _view(conn, "t2")
    t2_ids = {r["id"] for r in t2["experiments"]["eval_runs"]}
    assert other in t2_ids and legacy not in t2_ids and wm not in t2_ids
    assert [r["stage"] for r in t2["experiments"]["model_shadow"] if r["primary_model"] == "zz-live"] == ["t2_generate"]


@pytest.mark.asyncio
async def test_prompts_and_gate_read_the_live_tables(conn):
    run = await _run(conn, writer="t9_social", kind="r1", verdict="fail")
    # Own register row: the CIS 325 seed is gone when another test of the shard rebuilt shared.prompt_version (AA-1017).
    await conn.execute(
        "INSERT INTO shared.prompt_version (stage, version, prompt_sha256, source_path, created_by) "
        "VALUES ('t9_write', 'zz-lab-1', $1, 'prompts/zz_lab.txt', 'pytest') ON CONFLICT DO NOTHING",
        "c" * 64)
    v = await _view(conn, "t9_social")
    assert all(v["sections"][s]["ready"] for s in lab.SECTIONS), v["sections"]
    stages = {p["stage"] for p in v["prompts"]["versions"]}
    assert stages and stages <= set(lab.PROMPT_STAGES["t9_social"])
    assert "t9_write" in stages and all(len(p["sha_short"]) == 12 for p in v["prompts"]["versions"])
    g = v["gate"]
    assert g["last_eval"]["id"] == run and g["last_eval"]["verdict"] == "fail"
    assert g["state"]["code"] in ("no_release", "memory_off", "reverify", "regression")
    if g["active_release"] is None:
        assert g["state"]["code"] == "no_release"


@pytest.mark.asyncio
async def test_the_gate_reads_the_active_releases_evaluation_not_a_newer_candidates(conn):
    await conn.execute("UPDATE shared.writing_memory_writer SET memory_enabled = false WHERE writer = 't2'")
    await conn.execute("DELETE FROM shared.writing_memory_release WHERE writer = 't2'")
    live, base = await _live_release0(conn, "t2")
    # a newer candidate release fails its r1 (the gate rejecting it), and an r2 code-train run of no release fails
    cand = await rel.create_draft(conn, writer="t2", created_by=f"admin:{ADMIN}")
    failed_r1 = await _run(conn, writer="t2", kind="r1", verdict="fail", release=cand["release_id"])
    g = (await _view(conn, "t2"))["gate"]
    assert g["active_release"]["release_id"] == live["release_id"]
    assert g["active_eval"]["id"] == base and g["active_eval"]["run_kind"] == "baseline_import"
    assert g["last_eval"]["id"] == failed_r1 and g["last_eval"]["for_active"] is False
    assert g["state"] == {"code": "passing", "ok": True}
    # a seeded-fault r2 on the live release is an operator check of the stop, never its evaluation
    await _run(conn, writer="t2", kind="r2", verdict="fail", release=live["release_id"],
               details={"seeded_fault": "zz_fault", "candidate": "release"})
    assert (await _view(conn, "t2"))["gate"]["state"]["code"] == "passing"
    # the live release itself failing its reverify run is a regression
    bad = await _run(conn, writer="t2", kind="reverify", verdict="fail", release=live["release_id"])
    g = (await _view(conn, "t2"))["gate"]
    assert g["active_eval"]["id"] == bad and g["last_eval"]["for_active"] is True
    assert g["state"]["code"] == "regression"
