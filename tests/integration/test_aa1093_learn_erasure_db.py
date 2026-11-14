"""AA-1093 (S281 lane l2-erase-crm): the tenant erasure covers schema learn (CIS 420, AA-1105), on real Postgres.

A partner learner of an offboarded tenant with a row in every learn table, a partner learner of another tenant and a
staff learner (no tenant): the DRY RUN counts the tenant's rows and writes nothing, the apply deletes them (the
append-only learn.statement through the purge flag the apply sets for its own transaction), the other two keep every
row. Fixtures and helpers: tests/integration/test_aa839_erasure_job.py (its module fixture shapes CIS 244 as on Dev).
"""
from __future__ import annotations

import hashlib
import os
import sys
import uuid

import asyncpg
import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from test_aa839_erasure_job import ON, _rows, _run, _tenant, dev_shape, pool  # noqa: E402,F401 (fixtures)

LEARN_TABLES = ("learn.learner", "learn.enrollment", "learn.statement", "learn.lesson_progress", "learn.quiz_attempt",
                "learn.review_card", "learn.lab_run", "learn.certificate")


async def _seed_learner(admin, tid, kind="partner") -> str:
    """One learner with a row in every learn table (AA-1105, CIS 420): a partner learner of ``tid`` or a staff learner
    (no tenant). Invented identity only."""
    learner = await admin.fetchval(
        "INSERT INTO learn.learner (iss, sub, kind, tenant_id, display_name) "
        "VALUES ('https://idp.example.test/pool', $1, $2, $3::uuid, 'Lan Test Learner') RETURNING learner_id::text",
        f"sub-{uuid.uuid4().hex[:12]}", kind, tid)
    t = tid
    await admin.execute("INSERT INTO learn.enrollment (learner_id, tenant_id, path_id) VALUES ($1::uuid, $2::uuid, "
                        "'engineer-on-duty')", learner, t)
    await admin.execute("INSERT INTO learn.statement (learner_id, tenant_id, verb, object_type, object_id, "
                        "body_sha256) VALUES ($1::uuid, $2::uuid, 'opened', 'lesson', 'A1-01', $3)",
                        learner, t, "b" * 64)
    await admin.execute("INSERT INTO learn.lesson_progress (learner_id, lesson_id, tenant_id, module_id, "
                        "module_version, status, lang) VALUES ($1::uuid, 'A1-01', $2::uuid, 'A1', '1.0', 'opened', "
                        "'en')", learner, t)
    attempt = await admin.fetchval(
        "INSERT INTO learn.quiz_attempt (learner_id, tenant_id, scope, target_id, item_ids, module_versions, "
        "bundle_sha, pass_mark_pct, status) VALUES ($1::uuid, $2::uuid, 'module', 'A1', ARRAY['A1-Q1'], "
        "'{}'::jsonb, 'sha', 80, 'open') RETURNING attempt_id", learner, t)
    await admin.execute("INSERT INTO learn.review_card (learner_id, item_id, tenant_id, module_id, card, due_at, reps, "
                        "lapses) VALUES ($1::uuid, 'A1-Q1', $2::uuid, 'A1', '{}'::jsonb, now(), 0, 0)", learner, t)
    await admin.execute("INSERT INTO learn.lab_run (learner_id, tenant_id, module_id, status, result) "
                        "VALUES ($1::uuid, $2::uuid, 'A1', 'queued', '{}'::jsonb)", learner, t)
    await admin.execute(
        "INSERT INTO learn.certificate (learner_id, tenant_id, path_id, path_title, code_sha256, status, "
        "module_versions, badge, final_attempt_id) VALUES ($1::uuid, $2::uuid, 'engineer-on-duty', 'Engineer on duty', "
        "$3, 'valid', '{}'::jsonb, '{}'::jsonb, $4)", learner, t, hashlib.sha256(learner.encode()).hexdigest(), attempt)
    return learner


async def _learn_counts(admin, learner: str) -> dict:
    out = {}
    for table in LEARN_TABLES:
        out[table] = await admin.fetchval(f"SELECT count(*) FROM {table} WHERE learner_id = $1::uuid", learner)
    return out


@pytest.mark.asyncio
async def test_offboarding_erases_the_tenants_learn_records_and_no_staff_learner(pool):  # noqa: F811
    """AA-1093 (S281): the tenant erasure covers schema learn (CIS 420). A partner learner of the offboarded tenant with
    a row in every learn table, a partner learner of another tenant and a staff learner (no tenant): the DRY RUN counts
    the tenant's rows, the apply deletes them (learn.statement is append-only: the apply sets learn.retention_purge for
    its transaction), the other two learners keep everything."""
    admin, _ = pool
    tid, other = await _tenant(pool, offboarded_days=40), await _tenant(pool)
    mine, theirs = await _seed_learner(admin, tid), await _seed_learner(admin, other)
    staff = await _seed_learner(admin, None, kind="staff")
    full = {t: 1 for t in LEARN_TABLES}
    assert await _learn_counts(admin, mine) == full

    dry_id, dry = await _run(pool, {"subject": "tenant", "tenant": tid})
    assert dry["mode"] == "dry_run" and not dry["blocked"], dry["blockers"]
    counted = {t: n for (t, action), n in _rows(dry).items() if t in LEARN_TABLES}
    assert counted == full, counted
    assert await _learn_counts(admin, mine) == full                                 # a DRY RUN writes nothing

    _, res = await _run(pool, {"subject": "tenant", "tenant": tid, "mode": "apply", "dry_run_job_id": dry_id,
                               "approved_fingerprint": dry["fingerprint"], "approved_by": "Lan",
                               "snapshot_id": "dsar-test-pre", "actor": "admin:test"}, env=ON)
    assert res["mode"] == "apply" and res["verified"] is True, res.get("verified_left")
    assert await _learn_counts(admin, mine) == {t: 0 for t in LEARN_TABLES}
    assert await _learn_counts(admin, theirs) == full
    assert await _learn_counts(admin, staff) == full
    # the purge flag lived in the apply's transaction only: the append-only guard still refuses a plain DELETE
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await admin.execute("DELETE FROM learn.statement WHERE learner_id = $1::uuid", theirs)
