"""AA-920 WM-26 — the post-activation drill on real Postgres (CIS 280 / 287 / 292 applied), $0, invented text only.

A1 platform: release 0 active with memory on, then release 1 (one approved writer_instruction) with its R2 and shadow
runs, approved and activated. Two S1 writer v2 rows are written "under" the view `insertion.resolve` returns now
(the stamp of the real resolver, with the fingerprint): one clean, one with a QA trace (a ledger key in the text).
The check in DRY RUN reports the violation and moves nothing; with apply it rolls A1 back to release 0, writes the
audit row and the admin bell, and the next resolve gives release 0 (the next write stamps the previous release).
Runs inside the 287 file's fixture (its stand-ins and grants), one transaction, rolled back.
"""
import json
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from _constants import BATCH_ID, TENANT_ID  # noqa: E402
from test_aa919_migration_287 import ADMIN, _release0, _run, conn  # noqa: E402,F401 — `conn` is the fixture
from services.writing_memory import insertion  # noqa: E402
from services.writing_memory import post_activation as pa  # noqa: E402
from services.writing_memory import releases as rel  # noqa: E402


class _ConnPool:
    """One connection as a pool: the check's own transactions become savepoints of the test's transaction."""

    def __init__(self, c):
        self.c = c

    def acquire(self):
        c = self.c

        class _A:
            async def __aenter__(self):
                return c

            async def __aexit__(self, *exc):
                return False
        return _A()


async def _a1_release1(conn):
    await _release0(conn, "a1")
    rule = await conn.fetchval(
        "INSERT INTO shared.writing_rule (kind, scope, writer, cluster_key, rule_text, status, approved_by, "
        "approved_at) VALUES ('writer_instruction', 'platform', 'a1', $1, 'Cut a closing line that only restates "
        "the title.', 'approved', 'x', now()) RETURNING rule_id::text", f"zz920-{uuid.uuid4().hex[:6]}")
    d = await rel.create_draft(conn, writer="a1", rule_ids=[rule], created_by=f"admin:{ADMIN}")
    r2 = await _run(conn, "a1", "r2", release=d["release_id"], details={"candidate": "release"})
    sh = await _run(conn, "a1", "shadow", release=d["release_id"])
    await rel.attach_evidence(conn, d["release_id"], eval_run_id=r2, shadow_run_id=sh, actor=f"admin:{ADMIN}")
    await rel.approve(conn, d["release_id"], approved_by=ADMIN)
    return await rel.activate(conn, d["release_id"], actor=f"admin:{ADMIN}")


async def _a1_write(conn, stamp, summary):
    tour = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, country, src_name, src_subtitle, "
        "src_summary, src_highlights, src_itineraries, pipeline_status) VALUES ($1::uuid, $2::uuid, $3::uuid, "
        "'Nepal', 'Invented ridge walk', 'Three quiet days', 'An invented summary.', '[]', '[]', 'seo_done')",
        tour, TENANT_ID, BATCH_ID)
    return await conn.fetchval(
        "INSERT INTO silver_aa_internal.generated_content (id, tour_id, tenant_id, version_num, aa_name, aa_summary, "
        "model_editorial, model_schema, prompt_version, retry_count, status, metadata) VALUES (gen_random_uuid(), "
        "$1::uuid, $2::uuid, 1, 'Invented ridge walk', $3, 'm', 'm', 'v', 0, 'draft', $4::jsonb) RETURNING id::text",
        tour, TENANT_ID, summary, json.dumps({"writer_version": "v2", "writing_release": stamp}))


@pytest.mark.asyncio
async def test_seeded_violation_rolls_a1_back_and_the_next_write_uses_release0(conn):
    r1 = await _a1_release1(conn)
    view = await insertion.resolve(conn, "a1")
    assert view["platform"]["release_id"] == r1["release_id"]
    assert insertion.a1_house_rules(view).endswith("- Cut a closing line that only restates the title.")
    stamp = insertion.stamp(view, writer_version="v2")
    assert stamp["platform"]["items_sha256"] == r1["items_sha256"] and stamp["ignored"] is False
    await _a1_write(conn, stamp, "Two quiet days on an invented ridge.")
    bad = await _a1_write(conn, stamp, "Two quiet days. gate_ledger: passed.")

    dry = await pa.check_release(_ConnPool(conn), r1["release_id"])
    assert dry["verdict"] == "violation" and dry["action"] == "dry_run" and dry["carried"] == 2
    assert dry["violating_refs"] == [{"source": "generated_content", "ref": bad, "codes": ["qa_trace"]}]
    assert (await rel.get_release(conn, r1["release_id"]))["status"] == "active"

    out = await pa.check_release(_ConnPool(conn), r1["release_id"], apply=True, actor="test:aa920")
    assert out["action"] == "rolled_back" and out["active_release_no"] == 0
    after = await rel.get_release(conn, r1["release_id"])
    assert after["status"] == "retired" and after["retired_reason"].startswith("rollback: post_activation: qa_trace")
    audit = await conn.fetchval(
        "SELECT details FROM acp_shared.audit_log WHERE action = 'writing_memory.release_post_activation_failed' "
        "AND resource_id = $1", r1["release_id"])
    assert audit is not None and "gate_ledger: passed" not in str(audit)            # ids and codes, never text
    assert await conn.fetchval("SELECT count(*) FROM shared.notifications WHERE event_type = 'writing_memory_rollback' "
                               "AND entity_id = $1", r1["release_id"]) == 1
    nxt = insertion.stamp(await insertion.resolve(conn, "a1"))
    assert nxt["platform"]["release_no"] == 0                                         # the next write: release 0
    # the job's sweep finds nothing left to check (the rolled-back release is retired)
    assert r1["release_id"] not in await pa.content_releases(conn)
