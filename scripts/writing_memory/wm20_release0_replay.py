"""AA-919 WM-20 — Release 0 replay: rebuild the system + user prompt of the four writers (A1 S1 writer v2, T2, T9
blog, T9 social) from stored inputs and prove that the writing-memory insertion path changes no byte. $0 and
read-only: no model call (a recorder client captures the request on the live call path and raises before anything is
sent; `services.golden_set.llm_guard` makes every other model entry point raise), one READ ONLY transaction, no row
written. The report holds ids, sha256 digests and counts only, never text (the inputs are tenant text).

Phases (the same file runs on both images):

  pre   On the image BEFORE the WM-18 / WM-20 deploy (today's writers, no insertion path). Captures (system, user)
        through the live call path for >= --min stored inputs per writer and records their full sha256. This is the
        independent reference (AA-919 critic 1): the post phase compares the new code with it, not with itself.
  post  On the deployed image. For the same input ids (from --pre, else freshly selected) captures twice: (b) memory
        disabled (no view) and (c) the release-0 view (an empty release through the new insertion path), then
        compares (a) the pre digests, (b) and (c) byte for byte (sha256 of the full strings), plus the stored hash
        where the writer stores one: A1 `generated_content.metadata.prompt_version` (8 hex of the system prompt, the
        v2 writer) and T9 `content_piece.writer_input.writer_prompt_version` (8 hex of SYSTEM_PROMPT).

How each writer is captured (the live call path, AA-919 critic 3):
  a1   `s1_writer_v2_live.write(state, client=Recorder)` — the first call (the writer call) of the v2 writer. Inputs:
       the newest `generated_content` row per tour written by v2 (`metadata.writer_version = 'v2'`), its raw_tours
       source and agency fields, and the A1 brand rule as the live run resolves it today.
  t2   `graph.generate_node(state)` with `graph.LLMClient` replaced by the recorder (the T2 rewrite path). Inputs: the
       newest succeeded `shared.job` of kind t2_rewrite per published tour; tour, brand pack and SEO rebuilt as the
       job does (read-only).
  t9   `generate.write_content(...)` with `generate.LLMClient` replaced by the recorder. Inputs: the newest pieces with
       a `writer_input` per channel group (blog / not blog), kwargs rebuilt from it as `run_write_background` passes
       them (sibling openers are not stored: [] on every side).

No context rebuild may call a model (critic 7): the A1 fact table is deterministic, T2 SEO is read from the caches the
job reads, T9 runs from the stored writer_input. When S1_STRUCTURED_FACTS is on, the T2 rebuild skips the extraction
(a model call) and marks the input `structured_facts_skipped`.

Usage (S3-mediated ECS exec on aa-cis-dev-api; the DB login is the container's DATABASE_URL = cis_app; the bronze
bucket comes from BRONZE_BUCKET; give keys, never a bucket or account):
  python3 /tmp/s.py --phase pre  --min 20 --out-key scripts/out/wm20/pre/<stamp>.json
  python3 /tmp/s.py --phase post --min 20 --pre s3:scripts/out/wm20/pre/<stamp>.json \\
                    --out-key scripts/out/wm20/post/<stamp>.json
The post report is the input of `python -m services.writing_memory.release0 --report s3:<key> [--apply]`, which
re-derives the verdict per writer from the per-input digests and writes the r0_replay runs and the release-0 rows.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from unittest.mock import patch

SCHEMA = "wm20-release0-replay/1"
WRITERS = ("a1", "t2", "t9_blog", "t9_social")
A1_TENANT = "00000000-0000-0000-0000-000000000001"   # aa_internal (admin_pipeline TENANT_SLUG_MAP)
T9_SOCIAL_CHANNELS = ("linkedin", "facebook", "instagram", "tiktok", "email", "landing_page", "ads")


def sha(text: Optional[str]) -> Optional[str]:
    return None if text is None else hashlib.sha256(text.encode("utf-8")).hexdigest()


def short(text: Optional[str]) -> Optional[str]:
    """`batch_prompt.prompt_version_of`: 8 hex of the sha256 (what A1 and T9 store)."""
    h = sha(text)
    return h[:8] if h else None


# ------------------------------------------------------------------------------------------------ the recorder

class Captured(Exception):
    """Raised by the recorder after it captured a request: the writer stops there, nothing is sent."""


class Recorder:
    """Stands in for LLMClient: records (system, user) of every request and raises before any call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def generate(self, request, *args, **kwargs):  # noqa: ARG002 — the LLMClient signature
        self.calls.append((request.system_prompt, request.user_prompt))
        raise Captured()


@contextmanager
def _client_patched(module, recorder: Recorder):
    with patch.object(module, "LLMClient", lambda *a, **k: recorder):
        yield


# ------------------------------------------------------------------------------------------------ graph state

def _mirror_initial_state(tour: dict, *, brand_rules: Optional[dict] = None, seo: Optional[dict] = None,
                          model_tier: Optional[str] = None, is_tenant_rewrite: bool = False,
                          tenant_id: Optional[str] = None, generate_stage: str = "s1_generate",
                          subtitle_focus: str = "standard", seo_mode: str = "dataforseo", feedback: str = "",
                          tour_agency: Optional[dict] = None) -> dict:
    """The graph state `v1_pipeline._rewrite_tour` built before AA-919 (a copy, used on the pre image only, where
    `initial_state_for` does not exist yet). The CI test pins that both give the same prompts."""
    _br = brand_rules or {}
    state = {
        "tour": tour, "seo": seo or {}, "model_tier": model_tier, "is_tenant_rewrite": is_tenant_rewrite,
        "tenant_id": tenant_id, "generate_stage": generate_stage, "few_shots": [], "generated": {},
        "quality_score": 0.0, "retry_count": 0, "feedback": feedback, "error": "", "cost_usd": 0.0, "model_used": "",
        "brand_system_prompt": _br.get("system_prompt", ""), "brand_style_guide": _br.get("style_guide", ""),
        "brand_forbidden_words": _br.get("forbidden_words", []),
        "rewrite_language": _br.get("rewrite_language", "en-US"),
        "brand_core_idea": _br.get("core_idea", ""), "brand_customer_segment": _br.get("customer_segment", ""),
        "brand_customer_mindset": _br.get("customer_mindset", ""),
        "brand_voice_examples": _br.get("voice_examples", []), "brand_good_examples": _br.get("good_examples", ""),
        "brand_name": _br.get("brand_name", ""), "brand_type": _br.get("brand_type", ""),
        "brand_default_cta": _br.get("default_cta", ""), "brand_signature_words": _br.get("signature_words", []),
        "subtitle_focus": subtitle_focus, "seo_mode": seo_mode,
        "brand_audit_status": "", "brand_audit_codes": [], "brand_audit_issues": [], "brand_audit_fields": [],
        "lessons_extracted": [], "fix_pass_applied": False, "fix_pass_fields": [], "quality_shadow": [],
        "degraded": [],
    }
    if tour_agency and not is_tenant_rewrite:
        state["tour_agency"] = tour_agency
    return state


def initial_state(tour: dict, *, writing_memory: Optional[dict] = None, **kw) -> dict:
    """The graph state of a live rewrite: `v1_pipeline.initial_state_for` when this image has it (post), else the
    pre-AA-919 copy (pre). `writing_memory` (the release view) is passed only when given."""
    try:
        from api.routers.v1_pipeline import initial_state_for
    except ImportError:
        if writing_memory is not None:
            raise RuntimeError("this image has no insertion path (pre phase): a view cannot be passed")
        return _mirror_initial_state(tour, **kw)
    if writing_memory is None:
        return initial_state_for(tour, **kw)
    return initial_state_for(tour, writing_memory=writing_memory, **kw)


# ------------------------------------------------------------------------------------------------ capture

class Capture(dict):
    """{system, user, consumed (the stamp the writer returned, post only), flags}."""


def capture_a1(inp: dict, view: Optional[dict] = None) -> Capture:
    from services.content_generation import s1_writer_v2_live

    state = initial_state(inp["tour"], brand_rules=inp.get("brand_rules") or {}, tour_agency=inp.get("tour_agency"),
                          generate_stage="s1_generate", writing_memory=view)
    rec = Recorder()
    out = s1_writer_v2_live.write(state, client=rec)
    if not rec.calls:
        raise RuntimeError(f"a1: the v2 writer made no request ({out.get('error')})")
    system, user = rec.calls[0]
    return Capture(system=system, user=user, consumed=out.get("writing_release"), flags=[],
                   prompt_version=out.get("prompt_version"))


def capture_t2(inp: dict, view: Optional[dict] = None) -> Capture:
    from services.content_generation import graph
    from services.content_generation.source_facts import s1_structured_facts_enabled

    state = initial_state(inp["tour"], brand_rules=inp.get("brand_rules") or {}, seo=inp.get("seo") or {},
                          is_tenant_rewrite=True, tenant_id=inp.get("tenant_id"), generate_stage="t2_generate",
                          writing_memory=view)
    flags = []
    if s1_structured_facts_enabled(None):
        state["structured_facts"] = None       # never the extraction call (critic 7)
        flags.append("structured_facts_skipped")
    rec = Recorder()
    with _client_patched(graph, rec):
        graph.generate_node(state)
    if not rec.calls:
        raise RuntimeError("t2: generate_node made no request")
    system, user = rec.calls[0]
    return Capture(system=system, user=user, consumed=None, flags=flags)


def t9_kwargs(wi: dict, *, atom_id: Optional[str], tenant_id: Optional[str] = None,
              request_id: Optional[str] = None) -> dict:
    """`write_content` kwargs as `run_write_background` passes them, from a stored `writer_input`."""
    from services.acp_angle_gate.channel_style import get_channel_style
    from services.acp_angle_gate.goals import get_goal

    channel = wi["channel"]
    goal = get_goal(wi.get("goal"))
    style = get_channel_style(channel)
    if goal is None or style is None:
        raise ValueError(f"unknown goal {wi.get('goal')!r} or channel {channel!r}")
    moments = wi.get("route_moments") or []
    kw: dict[str, Any] = {
        "content_seed": wi.get("seed") or "", "goal": goal, "channel_style": style,
        "brand_audience": wi.get("brand_pack") or {}, "angle": wi.get("angle") or {}, "cta": wi.get("cta"),
        "destination": wi.get("destination"), "trip_name": wi.get("writer_trip", wi.get("trip")),
        "atom_id": atom_id, "scope": wi.get("scope"),
        "route_segments": [(m["atom_id"], m["seed"]) for m in moments] or None, "keyword": wi.get("keyword"),
        "tenant_id": tenant_id, "angle_gate_request_id": request_id, "facts_text": wi.get("facts") or "",
    }
    questions = [q["question"] if isinstance(q, dict) else q for q in (wi.get("search_questions") or [])]
    if questions:
        kw["search_questions"] = questions
    if wi.get("lead_focus"):
        kw["lead_focus"] = wi["lead_focus"]
    return kw


def capture_t9(inp: dict, view: Optional[dict] = None) -> Capture:
    from services.acp_content_writing import generate

    kw = t9_kwargs(inp["writer_input"], atom_id=inp.get("atom_id"), tenant_id=inp.get("tenant_id"),
                   request_id=inp.get("request_id"))
    if view is not None:
        kw["memory"] = view
    rec = Recorder()
    with _client_patched(generate, rec):
        try:
            generate.write_content(**kw)
        except Captured:
            pass
    if not rec.calls:
        raise RuntimeError("t9: write_content made no request")
    system, user = rec.calls[0]
    return Capture(system=system, user=user, consumed=None, flags=[])


CAPTURE: dict[str, Callable[..., Capture]] = {"a1": capture_a1, "t2": capture_t2, "t9_blog": capture_t9,
                                              "t9_social": capture_t9}


def release0_view(writer: str, release: Optional[dict] = None) -> dict:
    """The release-0 view as `insertion.resolve` returns it for an active empty platform release (a synthetic one
    before the DB holds release 0)."""
    part = {"release_id": (release or {}).get("release_id") or "00000000-0000-0000-0000-000000000000",
            "release_no": 0, "content_class": "empty", "rule_ids": []}
    return {"writer": writer, "platform": part, "tenant": None}


def replay_one(writer: str, inp: dict, *, phase: str, view: Optional[dict] = None) -> dict:
    """One input: digests of each capture and the comparisons (no text)."""
    row: dict[str, Any] = {"writer": writer, "input_id": inp["input_id"], "source": inp.get("source"),
                           "stored_hash": inp.get("stored_hash")}
    try:
        off = CAPTURE[writer](inp)
        row.update(system_sha256=sha(off["system"]), user_sha256=sha(off["user"]), flags=off.get("flags") or [],
                   system_short=short(off["system"]))
        if phase == "post":
            on = CAPTURE[writer](inp, view or release0_view(writer))
            row.update(r0_system_sha256=sha(on["system"]), r0_user_sha256=sha(on["user"]),
                       r0_stamp=on.get("consumed"))
            row["none_vs_r0_equal"] = (off["system"] == on["system"] and off["user"] == on["user"])
        if inp.get("stored_hash"):
            row["stored_hash_match"] = row["system_short"] == inp["stored_hash"]
    except Exception as e:  # noqa: BLE001 — one bad input is a counted error, never a silent pass
        row["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return row


# ------------------------------------------------------------------------------------------------ inputs (read-only)

_A1_SQL = """
    SELECT input_id, tour_id, stored_hash, created_at, src_name, src_subtitle, src_summary, src_description,
           src_highlights, src_itineraries, country, duration, price_raw, inclusions, exclusions, group_size,
           best_time_to_go
      FROM (SELECT DISTINCT ON (gc.tour_id)
                   gc.id::text AS input_id, gc.tour_id::text AS tour_id, gc.metadata->>'prompt_version' AS stored_hash,
                   gc.created_at, rt.src_name, rt.src_subtitle, rt.src_summary, rt.src_description, rt.src_highlights,
                   rt.src_itineraries, rt.country, rt.duration, rt.price_raw, rt.inclusions, rt.exclusions,
                   rt.group_size, rt.best_time_to_go
              FROM silver_aa_internal.generated_content gc
              JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = gc.tour_id
             WHERE gc.metadata->>'writer_version' = 'v2' AND rt.deleted_at IS NULL
             ORDER BY gc.tour_id, gc.created_at DESC) t
     WHERE ($2::text[] IS NULL OR input_id = ANY($2::text[]))
     ORDER BY created_at DESC
     LIMIT $1
"""

_T2_SQL = """
    SELECT input_id, tenant_id, published_tour_id, rewrite_language
      FROM (SELECT DISTINCT ON (j.payload->>'published_tour_id', j.payload->>'tenant_id')
                   j.id::text AS input_id, j.payload->>'tenant_id' AS tenant_id,
                   j.payload->>'published_tour_id' AS published_tour_id,
                   coalesce(j.payload->>'rewrite_language', 'EN-US') AS rewrite_language, j.finished_at
              FROM shared.job j
             WHERE j.kind = 't2_rewrite' AND j.status = 'succeeded'
               AND coalesce(j.result->>'skipped', 'false') <> 'true'
             ORDER BY j.payload->>'published_tour_id', j.payload->>'tenant_id', j.finished_at DESC) t
     WHERE ($2::text[] IS NULL OR input_id = ANY($2::text[]))
     ORDER BY finished_at DESC
     LIMIT $1
"""

_T9_SQL = """
    SELECT cp.piece_id::text AS input_id, cp.tenant_id::text AS tenant_id,
           cp.angle_gate_request_id::text AS request_id, r.atom_id, cp.writer_input,
           cp.writer_input->>'writer_prompt_version' AS stored_hash
      FROM acp_shared.content_piece cp
      JOIN acp_shared.angle_gate_request r ON r.request_id = cp.angle_gate_request_id AND r.tenant_id = cp.tenant_id
     WHERE cp.writer_input IS NOT NULL AND cp.writer_input ? 'brand_pack'
       AND (CASE WHEN $3 THEN cp.writer_input->>'channel' = 'blog' ELSE cp.writer_input->>'channel' <> 'blog' END)
       AND ($2::text[] IS NULL OR cp.piece_id::text = ANY($2::text[]))
     ORDER BY cp.created_at DESC
     LIMIT $1
"""

_PT_SQL = """
    SELECT pt.id, pt.tour_id, pt.aa_name, pt.aa_subtitle, pt.aa_summary, pt.aa_description, pt.aa_highlights,
           pt.aa_itineraries, pt.seo_title, pt.seo_meta, pt.seo_keywords_used, rt.country, rt.duration
      FROM gold_aa_internal.published_tours pt
      LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
     WHERE pt.id = $1::uuid
"""


def _json(v):
    return json.loads(v) if isinstance(v, str) else v


async def _a1_brand_rules(conn) -> dict:
    """The A1 brand rule as `_execute_run_tour` resolves it for aa_internal (no brand chosen), forbidden words and
    the brand fields the graph state carries. The v2 writer reads only the forbidden words."""
    from api.routers.admin_pipeline import _resolve_brand_rule

    br = await _resolve_brand_rule(conn, A1_TENANT, None, None)
    if not br:
        return {}
    fw = br["forbidden_words"]
    return {"system_prompt": br["system_prompt"] or "", "style_guide": br["style_guide"] or "",
            "forbidden_words": list(fw) if isinstance(fw, list) else json.loads(fw or "[]"),
            "rewrite_language": "en-US"}


async def load_a1(conn, n: int, ids: Optional[list[str]]) -> list[dict]:
    brand = await _a1_brand_rules(conn)
    out = []
    for r in await conn.fetch(_A1_SQL, n, ids):
        hl = _json(r["src_highlights"]) or []
        tour = {"name": r["src_name"], "subtitle": r["src_subtitle"], "summary": r["src_summary"],
                "description": r["src_description"], "highlights": hl, "itineraries": r["src_itineraries"],
                "country": r["country"], "duration": r["duration"], "price": r["price_raw"],
                "inclusions": r["inclusions"], "exclusions": r["exclusions"]}
        out.append({"input_id": r["input_id"], "source": "generated_content", "stored_hash": r["stored_hash"],
                    "tour": tour, "brand_rules": brand,
                    "tour_agency": {"group_size": r["group_size"], "best_time_to_go": r["best_time_to_go"]}})
    return out


async def _t2_seo(pool, pt, tenant_id: str) -> dict:
    """The SEO the T2 job passes (`t2_rewrite_job.t2_seo` when this image has it, else the same reads)."""
    try:
        from services.jobs.t2_rewrite_job import t2_seo
    except ImportError:
        t2_seo = None
    if t2_seo is not None:
        seo, _diag = await t2_seo(pool, pt, tenant_id)
        return seo
    seo: dict = {}
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT top_keywords, keyword_ideas, people_also_ask "
                                  "FROM silver_aa_internal.seo_context WHERE tour_id = $1::uuid "
                                  "ORDER BY fetched_at DESC LIMIT 1", pt["tour_id"])
        if row:
            seo = {"top_keywords": _json(row["top_keywords"]) or []}
            seo["keywords"] = {"top_keywords": seo["top_keywords"]}
            seo["people_also_ask"] = _json(row["people_also_ask"]) or []
        from services.seo_intelligence.s1_prefetch import tenant_market_seo
        _market, mkt = await tenant_market_seo(conn, tenant_id, str(pt["tour_id"]))
    return mkt or seo


async def load_t2(pool, conn, n: int, ids: Optional[list[str]]) -> list[dict]:
    from services.acp_shared.brand_pack import fetch_brand_pack_conn
    from services.jobs.t2_rewrite_job import brand_rules_from_pack, source_tour_dict

    out = []
    for r in await conn.fetch(_T2_SQL, n, ids):
        pt = await conn.fetchrow(_PT_SQL, r["published_tour_id"])
        if pt is None:
            continue
        pack = await fetch_brand_pack_conn(conn, r["tenant_id"])
        brand = {"rewrite_language": r["rewrite_language"]}
        if pack:
            brand = brand_rules_from_pack(pack, r["rewrite_language"])
        brand.pop("_brand_loaded", None)
        out.append({"input_id": r["input_id"], "source": "shared.job", "tenant_id": r["tenant_id"],
                    "tour": source_tour_dict(dict(pt)), "brand_rules": brand,
                    "seo": await _t2_seo(pool, dict(pt), r["tenant_id"])})
    return out


async def load_t9(conn, n: int, ids: Optional[list[str]], *, blog: bool) -> list[dict]:
    out = []
    for r in await conn.fetch(_T9_SQL, n, ids, blog):
        out.append({"input_id": r["input_id"], "source": "content_piece", "tenant_id": r["tenant_id"],
                    "request_id": r["request_id"], "atom_id": r["atom_id"], "stored_hash": r["stored_hash"],
                    "writer_input": _json(r["writer_input"])})
    return out


# ------------------------------------------------------------------------------------------------ the report

def summarize(rows: list[dict], *, phase: str, pre: Optional[dict] = None) -> dict:
    """Per writer: inputs, errors, the comparisons. `pre` = {input_id: {system_sha256, user_sha256}}.
    The verdict itself is re-derived by services.writing_memory.release0 from the rows; this is the readable view."""
    out: dict[str, Any] = {}
    for w in WRITERS:
        mine = [r for r in rows if r["writer"] == w]
        ok = [r for r in mine if "error" not in r]
        s: dict[str, Any] = {"inputs": len(ok), "errors": len(mine) - len(ok),
                             "stored_hashes": sum(1 for r in ok if r.get("stored_hash")),
                             "stored_hash_match": sum(1 for r in ok if r.get("stored_hash_match")),
                             "system_sha256": sorted({r["system_sha256"] for r in ok})[:5]}
        if phase == "post":
            s["byte_diffs_none_vs_r0"] = sum(1 for r in ok if not r.get("none_vs_r0_equal"))
            if pre is not None:
                compared = [r for r in ok if r["input_id"] in pre]
                s["compared_with_pre"] = len(compared)
                s["byte_diffs_vs_pre"] = sum(
                    1 for r in compared
                    if (r["system_sha256"], r["user_sha256"]) != (pre[r["input_id"]]["system_sha256"],
                                                                 pre[r["input_id"]]["user_sha256"]))
        out[w] = s
    return out


def _git_sha() -> Optional[str]:
    return os.environ.get("GIT_SHA") or os.environ.get("APP_GIT_SHA")


async def run(args) -> dict:
    import asyncpg

    from shared.secrets import get_database_url

    ssl: Any = None if os.environ.get("WM20_DB_SSL", "require") == "disable" else "require"
    dsn = get_database_url().strip()
    pre = _load_pre(args.pre) if args.pre else None
    ids_by_writer: dict[str, Optional[list[str]]] = {w: None for w in WRITERS}
    if pre is not None:
        for w in WRITERS:
            ids_by_writer[w] = [r["input_id"] for r in pre["rows"] if r["writer"] == w and "error" not in r]
    pool = await asyncpg.create_pool(dsn, ssl=ssl, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction(readonly=True):
                n = args.min + args.slack
                inputs = {
                    "a1": await load_a1(conn, n, ids_by_writer["a1"]),
                    "t2": await load_t2(pool, conn, n, ids_by_writer["t2"]),
                    "t9_blog": await load_t9(conn, n, ids_by_writer["t9_blog"], blog=True),
                    "t9_social": await load_t9(conn, n, ids_by_writer["t9_social"], blog=False),
                }
                views: dict[str, dict] = {}
                if args.phase == "post":
                    for w in WRITERS:
                        rel = await conn.fetchrow(
                            "SELECT release_id::text FROM shared.writing_memory_release WHERE writer = $1 "
                            "AND scope = 'platform' AND release_no = 0", w) if args.db_release0 else None
                        views[w] = release0_view(w, dict(rel) if rel else None)
    finally:
        await pool.close()
    rows = []
    for w in WRITERS:
        for inp in inputs[w]:
            rows.append(replay_one(w, inp, phase=args.phase, view=views.get(w)))
    pre_map = {r["input_id"]: r for r in pre["rows"]} if pre else None
    from services.content_generation import s1_writer_v2_live
    report = {"schema": SCHEMA, "phase": args.phase, "created_at": datetime.now(timezone.utc).isoformat(),
              "git_sha": _git_sha(), "min_inputs": args.min, "pre_key": args.pre,
              "a1_v2_enabled_here": s1_writer_v2_live.enabled(), "cost_usd": 0,
              "summary": summarize(rows, phase=args.phase, pre=pre_map), "rows": rows}
    return report


def _read_ref(ref: str) -> bytes:
    if ref.startswith("s3://"):
        raise SystemExit("give the bronze key as s3:<key>; the bucket comes from the environment (BRONZE_BUCKET)")
    if ref.startswith("s3:"):
        import boto3

        from shared.aws_client.bronze import bronze_bucket
        return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-1")).get_object(
            Bucket=bronze_bucket(), Key=ref[3:].lstrip("/"))["Body"].read()
    with open(ref, "rb") as f:
        return f.read()


def _load_pre(ref: str) -> dict:
    data = json.loads(_read_ref(ref))
    if data.get("schema") != SCHEMA or data.get("phase") != "pre":
        raise SystemExit(f"{ref} is not a {SCHEMA} pre report")
    return data


def _write_out(report: dict, key: Optional[str]) -> None:
    """The full report to the bronze key (ids and digests only); when the task role may not write there, the full
    report goes to stdout between markers instead (the operator saves it and uploads it with their own login)."""
    body = json.dumps(report, indent=1, sort_keys=True, default=str)
    stored = None
    if key:
        try:
            import boto3

            from shared.aws_client.bronze import bronze_bucket
            boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-1")).put_object(
                Bucket=bronze_bucket(), Key=key.lstrip("/"), Body=body.encode("utf-8"),
                ContentType="application/json")
            stored = key
        except Exception as e:  # noqa: BLE001
            print(f"out_key not written ({type(e).__name__}): full report below")
    print(json.dumps({"schema": SCHEMA, "phase": report["phase"], "out_key": stored, "git_sha": report["git_sha"],
                      "a1_v2_enabled_here": report["a1_v2_enabled_here"], "summary": report["summary"]},
                     indent=1, default=str))
    if not stored:
        print("=====WM20-REPORT-BEGIN=====")
        print(json.dumps(report, sort_keys=True, default=str))
        print("=====WM20-REPORT-END=====")


def _quiet_logs() -> None:
    """The T2 path logs the full prompt text (`llm_prompt_built`); a replay must not copy tenant text into the
    logs 3x over. Drop every structlog event of this process."""
    try:
        import structlog
    except ImportError:
        return

    def _drop(_logger, _name, _event):
        raise structlog.DropEvent

    structlog.configure(processors=[_drop], cache_logger_on_first_use=False)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--phase", choices=("pre", "post"), required=True)
    ap.add_argument("--min", type=int, default=20, help="stored inputs wanted per writer (default 20)")
    ap.add_argument("--slack", type=int, default=5, help="extra inputs selected per writer, for errors (default 5)")
    ap.add_argument("--pre", help="post: the pre report (path or s3:<key>); its input ids are replayed")
    ap.add_argument("--no-db-release0", dest="db_release0", action="store_false",
                    help="post: use a synthetic release-0 view even when the DB holds release 0")
    ap.add_argument("--out-key", help="bronze key for the full report (ids and digests only)")
    args = ap.parse_args(argv)
    sys.path.insert(0, "/app")
    _quiet_logs()
    from services.golden_set import llm_guard
    llm_guard.install("wm20_release0_replay")
    report = asyncio.run(run(args))
    _write_out(report, args.out_key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
