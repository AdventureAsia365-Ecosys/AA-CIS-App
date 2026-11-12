"""Dev-only fixtures for the live E2E write checks (AA-1087 A10b-3 / A10e, AA-1070 crm-13; S278 lane l18).

The S278 live verify could not prove two things on Dev:

1. **The CRM's E2E agency is not a CIS tenant.** AA-Partner seeds its synthetic agencies under fixed UUIDv5 ids
   (``api/app/seed/catalogue.py``: ``uuid5(SYNTHETIC_NAMESPACE, "aa-crm-synthetic-agency:<key>")``); CIS never held
   them, so ``catalog.v_tenant_account_v1.is_synthetic`` has no row for them and the Booking E2E write identities refuse
   their records (403 ``e2e_synthetic_only``). ``register-crm-agency`` makes CIS hold one, through the tenant
   provisioning saga (``tenant_provision`` job, mode ``create`` with ``tenant_id``, Dev + ``demo`` only): a ``demo``
   tenant is ``is_canary`` (synthetic), its content plan gives ``trade.book``, so Booking's ``bk_tenant_pull`` adopts
   the agency's existing partner row (active) and the CRM keeps its agency (no CRM add-on: its own L1 features stay).
2. **No open subject gate flag** for the A10b E2E admin to release. ``subject-gate-flag`` adds one new attempt to the
   latest piece request of a SYNTHETIC tenant, whose text carries a booking link to an invented host, and runs the
   real subject gate on it (``services.acp_shared.subject_gate.run``): the gate opens the ``subject_gate`` flag.

Rules: ``ENVIRONMENT`` exactly ``dev`` (else exit 2 before connecting); DRY RUN by default (``--apply`` writes);
synthetic tenants only (``is_canary`` and not the pilot, ``api.core.e2e_staff.is_synthetic_tenant``; a real tenant is
exit 3); idempotent (the saga's request key; the piece carries ``E2E fixture (synthetic) run <run>``). No LLM call.

Run in the CIS API container (``DATABASE_URL`` = role ``cis_app``) through ECS Exec on ``aa-cis-dev-api``::

    python -m services.tenant_account.e2e_fixtures register-crm-agency --key sen-vang [--apply]
    python -m services.tenant_account.e2e_fixtures subject-gate-flag [--tenant <uuid>] [--run <run>] [--apply]

Output: one JSON object (ids, codes, the piece's content sha256 the release needs; no tenant content).
TEMPORARY like A10b / A10e: removed with the A10 family at go-live (AA-1054).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

import asyncpg

from api.core import e2e_staff
from services.acp_shared import subject_gate
from services.jobs import tenant_provision_job
from services.tenant_account import dev_guard, provision

# AA-Partner api/app/seed/catalogue.py SYNTHETIC_NAMESPACE (fixed; the ids must match the CRM's).
CRM_SYNTHETIC_NAMESPACE = uuid.UUID("6f1d3c2e-9a7b-5c4d-8e2f-a935c0de0007")
MARK = "E2E fixture (synthetic)"
RUN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,30}$")
ACTOR = "system:e2e_fixture"
PLAN_TIER = "growth"


@dataclass(frozen=True)
class CrmAgency:
    key: str
    name: str

    @property
    def tenant_id(self) -> str:
        return str(uuid.uuid5(CRM_SYNTHETIC_NAMESPACE, f"aa-crm-synthetic-agency:{self.key}"))

    @property
    def slug(self) -> str:
        return f"crm-{self.key}"

    @property
    def request_key(self) -> str:
        return f"aa1087-register-{self.key}"


# The CRM's E2E agencies (same keys and names as AA-Partner's seed catalogue).
CRM_AGENCIES = {
    a.key: a
    for a in (
        CrmAgency("sen-vang", "Lữ hành Sen Vàng"),
        CrmAgency("southern-cross", "Southern Cross Journeys"),
        CrmAgency("harbour-lights", "Harbour Lights Travel Co."),
    )
}

TENANT_SQL = "SELECT tenant_id::text AS tenant_id, name, is_canary, is_pilot, kind FROM shared.tenants " \
             "WHERE tenant_id = $1::uuid"
JOB_SQL = ("SELECT id::text AS id, status FROM shared.job WHERE kind = $1 AND idempotency_key = $2 "
           "ORDER BY created_at DESC LIMIT 1")
# A synthetic tenant with a finished piece to copy the request from (the newest first).
SYNTHETIC_WITH_PIECE_SQL = """
    SELECT t.tenant_id::text AS tenant_id FROM shared.tenants t
     WHERE t.is_canary IS TRUE AND t.is_pilot IS NOT TRUE AND t.is_active
       AND EXISTS (SELECT 1 FROM acp_shared.content_piece cp
                    WHERE cp.tenant_id = t.tenant_id AND cp.status IN ('approved', 'held'))
     ORDER BY t.created_at, t.tenant_id LIMIT 1
"""
LATEST_PIECE_SQL = """
    SELECT cp.angle_gate_request_id::text AS request_id, COALESCE(cp.channel, r.channel) AS channel
      FROM acp_shared.content_piece cp
      JOIN acp_shared.angle_gate_request r ON r.request_id = cp.angle_gate_request_id
     WHERE cp.tenant_id = $1::uuid AND cp.status IN ('approved', 'held')
     ORDER BY cp.created_at DESC LIMIT 1
"""
RUN_PIECE_SQL = """
    SELECT cp.piece_id::text AS piece_id, cp.content_text FROM acp_shared.content_piece cp
     WHERE cp.tenant_id = $1::uuid AND cp.content_summary = $2
     ORDER BY cp.created_at DESC LIMIT 1
"""
OPEN_FLAG_SQL = """
    SELECT flag_id::text AS flag_id FROM acp_shared.content_compliance_flag
     WHERE piece_id = $1::uuid AND kind = 'subject_gate' AND state = 'open'
"""
INSERT_PIECE_SQL = """
    INSERT INTO acp_shared.content_piece
        (tenant_id, angle_gate_request_id, attempt_number, content_text, status, gate_ledger, repair_log,
         discarded_attempts, channel, content_summary, seo_title)
    SELECT $1::uuid, $2::uuid,
           COALESCE((SELECT max(attempt_number) FROM acp_shared.content_piece WHERE angle_gate_request_id = $2::uuid),
                    0) + 1,
           $3, 'approved', '{}'::jsonb, '[]'::jsonb, '[]'::jsonb, $4, $5, $6
    RETURNING piece_id::text
"""


class Refused(Exception):
    """The run cannot go ahead (exit 3)."""


def fixture_text(run: str) -> str:
    """Invented copy with one booking link to an invented host: the subject gate flags it (off_catalog_link)."""
    return (
        f"Lantern Cove at dawn: tide pools, a slow breakfast and a boat back by noon.\n\n"
        f"Book this escape now at https://booking.lantern-cove.invalid/tours/dawn-{run} before the dates fill up."
    )


def default_run(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y%m%d%H%M")


async def register_crm_agency(conn, pool, key: str, *, apply: bool) -> dict[str, Any]:
    agency = CRM_AGENCIES.get(key)
    if agency is None:
        raise Refused(f"unknown CRM agency key {key!r} (one of {', '.join(sorted(CRM_AGENCIES))})")
    out: dict[str, Any] = {"key": key, "tenant_id": agency.tenant_id, "slug": agency.slug}
    existing = await conn.fetchrow(TENANT_SQL, agency.tenant_id)
    if existing is not None:
        synthetic = await e2e_staff.is_synthetic_tenant(conn, agency.tenant_id)
        if not synthetic:
            raise Refused(f"CIS holds {agency.tenant_id} but not as a synthetic tenant: fix it by hand, not here")
        return {**out, "state": "registered", "synthetic": True}
    req = provision.parse({
        "request_key": agency.request_key, "mode": "create", "name": agency.name, "slug": agency.slug,
        "plan_tier": PLAN_TIER, "posts_per_week": 1, "kind": "demo", "tenant_id": agency.tenant_id,
        "requested_by": "e2e-fixture",
    })
    job = await conn.fetchrow(JOB_SQL, provision.KIND, req.idempotency_key)
    if not apply:
        return {**out, "state": "job_" + job["status"] if job else "would_enqueue", "request": req.to_payload()}
    job_id, created = await tenant_provision_job.enqueue_request(pool, req)
    return {**out, "state": "enqueued" if created else "job_exists", "job_id": job_id}


async def subject_gate_flag(conn, *, tenant: Optional[str], run: str, apply: bool) -> dict[str, Any]:
    tenant_id = tenant or await conn.fetchval(SYNTHETIC_WITH_PIECE_SQL)
    if tenant_id is None:
        raise Refused("no synthetic tenant with a finished content piece")
    if not await e2e_staff.is_synthetic_tenant(conn, tenant_id):
        raise Refused(f"tenant {tenant_id} is not synthetic (is_canary and not the pilot)")
    marker = f"{MARK} run {run}"
    out: dict[str, Any] = {"tenant_id": str(tenant_id), "run": run}
    piece = await conn.fetchrow(RUN_PIECE_SQL, tenant_id, marker)
    if piece is not None:
        flag = await conn.fetchval(OPEN_FLAG_SQL, piece["piece_id"])
        return {**out, "state": "exists", "piece_id": piece["piece_id"], "flag_id": flag,
                "content_sha256": subject_gate.content_sha256(piece["content_text"])}
    source = await conn.fetchrow(LATEST_PIECE_SQL, tenant_id)
    if source is None:
        raise Refused(f"tenant {tenant_id} has no finished content piece to add an attempt to")
    text = fixture_text(run)
    if not apply:
        return {**out, "state": "would_create", "request_id": source["request_id"]}
    async with conn.transaction():
        piece_id = await conn.fetchval(INSERT_PIECE_SQL, tenant_id, source["request_id"], text, source["channel"],
                                       marker, f"{MARK} {run}")
        result = await subject_gate.run(
            conn,
            piece={"piece_id": piece_id, "tenant_id": str(tenant_id), "request_id": source["request_id"],
                   "content_text": text, "channel": source["channel"]},
            stage="write", actor=ACTOR)
        if result.ok:
            raise Refused("the subject gate passed the fixture text (no flag): the gate rules changed")
        flag = await conn.fetchval(OPEN_FLAG_SQL, piece_id)
    return {**out, "state": "created", "piece_id": piece_id, "flag_id": flag, "codes": result.codes,
            "content_sha256": subject_gate.content_sha256(text)}


async def _main(args: argparse.Namespace) -> int:
    if not dev_guard.on_dev():
        print(json.dumps({"error": "refused: ENVIRONMENT is not exactly 'dev' (Dev-only fixtures)"}))
        return 2
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print(json.dumps({"error": "DATABASE_URL not set"}))
        return 2
    pool = await asyncpg.create_pool(dsn, ssl=os.environ.get("DB_SSL", "require"), min_size=1, max_size=2,
                                     timeout=10, server_settings={"application_name": "cis-e2e-fixtures"})
    try:
        async with pool.acquire() as conn:
            if args.command == "register-crm-agency":
                result = await register_crm_agency(conn, pool, args.key, apply=args.apply)
            else:
                result = await subject_gate_flag(conn, tenant=args.tenant, run=args.run, apply=args.apply)
    except (Refused, provision.ProvisionError) as exc:
        print(json.dumps({"error": f"refused: {exc}"}))
        return 3
    finally:
        await pool.close()
    print(json.dumps({"mode": "APPLY" if args.apply else "DRY RUN", "result": result}, indent=1, default=str,
                     ensure_ascii=False))
    return 0


def _run_arg(value: str) -> str:
    if not RUN_RE.match(value):
        raise argparse.ArgumentTypeError("a run is 3-31 lower-case letters, digits or hyphens")
    return value


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Dev-only fixtures for the CIS / Booking E2E write identities.")
    sub = parser.add_subparsers(dest="command", required=True)
    reg = sub.add_parser("register-crm-agency", help="make CIS hold a CRM E2E agency as a synthetic tenant")
    reg.add_argument("--key", required=True, choices=sorted(CRM_AGENCIES))
    reg.add_argument("--apply", action="store_true")
    flag = sub.add_parser("subject-gate-flag", help="open one subject gate flag on a synthetic tenant")
    flag.add_argument("--tenant", default=None, type=lambda v: str(uuid.UUID(v)))
    flag.add_argument("--run", type=_run_arg, default=default_run())
    flag.add_argument("--apply", action="store_true")
    return asyncio.run(_main(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
