"""AA-919 WM-21 — the targeted shadow: a candidate release written on recent REAL inputs, never shown to anyone.

Shadow is the pre-activation check for hard stops 2 (tenant leak) and 3 (QA traces), WM README §8.2, with the
approved cut E2 as corrected by cost-cuts K2 / K4:

  size (eval_checks.shadow_size)  A1, T9 blog, LinkedIn and the other social channels 20; T2 and Facebook 50 (the
                                  golden set cannot gate them: holdout T2 = 3 items, no Facebook tier); a t9_social
                                  rules release with no --channel 50 Facebook + 20 other channels (shadow_quota: one
                                  shadow run is the release's evidence for every channel); a repair_hint-only release
                                  <= 10; lexicon_only none (the L0 path); an empty release none (CIS 287: nothing to
                                  shadow). `size` may lower it, never raise it; `force_size` (an operator check on Dev,
                                  recorded) runs an empty release anyway. A lowered or forced size is informative.
  inputs                          the most recent real inputs of the writer (last 90 days) where the release's
                                  target gates fired (its rules' quality classes -> the gates of those classes; T9
                                  reads the final gate ledger), else the most recent; golden items excluded (every id
                                  in their refs, matched on the row id AND on its tour / published tour, so a newer
                                  version of a golden tour is excluded too). A1 generated_content (writer v2 rows),
                                  T2 the newest succeeded t2_rewrite job per tour and tenant (input id = its
                                  tenant_tour_version), T9 the pieces with a writer_input snapshot. Gate targeting
                                  is T9 only (A1 / T2 keep no per-gate ledger on the row the shadow reads; they take
                                  the most recent).
  tenant release (scope 2)        its tenant's inputs + 1-2 inputs of ANOTHER tenant (`probe`), written with the
                                  candidate's view: a release resolved for the wrong tenant shows up as a leak.
  per input                       one write through the production writer path (eval_writers.regenerate) with the
                                  candidate view; the QA-artefact grep (the release's rule texts included) and the
                                  isolation probe against every other tenant's brand pack. pass / fail / error.
  stored                          one shared.writing_memory_shadow row per input (scope / tenant = the INPUT's owner,
                                  RLS, deleted with that tenant), one prompt_eval_runs row (run_kind shadow) the
                                  Releases tab attaches as `shadow_run_id`; every model call under stage
                                  `writing_memory_shadow` with quality_signal.eval_run (call_log.bind_eval_stage).
  money                           a hard cap per run (default $7, cost-cuts K3), read from llm_call_log before and
                                  after each input; the $25 month cap; stop at the first hard stop (fail-only).

Verdict: pass = every input written clean and as many inputs as the size rule asks (per bucket); fail = a hard stop;
informative = fewer real inputs than the rule (Dev), a lowered / forced size, or an error; invalid = the cap or > 20%
errors.

Cross-tenant probe rows (WM-18 open item, decided here): a probe row belongs to the probed tenant (its text) and points
at the other tenant's release (ON DELETE CASCADE). Erasing either tenant deletes it; the AA-839 planner reports it as a
cross-tenant check. Accepted: shadow rows are short-lived evidence (DM-7: 90 days after the release retires), and both
erasure directions remove tenant text, never keep it.

CLI (inside the api/worker container; DRY RUN prints the selection and the estimate, $0):
  python -m services.writing_memory.shadow --release <id> [--channel facebook] [--size 5] [--force-size 3]
         [--cap-usd 2] [--enqueue]
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import structlog

from services.writing_memory import config as cfg
from services.writing_memory import eval_checks as ec

logger = structlog.get_logger()

A1_TENANT = "00000000-0000-0000-0000-000000000001"


class ShadowRefused(ValueError):
    """The run cannot start; nothing was spent."""


@dataclass
class ShadowConfig:
    release_id: str
    channel: Optional[str] = None
    size: Optional[int] = None
    force_size: Optional[int] = None
    cap_usd: float = cfg.SHADOW_RUN_CAP_USD
    triggered_by: str = "manual"

    def validate(self) -> None:
        if not self.release_id:
            raise ShadowRefused("a shadow run needs a release")
        if not (0 < float(self.cap_usd) <= cfg.SHADOW_RUN_CAP_USD):
            raise ShadowRefused(f"cap_usd must be > 0 and <= {cfg.SHADOW_RUN_CAP_USD}")
        top = cfg.SHADOW_SIZE_WIDE + cfg.SHADOW_SIZE_DEFAULT          # the biggest rule (t9_social, no channel)
        for name in ("size", "force_size"):
            v = getattr(self, name)
            if v is not None and not 1 <= v <= top:
                raise ShadowRefused(f"{name} must be 1..{top}")


def planned_size(release: dict, conf: ShadowConfig) -> tuple[int, Optional[str]]:
    """(inputs to write, why it differs from the rule). The rule (shadow_size) caps `size`; `force_size` replaces a
    0 (lexicon / empty) for an operator check and is recorded."""
    rule = ec.shadow_size(release["writer"], release["content_class"], channel=conf.channel)
    if rule == 0:
        if conf.force_size:
            return conf.force_size, "force_size"
        return 0, None
    if conf.size is not None and conf.size < rule:
        return conf.size, "size_lowered"
    return rule, None


def planned_quota(release: dict, conf: ShadowConfig, n: int) -> dict[str, int]:
    """Per-bucket input counts for `n` inputs (eval_checks.shadow_quota; a lowered size fills Facebook first)."""
    rule = ec.shadow_quota(release["writer"], release["content_class"], channel=conf.channel)
    if set(rule) == {"facebook", "other"}:
        fb = min(n, rule["facebook"])
        return {"facebook": fb, "other": max(0, n - fb)}
    return {"all": n}


def _prompt_version(release: dict, writer: str) -> str:
    """The prompt register of the release (`fingerprint.prompt_sha256`, recorded by its R0 replay), else
    '<writer>:unrecorded' (a writer's system prompt is built per input; a shadow run does not measure it)."""
    fp = _j(release.get("fingerprint")) or {}
    sha_ = fp.get("prompt_sha256") if isinstance(fp, dict) else None
    return str(sha_ or f"{writer}:unrecorded")[:64]


def sha(obj: Any) -> str:
    raw = obj if isinstance(obj, str) else json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def target_gates(rule_classes: set[str]) -> list[str]:
    """The gates whose quality classes include one of the rules' classes (empty: no targeting)."""
    if not rule_classes:
        return []
    from services.quality import registry
    return sorted(g for g in registry.gates() if set(registry.gate_classes(g)) & rule_classes)


def _j(v):
    return json.loads(v) if isinstance(v, str) else v


# ── input selection (read-only) ────────────────────────────────────────────────────────────────────────────────────

_GOLDEN_IDS_SQL = """
    SELECT refs, input->>'piece_id' AS piece_id FROM shared.eval_golden_item i
      JOIN shared.eval_golden_set s ON s.set_id = i.set_id AND s.status = 'active'
"""

_T9_SQL = """
    SELECT cp.piece_id::text AS input_id, cp.tenant_id::text AS tenant_id, r.atom_id, cp.writer_input,
           cp.writer_input->>'channel' AS channel
      FROM acp_shared.content_piece cp
      JOIN acp_shared.angle_gate_request r ON r.request_id = cp.angle_gate_request_id AND r.tenant_id = cp.tenant_id
     WHERE cp.writer_input IS NOT NULL AND cp.writer_input ? 'brand_pack'
       AND cp.created_at >= now() - make_interval(days => $1)
       AND (CASE WHEN $2 THEN cp.writer_input->>'channel' = 'blog' ELSE cp.writer_input->>'channel' <> 'blog' END)
       AND ($3::text IS NULL OR cp.writer_input->>'channel' = $3)
       AND ($9::text IS NULL OR cp.writer_input->>'channel' IS DISTINCT FROM $9)
       AND ($4::uuid IS NULL OR cp.tenant_id = $4::uuid)
       AND ($5::uuid IS NULL OR cp.tenant_id <> $5::uuid)
       AND NOT (cp.piece_id::text = ANY($6::text[]))
       AND (cardinality($7::text[]) = 0 OR EXISTS (
             SELECT 1 FROM jsonb_array_elements(cp.gate_ledger) g
              WHERE g->>'gate' = ANY($7::text[]) AND coalesce((g->>'passed')::boolean, true) = false))
     ORDER BY cp.created_at DESC
     LIMIT $8
"""

_T2_SQL = """
    SELECT input_id, job_id, tenant_id, published_tour_id, rewrite_language
      FROM (SELECT DISTINCT ON (j.payload->>'published_tour_id', j.payload->>'tenant_id')
                   j.result->>'version_id' AS input_id, j.id::text AS job_id, j.payload->>'tenant_id' AS tenant_id,
                   j.payload->>'published_tour_id' AS published_tour_id,
                   coalesce(j.payload->>'rewrite_language', 'EN-US') AS rewrite_language, j.finished_at
              FROM shared.job j
             WHERE j.kind = 't2_rewrite' AND j.status = 'succeeded'
               AND coalesce(j.result->>'skipped', 'false') <> 'true'
               AND j.result->>'version_id' ~ '^[0-9a-f-]{36}$'
               AND j.finished_at >= now() - make_interval(days => $1)
               AND ($2::text IS NULL OR j.payload->>'tenant_id' = $2)
               AND ($3::text IS NULL OR j.payload->>'tenant_id' <> $3)
               AND NOT (coalesce(j.payload->>'published_tour_id', '') = ANY($4::text[]))
             ORDER BY j.payload->>'published_tour_id', j.payload->>'tenant_id', j.finished_at DESC) t
     WHERE NOT (input_id = ANY($4::text[]))
     ORDER BY finished_at DESC
     LIMIT $5
"""

_A1_SQL = """
    SELECT input_id, src_name, src_subtitle, src_summary, src_description, src_highlights, src_itineraries, country,
           duration, price_raw, inclusions, exclusions, group_size, best_time_to_go
      FROM (SELECT DISTINCT ON (gc.tour_id)
                   gc.id::text AS input_id, gc.created_at, rt.src_name, rt.src_subtitle, rt.src_summary,
                   rt.src_description, rt.src_highlights, rt.src_itineraries, rt.country, rt.duration, rt.price_raw,
                   rt.inclusions, rt.exclusions, rt.group_size, rt.best_time_to_go
              FROM silver_aa_internal.generated_content gc
              JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = gc.tour_id
             WHERE gc.metadata->>'writer_version' = 'v2' AND rt.deleted_at IS NULL
               AND gc.created_at >= now() - make_interval(days => $1)
               AND NOT (gc.id::text = ANY($2::text[]))
               AND NOT (gc.tour_id::text = ANY($2::text[]))
             ORDER BY gc.tour_id, gc.created_at DESC) t
     ORDER BY created_at DESC
     LIMIT $3
"""


async def golden_ids(conn) -> list[str]:
    """Every id a golden item names (refs values that look like ids, the piece id of its input): never a shadow
    input (WM-21 scope 1)."""
    ids: set[str] = set()
    for r in await conn.fetch(_GOLDEN_IDS_SQL):
        for v in (_j(r["refs"]) or {}).values():
            if isinstance(v, str) and len(v) == 36 and v.count("-") == 4:
                ids.add(v)
        if r["piece_id"]:
            ids.add(r["piece_id"])
    return sorted(ids)


async def _load_t9(conn, writer, *, n, channel, tenant, not_tenant, exclude, gates,
                   not_channel: Optional[str] = None) -> list[dict]:
    if n <= 0:
        return []
    rows = await conn.fetch(_T9_SQL, cfg.SHADOW_WINDOW_DAYS, writer == "t9_blog",
                            channel if writer == "t9_social" else None, tenant, not_tenant, exclude, gates, n,
                            not_channel if writer == "t9_social" else None)
    return [{"input_source": "content_piece", "input_id": r["input_id"], "tenant_id": r["tenant_id"],
             "channel": r["channel"], "input": {"writer_input": _j(r["writer_input"]), "atom_id": r["atom_id"],
                                                "tenant_id": r["tenant_id"]}} for r in rows]


async def _load_t2(pool, conn, *, n, tenant, not_tenant, exclude) -> list[dict]:
    from services.acp_shared.brand_pack import fetch_brand_pack_conn
    # the T2 job's own source query and builders: the shadow input is what production writes from
    from services.jobs.t2_rewrite_job import _PT_SQL, brand_rules_from_pack, source_tour_dict, t2_seo

    out = []
    for r in await conn.fetch(_T2_SQL, cfg.SHADOW_WINDOW_DAYS, tenant, not_tenant, exclude, n):
        pt = await conn.fetchrow(_PT_SQL, r["published_tour_id"])
        if pt is None:
            continue
        pack = await fetch_brand_pack_conn(conn, r["tenant_id"])
        brand = brand_rules_from_pack(pack, r["rewrite_language"]) if pack else {}
        brand.pop("_brand_loaded", None)
        brand.setdefault("rewrite_language", r["rewrite_language"])
        seo, _diag = await t2_seo(pool, dict(pt), r["tenant_id"])
        out.append({"input_source": "tenant_tour_version", "input_id": r["input_id"], "tenant_id": r["tenant_id"],
                    "channel": None, "input": {"tour": source_tour_dict(dict(pt)), "brand_rules": brand, "seo": seo,
                                               "tenant_id": r["tenant_id"],
                                               "rewrite_language": r["rewrite_language"]}})
    return out


async def _load_a1(conn, *, n, exclude) -> list[dict]:
    from api.routers.admin_pipeline import _resolve_brand_rule

    br = await _resolve_brand_rule(conn, A1_TENANT, None, None)
    brand: dict = {}
    if br:
        fw = br["forbidden_words"]
        brand = {"system_prompt": br["system_prompt"] or "", "style_guide": br["style_guide"] or "",
                 "forbidden_words": list(fw) if isinstance(fw, list) else json.loads(fw or "[]"),
                 "rewrite_language": "en-US"}
    out = []
    for r in await conn.fetch(_A1_SQL, cfg.SHADOW_WINDOW_DAYS, exclude, n):
        tour = {"name": r["src_name"], "subtitle": r["src_subtitle"], "summary": r["src_summary"],
                "description": r["src_description"], "highlights": _j(r["src_highlights"]) or [],
                "itineraries": r["src_itineraries"], "country": r["country"], "duration": r["duration"],
                "price": r["price_raw"], "inclusions": r["inclusions"], "exclusions": r["exclusions"]}
        out.append({"input_source": "generated_content", "input_id": r["input_id"], "tenant_id": None,
                    "channel": None, "input": {"tour": tour, "brand_rules": brand,
                                               "tour_agency": {"group_size": r["group_size"],
                                                               "best_time_to_go": r["best_time_to_go"]}}})
    return out


async def select_inputs(pool, release: dict, *, n: int, channel: Optional[str], gates: list[str],
                        quota: Optional[dict[str, int]] = None) -> list[dict]:
    """Owner inputs (n, per `quota` bucket: {"all": n} or {"facebook": f, "other": o}) + the cross-tenant probe inputs
    of a tenant release (marked `probe`). Every input carries its `bucket`."""
    writer = release["writer"]
    tenant = release["tenant_id"] if release["scope"] == "tenant" else None
    quota = quota or {"all": n}
    async with pool.acquire() as conn:
        exclude = await golden_ids(conn)
        if writer == "a1":
            return [{**x, "bucket": "all"} for x in await _load_a1(conn, n=n, exclude=exclude)]
        if writer == "t2":
            own = await _load_t2(pool, conn, n=n, tenant=tenant, not_tenant=None, exclude=exclude)
            own = [{**x, "bucket": "all"} for x in own]
            probe = await _load_t2(pool, conn, n=cfg.SHADOW_PROBE_INPUTS, tenant=None, not_tenant=tenant,
                                   exclude=exclude) if tenant else []
        else:
            own = []
            for bucket, k in quota.items():
                ch, not_ch = {"facebook": ("facebook", None), "other": (None, "facebook")}.get(bucket, (channel, None))
                got = await _load_t9(conn, writer, n=k, channel=ch, tenant=tenant, not_tenant=None,
                                     exclude=exclude, gates=gates, not_channel=not_ch)
                if gates and len(got) < k:       # targeted first, then the most recent to fill the size
                    seen = [x["input_id"] for x in got]
                    got += await _load_t9(conn, writer, n=k - len(got), channel=ch, tenant=tenant, not_tenant=None,
                                          exclude=exclude + seen, gates=[], not_channel=not_ch)
                own += [{**x, "bucket": bucket} for x in got]
            probe = await _load_t9(conn, writer, n=cfg.SHADOW_PROBE_INPUTS, channel=channel, tenant=None,
                                   not_tenant=tenant, exclude=exclude, gates=[]) if tenant else []
    k = ec.shadow_probe_count(release["scope"], len(probe))
    return own + [{**p, "probe": True, "bucket": "probe"} for p in probe[:k]]


async def tenant_packs(pool, tenant_ids: set[str]) -> dict[str, dict]:
    """Brand packs of the tenants a run touches + the golden set's tenants: the isolation probe's foreign texts."""
    from services.acp_shared.brand_pack import fetch_brand_pack_conn
    from services.writing_memory.r2 import PgStore
    packs: dict[str, dict] = {}
    gs = await PgStore(pool).golden_set()
    if gs:
        packs.update(await PgStore(pool).tenant_packs(gs["set_id"]))
    async with pool.acquire() as conn:
        for tid in sorted(t for t in tenant_ids if t):
            try:
                p = await fetch_brand_pack_conn(conn, tid)
            except Exception:  # noqa: BLE001
                p = None
            if p:
                packs[tid] = {**packs.get(tid, {}), **dict(p)}
    return packs


# ── the run ────────────────────────────────────────────────────────────────────────────────────────────────────────

async def run_shadow(pool, conf: ShadowConfig, *, writer_fn=None, flush=None, select=None, packs_fn=None,
                     store=None, progress=None) -> dict:
    """The shadow run. The injectable parts (writer_fn, flush, select, packs_fn, store) are the tests' seams."""
    from services.writing_memory import eval_writers as ew
    from services.writing_memory.r2 import build_views, flush_call_log
    from shared.llm_client.call_log import bind_eval_stage

    conf.validate()
    writer_fn = writer_fn or ew.regenerate
    flush = flush or flush_call_log
    select = select or select_inputs
    packs_fn = packs_fn or tenant_packs
    store = store or ShadowPgStore(pool)
    release = await store.get_release(conf.release_id)
    if release is None:
        raise ShadowRefused("unknown release")
    if release["status"] not in ("draft", "evaluated"):
        raise ShadowRefused(f"a {release['status']} release keeps its evidence (draft / evaluated only)")
    n, size_note = planned_size(release, conf)
    if n == 0:
        raise ShadowRefused(f"a {release['content_class']} release has nothing to shadow (WM-21 scope 1); "
                            "--force-size runs an operator check")
    writer = release["writer"]
    rules = await store.rules(list(release.get("rule_ids") or []))
    gates = target_gates({r["gate_class"] for r in rules if r.get("gate_class")})
    rule_texts = [(r["rule_id"], r["rule_text"]) for r in rules]
    quota = planned_quota(release, conf, n)
    inputs = await select(pool, release, n=n, channel=conf.channel, gates=gates, quota=quota)
    packs = await packs_fn(pool, {i.get("tenant_id") for i in inputs} | {release.get("tenant_id")})
    fp = await store.fingerprint(writer)
    month_start = await store.month_spend()
    started = datetime.now(timezone.utc)
    t0 = time.monotonic()
    details: dict[str, Any] = {"size_rule": ec.shadow_size(writer, release["content_class"], channel=conf.channel),
                               "size": n, "size_note": size_note, "channel": conf.channel, "target_gates": gates,
                               "found": sum(1 for i in inputs if not i.get("probe")),
                               "quota": quota,
                               "found_by_bucket": {b: sum(1 for i in inputs if not i.get("probe")
                                                          and i.get("bucket", "all") == b) for b in quota},
                               "probe_inputs": sum(1 for i in inputs if i.get("probe")), "cap_usd": conf.cap_usd,
                               "t9_first_draft_only": writer in ew.T9_WRITERS}
    run_id = await store.create_run({"writer": writer, "release_id": conf.release_id, "cap": conf.cap_usd,
                                     "fingerprint": fp, "details": details, "triggered_by": conf.triggered_by[:128],
                                     "prompt_version": _prompt_version(release, writer)})
    counts = {"written": 0, "pass": 0, "fail": 0, "error": 0, "hard_stop_2": 0, "hard_stop_3": 0}
    stop_reason = None

    async def spent() -> float:
        await flush()
        return await store.run_spend(run_id, started)

    try:
        with bind_eval_stage(cfg.SHADOW_STAGE, run_id):
            for i, inp in enumerate(inputs):
                s = await spent()
                est = cfg.SHADOW_INPUT_ESTIMATE_USD[writer]
                if s + est > conf.cap_usd or month_start + s + est > cfg.MONTH_CAP_USD:
                    stop_reason = "budget_cap"
                    break
                owner = inp.get("tenant_id")
                active = await store.active_parts(writer, owner)
                _ctl, view = build_views(writer, active, release)
                before = s
                verdict, output, artefacts, iso = "error", None, [], {"leak": False}
                try:
                    output = await writer_fn(writer, inp["input"], view=view, shared_inputs=None)
                    text = ew.output_text(writer, output)
                    artefacts = ec.qa_artefacts(text, rule_texts=rule_texts)
                    foreign = {t: ec.tenant_fingerprint(p) for t, p in packs.items() if t != owner}
                    iso = ec.isolation_probe(text, foreign=foreign, allowed_text=ew.allowed_text(writer, inp["input"]))
                    verdict = "fail" if (artefacts or iso["leak"]) else "pass"
                except Exception as e:  # noqa: BLE001 — a writer failure is a counted error, never a pass
                    logger.warning("aa919_shadow_writer_failed", run_id=run_id, input_id=inp["input_id"],
                                   error=f"{type(e).__name__}: {str(e)[:200]}")
                cost = max(await spent() - before, 0.0)
                await store.insert_shadow({
                    "release_id": conf.release_id, "run_id": run_id, "writer": writer,
                    "channel": inp.get("channel") if writer in ew.T9_WRITERS else None,
                    "scope": "tenant" if owner else "platform", "tenant_id": owner, "probe": bool(inp.get("probe")),
                    "input_source": inp["input_source"], "input_id": inp["input_id"], "input_sha256": sha(inp["input"]),
                    "output": output, "output_sha256": sha(output) if output else None,
                    "control_ref": {"source": inp["input_source"], "id": inp["input_id"]},
                    "isolation": iso, "qa_artefacts": artefacts[:50], "verdict": verdict, "cost_usd": round(cost, 6)})
                counts["written"] += 1
                counts[verdict] += 1
                if artefacts:
                    counts["hard_stop_3"] += 1
                if iso.get("leak"):
                    counts["hard_stop_2"] += 1
                if progress:
                    await progress(done=i + 1, total=len(inputs))
                if verdict == "fail":
                    stop_reason = "hard_stop"
                    break
                if await spent() > conf.cap_usd:
                    stop_reason = "budget_cap"
                    break
    except Exception as e:  # noqa: BLE001
        details["error"] = f"{type(e).__name__}: {str(e)[:300]}"
        stop_reason = "error"
        await _finish_shadow(store, run_id, details, counts, stop_reason, n, spent, t0)
        raise
    return await _finish_shadow(store, run_id, details, counts, stop_reason, n, spent, t0)


async def _finish_shadow(store, run_id, details, counts, stop_reason, n, spent, t0) -> dict:
    if stop_reason is None and ec.too_many_errors(counts["error"], counts["written"]):
        stop_reason = "error"
    stop_reason = stop_reason or "completed"
    if stop_reason == "hard_stop":
        verdict = "fail"
    elif stop_reason in ("budget_cap", "error") or counts["written"] == 0:
        verdict = "invalid"
    elif (details.get("size_note") or details["found"] < max(n, details.get("size_rule") or 0)
          or any(details.get("found_by_bucket", {}).get(b, 0) < k for b, k in (details.get("quota") or {}).items())
          or counts["error"] > 0):
        # a lowered / forced size, fewer real inputs than the rule (in total or in a bucket: Facebook's 50) or any
        # error: evidence of the machinery, never an attachable pass (WM-21 scope 1; R2's --limit is the same)
        verdict = "informative"
    else:
        verdict = "pass"
    try:
        cost = await spent()
    except Exception:  # noqa: BLE001
        cost = 0.0
    details.update({"counts": counts, "seconds": round(time.monotonic() - t0, 1)})
    await store.finish_run(run_id, {"tour_count": counts["written"], "cost_usd": round(cost, 6),
                                    "regression_detected": verdict == "fail", "details": details,
                                    "stop_reason": stop_reason, "verdict": verdict, "judge_kappa": None,
                                    "judge_counts": None})
    logger.info("aa919_shadow_done", run_id=run_id, verdict=verdict, stop_reason=stop_reason, cost_usd=round(cost, 4),
                **{k: counts[k] for k in ("written", "pass", "fail", "error")})
    return {"run_id": run_id, "verdict": verdict, "stop_reason": stop_reason, "cost_usd": round(cost, 6),
            "counts": counts}


class ShadowPgStore:
    """Postgres side of the shadow run (cis_app)."""

    def __init__(self, pool):
        from services.writing_memory.r2 import PgStore
        self.pool = pool
        self._r2 = PgStore(pool, stages=(cfg.SHADOW_STAGE,))

    async def get_release(self, release_id):
        return await self._r2.get_release(release_id)

    async def active_parts(self, writer, tenant_id):
        return await self._r2.active_parts(writer, tenant_id)

    async def fingerprint(self, writer):
        return await self._r2.fingerprint(writer)

    async def month_spend(self):
        return await self._r2.month_spend()

    async def run_spend(self, run_id, since):
        return await self._r2.run_spend(run_id, since)

    async def finish_run(self, run_id, row):
        await self._r2.finish_run(run_id, row)

    async def rules(self, rule_ids):
        if not rule_ids:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT rule_id::text AS rule_id, rule_text, gate_class FROM shared.writing_rule "
                                    "WHERE rule_id = ANY($1::uuid[])", rule_ids)
        return [dict(r) for r in rows]

    async def create_run(self, row):
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """INSERT INTO shared.prompt_eval_runs
                       (pipeline, prompt_version, tour_count, cost_usd, regression_detected, details, triggered_by,
                        run_kind, writer, release_id, cost_cap_usd, fingerprint)
                   VALUES ('writing_memory', $1, 0, 0, false, $2::jsonb, $3, 'shadow', $4, $5::uuid, $6, $7::jsonb)
                   RETURNING id::text""",
                row["prompt_version"], json.dumps(row["details"], default=str), row["triggered_by"], row["writer"],
                row["release_id"], row["cap"], json.dumps(row["fingerprint"], default=str))

    async def insert_shadow(self, row):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO shared.writing_memory_shadow
                       (release_id, run_id, writer, channel, scope, tenant_id, probe, input_source, input_id,
                        input_sha256, output, output_sha256, control_ref, isolation, qa_artefacts, verdict, cost_usd)
                   VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6::uuid, $7, $8, $9::uuid, $10, $11::jsonb, $12,
                           $13::jsonb, $14::jsonb, $15::text[], $16, $17)""",
                row["release_id"], row["run_id"], row["writer"], row["channel"], row["scope"], row["tenant_id"],
                row["probe"], row["input_source"], row["input_id"], row["input_sha256"],
                None if row["output"] is None else json.dumps(row["output"], ensure_ascii=False, default=str),
                row["output_sha256"], json.dumps(row["control_ref"]), json.dumps(row["isolation"], default=str),
                row["qa_artefacts"], row["verdict"], row["cost_usd"])


async def estimate(pool, conf: ShadowConfig) -> dict:
    """The DRY RUN: the size, the selected input ids (no text) and the estimate, $0."""
    conf.validate()
    store = ShadowPgStore(pool)
    release = await store.get_release(conf.release_id)
    if release is None:
        raise ShadowRefused("unknown release")
    n, note = planned_size(release, conf)
    rules = await store.rules(list(release.get("rule_ids") or []))
    gates = target_gates({r["gate_class"] for r in rules if r.get("gate_class")})
    quota = planned_quota(release, conf, n)
    inputs = await select_inputs(pool, release, n=n, channel=conf.channel, gates=gates, quota=quota) if n else []
    return {"release_id": conf.release_id, "writer": release["writer"], "content_class": release["content_class"],
            "size": n, "size_note": note, "quota": quota, "target_gates": gates,
            "inputs": [{"source": i["input_source"], "id": i["input_id"], "probe": bool(i.get("probe"))}
                       for i in inputs],
            "estimated_usd_max": round(len(inputs) * cfg.SHADOW_INPUT_ESTIMATE_USD[release["writer"]], 3),
            "cap_usd": conf.cap_usd, "month_spend_usd": round(await store.month_spend(), 4)}


def payload_of(conf: ShadowConfig) -> dict:
    return {k: v for k, v in asdict(conf).items() if v is not None}


def config_from_payload(payload: dict) -> ShadowConfig:
    known = set(ShadowConfig.__dataclass_fields__)
    return ShadowConfig(**{k: v for k, v in (payload or {}).items() if k in known})


async def _main(argv=None) -> int:
    import asyncpg

    from shared.secrets import get_database_url
    ap = argparse.ArgumentParser(description="AA-919 WM-21 targeted shadow (DRY RUN by default, $0)")
    ap.add_argument("--release", required=True)
    ap.add_argument("--channel")
    ap.add_argument("--size", type=int)
    ap.add_argument("--force-size", type=int)
    ap.add_argument("--cap-usd", type=float, default=cfg.SHADOW_RUN_CAP_USD)
    ap.add_argument("--requested-by", default="cli")
    ap.add_argument("--enqueue", action="store_true")
    a = ap.parse_args(argv)
    conf = ShadowConfig(release_id=a.release, channel=a.channel, size=a.size, force_size=a.force_size,
                        cap_usd=a.cap_usd, triggered_by=f"cli:{a.requested_by}")
    pool = await asyncpg.create_pool(get_database_url().strip(), ssl="require", min_size=1, max_size=2)
    try:
        print(json.dumps(await estimate(pool, conf), indent=1, default=str))
        if a.enqueue:
            import services.jobs.writing_memory_job  # noqa: F401 — registers the kinds
            from services.jobs.writing_memory_job import SHADOW
            from shared.jobs.registry import enqueue
            job_id, created = await enqueue(pool, SHADOW, payload_of(conf), created_by=conf.triggered_by)
            print(json.dumps({"enqueued": SHADOW, "job_id": job_id, "created": created}))
    finally:
        await pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
