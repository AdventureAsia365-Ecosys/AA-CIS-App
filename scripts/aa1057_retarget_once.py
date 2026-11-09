#!/usr/bin/env python3
"""AA-1057 GAP 2 one-off: move picked / used Route Subjects off superseded Routes and angle-gate requests off atoms
that are no longer live, with the rule of the ``retarget`` recompute stage (services/acp_shared/retarget.py).

S273 audit: 7 used Subjects on a superseded Route, 3 requests on deleted atoms (2 approved, 1 pending_choice). The
stage ships in ``report`` mode (it plans, writes nothing) so this script is the audited first run (DRY RUN procedure,
skill/aa-cis-schema/references/destructive-ops.md: rows are re-pointed, nothing is deleted); after it, a follow-up
change makes the stage's default ``apply`` and the stage keeps the count at 0.

Default = DRY RUN (read only). Prints a JSON summary between AA1057-JSON-BEGIN and AA1057-JSON-END:
  - ``subjects`` / ``requests``: rows found stale, the count per outcome (subjects: retarget, taken, ambiguous,
    no_match; requests: repoint, reseed, ambiguous, no_match, no_atom, paused) and ``pin``, a sha256 fingerprint of
    the ids an apply would change (the apply must see the same set);
  - ``rules``: how many changes each rule makes (identity / overlap / place* / card* / segment_seed).
  Ids are not printed in a DRY RUN; no tenant text is read.

--apply --expect-subjects N --expect-requests M --expect-subject-pin P --expect-request-pin Q (all four from the
DRY RUN): plans again and refuses unless exactly the reviewed rows would change (the counts AND the id pins: a set
that changed with the same size is refused too), prints the restore list (ids and the old value of every column it
writes: route_id; atom_id, and for a re-seed trip_id and route_segment_ids) BEFORE writing, applies everything in ONE
transaction, refuses and rolls back unless exactly N + M rows changed, then re-plans on a NEW connection (the verify:
0 changes left). With --restore-file PATH the restore list is also written there before the UPDATE.

Runs in the CIS api or worker container (role cis_app: SELECT / UPDATE on acp_shared.subject and
acp_shared.angle_gate_request, SELECT on the read tables) after the deploy that carries the module:
  tools/ecs/ecsx.sh apps/AA-CIS-App/scripts/aa1057_retarget_once.py                  (DRY RUN)
  tools/ecs/ecsx.sh apps/AA-CIS-App/scripts/aa1057_retarget_once.py \
      ARGS="--apply --expect-subjects N --expect-requests M --expect-subject-pin P --expect-request-pin Q"
ecsx passes VAR=value pairs as environment (not arguments), so the arguments come from env ARGS.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import sys
from typing import Any

import asyncpg

sys.path.insert(0, os.getcwd())

from services.acp_shared import retarget as rt  # noqa: E402


def _rules(plan: rt.Plan) -> dict[str, int]:
    out: dict[str, int] = {}
    for d in plan.subject_changes + plan.request_changes:
        out[d["rule"] or "-"] = out.get(d["rule"] or "-", 0) + 1
    return out


def summary(plan: rt.Plan) -> dict[str, Any]:
    return {**plan.summary(), "rules": _rules(plan)}


def emit(payload: dict[str, Any]) -> None:
    print("AA1057-JSON-BEGIN")
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    print("AA1057-JSON-END")


async def connect() -> asyncpg.Connection:
    return await asyncpg.connect(os.environ["DATABASE_URL"], ssl=os.environ.get("AA1057_SSL", "require") or None)


async def dry_run(conn) -> dict[str, Any]:
    return {"mode": "dry_run", **summary(await rt.build_plan(conn))}


async def apply(conn, expect_subjects: int, expect_requests: int, expect_subject_pin: str, expect_request_pin: str,
                restore_file: str | None, reconnect=connect) -> dict[str, Any]:
    async with conn.transaction():
        plan = await rt.build_plan(conn)
        n_s, n_r = len(plan.subject_changes), len(plan.request_changes)
        if (n_s, n_r) != (expect_subjects, expect_requests):
            raise SystemExit(f"refused: {n_s} subjects / {n_r} requests to change now, expected "
                             f"{expect_subjects} / {expect_requests} (re-run the dry run)")
        now = plan.summary()
        pins = (now["subjects"]["pin"], now["requests"]["pin"])
        if pins != (expect_subject_pin, expect_request_pin):
            raise SystemExit(f"refused: the rows to change are not the reviewed ones (pins now {pins[0]} / "
                             f"{pins[1]}, expected {expect_subject_pin} / {expect_request_pin}; re-run the dry run)")
        restore = rt.restore_rows(plan)
        print("AA1057-RESTORE " + json.dumps(restore, sort_keys=True))
        if restore_file:
            with open(restore_file, "w", encoding="utf-8") as f:
                json.dump(restore, f, indent=2, sort_keys=True)
        changed = await rt.apply_plan(conn, plan)
        if (changed["subjects"], changed["requests"]) != (n_s, n_r):
            raise SystemExit(f"refused: {changed} rows changed, planned {n_s} / {n_r}; rolled back")
    fresh = await reconnect()
    try:
        after = summary(await rt.build_plan(fresh))
    finally:
        await fresh.close()
    return {"mode": "apply", "planned": summary(plan), "changed": changed, "after": after}


def parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--apply", action="store_true")
    p.add_argument("--expect-subjects", type=int)
    p.add_argument("--expect-requests", type=int)
    p.add_argument("--expect-subject-pin")
    p.add_argument("--expect-request-pin")
    p.add_argument("--restore-file")
    args = p.parse_args(argv)
    needed = (args.expect_subjects, args.expect_requests, args.expect_subject_pin, args.expect_request_pin)
    if args.apply and any(v is None for v in needed):
        p.error("--apply needs --expect-subjects, --expect-requests, --expect-subject-pin and --expect-request-pin "
                "(the counts and pins of the dry run)")
    return args


async def main(argv: list[str]) -> None:
    args = parse(argv)
    conn = await connect()
    try:
        if args.apply:
            out = await apply(conn, args.expect_subjects, args.expect_requests, args.expect_subject_pin,
                              args.expect_request_pin, args.restore_file)
        else:
            out = await dry_run(conn)
    finally:
        await conn.close()
    emit(out)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or shlex.split(os.environ.get("ARGS", ""))))
