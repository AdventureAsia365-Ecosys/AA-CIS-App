"""AA-645 step 0 — the S1 writer v2 (arm W of `s1_writer_v2`) on the live A1 path, behind a flag.

Decisions (Nghiep, S248, d01 / d02 — root platform-impact memo section 9): the writer is W, its model is whatever the
Model Gateway route of the A1 write stage (`s1_generate`) names — W x Sonnet is a route change in admin Settings, never
a model id here — and the A1 judge's brand-fit score is computed and logged but does not gate a v2 draft (the offline
read on 125 pages: brand-fit does not reward source fidelity, Spearman 0.08 with the serious-error rate).

Scope. `S1_WRITER_V2` (env, worker task definition; default OFF) switches the A1 admin rewrite only:

* never a tenant (T2) rewrite — T2 shares graph.py, and the v2 writer drops the brand-differentiation block a tenant
  rewrite needs (risk T8): `applies()` refuses `is_tenant_rewrite` and any write stage other than `s1_generate`;
* never the Bedrock Batch seed path (`batch_text`): attempt 1 there was written by today's writer, so the retries of
  that run stay on it too.

Per draft, at most three calls through `LLMClient.generate()` on the `s1_generate` stage (writer, meta, one meta
retry — the harness shape, `scripts/aa645/s1_screen.py::run_unit`), each logged to `shared.llm_call_log` with role
`writer` and `quality_signal.writer_version = "v2"` + `call`. A retry of the graph (score < 7) writes a new draft with
validate's feedback appended. The draft carries `writer_version = "v2"` into graph state, which validate (the
length-ratio and DFS-keyword codes are report-only for v2: there is no nudge and keywords are optional by design) and
the judge (brand-fit report-only) read, and which lands in `generated_content.metadata.writer_version`.
"""
from __future__ import annotations

import os
from typing import Optional

import structlog

from shared.llm_client.call_log import billed_fields, record_call_sync, record_unusable_sync
from shared.llm_client.client import LLMClient, _billed_usage_of
from shared.llm_client.errors import is_llm_access_denied
from shared.llm_client.models import LLMRequest

from . import s1_writer_v2 as v2

logger = structlog.get_logger()

S1_WRITER_V2_ENV = "S1_WRITER_V2"
WRITER_VERSION = "v2"
A1_WRITE_STAGE = "s1_generate"
_ON = ("1", "true", "on", "yes")


def enabled() -> bool:
    """The flag alone (default OFF)."""
    return os.environ.get(S1_WRITER_V2_ENV, "").strip().lower() in _ON


def applies(state: dict) -> bool:
    """True when this graph run writes with v2: flag on, A1 admin rewrite on the s1_generate stage, not a batch seed."""
    return (enabled()
            and not state.get("is_tenant_rewrite")
            and (state.get("generate_stage") or A1_WRITE_STAGE) == A1_WRITE_STAGE
            and state.get("batch_text") is None)


def is_v2(state: dict) -> bool:
    """The draft in `state` was written by v2 (A1 only: a tenant rewrite never is)."""
    return state.get("writer_version") == WRITER_VERSION and not state.get("is_tenant_rewrite")


def _call(client: LLMClient, state: dict, *, call: str, system: str, user: str, max_tokens: int,
          raise_access_denied: bool = True) -> dict:
    """One gateway call on the A1 write stage. Returns {ok, parsed, cost_usd, model_used, ...}.
    Never raises, with one exception (AA-900): a call refused at account level before anything was billed
    (``LLMAccessDenied``) is re-raised when ``raise_access_denied``, so the writer call fails the job at once. The meta
    calls pass False: they run after a paid draft, which is kept; the refusal is then ``access_denied`` in the result.
    The row: written right after generate() returns; a reply billed then lost to a post-call error gets a
    billed_unusable row; a call that raised inside the gateway was logged by the gateway (AA-927)."""
    stage = state.get("generate_stage") or A1_WRITE_STAGE
    out = {"call": call, "ok": False, "parsed": None, "cost_usd": 0.0}
    resp = None
    logged = False
    try:
        resp = client.generate(LLMRequest(system_prompt=system, user_prompt=user, model_tier=state.get("model_tier"),
                                          stage=stage, max_tokens=max_tokens))
        parsed = v2.parse_json(resp.content)
        record_call_sync(
            stage=stage, role="writer", model=resp.model_used,
            tokens_in=getattr(resp, "input_tokens", None), tokens_out=getattr(resp, "output_tokens", None),
            cost_usd=resp.cost_usd, tenant_id=state.get("tenant_id"),
            quality_signal={"writer_version": WRITER_VERSION, "call": call, "json_parsed": parsed is not None,
                            "fallback_used": resp.fallback_used},
            stop_reason=getattr(resp, "stop_reason", None), account=getattr(resp, "satellite_account", None),
            fallback_used=getattr(resp, "fallback_used", None), **billed_fields(resp),
        )
        logged = True
        out.update(ok=parsed is not None, parsed=parsed, cost_usd=resp.cost_usd or 0.0, model_used=resp.model_used,
                   fallback_used=resp.fallback_used, satellite_account=resp.satellite_account,
                   cache_read_tokens=getattr(resp, "cache_read_tokens", 0) or 0,
                   cache_write_tokens=getattr(resp, "cache_write_tokens", 0) or 0)
        if parsed is None:
            out["error"] = f"{call}: reply is not a JSON object"
    except Exception as e:  # noqa: BLE001 — a failed call ends the draft, never the graph
        if resp is None and is_llm_access_denied(e):
            if raise_access_denied:
                raise  # AA-900: refused at account level; the job fails at once (worker.failure_outcome)
            out["access_denied"] = True
        if resp is not None and not logged:
            record_unusable_sync(resp, stage=stage, role="writer", error=e, tenant_id=state.get("tenant_id"))
            out["cost_usd"] = resp.cost_usd or 0.0
        else:
            out["cost_usd"] = sum(b.get("cost_usd") or 0.0 for b in _billed_usage_of(e))
        out["error"] = f"{call}: {type(e).__name__}: {str(e)[:300]}"
        logger.warning("s1_writer_v2_call_failed", call=call, error=out["error"])
    return out


def write(state: dict, client: Optional[LLMClient] = None) -> dict:
    """One v2 draft for the A1 graph state. Returns {"generated", "cost_usd", "error", "model_used",
    "prompt_version", "calls", "meta_problems", ...}: `generated` is the page dict the rest of the graph reads (name,
    subtitle, summary, description, highlights, itineraries as the canonical "Day N — Title\\nBody" string,
    seo_title, seo_meta, seo_keywords_used), {} when the writer call failed."""
    from services.quality.s1_facts import build_fact_table
    from .batch_prompt import prompt_version_of
    from .forbidden_words import all_forbidden

    client = client or LLMClient()
    tour = v2.with_agency_fields(state.get("tour") or {}, state.get("tour_agency"))
    facts = build_fact_table(tour)
    plan = v2.day_plan(tour, facts)
    forbidden = all_forbidden(state.get("brand_forbidden_words"))
    # AA-919 (WM-18 / WM-20): the writing-memory release this graph run resolved (None = memory off); release 0
    # renders "" (byte-identical prompt). The stamp says which release the draft was written with.
    from services.writing_memory import insertion
    view = state.get("writing_memory")
    system = v2.w_system_prompt(forbidden, house_rules=insertion.a1_house_rules(view))
    user = v2.w_user_prompt(tour, facts, plan)
    if state.get("feedback"):
        user += f"\n\nPREVIOUS ATTEMPT FEEDBACK:\n{state['feedback']}\nFix these issues; every rule above still holds."
    result = {"generated": {}, "cost_usd": 0.0, "error": "", "prompt_version": prompt_version_of(system),
              "plan_mode": plan["mode"], "plan_days": len(plan["days"]), "calls": 0, "meta_problems": [],
              "cache_read_tokens": 0, "cache_write_tokens": 0,
              "writing_release": insertion.stamp(view, writer_version=WRITER_VERSION)}

    def _add(rec: dict) -> None:
        result["calls"] += 1
        result["cost_usd"] += rec.get("cost_usd") or 0.0
        result["cache_read_tokens"] += rec.get("cache_read_tokens") or 0
        result["cache_write_tokens"] += rec.get("cache_write_tokens") or 0

    w = _call(client, state, call="write", system=system, user=user, max_tokens=v2.WRITER_MAX_TOKENS)
    _add(w)
    if not w["ok"]:
        result["error"] = w.get("error") or "writer reply is not JSON"
        return result
    result.update(model_used=w.get("model_used"), fallback_used=w.get("fallback_used", False),
                  satellite_account=w.get("satellite_account"))
    page = v2.page_from_writer("w", tour, facts, plan, w["parsed"])

    keywords = v2.relevant_keywords(state.get("seo"), tour)
    msys, muser = v2.meta_system_prompt(forbidden), v2.meta_user_prompt(page, tour, facts, keywords)
    m1 = _call(client, state, call="meta", system=msys, user=muser, max_tokens=v2.META_MAX_TOKENS,
               raise_access_denied=False)
    _add(m1)
    meta_json = m1.get("parsed")
    problems = (v2.meta_problems(meta_json, tour, facts, forbidden) if m1["ok"]
                else ["the answer is not the JSON object asked for"])
    # One retry, also after an unusable first answer (still <= 3 calls); none after an account-level refusal (AA-900).
    if problems and not m1.get("access_denied"):
        m2 = _call(client, state, call="meta_retry", system=msys,
                   user=muser + v2.meta_retry_prompt(meta_json, problems), max_tokens=v2.META_MAX_TOKENS,
                   raise_access_denied=False)
        _add(m2)
        if m2["ok"]:
            meta_json = m2["parsed"]
            problems = v2.meta_problems(meta_json, tour, facts, forbidden)
    page = v2.finalize_meta(page, meta_json)
    page["seo_keywords_used"] = keywords
    if page.get("trip_type") is None:
        page.pop("trip_type", None)
    result["generated"] = page
    result["meta_problems"] = problems
    logger.info("s1_writer_v2_done", plan_mode=plan["mode"], days=len(plan["days"]), calls=result["calls"],
                cost=round(result["cost_usd"], 6), meta_problems=len(problems))
    return result
