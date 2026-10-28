"""AA-919 WM-19 / WM-21 — run a writer on a stored input with a given release view (the R2 candidate / control arm, the
shadow candidate). Model calls happen here; nothing is written to app data.

  writer      path (the production writer path of that writer, release view passed through the insertion path)
  a1          `v1_pipeline._rewrite_tour` (the S1 graph: writer v1 or v2 per S1_WRITER_V2 on the worker, judge, brand
              audit, flag-fix), platform brand pack. The AA-736 Sonnet retry of the admin executor is not run.
  t2          `_rewrite_tour(is_tenant_rewrite=True, generate_stage='t2_generate')` + `run_t3_qa_gate` (the T3 repair
              rounds get the same view), the tenant's brand pack.
  t9_blog /   `generate.write_content(...)` (the first write of `run_write_background`) from the stored writer context.
  t9_social   The T10 gate / repair loop is NOT run: the full loop writes the piece row on every step and is not safe
              to drive from an eval. A T9 candidate is therefore a first draft; the stored control is a final text.
              The judge comparison is conservative for T9 (it can only make the candidate look worse) and is said so
              in the run details (`t9_first_draft_only`).

Side effects checked: `_rewrite_tour` / `run_t3_qa_gate` return their result and write no app row; flag-fix lessons
are written only with PIPELINE_LESSONS_ENABLED (default off) and never inside an eval run (call_log.eval_label). Every
llm_call_log row is relabelled to the eval stage by the caller's `call_log.bind_eval_stage` (tenant NULL), and the
gateway starts no A/B shadow call inside it.
"""
from __future__ import annotations

import copy
import json
from typing import Any, Optional

from services.writing_memory.eval_checks import flatten_text, tenant_fingerprint

T9_WRITERS = ("t9_blog", "t9_social")
# The angle fields of a T9 writer context that brief the writer (T8's chosen angle), shown to the judge as BRIEF.
T9_BRIEF_KEYS = ("name", "formula_fit", "best_final_style")


class WriterFailed(RuntimeError):
    """The writer path crashed or produced nothing: an error row, never a quality verdict."""


def _brand_rules(inp: dict, shared_inputs: Optional[dict]) -> dict:
    """The item's brand pack: inlined on tenant items (CIS 284), the set-level s1:* pack for A1."""
    if isinstance(inp.get("brand_rules"), dict):
        return copy.deepcopy(inp["brand_rules"])
    ref = inp.get("brand_rules_ref")
    packs = ((shared_inputs or {}).get("brand_rules") or {})
    return copy.deepcopy(packs.get(ref) or {}) if ref else {}


def _tour_text(tour: dict) -> str:
    keys = ("name", "subtitle", "country", "duration", "price", "summary", "description", "highlights", "itineraries",
            "inclusions", "exclusions", "group_size", "best_time_to_go")
    return "\n".join(f"{k.upper()}: {flatten_text(tour.get(k))}" for k in keys if tour.get(k))


def _t9_context(inp: dict) -> dict:
    """The T9 writer context of an input. Two stored shapes: a golden item's `context` (the run_write_background
    context of its job), or a live piece's `writer_input` snapshot (the shadow's input), normalised to the same keys."""
    ctx = inp.get("context")
    if isinstance(ctx, dict):
        return ctx
    wi = inp.get("writer_input")
    if isinstance(wi, dict):
        moments = wi.get("route_moments") or []
        return {"channel": wi.get("channel"), "goal": wi.get("goal"), "atom_text": wi.get("seed") or "",
                "brand_audience": wi.get("brand_pack") or {}, "chosen": wi.get("angle") or {}, "cta": wi.get("cta"),
                "destination": wi.get("destination"), "writer_trip_name": wi.get("writer_trip", wi.get("trip")),
                "atom_id": inp.get("atom_id"), "scope": wi.get("scope"),
                "route_segments": [(m.get("atom_id"), m.get("seed")) for m in moments if isinstance(m, dict)] or None,
                "keyword": wi.get("keyword"), "facts_text": wi.get("facts") or "",
                "search_questions": wi.get("search_questions") or [], "lead_focus": wi.get("lead_focus"),
                "tenant_id": inp.get("tenant_id")}
    raise WriterFailed("t9 input has no writer context")


def source_text(writer: str, inp: dict, shared_inputs: Optional[dict] = None) -> str:
    """What the writer was given, as the judge reads it (and the isolation probe's allowed text, with the brand)."""
    if writer in ("a1", "t2"):
        br = _brand_rules(inp, shared_inputs)
        brand = {k: br.get(k) for k in ("brand_name", "default_cta") if br.get(k)}
        out = _tour_text(inp.get("tour") or {})
        if brand:
            out += "\nBRAND: " + json.dumps(brand, ensure_ascii=False)
        return out
    ctx = _t9_context(inp)
    parts = [f"SEED: {ctx.get('atom_text') or ''}"]
    # The scope line and the angle brief the writer was told to follow (S255, AA-919 kappa diagnosis): the golden
    # labels were made on the full writer input, which includes them; without them the judge flags brief wording and
    # cannot check scope. They (and the brand) come BEFORE the facts: the judge reads the first
    # eval_judge.SOURCE_CHARS of this text, and a T9 blog's facts run to 15-40k chars, so at the end they were cut.
    if ctx.get("scope"):
        parts.append(f"SCOPE: {flatten_text(ctx.get('scope'))}")
    angle = ctx.get("chosen")
    if isinstance(angle, dict):
        brief = [flatten_text(angle.get(k)) for k in T9_BRIEF_KEYS if angle.get(k)]
        if brief:
            parts.append("BRIEF: " + "\n".join(brief))
    ba = ctx.get("brand_audience") or {}
    if ba.get("brand_name"):
        parts.append(f"BRAND: {ba['brand_name']}")
    for seg in ctx.get("route_segments") or []:
        if isinstance(seg, (list, tuple)) and len(seg) > 1:
            parts.append(f"SEED: {seg[1]}")
    for k in ("trip_name", "destination", "cta", "keyword", "facts_text"):
        if ctx.get(k):
            parts.append(f"{k.upper()}: {flatten_text(ctx.get(k))}")
    return "\n".join(parts)


def own_pack(writer: str, inp: dict, shared_inputs: Optional[dict] = None) -> dict:
    """The brand pack of the input's owner (allowed text for the isolation probe)."""
    if writer in T9_WRITERS:
        try:
            return dict(_t9_context(inp).get("brand_audience") or {})
        except WriterFailed:
            return {}
    return _brand_rules(inp, shared_inputs)


def allowed_text(writer: str, inp: dict, shared_inputs: Optional[dict] = None) -> str:
    fp = tenant_fingerprint(own_pack(writer, inp, shared_inputs))
    return "\n".join([source_text(writer, inp, shared_inputs), fp["brand_name"], *fp["texts"]])


def output_text(writer: str, output: Optional[dict]) -> str:
    return flatten_text(output or {})


# ── the writer paths ───────────────────────────────────────────────────────────────────────────────────────────────

async def _a1(inp: dict, view: Optional[dict], shared_inputs: Optional[dict]) -> dict:
    from api.routers.v1_pipeline import _rewrite_tour

    result = await _rewrite_tour(copy.deepcopy(inp["tour"]), 0, 1, brand_rules=_brand_rules(inp, shared_inputs),
                                 seo=copy.deepcopy(inp.get("seo") or {}), model_tier=None, subtitle_focus="standard",
                                 seo_mode="dataforseo", tour_agency=inp.get("tour_agency"), writing_memory=view)
    if result.get("status") != "success" or not result.get("generated"):
        raise WriterFailed(f"a1 rewrite: {(result.get('error') or 'no generated output')[:200]}")
    return dict(result["generated"])


async def _t2(inp: dict, view: Optional[dict], shared_inputs: Optional[dict]) -> dict:
    from api.routers.v1_pipeline import _rewrite_tour
    from services.acp_produce.tenant_pipeline import run_t3_qa_gate

    tour, seo = copy.deepcopy(inp["tour"]), copy.deepcopy(inp.get("seo") or {})
    br, tid = _brand_rules(inp, shared_inputs), inp.get("tenant_id")
    if inp.get("rewrite_language") and not br.get("rewrite_language"):
        br["rewrite_language"] = inp["rewrite_language"]
    result = await _rewrite_tour(tour, 0, 1, brand_rules=br, seo=seo, is_tenant_rewrite=True, tenant_id=tid,
                                 generate_stage="t2_generate", writing_memory=view)
    if result.get("status") != "success" or not result.get("generated"):
        raise WriterFailed(f"t2 rewrite: {(result.get('error') or 'no generated output')[:200]}")
    qa = await run_t3_qa_gate(tour, [str(v) for v in tour.values() if v], result, br, seo_data=seo, tenant_id=tid,
                              writing_memory=view)
    out = (qa or {}).get("result") or result
    if not out.get("generated"):
        raise WriterFailed("t2 QA gate: no generated output")
    return dict(out["generated"])


def t9_kwargs(ctx: dict, *, tenant_id: Optional[str], view: Optional[dict]) -> dict[str, Any]:
    """`write_content` kwargs as `run_write_background` passes them on attempt 1, from a stored writer context."""
    from services.acp_angle_gate.channel_style import get_channel_style
    from services.acp_angle_gate.goals import get_goal

    channel = ctx.get("channel")
    # today's goal and channel definitions (as production builds them), the stored copy only when the key is gone
    stored_goal = ctx.get("goal")
    goal_key = stored_goal.get("key") if isinstance(stored_goal, dict) else stored_goal
    goal = get_goal(goal_key) if goal_key else None
    goal = goal or (stored_goal if isinstance(stored_goal, dict) else None)
    style = get_channel_style(channel) if channel else None
    style = style or (ctx.get("channel_style") if isinstance(ctx.get("channel_style"), dict) else None)
    if not goal or not style:
        raise WriterFailed(f"t9 context: unknown goal or channel {channel!r}")
    route = ctx.get("route_segments")
    kw: dict[str, Any] = {
        "content_seed": ctx.get("atom_text") or "", "goal": goal, "channel_style": style,
        "brand_audience": ctx.get("brand_audience") or {}, "angle": ctx.get("chosen") or {}, "cta": ctx.get("cta"),
        "destination": ctx.get("destination"), "trip_name": ctx.get("writer_trip_name") or ctx.get("trip_name"),
        "atom_id": ctx.get("atom_id"), "scope": ctx.get("scope"),
        "route_segments": [tuple(s) for s in route] if route else None, "keyword": ctx.get("keyword"),
        "tenant_id": tenant_id, "angle_gate_request_id": None, "facts_text": ctx.get("facts_text") or "",
    }
    questions = [q["question"] if isinstance(q, dict) else q for q in (ctx.get("search_questions") or [])]
    if questions:
        kw["search_questions"] = questions
    if ctx.get("lead_focus"):
        kw["lead_focus"] = ctx["lead_focus"]
    if view is not None:
        kw["memory"] = view
    return kw


async def _t9(inp: dict, view: Optional[dict], shared_inputs: Optional[dict]) -> dict:  # noqa: ARG001
    import asyncio

    from services.acp_content_writing import generate

    ctx = _t9_context(inp)
    kw = t9_kwargs(ctx, tenant_id=inp.get("tenant_id") or ctx.get("tenant_id"), view=view)
    text, _cost, seo_meta, _summary = await asyncio.to_thread(generate.write_content, **kw)
    if not (text or "").strip():
        raise WriterFailed("t9 write: empty text")
    out = {"content_text": text}
    out.update({k: v for k, v in (seo_meta or {}).items() if k in ("seo_title", "meta_description", "slug")})
    return out


_PATHS = {"a1": _a1, "t2": _t2, "t9_blog": _t9, "t9_social": _t9}


async def regenerate(writer: str, inp: dict, *, view: Optional[dict], shared_inputs: Optional[dict] = None) -> dict:
    """One write of `writer` on the stored input `inp` with the release view `view` (None = memory off). Returns
    the output dict (reader-facing fields only). Raises WriterFailed (or the writer's own error)."""
    fn = _PATHS.get(writer)
    if fn is None:
        raise ValueError(f"unknown writer {writer!r}")
    return await fn(inp, view, shared_inputs)


__all__ = ["T9_WRITERS", "WriterFailed", "allowed_text", "output_text", "own_pack", "regenerate", "source_text",
           "t9_kwargs"]
