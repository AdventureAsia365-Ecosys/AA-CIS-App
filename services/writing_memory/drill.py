"""AA-919 WM-18 — the rollback drill ($0, no model call): proof that after a rollback the next write uses the previous
release, read from the stamp the writer would record.

On one tenant (an E2E test tenant, never a demo agency: plan v2 2.5) and one writer: tenant release 0 and tenant
release 1, both empty (they reuse the writer's passing R0 run and its AA-729 baseline run: an empty release needs no
other evidence), approved with the human decision in `approval_ref`, activated in order; the writer's resolve step
(`insertion.resolve`, what `start_write` / the T2 job / the A1 run call) then gives release 1; `releases.rollback`
moves the pointer back; the same resolve gives release 0. For T9 the stamp is built by `writer_input_snapshot`, the
function that writes `content_piece.writer_input`. No platform release 1 is ever created (it would shift the
platform numbering for good), and the tenant's releases stay as the drill leaves them (release 0 active).

    python -m services.writing_memory.drill --tenant <uuid> --writer t9_social --approval-ref <link> [--apply]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Optional

from services.writing_memory import insertion
from services.writing_memory import releases as rel

_EVIDENCE_SQL = """
    SELECT (SELECT r0_run_id::text FROM shared.writing_memory_release
             WHERE writer = $1 AND scope = 'platform' AND status = 'active') AS r0_run_id,
           (SELECT eval_run_id::text FROM shared.writing_memory_release
             WHERE writer = $1 AND scope = 'platform' AND status = 'active') AS eval_run_id
"""


def stamp_for(writer: str, view: Optional[dict]) -> Optional[dict]:
    """The stamp the writer records for this view: T9 through writer_input_snapshot (content_piece.writer_input),
    A1 / T2 through insertion.stamp (metadata / job result)."""
    if writer.startswith("t9_"):
        from services.acp_content_writing.service import writer_input_snapshot
        return writer_input_snapshot({"writing_memory": view} if view else {}).get("writing_release")
    return insertion.stamp(view)


async def rollback_drill(conn, *, writer: str, tenant_id: str, actor: str, approval_ref: str) -> dict:
    """Run the drill in the caller's transaction; returns the two stamps and the release ids."""
    if writer == "a1":
        raise rel.ReleaseError(422, "invalid", "A1 has no tenant releases; drill a tenant writer")
    ev = await conn.fetchrow(_EVIDENCE_SQL, writer)
    if not ev or not ev["r0_run_id"] or not ev["eval_run_id"]:
        raise rel.ReleaseError(409, "no_platform_release0", f"{writer} has no active platform release 0 with evidence")
    made = []
    for _ in range(2):
        d = await rel.create_draft(conn, writer=writer, scope="tenant", tenant_id=tenant_id, created_by=actor,
                                   notes="WM-18 rollback drill ($0)")
        await rel.attach_evidence(conn, d["release_id"], r0_run_id=ev["r0_run_id"], eval_run_id=ev["eval_run_id"],
                                  actor=actor)
        await rel.approve(conn, d["release_id"], approved_by=actor, approval_ref=approval_ref, allow_agent=True)
        await rel.activate(conn, d["release_id"], actor=actor)
        made.append(d)
    before = stamp_for(writer, await insertion.resolve(conn, writer, tenant_id))
    current = made[-1]
    back = await rel.rollback(conn, current["release_id"], actor=actor, reason="WM-18 rollback drill")
    after = stamp_for(writer, await insertion.resolve(conn, writer, tenant_id))
    return {"writer": writer, "tenant_id": tenant_id, "releases": [m["release_id"] for m in made],
            "release_numbers": [m["release_no"] for m in made], "stamp_before_rollback": before,
            "active_after_rollback": back["release_id"], "stamp_after_rollback": after,
            "next_write_uses_previous": bool(after and after.get("tenant")
                                             and after["tenant"]["release_id"] == back["release_id"]
                                             and before and before.get("tenant")
                                             and before["tenant"]["release_id"] == current["release_id"])}


async def _main(args) -> dict:
    import asyncpg

    from shared.secrets import get_database_url

    ssl: Any = None if os.environ.get("WM20_DB_SSL", "require") == "disable" else "require"
    conn = await asyncpg.connect(get_database_url().strip(), ssl=ssl)
    try:
        tr = conn.transaction()
        await tr.start()
        try:
            out = await rollback_drill(conn, writer=args.writer, tenant_id=args.tenant, actor=args.actor,
                                       approval_ref=args.approval_ref)
        except Exception:
            await tr.rollback()
            raise
        if args.apply:
            await tr.commit()
        else:
            await tr.rollback()
        out["mode"] = "apply" if args.apply else "dry_run (rolled back)"
        out["llm_calls"] = 0
        return out
    finally:
        await conn.close()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="AA-919 WM-18 rollback drill ($0)")
    ap.add_argument("--tenant", required=True, help="an E2E test tenant id")
    ap.add_argument("--writer", default="t9_social", choices=("t2", "t9_blog", "t9_social"))
    ap.add_argument("--actor", default="agent:wm18-drill")
    ap.add_argument("--approval-ref", required=True, help="the link of the human decision approving the drill")
    ap.add_argument("--apply", action="store_true", help="commit (default: run and roll back)")
    args = ap.parse_args(argv)
    if not args.actor.startswith("agent:"):
        raise SystemExit("--actor must be agent:<session>")
    sys.path.insert(0, "/app")
    from services.golden_set import llm_guard
    llm_guard.install("wm18_drill")
    print(json.dumps(asyncio.run(_main(args)), indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
