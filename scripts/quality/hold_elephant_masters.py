"""AA-1049 (tech lead decision S277, root ADR 0020 D8) — a one-time hold of the Masters that offer elephant riding.

Masters written before the S271 detector (the old Haiku writer, published 01-10/10/2026) and a few v2 Masters carry
elephant riding and are on sale with no review row. The policy: a Master whose product offers elephant riding is not
on sale until a person reviews it; it goes to the Review Queue and only an approval puts it back on sale.

Runs in the CIS api container through ``tools/ecs/ecsx.sh`` (cwd /app, the container DATABASE_URL = cis_app,
ssl=require). No model call.

DRY RUN (default): no write. Reads every Master of the AA master tenant that is active, or already held by this
policy (``master_status_reason = 'policy_hold'``, so a re-run after a partial APPLY gives the same set), runs the
detector (``services/quality/elephant_policy.scan``) over its published text and its raw source, and prints:

- ``to_hold`` (active Masters with a hit), ``already_held`` and ``approved`` (a person approved the served version
  in its policy review: on sale by that approval, never held again, left out of the pin), plus
  ``approved_other`` (to_hold Masters whose served version has an approved Review Queue row with no policy marker,
  e.g. a score review before the S277 marker: still held, counted so the tech lead sees them before APPLY);
- one line per tour: the 8-char tour id, its state and the matched fields as codes (``master.highlights``,
  ``source.itineraries``); never the matched words or any page text;
- the fingerprint pin of the set to hold (``count:md5`` of the sorted ``tour_id:served version id`` of the
  to_hold + already_held tours: the id plus a change marker, skill aa-cis-schema references/destructive-ops.md
  step 1; a re-publish of another version, or an approval landing, changes it).

APPLY=1 PIN=<count:md5 from the DRY RUN>: recomputes the set, refuses when the pin differs (the set moved since the
DRY RUN: run it again and show the new pin), then holds each tour through the lifecycle service
(``services.tour_lifecycle.policy_hold``: inactive + reason policy_hold, a pending policy review row, the pause
cascade) and runs its post-commit work (TripPlanner purge, ranking / route caches, place graph, trip pages). One tour
per transaction; idempotent (a held tour with its pending review changes nothing) and resumable (a held tour stays in
the set and the hold keeps its served version, so the pin holds across a re-run). A tour a reviewer approved after an
APPLY is ``approved`` on a re-run: never re-held (the hold itself also refuses it, 409 policy_approved), and the pin
changes, so an APPLY with the old pin stops and asks for a new DRY RUN.

Check after APPLY (read-only): active Masters with a detector hit and no approved policy review of the served version
= 0 (``to_hold=0`` in a DRY RUN).

Env: APPLY (1 to write), PIN (required with APPLY=1), LIMIT (APPLY only: hold at most this many this run).
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from collections import Counter

sys.path.insert(0, "/app")

MASTER_TENANT = "00000000-0000-0000-0000-000000000001"   # aa_internal (admin_pipeline TENANT_SLUG_MAP)
ACTOR = "script:hold_elephant_masters"


def candidates_sql() -> str:
    from services.tour_lifecycle.policy import MASTER_SCAN_COLUMNS, MASTER_SCAN_FROM, POLICY_HOLD, policy_approved_sql
    return f"""
        SELECT {MASTER_SCAN_COLUMNS},
               {policy_approved_sql("pt.generated_content_id")} AS policy_approved,
               EXISTS (SELECT 1 FROM silver_aa_internal.review_queue o
                        WHERE o.generated_content_id = pt.generated_content_id
                          AND o.review_status = 'approved') AS any_approved
          {MASTER_SCAN_FROM}
         WHERE pt.tenant_id = $1::uuid AND pt.deleted_at IS NULL
           AND (pt.master_status = 'active'
                OR (pt.master_status = 'inactive' AND pt.master_status_reason = '{POLICY_HOLD}'))
         ORDER BY pt.tour_id
    """


HOLD_STATES = ("active", "held")


def pin_of(found) -> str:
    """``count:md5`` of the sorted ``tour_id:served version id`` of the tours to hold (to_hold + already_held)."""
    keys = sorted(f"{f['tour_id']}:{f.get('version_id') or ''}" for f in found if f["state"] in HOLD_STATES)
    return f"{len(keys)}:{hashlib.md5(','.join(keys).encode()).hexdigest()}"


def _get(row, key):
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def scan_rows(rows) -> list[dict]:
    """[{tour_id, version_id, state, fields, approved_other}] of the rows with a detector hit (fields = sorted codes
    ``where.field``). state: ``active`` (to hold), ``held`` (already held), ``approved`` (a person approved the served
    version in its policy review: never held)."""
    from services.tour_lifecycle.policy import hits_from_row
    out = []
    for r in rows:
        hits = hits_from_row(r, master=True)
        if not hits:
            continue
        policy_ok = _get(r, "policy_approved") is True
        state = "approved" if policy_ok else ("held" if r["master_status"] != "active" else "active")
        out.append({"tour_id": r["tour_id"], "version_id": _get(r, "generated_content_id"), "state": state,
                    "fields": sorted({f"{h['where']}.{h['field']}" for h in hits}),
                    "approved_other": not policy_ok and _get(r, "any_approved") is True})
    return out


def report(found: list[dict]) -> list[str]:
    n = Counter(f["state"] for f in found)
    lines = [f"to_hold={n['active']} already_held={n['held']} approved={n['approved']} "
             f"approved_other={sum(1 for f in found if f['state'] == 'active' and f['approved_other'])} "
             f"total={len(found)}"]
    by_field = Counter(code for f in found for code in f["fields"])
    lines.append("fields: " + ", ".join(f"{k}={v}" for k, v in sorted(by_field.items())))
    lines += [f"  {f['tour_id'][:8]} {f['state']:<6} {','.join(f['fields'])}" for f in found]
    lines.append(f"PIN={pin_of(found)}")
    return lines


async def hold_all(pool, found: list[dict], *, limit: int | None = None, hold=None, after_commit=None) -> Counter:
    """Hold each tour of ``found`` (active first, ``approved`` ones never); one transaction each. Returns the outcome
    counts."""
    from services.tour_lifecycle import LifecycleError, policy_hold, run_after_commit
    hold = hold or policy_hold
    after_commit = after_commit or run_after_commit
    done: Counter = Counter()
    todo = sorted((f for f in found if f["state"] in HOLD_STATES), key=lambda f: (f["state"] != "active", f["tour_id"]))
    for f in todo[:limit] if limit else todo:
        try:
            async with pool.acquire() as conn:
                out = await hold(conn, f["tour_id"], actor=ACTOR)
        except LifecycleError as e:
            done[f"refused_{e.code}"] += 1
            print(f"  {f['tour_id'][:8]} refused {e.code}", flush=True)
            continue
        except Exception as e:  # noqa: BLE001 — one tour's failure never stops the run (re-run resumes)
            done["error"] += 1
            print(f"  {f['tour_id'][:8]} error {type(e).__name__}", flush=True)
            continue
        await after_commit(pool, out)
        state = "held" if out.extra.get("held") else "kept"
        done[state] += 1
        print(f"  {f['tour_id'][:8]} {state} review={str(out.extra.get('review_id') or '')[:8]}", flush=True)
    return done


async def main() -> int:
    import asyncpg

    apply = os.environ.get("APPLY") == "1"
    pin = (os.environ.get("PIN") or "").strip()
    limit = int(os.environ["LIMIT"]) if os.environ.get("LIMIT") else None
    if apply and not pin:
        print("APPLY=1 needs PIN=<count:md5> from the DRY RUN")
        return 2
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], ssl="require", min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(candidates_sql(), MASTER_TENANT)
        found = scan_rows(rows)
        print(f"mode={'APPLY' if apply else 'DRY RUN'} masters_read={len(rows)}")
        for line in report(found):
            print(line)
        if not apply:
            return 0
        now_pin = pin_of(found)
        if now_pin != pin:
            print(f"PIN mismatch: given {pin}, now {now_pin}. Nothing written; run the DRY RUN again.")
            return 3
        done = await hold_all(pool, found, limit=limit)
        print("applied: " + ", ".join(f"{k}={v}" for k, v in sorted(done.items())))
        return 0 if not done.get("error") else 1
    finally:
        await pool.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
