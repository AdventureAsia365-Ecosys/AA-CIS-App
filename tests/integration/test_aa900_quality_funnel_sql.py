"""AA-900 (S271 W0) — the quality funnel SQL (services/quality/funnel.py) on real Postgres, and the frozen baseline.

One transaction, rolled back. The rows are invented (no text from the DB, audits or prompts) and dated in 2031, so no
row another test leaves in this database falls in the window. FK triggers are off for the transaction
(session_replication_role = replica: the tenants, tours and angle-gate requests the rows point at are not the point
here); CHECK constraints still apply. ``gold_aa_internal.tenant_tour_versions`` is created by migration 001, which the
replay skips: a stand-in with the columns the funnel reads is created inside the transaction when it is missing; so is
``acp_shared.content_piece.channel`` (its migration also adds a pgvector column the CI Postgres does not have).

What it proves:
  * every query of compute() runs (types, JSON operators, the f-string braces) on the replayed schema;
  * T9 counts every failing gate of every attempt (discarded attempts too) and splits F9 into the judge outage,
    TEXT_INTEGRITY and the claims; the first-gate count stays the held_reason prefix;
  * delivered / held / first pass / attempts / USD per delivered for T9 (per channel), S1 and T2; an S1 draft
    repaired by the fix pass or approved by a reviewer is not first pass, and a draft superseded by a later rewrite
    stays delivered in its week; the T9 headline cost is the whole T9 stage cost (attributed + unattributed);
  * the watch window raises the cut, fallback and drift findings at the documented thresholds; AI gateway route-test
    rows (quality_signal.route_test) count nowhere; a stage's llm_route_policy fallback_alert_pct is honoured;
  * a CIS-08 run stored with store.finish_ok is what GET /admin/quality/funnel's SQL reads back (scheduled runs only;
    a manual run is listed apart), and a finding the previous scheduled run already had is stored as a repeat.

The baseline (tests/integration/snapshots/aa900_quality_funnel_baseline.json) is this dataset's snapshot, frozen: a
change of any funnel definition shows up as a diff of that file (regenerate with AA900_WRITE_BASELINE=1 and review
the diff). The live baseline of Dev is the first scheduled CIS-08 run (captured by the orchestrator).
"""
import json
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from services.quality import funnel  # noqa: E402
from services.reports import funnel_report, store  # noqa: E402
from services.reports.funnel_report import HISTORY_SQL, LATEST_MANUAL_SQL, LATEST_SQL  # noqa: E402
from services.reports.registry import load  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
UTC = timezone.utc
DAY = date(2031, 3, 5)                                   # a Wednesday; its ISO week starts 2031-03-03
NOW = datetime(2031, 3, 6, 1, 0, tzinfo=UTC)
W = datetime(2031, 3, 4, 3, 0, tzinfo=UTC)               # inside the day's week and the watch window
P = datetime(2031, 2, 25, 3, 0, tzinfo=UTC)              # the week before, still inside the watch window
PRIOR = datetime(2031, 2, 21, 3, 0, tzinfo=UTC)          # the 7 days before the watch window
TENANT = uuid.UUID("00000000-0000-0000-0000-0000000a9000")
BASELINE = Path(__file__).resolve().parent / "snapshots" / "aa900_quality_funnel_baseline.json"

PASS = {"gate": "F1_grounding", "passed": True, "violations": [], "blocking": True}
F9_CLAIM = {"gate": "F9_cta_fact", "passed": False, "blocking": True,
            "violations": ["FACT_CHECK: 'a sunset cruise on the invented lake' - not in the source"]}
F9_TEXT = {"gate": "F9_cta_fact", "passed": False, "blocking": True,
           "violations": ["TEXT_INTEGRITY: 'The trail winds to the.'",
                          "FACT_CHECK: 'a private chef at the invented lodge' - not in the source"]}
F9_DOWN = {"gate": "F9_cta_fact", "passed": False, "blocking": False, "repairable": False,
           "violations": ["judge unavailable: reply cut - not checked"], "degraded": "f9_judge_unavailable"}
F4 = {"gate": "F4_seo_surface", "passed": False, "blocking": True, "violations": ["meta length 171"]}
F16 = {"gate": "F16_faq", "passed": False, "blocking": True, "violations": ["only 1 FAQ pair"]}


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


async def _piece(conn, *, channel, status, attempt, created, ledger, discarded=(), held_reason=None, flags=None):
    pid, req = uuid.uuid4(), uuid.uuid4()
    await conn.execute(
        "INSERT INTO acp_shared.content_piece (piece_id, tenant_id, angle_gate_request_id, attempt_number, "
        "content_text, status, held_reason, gate_ledger, repair_log, created_at, channel, flags, discarded_attempts) "
        "VALUES ($1, $2, $3, $4, 'invented text', $5, $6, $7::jsonb, '[]'::jsonb, $8, $9, $10::jsonb, $11::jsonb)",
        pid, TENANT, req, attempt, status, held_reason, json.dumps(ledger), created, channel,
        json.dumps(flags) if flags is not None else None,
        json.dumps([{"attempt_number": n, "content_text": "x", "gate_ledger": g} for n, g in discarded]))
    return pid, req


async def _call(conn, stage, model, created, *, n=1, role="writer", cost=0.01, status="ok", stop=None,
                fallback=False, piece=None, request=None, attempt_no=1, signal=None):
    for _ in range(n):
        await conn.execute(
            "INSERT INTO shared.llm_call_log (stage, role, model, tokens_out, cost_usd, quality_signal, created_at, "
            "status, stop_reason, fallback_used, attempt_no, content_piece_id, angle_gate_request_id) "
            "VALUES ($1, $2, $3, 100, $4, $12::jsonb, $5, $6, $7, $8, $9, $10, $11)",
            stage, role, model, cost, created, status, stop, fallback, attempt_no, piece, request,
            json.dumps(signal or {}))


async def _t2_table(conn):
    if await conn.fetchval("SELECT to_regclass('gold_aa_internal.tenant_tour_versions') IS NOT NULL AS ok"):
        return
    await conn.execute(
        "CREATE TABLE gold_aa_internal.tenant_tour_versions (id uuid PRIMARY KEY, tenant_id uuid NOT NULL, "
        "published_tour_id uuid NOT NULL, version_number int NOT NULL, rewritten_content jsonb NOT NULL, "
        "status text NOT NULL, quality_score numeric(4,2), edit_source text NOT NULL, rewrite_language text NOT NULL, "
        "seo_mode text NOT NULL, created_at timestamptz NOT NULL, qa_status text NOT NULL, "
        "qa_repair_count smallint NOT NULL, qa_auto_passed boolean NOT NULL, judge_breakdown jsonb)")


async def _version(conn, n, *, status, edit_source, qa_status, repairs, created, breakdown=None):
    await conn.execute(
        "INSERT INTO gold_aa_internal.tenant_tour_versions (id, tenant_id, published_tour_id, version_number, "
        "rewritten_content, status, edit_source, rewrite_language, seo_mode, created_at, qa_status, qa_repair_count, "
        "qa_auto_passed, judge_breakdown) VALUES ($1, $2, $3, $4, '{}'::jsonb, $5, $6, 'en', 'off', $7, $8, $9, "
        "$10, $11::jsonb)",
        uuid.uuid4(), TENANT, uuid.uuid4(), n, status, edit_source, created, qa_status, repairs,
        qa_status == "escalated", json.dumps(breakdown) if breakdown else None)


async def _draft(conn, *, status, retry, created, metadata=None, published=False, evaluations=(), tour=None,
                 version=1, fix_pass=None, review=None):
    gid, tour = uuid.uuid4(), tour or uuid.uuid4()
    await conn.execute(
        "INSERT INTO silver_aa_internal.generated_content (id, tour_id, version_num, status, retry_count, created_at, "
        "metadata, fix_pass_applied) VALUES ($1, $2, $3, $4::content_status_enum, $5, $6, $7::jsonb, $8)",
        gid, tour, version, status, retry, created, json.dumps(metadata or {}), fix_pass)
    for i, (codes, brand) in enumerate(evaluations):
        await conn.execute(
            "INSERT INTO silver_aa_internal.quality_scores (generated_content_id, tour_id, score_overall, "
            "failure_codes, brand_audit_codes, evaluated_at) VALUES ($1, $2, 70, $3::jsonb, $4::jsonb, $5)",
            gid, tour, json.dumps(codes), json.dumps(brand) if brand is not None else None,
            created + timedelta(minutes=i))
    if review:
        await conn.execute(
            "INSERT INTO silver_aa_internal.review_queue (tour_id, generated_content_id, tenant_id, review_status, "
            "reviewed_by, reviewed_at) VALUES ($1, $2, $3, $4::review_status_enum, 'reviewer@example.test', $5)",
            tour, gid, TENANT, review, created + timedelta(hours=1))
    if published:
        await conn.execute(
            "INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, aa_name) "
            "VALUES ($1, $2, 'Invented Lake Walk')", tour, gid)
    return gid, tour


async def _dataset(conn):
    # the replay may lack the columns a pgvector migration adds next to them (as test_aa1039 / test_aa1006 note)
    await conn.execute("ALTER TABLE acp_shared.content_piece ADD COLUMN IF NOT EXISTS channel text")
    # T9: a clean first pass, a pass on attempt 2, a held piece after 3 attempts, a failed piece; last week's piece
    await _piece(conn, channel="blog", status="approved", attempt=1, created=W, ledger=[PASS])
    p2, _ = await _piece(conn, channel="blog", status="approved", attempt=2, created=W, ledger=[PASS],
                         discarded=[(1, [PASS, F9_CLAIM, F4])])
    _, r3 = await _piece(conn, channel="blog", status="held", attempt=3, created=W, ledger=[F9_CLAIM, F16],
                         discarded=[(1, [F9_DOWN]), (2, [F9_TEXT])], held_reason="F9_cta_fact: FACT_CHECK: x",
                         flags=[{"gate": "degraded", "passed": False, "blocking": False}])
    await _piece(conn, channel="facebook", status="failed", attempt=1, created=W, ledger=[])
    await _piece(conn, channel="facebook", status="approved", attempt=1, created=P, ledger=[PASS])
    # T9 ledger: the writer cut 2 of 20 replies (10 %); one row carries piece 2, one only piece 3's request
    await _call(conn, "t9_write", "zz-writer", W, n=17)
    await _call(conn, "t9_write", "zz-writer", W, piece=p2, cost=0.05)
    await _call(conn, "t9_write", "zz-writer", W, n=2, status="truncated", stop="max_tokens")
    await _call(conn, "t9_repair", "zz-writer", W, request=r3, cost=0.07)
    # AI gateway route tests (16-token real requests, all cut): no stage, cut, cost or watch number counts them
    await _call(conn, "t9_write", "zz-writer", W, n=5, status="truncated", stop="max_tokens",
                signal={"route_test": True, "route_trace": "rt-1"})
    await _call(conn, "t9_write", "zz-writer", W, piece=p2, cost=1.0, signal={"route_test": True})
    # the F9 judge: 25 primary answers the week before, then 20 primary + 5 from a new fallback model (20 %)
    await _call(conn, "t10_judge", "zz-judge-a", PRIOR, n=25, role="judge", cost=0.002)
    await _call(conn, "t10_judge", "zz-judge-a", W, n=20, role="judge", cost=0.002)
    await _call(conn, "t10_judge", "zz-judge-b", W, n=5, role="judge", cost=0.004, fallback=True)
    # S1: one draft published on its first attempt, one held after two retries (degraded), one rejected
    await _draft(conn, status="published", retry=0, created=W, published=True, evaluations=[([], None)])
    await _draft(conn, status="hitl", retry=2, created=W, metadata={"degraded": [{"name": "fallback_writer"}]},
                 evaluations=[(["seo.meta_length"], None), ({"fact.embellish": 1}, ["voice.generic"])])
    await _draft(conn, status="rejected", retry=1, created=P, evaluations=[(["fact.embellish"], [])])
    # S1 repaired / human: approved on attempt 1 only after the fix pass; a hitl draft a reviewer approved
    await _draft(conn, status="approved", retry=0, created=W, published=True, fix_pass=True)
    await _draft(conn, status="approved", retry=0, created=W, published=True, review="approved")
    # S1 supersede: last week's draft of a tour was published, this week's rewrite took its published_tours row
    _, tour = await _draft(conn, status="approved", retry=0, created=P)
    await _draft(conn, status="approved", retry=0, created=W, published=True, tour=tour, version=2)
    await _call(conn, "s1_generate", "zz-writer", W, n=2, cost=0.25)
    # T2: a clean version, an escalated one written by a fallback model, a failed one, a tenant edit (left out)
    await _t2_table(conn)
    await _version(conn, 1, status="ai_generated", edit_source="ai_generated", qa_status="passed", repairs=0,
                   created=W)
    await _version(conn, 2, status="needs_review", edit_source="re_rewrite", qa_status="escalated", repairs=2,
                   created=W, breakdown={"fallback_writer": {"held": False}})
    await _version(conn, 3, status="failed", edit_source="ai_generated", qa_status="pending", repairs=0, created=W)
    await _version(conn, 4, status="approved", edit_source="tenant_edit", qa_status="passed", repairs=0, created=W)
    await _call(conn, "t2_generate", "zz-writer", W, n=3, cost=0.10)


def _frozen(snap: dict) -> dict:
    """The deterministic part of a snapshot (route primaries come from the replayed seeds, so they are left out)."""
    watch = [{k: v for k, v in s.items() if k not in ("primary_model", "off_primary_pct")}
             for s in snap["watch"]["stages"]]
    return {"workflows": snap["workflows"], "stages": snap["stages"], "totals": snap["totals"],
            "watch_stages": watch, "findings": sorted(f.check for f in funnel.watch_findings(snap["watch"])),
            "unavailable": snap["unavailable"]}


@pytest.mark.asyncio
async def test_funnel_sql_counts_and_frozen_baseline(conn):
    await _dataset(conn)
    snap = await funnel.compute(conn, DAY, now=NOW)
    assert snap["unavailable"] == {}
    assert snap["window"] == {"first_day": "2031-01-13", "last_day": "2031-03-05", "weeks": 8,
                              "week_start": "ISO week, ICT, Monday"}

    t9 = {(r["week"], r["channel"]): r for r in snap["workflows"]["t9"]["by_week_channel"]}
    blog = t9[("2031-03-03", "blog")]
    assert (blog["created"], blog["approved"], blog["held"], blog["delivered"]) == (3, 2, 1, 3)
    assert blog["first_pass"] == 1 and blog["attempts_delivered"] == 6 and blog["degraded"] == 1
    assert blog["first_pass_yield"] == round(1 / 3, 4) and blog["attempts_per_held"] == 3.0
    assert blog["cost_usd"] == 0.12 and blog["usd_per_delivered"] == 0.04
    fb = t9[("2031-03-03", "facebook")]
    assert (fb["created"], fb["failed"], fb["delivered"]) == (1, 1, 0) and fb["usd_per_delivered"] is None
    assert t9[("2031-02-24", "facebook")]["first_pass"] == 1

    gates = {(g["attempt"], g["gate"], g["blocking"]): g["fires"] for g in snap["workflows"]["t9"]["gate_fails"]}
    assert gates == {(1, "F9_cta_fact", True): 1, (1, "F4_seo_surface", True): 1,
                     (1, funnel.F9_JUDGE_CLASS, False): 1,
                     (2, funnel.F9_TEXT_CLASS, True): 1, (2, "F9_cta_fact", True): 1,
                     (3, "F9_cta_fact", True): 1, (3, "F16_faq", True): 1}
    assert snap["workflows"]["t9"]["first_gate_holds"] == [{"week": "2031-03-03", "channel": "blog",
                                                            "gate": "F9_cta_fact", "held": 1}]

    s1 = {r["week"]: r for r in snap["workflows"]["s1"]["by_week"]}
    sw = s1["2031-03-03"]
    assert (sw["created"], sw["delivered"], sw["held"]) == (5, 4, 1)
    # first pass = the clean draft and the rewrite; the fix-pass draft and the reviewer-approved draft are not
    assert sw["first_pass"] == 2 and sw["fix_pass"] == 1 and sw["human"] == 1
    assert sw["attempts_delivered"] == 5                     # 1 + (1 + fix pass) + 1 + 1
    assert sw["usd_per_delivered"] == 0.125
    assert sw["first_pass_yield"] == 0.4 and sw["hold_rate"] == 0.2                      # hitl not delivered
    # the superseded draft stays delivered (and first pass) in its own week
    assert (s1["2031-02-24"]["delivered"], s1["2031-02-24"]["first_pass"], s1["2031-02-24"]["failed"]) == (1, 1, 1)
    s1_gates = {(g["week"], g["attempt"], g["gate"]): g["fires"] for g in snap["workflows"]["s1"]["gate_fails"]}
    assert s1_gates == {("2031-03-03", 1, "seo.meta_length"): 1, ("2031-03-03", 2, "fact.embellish"): 1,
                        ("2031-03-03", 2, "brand:voice.generic"): 1, ("2031-02-24", 1, "fact.embellish"): 1}

    t2 = snap["workflows"]["t2"]["by_week"]
    assert len(t2) == 1 and (t2[0]["created"], t2[0]["delivered"], t2[0]["held"], t2[0]["failed"]) == (3, 2, 1, 1)
    assert t2[0]["first_pass"] == 1 and t2[0]["attempts_delivered"] == 4 and t2[0]["fallback_writer"] == 1
    assert t2[0]["usd_per_delivered"] == 0.15

    stages = {(s["stage"], s["week"]): s for s in snap["stages"]}
    w = stages[("t9_write", "2031-03-03")]
    assert (w["answered"], w["replies"], w["cut"], w["cut_rate"]) == (18, 20, 2, 0.1)
    j = stages[("t10_judge", "2031-03-03")]
    assert j["fallback_share"] == 0.2 and j["rung_share"]["rung1"] == 0.2 and j["rung_share"]["primary"] == 0.8
    # T9 stages of the week: writer 0.24 + repair 0.07 + judge 0.06 = 0.37, of which 0.12 carries a blog piece
    assert snap["workflows"]["t9"]["unattributed_cost_usd"]["2031-03-03"] == 0.25
    # the T9 headline uses the whole T9 stage cost, like S1 and T2; the per-channel part is "attributed"
    week = snap["totals"]["t9"]["this_week"]
    assert (week["cost_usd"], week["attributed_cost_usd"], week["unattributed_cost_usd"]) == (0.37, 0.12, 0.25)
    assert week["usd_per_delivered"] == round(0.37 / 3, 4) and week["attributed_usd_per_delivered"] == 0.04
    win = snap["totals"]["t9"]["window"]           # + the judge rows of the week before the watch window (0.05)
    assert (win["cost_usd"], win["unattributed_cost_usd"], win["usd_per_delivered"]) == (0.42, 0.3, 0.105)

    assert sorted(f.check for f in funnel.watch_findings(snap["watch"])) == [
        "qf_cut.t9_write", "qf_drift.t10_judge", "qf_fb.t10_judge"]

    frozen = json.loads(json.dumps(_frozen(snap), default=str))
    if os.environ.get("AA900_WRITE_BASELINE") == "1":
        BASELINE.parent.mkdir(exist_ok=True)
        BASELINE.write_text(json.dumps(frozen, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert frozen == json.loads(BASELINE.read_text(encoding="utf-8")), \
        "funnel definitions changed: regenerate with AA900_WRITE_BASELINE=1 and review the diff"


@pytest.mark.asyncio
async def test_a_stage_route_policy_sets_the_fallback_threshold(conn):
    await _dataset(conn)
    await conn.execute("DELETE FROM shared.llm_route_policy WHERE stage = 't10_judge'")
    await conn.execute("INSERT INTO shared.llm_route_policy (stage, fallback_alert_pct) VALUES ('t10_judge', 50)")
    snap = await funnel.compute(conn, DAY, now=NOW)
    judge = {s["stage"]: s for s in snap["watch"]["stages"]}["t10_judge"]
    assert judge["policy"]["fallback_alert_pct"] == 50.0 and judge["fallback_pct"] == 20.0
    assert sorted(f.check for f in funnel.watch_findings(snap["watch"])) == ["qf_cut.t9_write", "qf_drift.t10_judge"]


async def _store(conn, out, day, trigger="schedule"):
    run_id = await store.create_run(conn, "CIS-08", day, day, trigger=trigger, triggered_by="pytest")
    await store.mark_running(conn, run_id, str(uuid.uuid4()))
    await store.finish_ok(conn, run_id, summary=out.summary, output=out.output, markdown=out.markdown)
    return run_id


@pytest.mark.asyncio
async def test_a_stored_cis08_run_is_what_the_endpoint_reads(conn):
    await _dataset(conn)
    report = load().report("CIS-08")
    # the night before: a scheduled run that already had the cut finding (and a manual run of that day: no baseline)
    before = DAY - timedelta(days=1)
    prev = funnel_report.build(report, {**(await funnel.compute(conn, before, now=NOW)),
                                        "watch": {"stages": [], "findings": []}}, NOW)
    prev.output["watch"]["findings"] = [{"check": "qf_cut.t9_write", "summary": "s", "details": {}}]
    prev_id = await _store(conn, prev, before)
    manual_before = funnel_report.build(report, {**prev.output, "watch": {"stages": []}}, NOW)
    manual_before.output["watch"]["findings"] = [{"check": "qf_fb.t10_judge", "summary": "s", "details": {}}]
    await _store(conn, manual_before, before, trigger="manual")

    out = await funnel_report.render(conn, load(), report, DAY, DAY, NOW)
    assert out.output["watch"]["compared_with"] == {"run_id": prev_id, "day": before.isoformat()}
    run_id = await _store(conn, out, DAY)
    manual_id = await _store(conn, out, DAY, trigger="manual")       # a later "Generate now" of the same day

    latest = await conn.fetchrow(LATEST_SQL, "CIS-08")
    assert str(latest["id"]) == run_id and latest["period_end"] == DAY and latest["trigger"] == "schedule"
    assert str((await conn.fetchrow(LATEST_MANUAL_SQL, "CIS-08"))["id"]) == manual_id
    stored = json.loads(latest["output"])
    assert {f["check"]: f["repeat"] for f in stored["watch"]["findings"]} == {
        "qf_cut.t9_write": True, "qf_drift.t10_judge": False, "qf_fb.t10_judge": False}
    hist = await conn.fetch(HISTORY_SQL, "CIS-08", 5)
    assert [str(h["id"]) for h in hist] == [run_id, prev_id]
    assert (hist[0]["findings"], hist[0]["new_findings"]) == (3, 2)
    assert json.loads(hist[0]["totals"])["t9"]["window"]["delivered"] == 4
