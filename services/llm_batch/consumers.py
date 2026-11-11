"""AA-1076 — the consumers of the gateway batch lane: what a batch of each kind sends (``prepare``) and where its
replies go (``ingest``). The ``llm_batch`` job (services/jobs/llm_batch_job.py) drives them.

- ``s1_attempt1`` (S1 attempt 1 in a wave, the AA-606 split): one request per tour, the S1 writer v2's writer call
  exactly as ``s1_writer_v2_live.writer_prompts`` builds it for the live run. At ingest each tour gets its normal
  ``s1_rewrite`` job (same idempotency key as ``POST /admin/run-tour-async``: tour + batch) carrying its usable reply;
  the job binds it and the writer takes it when the prompt it builds then is byte-identical. The meta calls,
  validate, judge, repairs and retries all run on demand as today. A tour whose request failed, expired or was
  cancelled (or whose prompt changed since) is rewritten fully on demand by the same job. Writer v2 only: with the
  v2 switch off the lane refuses (the v1 seed path is Bedrock Batch's, ``s1_batch.py``).
- ``a3_atomize`` (A3 re-atomize waves): one request per day that the live atomize would read (its fingerprint
  differs), the per-day prompt ``build_day_user_prompt(atomize_row(...))`` of the tour's served Master. At ingest each
  tour gets an ``a3_atomize`` job carrying its usable day replies (idempotency key on version + batch); days without a
  usable reply are read live by that job. Tours in the legacy (non-canonical) format are not batched: they keep the
  normal path.
- ``adhoc`` (backfills, eval panels): the caller gives the requests (a stage ``adhoc_<issue>`` for the ledger and the
  stage whose route to run); the results stay with the job (``result.items``, text trimmed) and on the provider for
  29 days.
"""
from __future__ import annotations

import json
from typing import Optional

import structlog

from shared.llm_client import batch_lane
from shared.llm_client.batch_lane import BatchItem, BatchTarget, BatchUnavailable, ItemResult

logger = structlog.get_logger()

PLATFORM_TENANT_ID = "00000000-0000-0000-0000-000000000001"
MAX_TOURS = 200                 # one wave per batch: 3, then at most 50, then per country (skill aa-wave-rerun)
MAX_ADHOC_ITEMS = 5000
ADHOC_TEXT_KEEP = 20000         # characters of each ad-hoc reply kept in the job result


def _tour_ids(payload: dict) -> list[str]:
    """The batch's tours: the payload's top-level ``tour_ids`` (the wave report and the wave quality check find a
    batch's tours there, services/waves)."""
    raw = payload.get("tour_ids")
    if not isinstance(raw, list) or not raw:
        raise BatchUnavailable("tour_ids must be a non-empty list")
    seen, out = set(), []
    for t in raw:
        t = str(t).strip().lower()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    if len(out) > MAX_TOURS:
        raise BatchUnavailable(f"{len(out)} tours: one batch takes at most {MAX_TOURS} (one wave)")
    return out


def _hex(tour_id: str) -> str:
    return tour_id.replace("-", "")


# ── S1 attempt 1 ─────────────────────────────────────────────────────────────────────────────────────────────────

S1_STAGE = "s1_generate"


async def _s1_state(conn, tour_id: str, tenant_uuid: str, writing_memory) -> Optional[dict]:
    """The A1 graph state fields the writer v2's prompts read, loaded the way ``_execute_run_tour`` loads them (raw
    row, the agency fields, the brand's forbidden words, the run's writing-memory release). None: missing / trashed /
    superseded (the run would refuse it too)."""
    from api.routers.admin_pipeline import _resolve_brand_rule

    row = await conn.fetchrow("SELECT * FROM silver_aa_internal.raw_tours WHERE tour_id = $1::uuid", tour_id)
    if not row or str(row.get("source_status") or "") in ("trashed", "superseded"):
        return None
    highlights = row["src_highlights"]
    if not isinstance(highlights, list):
        highlights = json.loads(highlights) if highlights else []
    tour = {
        "name": row["src_name"], "subtitle": row["src_subtitle"], "summary": row["src_summary"],
        "description": row["src_description"], "highlights": highlights, "itineraries": row["src_itineraries"],
        "country": row["country"], "duration": row["duration"], "price": row["price_raw"],
        "inclusions": row["inclusions"], "exclusions": row["exclusions"],
    }
    forbidden: list = []
    try:
        br = await _resolve_brand_rule(conn, tenant_uuid, None, None)
        if br:
            fw = br["forbidden_words"]
            forbidden = list(fw) if isinstance(fw, list) else json.loads(fw or "[]")
    except Exception as e:  # noqa: BLE001 — as the run: no brand row = no brand words (the prompt sha then decides)
        logger.warning("llm_batch_s1_brand_failed", tour_id=tour_id, error=str(e)[:200])
    state = {"tour": tour, "tour_agency": {"group_size": row.get("group_size"),
                                           "best_time_to_go": row.get("best_time_to_go")},
             "brand_forbidden_words": forbidden, "generate_stage": S1_STAGE}
    if writing_memory is not None:
        state["writing_memory"] = writing_memory
    return state


async def prepare_s1(pool, payload: dict) -> tuple[str, list[BatchItem], dict]:
    from services.content_generation import s1_writer_v2 as v2
    from services.content_generation import s1_writer_v2_live
    from services.writing_memory.insertion import resolve as wm_resolve

    if not s1_writer_v2_live.enabled():
        raise BatchUnavailable("the S1 batch lane seeds the writer v2 only, and the S1 writer v2 switch is off: "
                               "rewrite through the per-tour path")
    tour_ids = _tour_ids(payload)
    items: list[BatchItem] = []
    skipped: list[str] = []
    async with pool.acquire() as conn:
        view = await wm_resolve(conn, "a1")
        for tid in tour_ids:
            state = await _s1_state(conn, tid, PLATFORM_TENANT_ID, view)
            if state is None:
                skipped.append(tid)
                continue
            wp = s1_writer_v2_live.writer_prompts(state)
            items.append(BatchItem(custom_id=f"s1-{_hex(tid)}", system=wp["system"], user=wp["user"],
                                   max_tokens=v2.WRITER_MAX_TOKENS, tour_id=tid))
    return S1_STAGE, items, {"skipped": skipped}


async def ingest_s1(pool, payload: dict, target: BatchTarget, items: list[dict], results: dict[str, ItemResult],
                    *, batch_id: str) -> dict:
    from api.routers.admin_pipeline import TourRunRequest
    from shared.jobs.registry import enqueue

    args = payload.get("args") or {}
    run_batch = str(args.get("batch_id") or batch_id)
    # The live fallback drill (an operator option, at most one tour): the reply of the first usable item is withheld,
    # so that tour is rewritten on demand exactly as a failed / expired / cancelled item is. Its ledger row stays.
    withhold = min(int(args.get("drill_withhold") or 0), 1)
    seeded = on_demand = 0
    jobs = []
    withheld: list[str] = []
    for it in items:
        tid = it["tour_id"]
        r = results.get(it["custom_id"])
        usable = r is not None and r.usable
        if usable and len(withheld) < withhold:
            withheld.append(tid)
            usable = False
        replies = [batch_lane.reply_record(target, r, it["prompt_sha"], batch_id=batch_id)] if usable else []
        req = TourRunRequest(tour_id=tid, batch_id=run_batch, tenant_id=PLATFORM_TENANT_ID,
                             seo_mode=str(args.get("seo_mode") or "standard"))
        body = {**req.model_dump(exclude={"review_id", "reason_code"}), "batch_replies": replies,
                "llm_batch_id": str(batch_id)}
        job_id, created = await enqueue(pool, "s1_rewrite", body, idempotency_key=f"s1_rewrite:{tid}:{run_batch}",
                                        created_by=f"llm_batch:{batch_id}")
        seeded += bool(replies)
        on_demand += not replies
        jobs.append({"tour_id": tid, "job_id": job_id, "created": created, "seeded": bool(replies),
                     "outcome": r.outcome if r is not None else "missing", **({"drill_withheld": True}
                                                                            if tid in withheld else {})})
    return {"kind": "s1_rewrite", "enqueued": len(jobs), "seeded": seeded, "on_demand": on_demand,
            "drill_withheld": withheld, "jobs": jobs}


# ── A3 atomize ───────────────────────────────────────────────────────────────────────────────────────────────────

A3_STAGE = "a3_atomize"

_MASTERS_SQL = """
SELECT pt.tour_id::text AS tour_id, pt.generated_content_id::text AS version_id, pt.aa_name, pt.aa_summary,
       pt.aa_highlights, pt.aa_itineraries, rt.country
  FROM gold_aa_internal.published_tours pt
  JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
 WHERE pt.tenant_id = $1::uuid AND pt.tour_id = ANY($2::uuid[]) AND pt.deleted_at IS NULL
"""


def _rewritten(r) -> dict:
    return {"name": r["aa_name"], "summary": r["aa_summary"], "highlights": r["aa_highlights"],
            "itineraries": r["aa_itineraries"]}


async def _masters(pool, tour_ids: list[str]) -> dict[str, dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(_MASTERS_SQL, PLATFORM_TENANT_ID, tour_ids)
    return {r["tour_id"]: dict(r) for r in rows if r["version_id"]}


async def prepare_a3(pool, payload: dict) -> tuple[str, list[BatchItem], dict]:
    from services.acp_contract import a3_atomize as a3
    from shared.llm_client.role_config import get_stage_config

    tour_ids = _tour_ids(payload)
    masters = await _masters(pool, tour_ids)
    cfg = await get_stage_config(A3_STAGE)
    items: list[BatchItem] = []
    legacy, unchanged, missing = [], [], [t for t in tour_ids if t not in masters]
    async with pool.acquire() as conn:
        for tid in tour_ids:
            m = masters.get(tid)
            if m is None:
                continue
            row = a3.atomize_row(tid, _rewritten(m))
            days = a3.parse_canonical_itinerary_days(row["itinerary_source"])
            if not days:
                legacy.append(tid)              # the whole-tour legacy read keeps the normal path
                continue
            fps = await conn.fetch("SELECT day_number, fingerprint_hash FROM acp_contract.atomize_day_fingerprint "
                                   "WHERE tenant_tour_version_id = $1::uuid", m["version_id"])
            existing = {r["day_number"]: r["fingerprint_hash"] for r in fps}
            n = 0
            for day_num in sorted(days):
                day = days[day_num]
                if existing.get(day_num) == a3._day_fingerprint(day["title"], day["body"], cfg.model_id):
                    continue
                items.append(BatchItem(
                    custom_id=f"a3-{_hex(tid)}-{day_num}", system=a3._SYSTEM_PROMPT,
                    user=a3._build_day_user_prompt(row, day_num, day["title"], day["body"]),
                    max_tokens=a3._MAX_TOKENS, tour_id=tid, meta={"day": day_num, "version_id": m["version_id"]}))
                n += 1
            if not n:
                unchanged.append(tid)
    return A3_STAGE, items, {"legacy_on_demand": legacy, "unchanged": unchanged, "not_served": missing}


async def ingest_a3(pool, payload: dict, target: BatchTarget, items: list[dict], results: dict[str, ItemResult],
                    *, batch_id: str) -> dict:
    from services.jobs.a3_atomize_job import enqueue_a3_atomize

    by_tour: dict[str, list[dict]] = {}
    for it in items:
        r = results.get(it["custom_id"])
        lst = by_tour.setdefault(it["tour_id"], [])
        if r is not None and r.usable:
            lst.append(batch_lane.reply_record(target, r, it["prompt_sha"], batch_id=batch_id))
    masters = await _masters(pool, list(by_tour))
    jobs = []
    for tid, replies in by_tour.items():
        m = masters.get(tid)
        if m is None:
            jobs.append({"tour_id": tid, "job_id": None, "reason": "no served Master any more"})
            continue
        job_id = await enqueue_a3_atomize(pool, tour_id=tid, version_id=m["version_id"], country=m["country"] or "",
                                          rewritten=_rewritten(m), created_by=f"llm_batch:{batch_id}",
                                          batch_replies=replies, batch_id=batch_id)
        jobs.append({"tour_id": tid, "job_id": job_id, "seeded_days": len(replies)})
    return {"kind": "a3_atomize", "enqueued": sum(1 for j in jobs if j.get("job_id")),
            "seeded_days": sum(j.get("seeded_days") or 0 for j in jobs), "jobs": jobs}


# ── ad hoc ───────────────────────────────────────────────────────────────────────────────────────────────────────

async def prepare_adhoc(pool, payload: dict) -> tuple[str, list[BatchItem], dict]:
    args = payload.get("args") or {}
    stage = str(args.get("stage") or "")
    if not stage.startswith("adhoc_"):
        raise BatchUnavailable("an ad-hoc batch logs under a stage named adhoc_<issue>")
    raw = args.get("items")
    if not isinstance(raw, list) or not raw or len(raw) > MAX_ADHOC_ITEMS:
        raise BatchUnavailable(f"args.items must hold 1 to {MAX_ADHOC_ITEMS} requests")
    items = [BatchItem(custom_id=str(i["custom_id"]), system=str(i.get("system") or ""), user=str(i["user"]),
                       max_tokens=int(i.get("max_tokens") or 4096), json_schema=i.get("json_schema"),
                       tour_id=i.get("tour_id")) for i in raw]
    return str(args.get("route_stage") or ""), items, {"ledger_stage": stage}


async def ingest_adhoc(pool, payload: dict, target: BatchTarget, items: list[dict], results: dict[str, ItemResult],
                       *, batch_id: str) -> dict:
    out = []
    for it in items:
        r = results.get(it["custom_id"])
        out.append({"custom_id": it["custom_id"], "outcome": r.outcome if r else "missing",
                    "usable": bool(r and r.usable), "text": (r.text or "")[:ADHOC_TEXT_KEEP] if r else ""})
    return {"kind": "adhoc", "items": out}


CONSUMERS = {
    "s1_attempt1": (prepare_s1, ingest_s1),
    "a3_atomize": (prepare_a3, ingest_a3),
    "adhoc": (prepare_adhoc, ingest_adhoc),
}


def consumer(name: str):
    try:
        return CONSUMERS[name]
    except KeyError:
        raise BatchUnavailable(f"unknown batch consumer {name!r} (one of {sorted(CONSUMERS)})") from None
