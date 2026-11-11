"""AA-1049 path A-new (S277) — recompose the FIXED fields of active S1 Masters with today's S1 train compose code and
publish each result as a new approved version. No model call.

Runs in the CIS api container through ``tools/ecs/ecsx.sh`` (cwd /app, the container DATABASE_URL = cis_app,
ssl=require):

    tools/ecs/ecsx.sh apps/AA-CIS-App/scripts/quality/recompose_masters.py WAVE_ID=<id>            # DRY RUN
    tools/ecs/ecsx.sh apps/AA-CIS-App/scripts/quality/recompose_masters.py WAVE_ID=<id> APPLY=1 PIN=<pin>

What it does per Master: ``services/content_generation/s1_recompose.py`` (the pure recompose: day headings, the
source day's verbatim fact lines, the SEO title fit, the code meta template; the narrative unchanged; the final
contract before / after) and ``s1_recompose_store.py`` (the selection, the new version, the publish through
``process_export``: A3 re-atomize, the master source fan-out, catalog ``text_updated_at`` and TripPlanner follow).
Skipped and counted: a source not live, a pending review, a newer pending / approved version, an active pipeline job,
a writer other than v2, a human-edited Master (``INCLUDE_HUMAN_EDITED=1`` takes them), the elephant-riding policy, a
heading the canonical parser would read differently, a narrative change, a recompose that would lose a fact the
served page states (``facts_would_be_lost``: a writer fact clause is cut only when the day gets the source's fact
line with the same label and value), a no-op, a contract verdict worse than the stored or today's one (or a new
blocking code), a recompose version that was published and then rolled back by a person (``rolled_back``: never
republished).

DRY RUN (default): no write. Prints the counts per state, per changed field and per verdict move, the line counts of
the Masters due (``fact_clauses_cut lines_removed lines_added facts_lost``), one line per Master (8-char tour id,
state, changed field names, -removed/+added lines, facts lost, verdict old -> new) and the pin of the Masters due
(``count:md5`` of the sorted ``tour_id:served version id``); then a capped SAMPLE of before / after lines (the first
SAMPLE Masters due, their first two day blocks, "-" removed / "+" added; page text on the terminal only).

APPLY=1: needs WAVE_ID of an OPEN, not paused rerun wave (``/admin/waves``; every tour of the set must be in its
expected set) and PIN from the DRY RUN of the same set; recomputes the set, refuses when the pin moved or when a
Master due would lose a fact (facts_lost > 0, exit code 2; ``decide`` already skips such a Master), then writes
and publishes each due Master (one transaction for the version, then the publish). Re-runnable: a recomposed Master
is a no-op on the next run (the pin then changes: run the DRY RUN again); a version written whose publish did not
happen is published on the next run (``resume``). The wave is read again before each Master: a pause (staff in
``/admin/waves`` or the capacity guard) or a close stops the run (``stopped: ...``, exit code 4); the DRY RUN after
the resume picks up the rest.

Env:
  WAVE_ID               the wave whose expected set is the run's set (required with APPLY=1)
  TOUR_IDS              comma-separated full tour ids (a subset of the wave's set when WAVE_ID is given)
  ALL=1                 DRY RUN only: every active Master of the master tenant
  LIMIT                 the first N Masters of the set by tour id (DRY RUN and APPLY)
  APPLY=1, PIN          write (see above)
  INCLUDE_HUMAN_EDITED=1  also recompose a Master a person edited (its edited fact lines become the source's)
  EMIT_IDS=1            DRY RUN: also print the full tour ids of the Masters due (``DUE <tour_id>``), to make a wave
  SAMPLE                DRY RUN: Masters in the before / after sample (default 3, 0 = none)
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.getcwd())
if os.path.isdir("/app") and "/app" not in sys.path:
    sys.path.insert(0, "/app")

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def parse_ids(raw: str) -> list[str]:
    """Full tour ids from a comma / space separated list; raises ValueError on anything else."""
    ids = [x.strip().lower() for x in re.split(r"[,\s]+", raw or "") if x.strip()]
    bad = [x for x in ids if not _UUID_RE.match(x)]
    if bad:
        raise ValueError(f"not a full tour id: {bad[0][:40]}")
    return sorted(set(ids))


def resolve_set(env: dict, wave: list[str] | None) -> tuple[list[str] | None, str]:
    """(the tour ids of the run, or None = every active Master; "" or why the run is refused). Pure."""
    apply = env.get("APPLY") == "1"
    ids = parse_ids(env.get("TOUR_IDS") or "")
    if apply:
        if wave is None:
            return None, "APPLY=1 needs WAVE_ID of an open rerun wave"
        if not (env.get("PIN") or "").strip():
            return None, "APPLY=1 needs PIN=<count:md5> from the DRY RUN"
        if env.get("ALL") == "1":
            return None, "ALL=1 is a DRY RUN option; APPLY runs on the wave's set"
    if wave is not None:
        outside = sorted(set(ids) - set(wave))
        if outside:
            return None, f"{len(outside)} tour id(s) outside the wave's expected set (first {outside[0][:8]})"
        return (ids or list(wave)), ""
    if ids:
        return ids, ""
    if env.get("ALL") == "1":
        return None, ""
    return None, "name the set: WAVE_ID, TOUR_IDS or ALL=1"


async def run(pool, env: dict, *, publish=None, out=print) -> int:
    """The whole run on ``pool`` (exit code: 0 ok, 1 a Master failed on APPLY, 2 refused, 3 pin moved, 4 stopped:
    the wave was paused or closed during APPLY)."""
    from services.content_generation import s1_recompose_store as st
    from services.quality.final_contract_store import brand_key, brand_words_by_key

    apply = env.get("APPLY") == "1"
    limit = int(env["LIMIT"]) if env.get("LIMIT") else None
    async with pool.acquire() as conn:
        wave = None
        if env.get("WAVE_ID"):
            wave, why = await st.wave_set(conn, env["WAVE_ID"].strip())
            if wave is None:
                out(f"refused: {why}")
                return 2
        try:
            ids, why = resolve_set(env, wave)
        except ValueError as e:
            ids, why = None, str(e)
        if why:
            out(f"refused: {why}")
            return 2
        rows = await st.load(conn, ids)
        if limit:
            rows = rows[:limit]
        words = await brand_words_by_key(conn, rows)   # the tenant's active brand list, per brand key
    keep_edited = env.get("INCLUDE_HUMAN_EDITED") == "1"
    decisions = [st.decide(r, words.get(brand_key(r)) or [], include_human_edited=keep_edited) for r in rows]
    asked = len(ids) if ids is not None else "all"
    out(f"mode={'APPLY' if apply else 'DRY RUN'} set={asked} masters_read={len(rows)} "
        f"wave={(env.get('WAVE_ID') or '-')[:8]}")
    for line in st.report_lines(decisions):
        out(line)
    if not apply:
        for line in st.sample_lines(decisions, int(env["SAMPLE"]) if env.get("SAMPLE") else st.SAMPLE_TOURS):
            out(line)
        if env.get("EMIT_IDS") == "1":
            for d in decisions:
                if d["state"] in st.DUE:
                    out(f"DUE {d['tour_id']}")
        return 0
    pin, now_pin = env["PIN"].strip(), st.pin_of(decisions)
    if now_pin != pin:
        out(f"PIN mismatch: given {pin}, now {now_pin}. Nothing written; run the DRY RUN again.")
        return 3
    lost = st.apply_refusal(decisions)
    if lost:
        out(f"refused: {lost}")
        return 2
    done: Counter = Counter()
    for d in decisions:
        if d["state"] not in st.DUE:
            continue
        # the wave stop rule holds during the run: a pause or a close since the start stops it before the next write
        async with pool.acquire() as conn:
            still, why = await st.wave_set(conn, env["WAVE_ID"].strip())
        if still is None:
            out(f"stopped: {why}")
            out("applied: " + (", ".join(f"{k}={v}" for k, v in sorted(done.items())) or "nothing")
                + " (stopped; the rest stays due, resume the wave and run the DRY RUN again)")
            return 4
        res = await st.apply_one(pool, d, publish=publish)
        done[res["state"]] += 1
        vid = str(res.get("version_id") or "")[:8]
        job = str(res.get("atomize_job_id") or "")[:8]
        out(f"  {d['tour_id'][:8]} {res['state']} version={vid or '-'} atomize_job={job or '-'}"
            + (f" error={res['error']}" if res.get("error") else ""))
    out("applied: " + (", ".join(f"{k}={v}" for k, v in sorted(done.items())) or "nothing due"))
    return 0 if set(done) <= {"published"} else 1


async def main() -> int:
    import asyncpg

    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], ssl="require", min_size=1, max_size=2)
    try:
        return await run(pool, dict(os.environ))
    finally:
        await pool.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
