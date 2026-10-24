"""AA-917 — the writing-memory backfill, fact filter, rule store and mining run on real Postgres (CIS 277 + 280
replayed by conftest). The drafter (the only paid call) is replaced by a stub; nothing here calls a model.

  * WM-10: the backfill copies a legacy admin sentence audit row, a T2 tenant edit (one signal per changed field) and
    an A1 brand-audit lesson, with scope / tenant / created_at; a re-run inserts 0 rows; a dry run inserts nothing;
  * WM-11: the fact filter leaves no `pending` row: "lunch" -> "lunch included" fails, a style edit passes, an added
    name is grey, an admin fact_not_in_source stays fail;
  * WM-09 code reads: rules_for never resolves another tenant's rule; insert_candidate is idempotent per cluster; a
    lint failure is stored rejected; reject / expire;
  * WM-12: a run over 3 tenant edits stores one candidate with its provenance and support; a second run skips the open
    cluster; a month over the $25 cap makes no call; the trigger counts succeeded runs and new pass signals.
  * S250 review round: a 30-word draft is stored rejected inside the CIS 280 limits (the paid run never crashes) and
    is not redrafted while its evidence is unchanged; a failed run that spent money counts against the monthly
    limits; the automatic trigger is off unless WRITING_MEMORY_AUTO_MINE is set; a platform draft that copies tenant
    text is stored redacted, and an admin edit into tenant text is refused without a write.
Each test runs in one transaction that is rolled back.
"""
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.writing_memory import backfill as bf  # noqa: E402
from services.writing_memory import drafter  # noqa: E402
from services.writing_memory import fact_filter as ff  # noqa: E402
from services.writing_memory import mining  # noqa: E402
from services.writing_memory import rules as rules_mod  # noqa: E402
from services.writing_memory import signals as wm  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MASTER = wm.PLATFORM_TENANT_ID


class _OneConnPool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self, timeout=None):
        yield self.conn

    async def fetchval(self, *a):
        return await self.conn.fetchval(*a)

    async def fetchrow(self, *a):
        return await self.conn.fetchrow(*a)


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        # a clean slate for this transaction only: the tables are empty in CI, rows of other tests are rolled back
        await c.execute("DELETE FROM shared.writing_rule")
        await c.execute("DELETE FROM shared.writing_signal")
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _signal(c, *, tenant, piece, before="A truly breathtaking view.", after="A view.", reason="ai_phrasing",
                  status="pass", actor="tenant", writer="t9_blog"):
    scope, owner = wm.scope_for_tenant(tenant)
    ref = await wm.record_writer_feedback(
        c, writer=writer, scope=scope, tenant_id=owner, channel="blog", field="content_text", before_text=before,
        after_text=after, reason_code=reason, action="tenant_edit" if actor == "tenant" else "sentence_manual",
        actor_type=actor, actor_id=f"tenant:{tenant}", source_table="content_piece", source_id=piece,
        fact_status=status)
    return ref.signal_id


# ── WM-10 backfill ──────────────────────────────────────────────────────────────────────────────────────────────────

async def _ttv_standin(c):
    await c.execute("""CREATE TABLE IF NOT EXISTS gold_aa_internal.tenant_tour_versions (
                           id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL,
                           rewritten_content jsonb NOT NULL DEFAULT '{}'::jsonb,
                           edit_source text NOT NULL DEFAULT 'ai_generated',
                           created_at timestamptz NOT NULL DEFAULT now())""")
    await c.execute("ALTER TABLE gold_aa_internal.tenant_tour_versions "
                    "ADD COLUMN IF NOT EXISTS parent_version_id uuid, ADD COLUMN IF NOT EXISTS edited_at timestamptz")


@pytest.mark.asyncio
async def test_backfill_copies_each_source_once(conn):
    t = str(uuid.uuid4())
    when = datetime(2026, 9, 1, tzinfo=timezone.utc)
    audit_id = await conn.fetchval(
        """INSERT INTO acp_shared.audit_log (tenant_id, actor, action, resource_type, resource_id, details, created_at)
           VALUES ($1, $2, 'tenant_tour_version.admin_sentence_rewritten', 'tenant_tour_version', $3, $4::jsonb, $5)
           RETURNING id::text""",
        t, f"admin:{uuid.uuid4()}", str(uuid.uuid4()),
        json.dumps({"field": "summary", "index": None, "start": 0, "end": 9, "before": "Truly old.", "after": "Old.",
                    "gate": "T3_grounding", "problem": "p"}), when)
    # a row written since AA-916 names its signal and holds no text: not copied
    await conn.execute("""INSERT INTO acp_shared.audit_log (tenant_id, actor, action, resource_type, resource_id,
                                                          details)
                          VALUES ($1, 'admin:x', 'content_piece.admin_sentence_cut', 'content_piece', 'p',
                                  $2::jsonb)""",
                       t, json.dumps({"signal_id": str(uuid.uuid4())}))
    await _ttv_standin(conn)
    v1 = await conn.fetchval("INSERT INTO gold_aa_internal.tenant_tour_versions (tenant_id, rewritten_content) "
                             "VALUES ($1::uuid, $2::jsonb) RETURNING id", t,
                             json.dumps({"summary": "A summary.", "highlights": ["a", "b"], "name": "Same"}))
    await conn.execute("INSERT INTO gold_aa_internal.tenant_tour_versions (tenant_id, rewritten_content, edit_source, "
                       "parent_version_id) VALUES ($1::uuid, $2::jsonb, 'tenant_edit', $3)", t,
                       json.dumps({"summary": "My summary.", "highlights": ["a"], "name": "Same"}), v1)

    pool = _OneConnPool(conn)
    dry = await bf.run_backfill(pool, sources=("audit", "t2_edit"), dry_run=True)
    assert dry["inserted"] == 3 and await conn.fetchval("SELECT count(*) FROM shared.writing_signal") == 0
    first = await bf.run_backfill(pool, sources=("audit", "t2_edit"))
    assert first["per_source"]["audit"]["inserted"] == 1 and first["per_source"]["t2_edit"]["inserted"] == 2
    assert first["per_tenant"] == {t: {"audit": 1, "t2_edit": 2}}
    again = await bf.run_backfill(pool, sources=("audit", "t2_edit"))
    assert again["inserted"] == 0 and again["per_source"]["audit"]["existing"] == 1

    row = await conn.fetchrow("SELECT writer, scope, tenant_id::text AS t, before_text, after_text, action, "
                              "audit_log_id::text AS a, created_at, fact_status FROM shared.writing_signal "
                              "WHERE audit_log_id IS NOT NULL")
    assert (row["writer"], row["scope"], row["t"], row["before_text"], row["after_text"], row["action"], row["a"]) == \
        ("t2", "tenant", t, "Truly old.", "Old.", "sentence_rewrite", audit_id)
    assert row["created_at"] == when and row["fact_status"] == "pending"
    fields = {r["field"] for r in await conn.fetch("SELECT field FROM shared.writing_signal WHERE action = "
                                                  "'tenant_edit'")}
    assert fields == {"summary", "highlights"}


@pytest.mark.asyncio
async def test_backfill_lessons_and_missing_sources(conn):
    tour, gc = uuid.uuid4(), uuid.uuid4()
    await conn.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) VALUES ($1, 'AA master', "
                       "'aa-master-zz917', 3) ON CONFLICT (tenant_id) DO NOTHING", uuid.UUID(MASTER))
    await conn.execute("INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country) "
                       "VALUES ($1, $2, gen_random_uuid(), 'zz917', 'Laos')", tour, uuid.UUID(MASTER))
    await conn.execute("INSERT INTO silver_aa_internal.generated_content (id, tour_id, tenant_id, version_num, "
                       "aa_name) VALUES ($1, $2, $3, 1, 'zz917')", gc, tour, uuid.UUID(MASTER))
    await conn.execute(
        "INSERT INTO silver_aa_internal.quality_scores (generated_content_id, tour_id, tenant_id, score_overall, "
        "passed_count, failed_count, lessons_extracted) VALUES ($1, $2, $3, 6.5, 1, 1, $4::jsonb)", gc, tour,
        uuid.UUID(MASTER), json.dumps([{"pattern": "Generic opener", "failure_code": "GENERIC_AI_WORDING",
                                        "field": "summary", "example_before": "Embark on a journey.",
                                        "severity": "low"}]))
    pool = _OneConnPool(conn)
    out = await bf.run_backfill(pool, sources=("a1_lesson", "t9_attempt"))
    assert out["per_source"]["a1_lesson"]["inserted"] == 1 and out["per_tenant"] == {"platform": {"a1_lesson": 1}}
    row = await conn.fetchrow("SELECT writer, scope, tenant_id, gate, problem, source_table, source_id "
                              "FROM shared.writing_signal")
    assert tuple(row) == ("a1", "platform", None, "GENERIC_AI_WORDING", "Generic opener", "generated_content",
                          str(gc))
    assert (await bf.run_backfill(pool, sources=("a1_lesson",)))["inserted"] == 0


# ── WM-11 fact filter ───────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fact_filter_leaves_no_pending(conn):
    t = str(uuid.uuid4())
    ids = {
        "promise": await _signal(conn, tenant=t, piece="p1", before="We stop for lunch.",
                                 after="We stop for lunch included.", status="pending"),
        "style": await _signal(conn, tenant=t, piece="p2", status="pending"),
        "name": await _signal(conn, tenant=t, piece="p3", before="We see the temple.",
                              after="We see the temple near Lantern Bridge.", status="pending"),
        "admin": await _signal(conn, tenant=t, piece="p4", reason="fact_not_in_source", actor="admin",
                               status="pending"),
    }
    out = await ff.run_fact_filter(_OneConnPool(conn))
    assert out["checked"] == 4 and out["verdicts"] == {"fail": 2, "pass": 1, "grey": 1}
    assert out["totals"].get("pending", 0) == 0
    got = {k: await conn.fetchval("SELECT fact_status FROM shared.writing_signal WHERE signal_id = $1::uuid", v)
           for k, v in ids.items()}
    assert got == {"promise": "fail", "style": "pass", "name": "grey", "admin": "fail"}
    assert (await ff.run_fact_filter(_OneConnPool(conn)))["checked"] == 0


# ── WM-09 rule store reads / writes ─────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rules_isolation_idempotence_reject_expire(conn):
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    ok = {"ok": True}
    ra = await rules_mod.insert_candidate(conn, kind="writer_instruction", scope="tenant", tenant_id=a,
                                          writer="t9_blog", cluster_key="t9_blog|reason:x", rule_text="Cut it.",
                                          match_phrases=["it"], lint=ok)
    assert ra and await rules_mod.insert_candidate(conn, kind="writer_instruction", scope="tenant", tenant_id=a,
                                                   writer="t9_blog", cluster_key="t9_blog|reason:x",
                                                   rule_text="Again.", lint=ok) is None
    rb = await rules_mod.insert_candidate(conn, kind="writer_instruction", scope="tenant", tenant_id=b,
                                          writer="t9_blog", cluster_key="t9_blog|reason:x", rule_text="Cut it.",
                                          lint=ok)
    rp = await rules_mod.insert_candidate(conn, kind="lexicon", scope="platform", tenant_id=None, writer="t9_blog",
                                          cluster_key="t9_blog|phrase:truly", rule_text="truly",
                                          lint={"ok": False, "predict": {"ok": False}})
    assert (await rules_mod.get_rule(conn, rp))["status"] == "rejected"
    assert (await rules_mod.get_rule(conn, rp))["rejected_reason"] == "lint: predict"
    await conn.execute("UPDATE shared.writing_rule SET status = 'active', approved_by = 'zz', approved_at = now()")
    seen_a = {r["rule_id"] for r in await rules_mod.rules_for(conn, "t9_blog", a)}
    assert seen_a == {ra, rp} and rb not in seen_a                   # never another tenant's rule
    assert {r["rule_id"] for r in await rules_mod.rules_for(conn, "t9_blog", None)} == {rp}
    assert {r["rule_id"] for r in await rules_mod.rules_for(conn, "t9_blog", MASTER)} == {rp}
    assert await rules_mod.reject(conn, ra, reason="active", actor="admin:x") is None   # an active rule: a release
    await conn.execute("UPDATE shared.writing_rule SET status = 'candidate', expires_at = now() - interval '1 day' "
                       "WHERE rule_id = $1::uuid", rb)
    assert await rules_mod.expire_stale(conn) == 1
    assert (await rules_mod.get_rule(conn, rb))["status"] == "expired"


# ── WM-12 the mining run ────────────────────────────────────────────────────────────────────────────────────────────

def _stub_draft(**kw):
    return drafter.Draft(kind="lexicon", rule_text="breathtaking", replacement=None, match_phrases=["breathtaking"],
                         model="stub", cost_usd=0.001)


@pytest.mark.asyncio
async def test_mine_run_stores_a_candidate_with_provenance_then_skips_it(conn):
    t = str(uuid.uuid4())
    sids = [await _signal(conn, tenant=t, piece=f"p{i}", before=f"Trip {i} has truly breathtaking views.",
                          after=f"Trip {i} has views.") for i in range(4)]
    pool = _OneConnPool(conn)
    with patch.object(drafter, "draft_rule", AsyncMock(side_effect=lambda pool, **kw: _stub_draft())) as draft:
        out = await mining.run_mine(pool, None, trigger="manual", max_calls=50)
    assert out["calls"] == draft.await_count >= 1 and out["candidates"] >= 1
    rule = await conn.fetchrow("SELECT * FROM shared.writing_rule WHERE cluster_key = 't9_blog|reason:ai_phrasing'")
    assert rule["scope"] == "tenant" and str(rule["tenant_id"]) == t and rule["status"] == "candidate"
    assert sorted(str(s) for s in rule["signal_ids"]) == sorted(sids)
    assert (rule["support_signals"], rule["support_pieces"], rule["support_tenants"]) == (4, 4, 0)
    lint = json.loads(rule["lint"])
    assert lint["ok"] and lint["predict"]["explained"] == 2 and lint["basis"] == "tenant"
    with patch.object(drafter, "draft_rule", AsyncMock(side_effect=lambda pool, **kw: _stub_draft())) as draft:
        again = await mining.run_mine(pool, None, trigger="manual")
    assert again["skipped_open"] >= 1 and again["candidates"] == 0


@pytest.mark.asyncio
async def test_mine_run_makes_no_call_over_the_month_cap(conn):
    t = str(uuid.uuid4())
    for i in range(4):
        await _signal(conn, tenant=t, piece=f"p{i}")
    await conn.execute("INSERT INTO shared.llm_call_log (stage, role, model, cost_usd, quality_signal) "
                       "VALUES ('writing_memory_mine', 'judge', 'gpt-5.6-luna', 25.5, '{}'::jsonb)")
    with patch.object(drafter, "draft_rule", AsyncMock()) as draft:
        out = await mining.run_mine(_OneConnPool(conn), None, trigger="manual")
    draft.assert_not_awaited()
    assert out["calls"] == 0 and out["stopped"].startswith("budget") and "month budget" in out["stopped"]


@pytest.mark.asyncio
async def test_run_gate_counts_runs_and_new_pass_signals(conn, monkeypatch):
    monkeypatch.setenv("WRITING_MEMORY_AUTO_MINE", "true")
    await conn.execute("DELETE FROM shared.job WHERE kind = 'writing_memory_mine'")
    t = str(uuid.uuid4())
    for i in range(10):
        await _signal(conn, tenant=t, piece=f"p{i}")
    refusal, facts = await mining.run_gate(conn, "auto")
    assert refusal is None and facts["new_pass"] == 10 and facts["runs_this_month"] == 0
    for trig in ("auto", "auto"):
        await conn.execute("INSERT INTO shared.job (kind, payload, status, result, finished_at) VALUES "
                           "('writing_memory_mine', '{}'::jsonb, 'succeeded', $1::jsonb, now())",
                           json.dumps({"ran": True, "trigger": trig}))
    refusal, facts = await mining.run_gate(conn, "auto")
    assert facts["runs_this_month"] == 2 and "automatic runs" in refusal
    assert (await mining.run_gate(conn, "manual"))[0] is None


def _long_draft(**kw):
    words = " ".join(f"word{i}" for i in range(30))
    return drafter.Draft(kind="writer_instruction", rule_text=f"Cut {words}", replacement=None,
                         match_phrases=["breathtaking"], model="stub", cost_usd=0.001)


@pytest.mark.asyncio
async def test_over_long_draft_is_stored_rejected_and_not_redrafted(conn):
    t = str(uuid.uuid4())
    for i in range(4):
        await _signal(conn, tenant=t, piece=f"p{i}", before=f"Trip {i} has truly breathtaking views.",
                      after=f"Trip {i} has views.")
    pool = _OneConnPool(conn)
    with patch.object(drafter, "draft_rule", AsyncMock(side_effect=lambda pool, **kw: _long_draft())) as draft:
        out = await mining.run_mine(pool, None, trigger="manual")
    assert draft.await_count >= 1 and out["store_refused"] == 0 and out["lint_rejected"] == draft.await_count
    rows = await conn.fetch("SELECT status, rejected_by, rule_text, lint FROM shared.writing_rule")
    assert rows and all(r["status"] == "rejected" and r["rejected_by"] == "lint" for r in rows)
    assert all(len(r["rule_text"].split()) <= 25 and json.loads(r["lint"])["clamped"] for r in rows)
    with patch.object(drafter, "draft_rule", AsyncMock(side_effect=lambda pool, **kw: _long_draft())) as draft:
        again = await mining.run_mine(pool, None, trigger="manual")
    draft.assert_not_awaited()                                  # same evidence: no paid redraft
    assert again["skipped_lint_same"] == len(rows)
    await _signal(conn, tenant=t, piece="p9", before="Trip 9 has truly breathtaking views.", after="Trip 9 has views.")
    with patch.object(drafter, "draft_rule", AsyncMock(side_effect=lambda pool, **kw: _long_draft())) as draft:
        await mining.run_mine(pool, None, trigger="manual")
    assert draft.await_count >= 1                               # a new signal joined: drafted again


async def _mine_job(c, *, status, trigger="auto", cost=0.0, calls=None, result=None):
    await c.execute("INSERT INTO shared.job (kind, payload, status, result, cost_usd, progress, finished_at) VALUES "
                    "('writing_memory_mine', $1::jsonb, $2, $3::jsonb, $4, $5::jsonb, now())",
                    json.dumps({"trigger": trigger}), status, json.dumps(result) if result is not None else None,
                    cost, json.dumps({"calls": calls} if calls is not None else {}))


@pytest.mark.asyncio
async def test_failed_paid_runs_count_and_the_auto_switch(conn, monkeypatch):
    await conn.execute("DELETE FROM shared.job WHERE kind = 'writing_memory_mine'")
    t = str(uuid.uuid4())
    for i in range(12):
        await _signal(conn, tenant=t, piece=f"p{i}")
    monkeypatch.delenv("WRITING_MEMORY_AUTO_MINE", raising=False)
    refusal, facts = await mining.run_gate(conn, "auto")
    assert "off" in refusal and facts["auto_enabled"] is False
    assert (await mining.run_gate(conn, "manual"))[0] is None
    monkeypatch.setenv("WRITING_MEMORY_AUTO_MINE", "true")
    await _mine_job(conn, status="failed", cost=0.0)                         # crashed before any call: not a run
    refusal, facts = await mining.run_gate(conn, "auto")
    assert refusal is None and facts["runs_this_month"] == 0
    await _mine_job(conn, status="failed", cost=0.4, calls=3)                # crashed after paid calls: a run
    await _mine_job(conn, status="failed", calls=2)                          # calls made, cost not saved: a run
    refusal, facts = await mining.run_gate(conn, "auto")
    assert facts["runs_this_month"] == 2 and facts["auto_runs_this_month"] == 2 and "automatic runs" in refusal
    me = await conn.fetchval("INSERT INTO shared.job (kind, payload, status, locked_by, locked_until) VALUES "
                             "('writing_memory_mine', '{\"trigger\": \"manual\"}'::jsonb, 'running', 'w', "
                             "now() + interval '5 minutes') RETURNING id::text")
    assert (await mining.run_gate(conn, "manual", exclude_job_id=me))[1]["runs_this_month"] == 2
    assert (await mining.run_gate(conn, "manual"))[1]["runs_this_month"] == 3     # seen by any other caller


def _leaky_draft(**kw):
    return drafter.Draft(kind="writer_instruction", rule_text="Do not write serves warm noodle soup at dawn.",
                         replacement=None, match_phrases=["warm noodle soup"], model="stub", cost_usd=0.001)


@pytest.mark.asyncio
async def test_platform_draft_with_tenant_text_is_stored_redacted(conn):
    from fastapi import HTTPException

    from api.routers import admin_writing_memory as r
    for i in range(3):                                          # 3 tenants: a platform cluster from tenant evidence
        await _signal(conn, tenant=str(uuid.uuid4()), piece=f"p{i}",
                      before=f"Lodge {i} truly serves warm noodle soup at dawn.",
                      after=f"Lodge {i} serves warm noodle soup at dawn.")
    with patch.object(drafter, "draft_rule", AsyncMock(side_effect=lambda pool, **kw: _leaky_draft())) as draft:
        out = await mining.run_mine(_OneConnPool(conn), None, trigger="manual")
    assert draft.await_count >= 1 and out["redacted"] >= 1 and out["store_refused"] == 0
    rows = await conn.fetch("SELECT scope, status, rule_text, replacement, match_phrases, lint::text AS lint "
                            "FROM shared.writing_rule WHERE scope = 'platform'")
    assert rows and all(r_["status"] == "rejected" and r_["rule_text"] == rules_mod.REDACTED_TEXT
                        and r_["match_phrases"] == [] and r_["replacement"] is None for r_ in rows)
    assert not any("noodle" in (r_["lint"] + r_["rule_text"]).lower() for r_ in rows)

    # an admin edit of a platform candidate into tenant text is refused and writes nothing
    rid = await rules_mod.insert_candidate(conn, kind="writer_instruction", scope="platform", tenant_id=None,
                                           writer="t9_blog", cluster_key="t9_blog|reason:edit-probe",
                                           rule_text="Cut stock openers.", match_phrases=["truly"], lint={"ok": True})
    body = r.CandidateEdit(rule_text="Never write serves warm noodle soup at dawn.", match_phrases=["truly"])
    with pytest.raises(HTTPException) as e:
        await r.edit_candidate(rid, body, _request(conn), x_admin_user_id="7")
    assert e.value.status_code == 422 and e.value.detail["code"] == "tenant_text"
    assert "noodle" not in e.value.detail["detail"]
    kept = await rules_mod.get_rule(conn, rid)
    assert kept["status"] == "candidate" and kept["rule_text"] == "Cut stock openers."


# ── WM-14 the admin API (route functions on the real DB; the policy is checked by the AA-882 route test) ────────────

def _request(c):
    from types import SimpleNamespace
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=_OneConnPool(c))))


@pytest.mark.asyncio
async def test_admin_routes(conn):
    from fastapi import HTTPException

    from api.routers import admin_writing_memory as r
    t = str(uuid.uuid4())
    for i in range(4):
        await _signal(conn, tenant=t, piece=f"p{i}", before=f"Trip {i} has truly breathtaking views.",
                      after=f"Trip {i} has views.")
    grey = await _signal(conn, tenant=t, piece="g", status="grey")
    marked = await _signal(conn, tenant=t, piece="m", reason="fact_not_in_source", actor="admin", status="fail")
    req = _request(conn)

    s = await r.summary(req)
    assert s["signals"]["total"] == 6 and s["signals"]["fact_status"] == {"pass": 4, "grey": 1, "fail": 1}
    assert s["budget"]["month_cap_usd"] == 25.0 and "auto_refused" in s["mine_trigger"]
    lst = await r.list_signals(req, writer=None, scope=None, tenant_id=t, fact_status="pass", actor_type=None,
                               action=None, limit=50, offset=0)
    assert lst["total"] == 4
    cl = await r.signal_clusters(req)
    mine = [c for c in cl["clusters"] if c["label"] == "reason:ai_phrasing" and c["tenant_id"] == t]
    assert mine and mine[0]["draftable"] and not mine[0]["has_open_rule"] and mine[0]["support"]["pieces"] == 4
    one = await r.get_signal(grey, req)
    assert one["fact_check"]["status"] == "pass" and one["fact_status"] == "grey"

    assert (await r.set_signal_fact(grey, r.FactBody(status="pass"), req, None))["fact_status"] == "pass"
    with pytest.raises(HTTPException) as e:
        await r.set_signal_fact(marked, r.FactBody(status="pass"), req, None)
    assert e.value.status_code == 409
    with pytest.raises(HTTPException) as e:
        await r.get_signal(str(uuid.uuid4()), req)
    assert e.value.status_code == 404

    rid = await rules_mod.insert_candidate(
        conn, kind="lexicon", scope="tenant", tenant_id=t, writer="t9_blog", cluster_key="t9_blog|reason:ai_phrasing",
        rule_text="breathtaking", match_phrases=["breathtaking"], lint={"ok": True},
        signal_ids=[x["signal_id"] for x in lst["items"]])
    got = await r.get_candidate(rid, req)
    assert len(got["provenance"]) == 4 and got["status"] == "candidate"
    edited = await r.edit_candidate(rid, r.CandidateEdit(rule_text="truly breathtaking", replacement=None,
                                                         match_phrases=["truly breathtaking"]), req, None)
    assert edited["status"] == "candidate" and edited["lint"]["predict"]["explained"] == 4
    bad = await r.edit_candidate(rid, r.CandidateEdit(rule_text="breathtaking", replacement="breathtaking at 7am",
                                                      match_phrases=["breathtaking"]), req, None)
    assert bad["status"] == "rejected" and not bad["lint"]["lexicon_specifics"]["ok"]
    with pytest.raises(HTTPException) as e:
        await r.reject_candidate(rid, r.RejectBody(reason="already gone"), req, None)
    assert e.value.status_code == 409
    listed = await r.list_candidates(req, status="rejected", writer=None, scope=None, tenant_id=t, limit=50, offset=0)
    assert listed["total"] == 1

    await conn.execute("DELETE FROM shared.job WHERE kind = 'writing_memory_mine'")
    for _ in range(3):
        await conn.execute("INSERT INTO shared.job (kind, payload, status, result, finished_at) VALUES "
                           "('writing_memory_mine', '{}'::jsonb, 'succeeded', '{\"ran\": true}'::jsonb, now())")
    with pytest.raises(HTTPException) as e:
        await r.run(r.RunBody(kind="mine"), req, None)
    assert e.value.status_code == 409 and "runs this month" in e.value.detail["detail"]
    queued = await r.run(r.RunBody(kind="fact_filter"), req, None)
    assert queued["created"] and queued["kind"] == "writing_signal_fact_filter"
