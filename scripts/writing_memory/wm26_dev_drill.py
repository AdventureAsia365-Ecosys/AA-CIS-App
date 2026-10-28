"""AA-920 WM-26 — the post-activation drill on Dev: a seeded violation rolls the A1 release back by itself, and the next
write resolves (stamps) the previous release. $0: no model call (`services.golden_set.llm_guard` makes every model
entry point raise), invented text only, and ONE transaction that is always ROLLED BACK: nothing stays on Dev (no rule,
no release, no evidence run, no content row, no bell, no e-mail job), and the A1 pointer is where it was.

What it runs, on the real Dev schema, triggers and grants (the container's DATABASE_URL = cis_app):
  1. Precondition: the active A1 platform release is an empty one (release 0). A live A1 release with rules means
     real traffic depends on it: the drill refuses (`refused: live_content_release`) and changes nothing.
  2. Inside the transaction: memory on for A1 (if off), one approved `writer_instruction` (invented text), a release
     draft with it, its R2 and shadow runs (marked `wm26_dev_drill`), evidence, approval, activation: release N+1.
  3. `insertion.resolve(conn, "a1")` -> the view of release N+1; two S1 writer v2 rows written under its stamp on an
     existing v2 tour: one clean, one with a QA trace (a ledger key in the text).
  4. `post_activation.check_release` DRY RUN -> violation, nothing moves; then with `apply` (what the hourly job does)
     -> the pointer goes back to release N (`rolled_back`), the audit row, the admin bell and the e-mail job are
     written; the next `resolve` stamps release N; the hourly sweep no longer lists release N+1.
  5. ROLLBACK, then a read outside the transaction: the A1 pointer is the release it was before, release N+1 does not
     exist.

The report holds ids, numbers, codes and counts only, never text. Exit code 0 only when every step held.

Usage (S3-mediated ECS exec on aa-cis-dev-api, the image with AA-920 #1099 or later; no S3 write, stdout only):
  python3 /tmp/s.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from typing import Any, Optional

SCHEMA = "wm26-dev-drill/1"
DRILL = "wm26_dev_drill"
# A shared.admin_users-shaped id for the drill's approval (a rules release needs a human id): never a real admin,
# and the row it lands on is rolled back with the rest.
DRILL_APPROVER = "00000000-0000-4000-8000-00000000a926"
RULE_TEXT = "Cut a closing line that only restates the title."
CLEAN_SUMMARY = "Two quiet days on an invented ridge, with a slow descent to the river."
TRACE_SUMMARY = "Two quiet days on an invented ridge. gate_ledger: passed."


class DrillFailed(Exception):
    pass


class ConnPool:
    """One connection as a pool: the check's own transactions become savepoints of the drill's transaction, so its
    rollback, audit row, bell and e-mail job are all undone with it."""

    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Acquired:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False
        return _Acquired()


def _need(ok: bool, what: str) -> None:
    if not ok:
        raise DrillFailed(what)


async def _active_a1(conn) -> Optional[dict]:
    row = await conn.fetchrow(
        "SELECT release_id::text AS release_id, release_no, content_class, cardinality(rule_ids) AS n_rules "
        "FROM shared.writing_memory_release WHERE writer = 'a1' AND scope = 'platform' AND status = 'active'")
    return dict(row) if row else None


async def _run_row(conn, kind: str, release_id: str) -> str:
    return str(await conn.fetchval(
        "INSERT INTO shared.prompt_eval_runs (pipeline, prompt_version, tour_count, cost_usd, details, triggered_by, "
        "run_kind, writer, stop_reason, verdict, finished_at, release_id) VALUES ('writing_memory', $1, 0, 0, "
        "$2::jsonb, $1, $3, 'a1', 'completed', 'pass', now(), $4::uuid) RETURNING id",
        DRILL, json.dumps({"candidate": "release", "drill": DRILL}), kind, release_id))


async def _a1_write(conn, tour_id: str, tenant_id: str, stamp: dict, summary: str) -> str:
    version = await conn.fetchval(
        "SELECT coalesce(max(version_num), 0) + 1 FROM silver_aa_internal.generated_content WHERE tour_id = $1::uuid",
        tour_id)
    return await conn.fetchval(
        "INSERT INTO silver_aa_internal.generated_content (id, tour_id, tenant_id, version_num, aa_name, aa_summary, "
        "model_editorial, model_schema, prompt_version, retry_count, status, metadata) VALUES (gen_random_uuid(), "
        "$1::uuid, $2::uuid, $3, 'Invented ridge walk', $4, $5, $5, $5, 0, 'draft', $6::jsonb) RETURNING id::text",
        tour_id, tenant_id, version, summary, DRILL,
        json.dumps({"writer_version": "v2", "writing_release": stamp, "drill": DRILL}))


async def drill(conn) -> dict:
    """The drill inside the caller's open transaction (the caller rolls it back). Returns the report."""
    from services.writing_memory import insertion
    from services.writing_memory import post_activation as pa
    from services.writing_memory import releases as rel

    actor = f"admin:{DRILL_APPROVER}"
    rep: dict[str, Any] = {"steps": {}}
    await conn.execute("SET LOCAL lock_timeout = '5s'")
    await conn.execute("SET LOCAL statement_timeout = '60s'")

    before = await _active_a1(conn)
    _need(before is not None, "no active A1 platform release (release 0 is created by the release-0 CLI)")
    rep["before"] = {"release_id": before["release_id"], "release_no": before["release_no"],
                     "content_class": before["content_class"]}
    if before["content_class"] != "empty" or before["n_rules"]:
        rep["refused"] = "live_content_release"
        return rep
    memory_on = await conn.fetchval("SELECT memory_enabled FROM shared.writing_memory_writer WHERE writer = 'a1'")
    rep["memory_enabled_before"] = bool(memory_on)
    if not memory_on:
        await rel.set_memory_enabled(conn, "a1", True, actor=actor)

    rule = await conn.fetchval(
        "INSERT INTO shared.writing_rule (kind, scope, writer, cluster_key, rule_text, status, approved_by, "
        "approved_at) VALUES ('writer_instruction', 'platform', 'a1', $1, $2, 'approved', $3, now()) "
        "RETURNING rule_id::text", f"{DRILL}-{uuid.uuid4().hex[:8]}", RULE_TEXT, DRILL_APPROVER)
    draft = await rel.create_draft(conn, writer="a1", rule_ids=[rule], created_by=actor, notes=DRILL)
    r2 = await _run_row(conn, "r2", draft["release_id"])
    shadow = await _run_row(conn, "shadow", draft["release_id"])
    await rel.attach_evidence(conn, draft["release_id"], eval_run_id=r2, shadow_run_id=shadow, actor=actor)
    await rel.approve(conn, draft["release_id"], approved_by=DRILL_APPROVER, approval_ref=f"{DRILL} (rolled back)")
    r1 = await rel.activate(conn, draft["release_id"], actor=actor)
    rep["drill_release"] = {"release_id": r1["release_id"], "release_no": r1["release_no"],
                            "content_class": r1["content_class"], "previous_release_id": r1["previous_release_id"]}
    _need(str(r1["previous_release_id"]) == before["release_id"], "activation did not supersede the active release")
    rep["steps"]["activated"] = True

    view = await insertion.resolve(conn, "a1")
    _need(bool(view) and view["platform"]["release_id"] == r1["release_id"], "resolve did not return the new release")
    _need(insertion.a1_house_rules(view).endswith(f"- {RULE_TEXT}"), "the HOUSE RULES slot did not render the rule")
    stamp = insertion.stamp(view, writer_version="v2")
    _need(stamp["platform"]["items_sha256"] == r1["items_sha256"] and not stamp["ignored"], "stamp fingerprint")
    rep["steps"]["resolved_and_rendered"] = True

    tour = await conn.fetchrow(
        "SELECT tour_id::text AS tour_id, tenant_id::text AS tenant_id FROM silver_aa_internal.generated_content "
        "WHERE metadata->>'writer_version' = 'v2' ORDER BY created_at DESC LIMIT 1")
    _need(tour is not None, "no S1 writer v2 row on Dev to attach the drill writes to")
    clean = await _a1_write(conn, tour["tour_id"], tour["tenant_id"], stamp, CLEAN_SUMMARY)
    bad = await _a1_write(conn, tour["tour_id"], tour["tenant_id"], stamp, TRACE_SUMMARY)
    rep["writes"] = {"clean": clean, "seeded_violation": bad}

    pool = ConnPool(conn)
    dry = await pa.check_release(pool, r1["release_id"])
    rep["dry_run"] = {k: dry.get(k) for k in ("verdict", "action", "checked", "carried", "violations",
                                              "violation_codes", "violating_refs", "cost_usd")}
    _need(dry["verdict"] == "violation" and dry["action"] == "dry_run", "the dry run did not report the violation")
    _need({"source": "generated_content", "ref": bad, "codes": ["qa_trace"]} in dry["violating_refs"],
          "the seeded write is not the violating one")
    _need(clean not in {v["ref"] for v in dry["violating_refs"]}, "the clean write was flagged")
    _need((await rel.get_release(conn, r1["release_id"]))["status"] == "active", "the dry run moved the pointer")
    rep["steps"]["dry_run_moved_nothing"] = True

    out = await pa.check_release(pool, r1["release_id"], apply=True, actor=f"job:{DRILL}")
    rep["apply"] = {k: out.get(k) for k in ("verdict", "action", "active_release_id", "active_release_no", "cost_usd")}
    _need(out["action"] == "rolled_back" and out["active_release_id"] == before["release_id"],
          f"apply did not roll back to the previous release (action {out.get('action')})")
    after = await rel.get_release(conn, r1["release_id"])
    _need(after["status"] == "retired" and str(after.get("retired_reason") or "").startswith(
        "rollback: post_activation: qa_trace"), "the release was not retired with the rollback reason")
    audit = await conn.fetchval(
        "SELECT count(*) FROM acp_shared.audit_log WHERE action = 'writing_memory.release_post_activation_failed' "
        "AND resource_id = $1", r1["release_id"])
    bell = await conn.fetchval(
        "SELECT count(*) FROM shared.notifications WHERE event_type = 'writing_memory_rollback' AND entity_id = $1",
        r1["release_id"])
    email = await conn.fetchval(
        "SELECT count(*) FROM shared.job WHERE kind = 'ops_alert_email' AND payload->>'release_id' = $1",
        r1["release_id"])
    rep["alerts"] = {"audit_rows": audit, "bell_rows": bell, "email_jobs": email}
    _need(audit == 1 and bell == 1, "audit row or bell missing")
    nxt = insertion.stamp(await insertion.resolve(conn, "a1"))
    rep["next_write_stamp"] = {"release_id": nxt["platform"]["release_id"],
                               "release_no": nxt["platform"]["release_no"]}
    _need(nxt["platform"]["release_id"] == before["release_id"], "the next write does not stamp the previous release")
    _need(r1["release_id"] not in await pa.content_releases(conn), "the hourly sweep still lists the drill release")
    rep["steps"]["rolled_back_and_next_write_uses_previous"] = True
    return rep


async def run() -> dict:
    import asyncpg

    from shared.secrets import get_database_url

    ssl: Any = None if os.environ.get("WM26_DB_SSL", "require") == "disable" else "require"
    conn = await asyncpg.connect(get_database_url().strip(), ssl=ssl)
    try:
        llm_before = await conn.fetchval("SELECT count(*) FROM shared.llm_call_log")
        tr = conn.transaction()
        await tr.start()
        try:
            rep = await drill(conn)
        except DrillFailed as e:
            rep = {"failed": str(e)}
        finally:
            await tr.rollback()
        drill_id = (rep.get("drill_release") or {}).get("release_id")
        now = await _active_a1(conn)
        rep["after_rollback"] = {
            "active_release_id": now and now["release_id"], "active_release_no": now and now["release_no"],
            "pointer_unchanged": bool(now) and now["release_id"] == (rep.get("before") or {}).get("release_id"),
            "drill_release_rows": (await conn.fetchval(
                "SELECT count(*) FROM shared.writing_memory_release WHERE release_id = $1::uuid", drill_id)
                if drill_id else 0),
        }
        # other services may log model calls meanwhile; the drill's own process cannot (llm_guard)
        llm_after = await conn.fetchval("SELECT count(*) FROM shared.llm_call_log")
        rep["llm_call_log_rows_meanwhile"] = llm_after - llm_before
    finally:
        await conn.close()
    rep["schema"] = SCHEMA
    rep["git_sha"] = os.environ.get("GIT_SHA") or os.environ.get("APP_GIT_SHA")
    rep["ok"] = ("failed" not in rep and "refused" not in rep and rep["after_rollback"]["pointer_unchanged"]
                 and rep["after_rollback"]["drill_release_rows"] == 0)
    return rep


def main() -> int:
    sys.path.insert(0, "/app")
    from services.golden_set import llm_guard
    llm_guard.install(DRILL)
    rep = asyncio.run(run())
    print(json.dumps(rep, indent=1, sort_keys=True, default=str))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
