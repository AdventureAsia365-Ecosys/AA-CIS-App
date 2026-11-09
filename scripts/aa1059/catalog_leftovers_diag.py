"""AA-1059 — catalog leftovers of the S273 system picture (section 1, section 5 item 10). READ ONLY.

Runs in the CIS api container (python3 + asyncpg, DATABASE_URL = cis_app, ssl=require, a read-only session), through
tools/ecs/ecsx.sh in the root repo:

    tools/ecs/ecsx.sh apps/AA-CIS-App/scripts/aa1059/catalog_leftovers_diag.py [COUNTRIES=JP,CN,TW]

Four sections, one line per metric (`AA1059 <metric>=<value>`):
  SEG    the active Masters with live atoms but no Segment (S273: 9): per Master the atom counts the segment stage
         sees (with a place and an action, forming a Segment by rule, transit / unnamed by rule), its last
         `a3_atomize` job (status, the `segment_score_route` outcome class), tour-scope recompute runs, lifecycle
         events after that job, and one verdict (`segment_verdict`); plus how many the `segment_catchup` stage
         (this PR) would pick up.
  S1     the pending S1 review items (no tenant version) against the live sources with no Master (S273: 25 / 25): the
         match, and per tour its latest S1 version, its pending item, the approve gate's blocker CODES for that item
         (services/quality/approve_gate.py, nothing approved here) and one next step (`s1_next_step`).
  ROUTE  route coverage per country (active Masters / ranked / with a current route) and, for the tours with ranking
         but no current route, the reason `route_detection.route_gap_reason` gives, counted per country; one line
         per tour for COUNTRIES (default JP, CN, TW), with a what-if: would a Route form if a Segment counted on
         every day it occurs instead of its first day only (`whatif_all_days`). The route stage records no per-tour
         reason (its result has totals only), so the reason is re-derived here from the same inputs.
  GC     the segment_gc rule as the code implements it (constants), the last platform run's `segment_gc` result, and
         what it would remove today (ORPHANS_SQL + classify, read only: counts), with the kept orphans by reference
         class and, for the ones a live member keeps, the Master / source state of those members' tours.
Only counts, ids (tour, review, job), fixed codes, statuses and ages are printed: never tour names, places, actions or
texts, never error messages (exception class names only). Every query is isolated: a failure prints
`ERR <section> <ExceptionName>` and the script goes on.

Imports app code from the image (`segment_matching`, `atom_ranking`, `segment_gc`, `approve_gate` exist today;
`route_detection.route_gap_reason` and `segment_catchup.plan` come with the AA-1059 PR): on an older image a missing
import prints `ERR import <name>` and skips only what needs it (the per-tour route reasons, the catch-up count), so the
script also runs BEFORE the deploy, to capture the Masters without a Segment before the catch-up stage segments them.

Tables / columns: AA-Ecosys docs/architecture/db-schema-reference.md (verified 2026-11-08, CIS 390).
"""
import asyncio
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone

import asyncpg

MASTER_TENANT = "00000000-0000-0000-0000-000000000001"   # api/routers/v1_pipeline.py approve path (single-tenant CIS)
DEFAULT_COUNTRIES = ("JP", "CN", "TW")


def out(metric, value):
    print(f"AA1059 {metric}={value}")


def fmt(v):
    if v is None:
        return "null"
    if isinstance(v, float):
        return f"{v:.1f}"
    return str(v)


def age_d(ts, now=None):
    if ts is None:
        return "null"
    now = now or datetime.now(timezone.utc)
    return f"{(now - ts).total_seconds() / 86400:.1f}"


async def rows(conn, section, sql, *args):
    try:
        return await conn.fetch(sql, *args)
    except Exception as e:  # noqa: BLE001 — print the class only, never the message
        print(f"ERR {section} {type(e).__name__}")
        return None


def _import(section, fn):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        print(f"ERR import {section} {type(e).__name__}")
        return None


# ── pure helpers (unit tested) ────────────────────────────────────────────────────────────────────────────────────────

def ssr_class(value):
    """The `segment_score_route` outcome an a3_atomize job wrote, as a fixed label: ok | not_run | missing |
    failed:<ExceptionClass> (the message after the class is dropped)."""
    if value is None:
        return "missing"
    v = str(value)
    if v in ("ok", "not_run"):
        return v
    if v.startswith("failed"):
        parts = [p.strip() for p in v.split(":")]
        name = parts[1] if len(parts) > 1 and parts[1].isidentifier() else "unknown"
        return f"failed:{name}"
    return "other"


def atom_counts(atoms, forms_segment=None, classify_exclusion=None, segment_atom=None):
    """Counts of one tour's live atoms as the segment stage sees them. `atoms` = dicts with place, action,
    activity_type, itinerary_day. The three callables come from segment_matching / atom_ranking (None = unknown)."""
    c = {"atoms": 0, "with_place_action": 0, "forming": None, "transit": None, "unnamed": None, "no_day": 0}
    forming = transit = unnamed = 0
    for a in atoms:
        c["atoms"] += 1
        if a.get("itinerary_day") is None:
            c["no_day"] += 1
        if a.get("place") is None or a.get("action") is None:
            continue
        c["with_place_action"] += 1
        if forms_segment and segment_atom:
            atom = segment_atom("x", "x", None, a["place"], a["action"], activity_type=a.get("activity_type"))
            forming += bool(forms_segment(atom))
        if classify_exclusion:
            reason = classify_exclusion(a["place"], a["action"])
            transit += reason == "transit"
            unnamed += reason == "unnamed_place"
    if forms_segment and segment_atom:
        c["forming"] = forming
    if classify_exclusion:
        c["transit"], c["unnamed"] = transit, unnamed
    return c


def segment_verdict(counts, last_a3, events_after_a3):
    """Why an active Master with live atoms has no Segment, as far as the stored state tells:
      no_place_action        no atom has a place and an action (atomized before migration 129): re-atomize (paid)
      all_atoms_excluded     every atom is transit / an unnamed place by rule: no Segment by design
      no_atomize_job         no a3_atomize job on record (atomized before the job runner, or by another path)
      segment_step_failed:X  the job's segment step raised X (best-effort: the job succeeded, nothing retried it)
      segment_step_not_run   the job stopped before its segment step (failed atomize, stop, lock timeout)
      atomized_while_inactive a `restored` lifecycle event came after the job: the segment run read no active atom,
                             and the restore's platform pass had no segment stage (segment_catchup covers it now)
      segment_ran_no_member  the step reported ok and nothing above explains it: investigate the job result
    `counts` from atom_counts; `last_a3` dict with `ssr` (ssr_class) or None; `events_after_a3` = event names."""
    if counts["with_place_action"] == 0:
        return "no_place_action"
    if counts.get("forming") == 0:
        return "all_atoms_excluded"
    if last_a3 is None:
        return "no_atomize_job"
    ssr = last_a3.get("ssr") or "missing"
    if ssr.startswith("failed"):
        return f"segment_step_{ssr}"
    if ssr in ("not_run", "missing", "other"):
        return "segment_step_not_run"
    if "restored" in events_after_a3:
        return "atomized_while_inactive"
    return "segment_ran_no_member"


def master_flags(master_status):
    """(has_live_master, master_trashed) from a published_tours.master_status (None = no row). The same rule as
    `_enqueue_review` in api/routers/admin_pipeline.py: only a Master whose status is not 'trashed' is live, so a tour
    whose Master is trashed and whose source is live gets a legitimate pending S1 item."""
    if master_status is None:
        return False, False
    return master_status != "trashed", master_status == "trashed"


def s1_next_step(latest_status, pending, blockers, source_live=True, has_master=False, master_trashed=False):
    """The next step for a live source with no Master (or a pending S1 item), as a fixed label:
      stale_item_dismiss       the item's tour already has a live (not trashed) Master or its source is not live:
                               dismiss it
      trashed_master_pending   pending item, live source, the tour's Master is trashed: a legitimate item (approve
                               writes the Master again), outside the S273 no-Master set because that set excludes any
                               published_tours row; decide approve vs keep the Master trashed (gate codes printed)
      staff_approve            pending item on the latest version, the approve gate finds no blocker
      staff_edit_or_regenerate pending item, the gate blocks it unedited (codes printed): edit + re-validate, or
                               regenerate (paid S1 run)
      pending_not_latest       the pending item is on an older version than the tour's latest
      staff_review             pending item on the latest version, the gate could not be evaluated here
      approved_not_published   latest version approved but no Master: the export did not land (investigate)
      no_review_item           latest version is hitl but no pending item (queue row missing: investigate)
      rejected_regenerate      latest version rejected: regenerate (paid) or trash the source
      s1_in_flight             latest version still generating / pending (no verdict yet)
      no_s1_version            no S1 version at all: the rewrite never ran
    `pending` = None or {"on_latest": bool}; `blockers` = list of codes or None (gate not evaluated)."""
    if has_master or not source_live:
        return "stale_item_dismiss"
    if master_trashed and pending is not None:
        return "trashed_master_pending"
    if pending is not None:
        if not pending.get("on_latest", True):
            return "pending_not_latest"
        if blockers is None:
            return "staff_review"
        return "staff_approve" if not blockers else "staff_edit_or_regenerate"
    if latest_status is None:
        return "no_s1_version"
    if latest_status == "hitl":
        return "no_review_item"
    if latest_status == "rejected":
        return "rejected_regenerate"
    if latest_status == "approved":
        return "approved_not_published"
    return "s1_in_flight"


def tour_route_facts(segs, moment_cls, gap_reason, derive):
    """One tour's ranked Segments -> {reason, segments, days, whatif_all_days}. `segs` = list of
    (segment_id, place, [days...]) for the tour's current non-excluded ranked Segments (days = the itinerary days of
    its live atoms in that Segment). The stage builds one moment per Segment at its FIRST day; the what-if builds one
    per Segment per day."""
    first = [moment_cls(segment_id=s, tour_id="t", day=(min(d) if d else None), place=p) for s, p, d in segs]
    every = [moment_cls(segment_id=s, tour_id="t", day=day, place=p) for s, p, d in segs for day in sorted(set(d))]
    days = sorted({day for _s, _p, d in segs for day in d})
    return {"reason": gap_reason(first), "segments": len(segs), "days": len(days),
            "whatif_all_days": bool(every) and bool(derive(every))}


# ── SEG ───────────────────────────────────────────────────────────────────────────────────────────────────────────────

Q_SEG_ATOMS = """
WITH seg_tours AS (
    SELECT DISTINCT a.tour_id FROM acp_contract.v_active_tour_atoms a
      JOIN acp_contract.atom_segment_member m ON m.atom_id = a.atom_id)
SELECT a.tour_id::text AS tour_id, a.place, a.action, a.activity_type, a.itinerary_day
  FROM acp_contract.v_active_tour_atoms a
 WHERE NOT EXISTS (SELECT 1 FROM seg_tours s WHERE s.tour_id = a.tour_id)
 ORDER BY 1
"""

# The S273 measure (RECOMPUTE masters.active.atoms_but_no_segment): tour_atoms of active Masters, any source state.
Q_SEG_S273 = """
WITH seg AS (SELECT DISTINCT a.tour_id FROM acp_contract.tour_atoms a
               JOIN acp_contract.atom_segment_member m ON m.atom_id = a.atom_id
              WHERE NOT a.deleted AND NOT a.is_empty_marker),
     atoms AS (SELECT DISTINCT tour_id FROM acp_contract.tour_atoms WHERE NOT deleted AND NOT is_empty_marker)
SELECT count(*)::int AS n
  FROM gold_aa_internal.published_tours p
 WHERE p.master_status::text = 'active'
   AND p.tour_id IN (SELECT tour_id FROM atoms) AND p.tour_id NOT IN (SELECT tour_id FROM seg)
"""

Q_SEG_MASTER = """
SELECT p.tour_id::text AS tour_id, coalesce(rt.country_iso2, '??') AS country, p.published_at, p.updated_at
  FROM gold_aa_internal.published_tours p
  LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = p.tour_id
 WHERE p.tour_id = ANY($1::uuid[])
"""

Q_SEG_A3 = """
SELECT payload->>'tour_id' AS tour_id, status, attempt, created_at, finished_at,
       result->>'segment_score_route' AS ssr, result->'atomize'->>'status' AS atomize_status,
       count(*) OVER (PARTITION BY payload->>'tour_id')::int AS jobs,
       row_number() OVER (PARTITION BY payload->>'tour_id' ORDER BY created_at DESC, id) AS rn
  FROM shared.job
 WHERE kind = 'a3_atomize' AND payload->>'tour_id' = ANY($1::text[])
"""

Q_SEG_TOUR_RECOMPUTE = """
SELECT payload->>'tour_id' AS tour_id, status, count(*)::int AS n
  FROM shared.job
 WHERE kind = 'recompute' AND payload->>'scope' = 'tour' AND payload->>'tour_id' = ANY($1::text[])
 GROUP BY 1, 2
"""

Q_SEG_EVENTS = """
SELECT tour_id::text AS tour_id, event, occurred_at
  FROM gold_aa_internal.tour_lifecycle_event
 WHERE tour_id = ANY($1::uuid[])
 ORDER BY occurred_at
"""


async def section_seg(conn, now):
    sm = _import("segment_matching", lambda: __import__("services.acp_contract.segment_matching",
                                                         fromlist=["forms_segment", "SegmentAtom"]))
    ar = _import("atom_ranking", lambda: __import__("services.acp_contract.atom_ranking",
                                                     fromlist=["classify_exclusion"]))
    sc = _import("segment_catchup", lambda: __import__("services.recompute.segment_catchup", fromlist=["plan"]))
    r = await rows(conn, "seg_s273", Q_SEG_S273)
    if r:
        out("SEG.s273_measure.active_masters_atoms_but_no_segment", r[0]["n"])
    atoms = await rows(conn, "seg_atoms", Q_SEG_ATOMS)
    if atoms is None:
        return
    by_tour = defaultdict(list)
    for a in atoms:
        by_tour[a["tour_id"]].append(dict(a))
    tour_ids = sorted(by_tour)
    out("SEG.active_view.masters_with_live_atoms_no_segment", len(tour_ids))
    if sc is not None:
        planned = sc.plan([{"tour_id": t, "atom_id": str(i), "place": a["place"], "action": a["action"],
                            "activity_type": a["activity_type"]}
                           for t in tour_ids for i, a in enumerate(by_tour[t])
                           if a["place"] is not None and a["action"] is not None])
        out("SEG.catchup.would_pick", len(planned["tours"]))
        out("SEG.catchup.all_excluded", len(planned["all_excluded"]))
    if not tour_ids:
        return
    master = {r["tour_id"]: r for r in await rows(conn, "seg_master", Q_SEG_MASTER, tour_ids) or []}
    a3_all = await rows(conn, "seg_a3", Q_SEG_A3, tour_ids) or []
    last_a3 = {r["tour_id"]: r for r in a3_all if r["rn"] == 1}
    rec = defaultdict(list)
    for r in await rows(conn, "seg_recompute", Q_SEG_TOUR_RECOMPUTE, tour_ids) or []:
        rec[r["tour_id"]].append(f"{r['status']}:{r['n']}")
    events = defaultdict(list)
    for r in await rows(conn, "seg_events", Q_SEG_EVENTS, tour_ids) or []:
        events[r["tour_id"]].append(r)
    verdicts = Counter()
    for t in tour_ids:
        counts = atom_counts(by_tour[t],
                             forms_segment=getattr(sm, "forms_segment", None),
                             classify_exclusion=getattr(ar, "classify_exclusion", None),
                             segment_atom=getattr(sm, "SegmentAtom", None))
        j = last_a3.get(t)
        a3 = None if j is None else {"ssr": ssr_class(j["ssr"])}
        since = (j["finished_at"] or j["created_at"]) if j is not None else None
        after = [e["event"] for e in events.get(t, []) if since is not None and e["occurred_at"] > since]
        v = segment_verdict(counts, a3, after)
        verdicts[v.split(":")[0]] += 1
        m = master.get(t)
        out(f"SEG.tour.{t}",
            f"country={m['country'] if m else '??'} atoms={counts['atoms']} with_place_action="
            f"{counts['with_place_action']} forming={fmt(counts['forming'])} transit={fmt(counts['transit'])} "
            f"unnamed={fmt(counts['unnamed'])} no_day={counts['no_day']} "
            f"published_age_d={age_d(m['published_at'], now) if m else 'null'} "
            f"a3_jobs={j['jobs'] if j else 0} a3_last={j['status'] if j else '-'} "
            f"a3_last_age_d={age_d(j['created_at'], now) if j else 'null'} "
            f"a3_atomize={j['atomize_status'] or '-' if j else '-'} a3_segment={a3['ssr'] if a3 else '-'} "
            f"tour_recompute={','.join(sorted(rec.get(t, []))) or '0'} "
            f"events={','.join(e['event'] for e in events.get(t, [])) or '-'} "
            f"events_after_a3={','.join(after) or '-'} verdict={v}")
    for v, n in sorted(verdicts.items()):
        out(f"SEG.verdict.{v}", n)


# ── S1 ────────────────────────────────────────────────────────────────────────────────────────────────────────────────

Q_S1_NO_MASTER = """
SELECT rt.tour_id::text AS tour_id, coalesce(rt.country_iso2, '??') AS country, rt.pipeline_status::text AS ps,
       rt.ingest_at
  FROM silver_aa_internal.raw_tours rt
 WHERE rt.deleted_at IS NULL AND rt.source_status = 'active'
   AND NOT EXISTS (SELECT 1 FROM gold_aa_internal.published_tours pt WHERE pt.tour_id = rt.tour_id)
 ORDER BY 2, 1
"""

# Every pending S1 item (no tenant version), with the state of its tour.
Q_S1_PENDING = """
SELECT rq.id::text AS review_id, rq.tour_id::text AS tour_id, rq.generated_content_id::text AS gc_id, rq.created_at,
       (rq.tenant_id = $1::uuid) AS master_tenant,
       pt.master_status::text AS master_status,
       (rt.tour_id IS NOT NULL AND rt.deleted_at IS NULL AND rt.source_status = 'active') AS source_live,
       coalesce(rt.country_iso2, '??') AS country
  FROM silver_aa_internal.review_queue rq
  LEFT JOIN gold_aa_internal.published_tours pt ON pt.tour_id = rq.tour_id
  LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = rq.tour_id
 WHERE rq.review_status = 'pending' AND rq.tenant_tour_version_id IS NULL
 ORDER BY rq.created_at
"""

Q_S1_REVIEW_STATUS = """
SELECT CASE WHEN tenant_tour_version_id IS NULL THEN 's1' ELSE 't2' END AS kind, review_status::text AS st,
       count(*)::int AS n
  FROM silver_aa_internal.review_queue GROUP BY 1, 2 ORDER BY 1, 2
"""

Q_S1_LATEST = """
SELECT DISTINCT ON (gc.tour_id) gc.tour_id::text AS tour_id, gc.id::text AS gc_id, gc.version_num,
       gc.status::text AS status, coalesce(gc.human_edited, false) AS human_edited, gc.revalidate_passed,
       count(*) OVER (PARTITION BY gc.tour_id)::int AS versions
  FROM silver_aa_internal.generated_content gc
 WHERE gc.tour_id = ANY($1::uuid[])
 ORDER BY gc.tour_id, gc.version_num DESC, gc.created_at DESC, gc.id
"""

Q_S1_JOB = """
SELECT DISTINCT ON (payload->>'tour_id') payload->>'tour_id' AS tour_id, status, created_at
  FROM shared.job
 WHERE kind = 's1_rewrite' AND payload->>'tour_id' = ANY($1::text[])
 ORDER BY payload->>'tour_id', created_at DESC, id
"""


async def section_s1(conn, now):
    st = await rows(conn, "s1_review_status", Q_S1_REVIEW_STATUS)
    for r in st or []:
        out(f"S1.review_queue.{r['kind']}.{r['st']}", r["n"])
    no_master = await rows(conn, "s1_no_master", Q_S1_NO_MASTER)
    pending = await rows(conn, "s1_pending", Q_S1_PENDING, MASTER_TENANT)
    if no_master is None or pending is None:
        return
    a = {r["tour_id"]: r for r in no_master}
    by_tour_pending = defaultdict(list)
    for p in pending:
        by_tour_pending[p["tour_id"]].append(p)
    b = set(by_tour_pending)
    out("S1.live_sources_without_master", len(a))
    out("S1.pending_s1_items", len(pending))
    out("S1.pending_s1_tours", len(b))
    out("S1.match.both", len(set(a) & b))
    out("S1.match.no_master_without_pending_item", len(set(a) - b))
    # Pending items outside the no-Master set, split by why: a trashed Master with a live source is a legitimate item
    # (`_enqueue_review` treats a trashed Master as no Master); a live Master or a dead source makes it stale.
    outside = b - set(a)
    trashed_live = {t for t in outside
                    if any(p["master_status"] == "trashed" and p["source_live"] for p in by_tour_pending[t])}
    out("S1.match.pending_item_tour_trashed_master_live_source", len(trashed_live))
    out("S1.match.pending_item_tour_has_master_or_dead_source", len(outside - trashed_live))
    for cc, n in sorted(Counter(r["country"] for r in no_master).items()):
        out(f"S1.no_master_by_country.{cc}", n)
    ids = sorted(set(a) | b)
    latest = {r["tour_id"]: r for r in await rows(conn, "s1_latest", Q_S1_LATEST, ids) or []}
    jobs = {r["tour_id"]: r for r in await rows(conn, "s1_job", Q_S1_JOB, ids) or []}
    gate_sql = None
    gate = _import("approve_gate", lambda: __import__("services.quality.approve_gate", fromlist=["blockers"]))
    vp = _import("v1_pipeline", lambda: __import__("api.routers.v1_pipeline", fromlist=["_APPROVE_GATE_SQL"]))
    tl = _import("tour_liveness", lambda: __import__("shared.tour_liveness", fromlist=["live_source_exists_sql"]))
    if gate is not None and vp is not None and tl is not None:
        gate_sql = vp._APPROVE_GATE_SQL.format(live=tl.live_source_exists_sql("rq.tour_id"))
    steps = Counter()
    blocker_codes = Counter()
    for t in ids:
        lt = latest.get(t)
        items = by_tour_pending.get(t, [])
        item = items[-1] if items else None
        blockers = None
        if item is not None and gate_sql is not None:
            g = await rows(conn, "s1_gate", gate_sql, item["review_id"], MASTER_TENANT)
            if g:
                if g[0]["human_edited"]:
                    blockers = [] if lt is not None and lt["revalidate_passed"] else ["edited_not_revalidated"]
                else:
                    blockers = sorted({b["code"] for b in gate.blockers(g[0])})
                for code in blockers:
                    blocker_codes[code] += 1
        pend = None if item is None else {"on_latest": lt is None or item["gc_id"] == lt["gc_id"]}
        has_master, master_trashed = master_flags(item["master_status"] if item is not None else None)
        source_live = item["source_live"] if item is not None else True
        step = s1_next_step(lt["status"] if lt else None, pend, blockers, source_live=source_live,
                            has_master=has_master, master_trashed=master_trashed)
        steps[step] += 1
        j = jobs.get(t)
        country = a[t]["country"] if t in a else (item["country"] if item else "??")
        out(f"S1.tour.{t}",
            f"country={country} in_no_master={t in a} pending_items={len(items)} "
            f"review_id={item['review_id'] if item else '-'} "
            f"item_age_d={age_d(item['created_at'], now) if item else 'null'} "
            f"master={item['master_status'] if item and item['master_status'] else '-'} "
            f"versions={lt['versions'] if lt else 0} latest_v={lt['version_num'] if lt else '-'} "
            f"latest_status={lt['status'] if lt else '-'} edited={lt['human_edited'] if lt else '-'} "
            f"pending_on_latest={pend['on_latest'] if pend else '-'} "
            f"gate={'-' if blockers is None else (','.join(blockers) or 'clear')} "
            f"s1_job={j['status'] if j else '-'} next={step}")
    for s, n in sorted(steps.items()):
        out(f"S1.next_step.{s}", n)
    for code, n in sorted(blocker_codes.items()):
        out(f"S1.gate_blocker.{code}", n)


# ── ROUTE ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

Q_ROUTE_COUNTRY = """
WITH act AS (
    SELECT p.tour_id, coalesce(rt.country_iso2, '??') AS cc
      FROM gold_aa_internal.published_tours p
      JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = p.tour_id
     WHERE p.master_status::text = 'active' AND p.deleted_at IS NULL
       AND rt.deleted_at IS NULL AND rt.source_status = 'active'),
rk AS (SELECT DISTINCT tour_id FROM acp_contract.atom_ranking WHERE superseded_at IS NULL),
rk_ok AS (SELECT DISTINCT tour_id FROM acp_contract.atom_ranking
           WHERE superseded_at IS NULL AND excluded_reason IS NULL),
rt AS (SELECT DISTINCT tour_id FROM acp_contract.route WHERE superseded_at IS NULL)
SELECT act.cc, count(*)::int AS masters,
       count(*) FILTER (WHERE act.tour_id IN (SELECT tour_id FROM rk))::int AS ranked_any,
       count(*) FILTER (WHERE act.tour_id IN (SELECT tour_id FROM rk_ok))::int AS ranked_kept,
       count(*) FILTER (WHERE act.tour_id IN (SELECT tour_id FROM rt))::int AS with_route
  FROM act GROUP BY 1 ORDER BY 1
"""

# The route stage's own inputs (run_route_detection), with every day of the Segment, not only its first.
Q_ROUTE_MOMENTS = """
SELECT ar.tour_id::text AS tour_id, ar.segment_id, asg.canonical_place AS place,
       coalesce(array_agg(DISTINCT ta.itinerary_day) FILTER (WHERE ta.itinerary_day IS NOT NULL), '{}') AS days
  FROM (SELECT DISTINCT segment_id, tour_id FROM acp_contract.atom_ranking
         WHERE excluded_reason IS NULL AND superseded_at IS NULL) ar
  JOIN acp_contract.atom_segment asg ON asg.segment_id = ar.segment_id
  JOIN acp_contract.atom_segment_member asm ON asm.segment_id = ar.segment_id
  JOIN acp_contract.v_active_tour_atoms ta ON ta.atom_id = asm.atom_id AND ta.tour_id = ar.tour_id
 WHERE ar.tour_id NOT IN (SELECT tour_id FROM acp_contract.route WHERE superseded_at IS NULL)
 GROUP BY 1, 2, 3
"""

# Tours with current ranking rows but no current route, their country, and their excluded rows of one market (the
# exclusion does not vary by market; US is the market every recompute writes).
Q_ROUTE_GAP_TOURS = """
SELECT ar.tour_id::text AS tour_id, coalesce(rt.country_iso2, '??') AS cc,
       count(*) FILTER (WHERE ar.excluded_reason IS NULL)::int AS kept,
       count(*) FILTER (WHERE ar.excluded_reason = 'transit')::int AS transit,
       count(*) FILTER (WHERE ar.excluded_reason IS NOT NULL AND ar.excluded_reason <> 'transit')::int AS other_excl
  FROM acp_contract.atom_ranking ar
  LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = ar.tour_id
 WHERE ar.superseded_at IS NULL AND ar.market = 'US'
   AND ar.tour_id NOT IN (SELECT tour_id FROM acp_contract.route WHERE superseded_at IS NULL)
 GROUP BY 1, 2
"""

Q_ROUTE_LAST = """
SELECT result->'route' AS route, extract(epoch FROM now() - finished_at)::float8 AS age_s
  FROM shared.job
 WHERE kind = 'recompute' AND status = 'succeeded' AND jsonb_typeof(result->'route') = 'object'
 ORDER BY finished_at DESC NULLS LAST LIMIT 1
"""


async def section_route(conn, countries):
    rd = _import("route_detection", lambda: __import__("services.acp_contract.route_detection",
                                                        fromlist=["route_gap_reason", "Moment", "derive_routes"]))
    if rd is not None and not hasattr(rd, "route_gap_reason"):   # an image before this PR: country table only
        print("ERR import route_gap_reason AttributeError")
        rd = None
    for r in await rows(conn, "route_country", Q_ROUTE_COUNTRY) or []:
        ratio = f"{100.0 * r['with_route'] / r['ranked_kept']:.1f}" if r["ranked_kept"] else "null"
        out(f"ROUTE.country.{r['cc']}", f"masters={r['masters']} ranked_any={r['ranked_any']} "
                                         f"ranked_kept={r['ranked_kept']} with_route={r['with_route']} "
                                         f"route_pct_of_ranked_kept={ratio}")
    last = await rows(conn, "route_last", Q_ROUTE_LAST)
    if last:
        rr = last[0]["route"]
        rr = json.loads(rr) if isinstance(rr, str) else (rr or {})
        out("ROUTE.last_run", " ".join(f"{k}={rr.get(k)}" for k in sorted(rr)) + f" age_s={fmt(last[0]['age_s'])}")
    out("ROUTE.stage_records_per_tour_reason", "no (totals only: routes_written / superseded / unchanged / "
                                               "tours_ranked); reasons below are re-derived")
    gap = await rows(conn, "route_gap_tours", Q_ROUTE_GAP_TOURS)
    moments = await rows(conn, "route_moments", Q_ROUTE_MOMENTS)
    if gap is None or moments is None or rd is None:
        return
    segs = defaultdict(list)
    for m in moments:
        segs[m["tour_id"]].append((m["segment_id"], m["place"], list(m["days"] or [])))
    per_cc = defaultdict(Counter)
    whatif = Counter()
    for g in sorted(gap, key=lambda x: (x["cc"], x["tour_id"])):
        t = g["tour_id"]
        f = tour_route_facts(segs.get(t, []), rd.Moment, rd.route_gap_reason, rd.derive_routes)
        reason = f["reason"]
        if not g["kept"]:
            reason = "all_excluded"            # every current ranking row of the tour is excluded (transit / unnamed)
        elif not segs.get(t):
            reason = "stale_ranking"           # kept ranking rows, but no live atom of the tour is in those Segments
        per_cc[g["cc"]][reason] += 1
        if f["whatif_all_days"] and reason != "derivable":
            whatif[g["cc"]] += 1
        if g["cc"] in countries:
            out(f"ROUTE.tour.{t}", f"country={g['cc']} us_rows_kept={g['kept']} us_rows_transit={g['transit']} "
                                   f"us_rows_other_excluded={g['other_excl']} segments={f['segments']} "
                                   f"days={f['days']} reason={reason} whatif_all_days={f['whatif_all_days']}")
    for cc in sorted(per_cc):
        out(f"ROUTE.gap_reasons.{cc}", " ".join(f"{k}={v}" for k, v in sorted(per_cc[cc].items())))
        out(f"ROUTE.whatif_all_days_would_route.{cc}", whatif.get(cc, 0))


# ── GC ────────────────────────────────────────────────────────────────────────────────────────────────────────────────

Q_GC_LAST = """
SELECT result->'segment_gc' AS gc, extract(epoch FROM now() - finished_at)::float8 AS age_s
  FROM shared.job
 WHERE kind = 'recompute' AND status = 'succeeded' AND jsonb_typeof(result->'segment_gc') = 'object'
 ORDER BY finished_at DESC NULLS LAST LIMIT 1
"""

Q_GC_KEPT_MEMBERS = """
SELECT coalesce(pt.master_status::text, 'no_master') AS master, coalesce(rt.source_status::text, 'no_source') AS src,
       (rt.deleted_at IS NOT NULL) AS src_deleted, count(DISTINCT m.segment_id)::int AS segments
  FROM acp_contract.atom_segment_member m
  JOIN acp_contract.tour_atoms t ON t.atom_id = m.atom_id AND NOT t.deleted AND NOT t.is_empty_marker
  LEFT JOIN gold_aa_internal.published_tours pt ON pt.tour_id = t.tour_id
  LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = t.tour_id
 WHERE m.segment_id = ANY($1::text[])
 GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
"""


async def section_gc(conn):
    gc = _import("segment_gc", lambda: __import__("services.acp_contract.segment_gc",
                                                   fromlist=["ORPHANS_SQL", "classify"]))
    if gc is not None:
        out("GC.rule", "removed = no live member AND no ranking row AND no alias AND no subject AND no brand_fit "
                       "AND not on a current / subject route AND not in an angle request; orphan = no active member, "
                       "no current ranking, not on a current route")
        out("GC.mode_env_in_this_container", gc.gc_mode())
        out("GC.max_delete", gc.gc_max())
        out("GC.reference_classes", ",".join(gc.REFERENCE_CLASSES))
    last = await rows(conn, "gc_last", Q_GC_LAST)
    if last:
        g = last[0]["gc"]
        g = json.loads(g) if isinstance(g, str) else (g or {})
        out("GC.last_run", " ".join(f"{k}={g.get(k)}" for k in ("mode", "status", "orphans", "deletable", "kept",
                                                                 "dead_members", "deleted_segments",
                                                                 "kept_on_recheck"))
            + f" age_s={fmt(last[0]['age_s'])}")
        out("GC.last_run.kept_by", json.dumps(g.get("kept_by") or {}, sort_keys=True))
    if gc is None:
        return
    orphan_rows = await rows(conn, "gc_orphans", gc.ORPHANS_SQL)
    if orphan_rows is None:
        return
    plan = gc.classify([dict(r) for r in orphan_rows])
    out("GC.today.orphans", plan["orphans"])
    out("GC.today.would_delete", len(plan["delete_ids"]))
    out("GC.today.kept", len(plan["kept_ids"]))
    out("GC.today.kept_by", json.dumps(plan["kept"], sort_keys=True))
    only_live = [r["segment_id"] for r in orphan_rows if r["live_member"]]
    for r in await rows(conn, "gc_kept_members", Q_GC_KEPT_MEMBERS, only_live) or []:
        out(f"GC.today.kept_live_member.master={r['master']}.source={r['src']}.deleted={r['src_deleted']}",
            r["segments"])


async def main():
    countries = tuple(c.strip().upper() for c in os.environ.get("COUNTRIES", ",".join(DEFAULT_COUNTRIES)).split(",")
                      if c.strip())
    now = datetime.now(timezone.utc)
    try:
        conn = await asyncpg.connect(os.environ["DATABASE_URL"], ssl="require",
                                     server_settings={"application_name": "aa1059-catalog-leftovers-diag",
                                                      "default_transaction_read_only": "on"})
    except Exception as e:  # noqa: BLE001
        print(f"ERR connect {type(e).__name__}")
        return
    try:
        await section_seg(conn, now)
        await section_s1(conn, now)
        await section_route(conn, countries)
        await section_gc(conn)
    finally:
        await conn.close()
    out("done", 1)


if __name__ == "__main__":
    asyncio.run(main())
