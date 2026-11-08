"""AA-1049 W3 (root ADR 0020, T2 train) — the T2 linear orchestrator, OPT-IN behind ``T2_ORCHESTRATOR=linear``.

The default (``T2_ORCHESTRATOR`` unset, ``graph`` or any unknown value) is main's T2 path, unchanged: the LangGraph
rewrite (``v1_pipeline._rewrite_tour``) then the T3 loop (``tenant_pipeline.run_t3_qa_gate``) and the save in
``t2_rewrite_job._rewrite_and_save``. The parity test (tests/unit/test_aa1049_t2_orchestrator.py) replays the T2
scenarios of the full-chain harness against the golden recorded on main before this module existed: with the flag at
its default every model request (stage + prompt sha), every SQL statement and every stored field is byte-equal.

``linear`` runs one plain Python sequence instead of the graph (design review, T2 pick, final graph):

    bundle -> policy_route -> compose_slots -> ONE write -> normalize -> verify -> route (route table, t2 rows)
      -> local repair (sentence repair / meta regenerate) | field regen (one more writer call) -> normalize -> verify
      -> ... until clean or the VersionBudget is out -> select_best -> voice report (one judge call, report-only)
      -> final_contract -> save

Every step reuses main's code, so no prompt text changes (policy_sha and every registered prompt version unchanged):

- write: ``graph.generate_node`` on the state ``v1_pipeline.initial_state_for`` builds (the T2 v1 writer prompt, the
  gateway call, the forbidden strip, the meta fit, ``record_call_sync(**billed_fields(resp))``);
- normalize: the T3 per-candidate fixes (``fit_seo_title``, ``fit_seo_meta_final``, ``fix_t2_dashes``);
- verify: ``validate_node`` (score), T3's hard codes (``_t3_structural_issues``), T3 grounding + the Jev clear
  (``_t3_grounding_check``, ``t3_jev_clear``: Jev only CLEARS hits, as on main), and the Master day slots;
- local repair: ``_t3_sentence_repair`` (t2_flag_fix, repair contract) and ``_t3_meta_regenerate``;
- field regen: one more ``generate_node`` call with T3's deterministic feedback (``t3_repair_feedback``);
- voice report: ``judge_node`` once on the selected draft (T2 interim: report-only, never the score).

What ``linear`` does differently from main (opt-in, declared; the offline replay labels them, see
scripts/quality/replay_aa1049_t2.py):

- L1 status: ``ai_generated`` only when validate's score clears the line AND no enforce finding is left (T3 hard
  codes, T3 grounding, a Master day-slot break) AND no held fallback writer. Main's rule (score + T3 hard codes) is a
  subset: the opt-in rule can only move a version to ``needs_review``, never the other way.
- L2 writer calls: at most 2 per version (``VersionBudget``), counted across a job retry: each billed call is counted
  and persisted on the pending version BEFORE it is sent, and its cost right after it returns (before verify), so a
  retry never re-spends a call the failed attempt made; main's graph may write up to 3 times (a validate retry loop),
  then T3. The meta regenerate counts its real t2_flag_fix attempts (up to 3) and their cost.
- L3 one judge call per version (on the selected draft, report-only), not one per draft.
- L4 no brand-audit LLM fix (flag_fix) and no itinerary-day revise of the graph tail; the deterministic brand
  pre-audit codes are recorded (report).
- L5 select_best prefers fewer enforce findings before the score.
- L6 a job retry resumes from the stored best candidate (generation_meta.t2_orchestrator.checkpoint) instead of
  writing again.

The elephant-riding policy hold (D8) runs BEFORE either path in ``_rewrite_and_save`` and is the same for both.
``linear`` builds on the T2 interim profile: with ``T2_INTERIM=off`` (break glass) the flag is ignored and main's
path runs (a judge-gated status is not something this orchestrator reproduces).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import structlog

logger = structlog.get_logger()

ORCHESTRATOR_ENV = "T2_ORCHESTRATOR"
GRAPH = "graph"      # default: main's LangGraph + T3 path
LINEAR = "linear"    # opt-in: this module
MODES = (GRAPH, LINEAR)
RECORD_VERSION = "t2-linear-1"
STAGE = "t2_generate"


def _meta_attempts_max() -> int:
    from services.content_generation.flag_fix_node import _META_REREPAIR_MAX_ATTEMPTS
    return int(_META_REREPAIR_MAX_ATTEMPTS)


META_ATTEMPTS = _meta_attempts_max()   # the t2_flag_fix calls one meta regenerate may bill (flag_fix's loop bound)


def mode() -> str:
    """The T2 path of this process: ``linear`` only when the env says exactly that (case / spaces ignored) and the
    T2 interim profile is on; anything else (unset, ``graph``, a typo, ``on``) is main's ``graph`` path."""
    raw = (os.environ.get(ORCHESTRATOR_ENV) or "").strip().lower()
    if raw != LINEAR:
        return GRAPH
    from services.content_generation import t2_interim
    if not t2_interim.enabled():
        logger.warning("t2_orchestrator_ignored", reason="t2_interim_off", value=raw)
        return GRAPH
    return LINEAR


# ── budget ──────────────────────────────────────────────────────────────────────────────────────


@dataclass
class VersionBudget:
    """The model requests one version may spend, counted across a job retry (persisted in the checkpoint).
    ``writer`` = t2_generate calls (first write + field regen), ``repair`` = t2_flag_fix calls (a sentence repair is
    one, a meta regenerate its real attempts: up to ``META_ATTEMPTS``), ``voice`` = the report-only judge call,
    ``usd`` = the billed cost the calls reported."""
    writer_max: int = 2
    repair_max: int = 3
    voice_max: int = 1
    usd_max: float = 0.25
    writer: int = 0
    repair: int = 0
    voice: int = 0
    usd: float = 0.0

    def can(self, kind: str) -> bool:
        return getattr(self, kind) < getattr(self, f"{kind}_max") and self.usd < self.usd_max

    def spend(self, kind: str, usd: float = 0.0, n: int = 1) -> None:
        setattr(self, kind, getattr(self, kind) + int(n))
        self.charge(usd)

    def left(self, kind: str) -> int:
        return max(0, getattr(self, f"{kind}_max") - getattr(self, kind))

    def charge(self, usd: Any) -> None:
        """Add the billed cost a call reported (after the call; its count was spent before it)."""
        try:
            self.usd = round(self.usd + float(usd or 0.0), 6)
        except (TypeError, ValueError):
            pass

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "VersionBudget":
        b = cls()
        for k in ("writer", "repair", "voice"):
            try:
                setattr(b, k, max(0, int((d or {}).get(k) or 0)))
            except (TypeError, ValueError):
                pass
        try:
            b.usd = float((d or {}).get("usd") or 0.0)
        except (TypeError, ValueError):
            pass
        return b


# ── bundle + slots ──────────────────────────────────────────────────────────────────────────────


def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:16]


@dataclass
class Bundle:
    """Everything the steps read, built once (immutable by convention): the writer's source (the Master text,
    ``tour_dict``), the brand rules, the SEO and writing-memory views, T3's grounding baseline (``source_texts``),
    the code-composed slots and the raw FactSheet's sha (recorded, never fed to the writer: no prompt change)."""
    tour_dict: dict
    brand_rules: dict
    seo_data: dict
    writing_memory: Optional[dict]
    tenant_id: str
    source_texts: list
    slots: dict
    fs_sha: Optional[str] = None
    fs_pointer_failures: int = 0

    @property
    def sha(self) -> str:
        return _sha({"tour": self.tour_dict, "brand": self.brand_rules, "seo": self.seo_data,
                     "wm": (self.writing_memory or {}).get("stamp") if isinstance(self.writing_memory, dict)
                     else None, "slots": self.slots, "fs": self.fs_sha})


def day_slots(itineraries: Any) -> list[int]:
    """The day numbers of an itinerary in order, duplicates kept, from its "Day N" marker lines in either written form
    ("Day N — Title" as the Master stores it, "Day N: Title" after the AA-778 dash fix); a range marker gives every
    day it covers. ``final_contract._day_numbers`` (``output_days``) reads only the dash form, so it does not see the
    days of a dash-fixed T2 text (follow-up in docs/implementation-notes/AA-1049-l15-t2.md)."""
    from services.content_generation.day_parse import source_day_markers
    return [d for first, last, _ in source_day_markers(str(itineraries or "")) for d in range(first, last + 1)]


def compose_slots(tour_dict: dict) -> dict:
    """The fields code owns, from the Master (immutable: verify asserts them on every candidate). Today: the day
    numbers of the Master itinerary, in order."""
    try:
        days = day_slots(tour_dict.get("itineraries"))
    except Exception:  # noqa: BLE001 — a slot read never fails the rewrite: no slot, no slot check
        days = []
    return {"day_numbers": days}


def build_bundle(pt, tour_dict: dict, brand_rules: dict, seo_data: dict, writing_memory: Optional[dict],
                 tenant_id: str) -> Bundle:
    from services.quality import factsheet
    fs_sha, pf = None, 0
    try:
        tour, agency = factsheet.raw_source_tour(pt)
        sheet = factsheet.build_tour_factsheet(tour, agency=agency)
        fs_sha, pf = sheet.sha(), len(sheet.pointer_failures)
    except Exception as e:  # noqa: BLE001 — recorded only
        logger.warning("t2_orchestrator_factsheet_failed", error=str(e)[:200])
    return Bundle(tour_dict=tour_dict, brand_rules=brand_rules, seo_data=seo_data or {},
                  writing_memory=writing_memory, tenant_id=tenant_id,
                  source_texts=[str(v) for v in tour_dict.values() if v],   # T3's baseline, as main
                  slots=compose_slots(tour_dict), fs_sha=fs_sha, fs_pointer_failures=pf)


# ── steps ───────────────────────────────────────────────────────────────────────────────────────


def _graph_state(bundle: Bundle, *, feedback: str = "", previous_draft: Optional[dict] = None) -> dict:
    from api.routers.v1_pipeline import initial_state_for
    return initial_state_for(bundle.tour_dict, brand_rules=bundle.brand_rules, seo=bundle.seo_data,
                             is_tenant_rewrite=True, tenant_id=bundle.tenant_id, generate_stage=STAGE,
                             feedback=feedback, previous_draft=previous_draft, writing_memory=bundle.writing_memory)


def write(bundle: Bundle, *, feedback: str = "", previous_draft: Optional[dict] = None) -> dict:
    """ONE writer call: main's ``generate_node`` on main's initial state (same prompt bytes, same gateway call, same
    ``record_call_sync(**billed_fields(resp))`` row). Returns the result dict in ``_rewrite_tour``'s shape (the keys
    the save reads). Sync: run in a thread. A refused / budget-stopped call raises as on main."""
    from api.routers.v1_pipeline import _normalize_generated
    from services.content_generation import graph
    out = graph.generate_node(_graph_state(bundle, feedback=feedback, previous_draft=previous_draft))
    gen = out.get("generated") or {}
    return {"generated": _normalize_generated(json.loads(json.dumps(gen, default=str)), bundle.tour_dict)
            if gen else {}, "raw_generated": gen,
            "quality_score": 0.0, "model_used": out.get("model_used", ""), "cost_usd": out.get("cost_usd", 0.0),
            "fallback_used": out.get("fallback_used", False), "satellite_account": out.get("satellite_account"),
            "prompt_version": out.get("prompt_version", ""), "error": out.get("error", ""),
            "degraded": list(out.get("degraded") or [])}


def normalize(generated: dict, bundle: Bundle) -> int:
    """T3's per-candidate code fixes, in place (same order as run_t3_qa_gate under the interim profile): title fit,
    meta fit, em-dash fix. Returns the em-dashes fixed."""
    from services.content_generation.seo_meta_utils import fit_seo_meta_final, fit_seo_title
    from services.content_generation.t2_dashes import TEXT_FIELDS, fix_t2_dashes
    td = bundle.tour_dict
    if isinstance(generated.get("seo_title"), str):
        generated["seo_title"] = fit_seo_title(generated["seo_title"])
    if isinstance(generated.get("seo_meta"), str):
        generated["seo_meta"] = fit_seo_meta_final(
            generated["seo_meta"], {"duration": td.get("duration"), "country": td.get("country"),
                                    "name": td.get("name")}, bundle.brand_rules.get("forbidden_words"))
    fixed, log = fix_t2_dashes(generated, td)
    if log:
        generated.update({k: fixed[k] for k in TEXT_FIELDS if k in generated})
    return len(log)


def slot_findings(generated: dict, bundle: Bundle) -> list[str]:
    """The Master day slots on a candidate: every Master day present, in order, none added or repeated."""
    want = bundle.slots.get("day_numbers") or []
    if not want:
        return []
    try:
        got = day_slots(generated.get("itineraries"))
    except Exception:  # noqa: BLE001 — unreadable = broken slot (fail closed)
        got = None
    return [] if got == want else ["T2_DAY_COUNT_VS_MASTER"]


def validate_score(generated: dict, bundle: Bundle) -> float:
    """validate_node's score of a candidate on the graph's own state (the score main's status reads)."""
    from services.content_generation.graph import validate_node
    state = {**_graph_state(bundle), "generated": generated}
    try:
        return float(validate_node(state).get("quality_score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


async def verify(generated: dict, bundle: Bundle, degraded_list: list) -> dict:
    """The deterministic findings of one candidate (+ Jev, which can only clear a grounding hit, as on main)."""
    from services.acp_produce import tenant_pipeline as tp
    from services.content_generation.t2_repair import background_facts_enabled
    from services.quality import degraded
    structural = tp._t3_structural_issues(generated, bundle.tour_dict, bundle.brand_rules)
    background: list = []
    grounding = tp._t3_grounding_check(generated, bundle.source_texts, background=background_facts_enabled(),
                                       cleared=background)
    jev_errors: list = []
    grounding, cleared = await tp.t3_jev_clear(grounding, bundle.tour_dict, bundle.tenant_id, jev_errors)
    for e in degraded.jev_entries(jev_errors, detail=tp.T3_JEV_STAGE):
        degraded_list[:] = degraded.add(degraded_list, e["name"], detector="t3_jev_clear", count=e["count"],
                                        severity="count", detail=f"{e['detail']} (hits kept)")
    slots = slot_findings(generated, bundle)
    return {"structural": structural, "grounding": grounding, "slots": slots, "background": len(background),
            "jev_cleared": len(cleared), "score": validate_score(generated, bundle)}


def enforce_codes(f: dict) -> list[str]:
    from services.quality.registry import T3_GROUNDING
    return list(f["structural"]) + ([T3_GROUNDING] if f["grounding"] else []) + list(f["slots"])


def plan(codes: list[str]) -> dict[str, list[str]]:
    """{action: [codes]} from the route table's t2 rows (unknown code -> the table default)."""
    from services.acp_content_writing import route_table as rt
    out: dict[str, list[str]] = {}
    for c in codes:
        out.setdefault(rt.t2_route(c).action, []).append(c)
    return out


def best_key(c: dict) -> tuple:
    """select_best: a clean candidate first, then fewer enforce findings, then validate's score, then the later
    candidate (a repaired copy of an equal draft)."""
    return (not c["codes"], -len(c["codes"]), c["score"], c["attempt"])


def linear_status(score: float, codes: list[str], fallback_held: bool) -> str:
    """L1: main's line (``_t2_status``: score + T3 hard codes) AND no enforce finding AND no held fallback writer."""
    from services.jobs.t2_rewrite_job import _t2_status
    if _t2_status(score, codes) == "ai_generated" and not fallback_held:
        return "ai_generated"
    return "needs_review"


# ── checkpoint (resume-from-best) ───────────────────────────────────────────────────────────────


async def load_checkpoint(pool, version_id: str) -> Optional[dict]:
    """The checkpoint of a pending version: ``{"budget": {...}, "best": {...}?}``. A checkpoint without ``best`` (the
    budget was spent before any candidate existed) still carries the budget, so a retry does not re-spend it. Inside
    ``t2_store.use_store`` it reads the dry store's checkpoints, never the pool."""
    store = active()
    if store is not None:
        cp = store.checkpoints.get(str(version_id))
        return json.loads(json.dumps(cp)) if cp else None
    try:
        async with pool.acquire() as conn:
            raw = await conn.fetchval(
                "SELECT generation_meta FROM gold_aa_internal.tenant_tour_versions WHERE id = $1::uuid", version_id)
        meta = json.loads(raw) if isinstance(raw, str) else (raw or {})
        cp = (meta.get("t2_orchestrator") or {}).get("checkpoint") if isinstance(meta, dict) else None
        if not isinstance(cp, dict) or not isinstance(cp.get("budget"), dict):
            return None
        return cp if isinstance(cp.get("best"), dict) or cp.get("best") is None else {"budget": cp["budget"]}
    except Exception as e:  # noqa: BLE001 — no checkpoint = a fresh write (the budget then starts at 0)
        logger.warning("t2_orchestrator_checkpoint_unreadable", error=str(e)[:200])
        return None


_BEST_KEYS = ("attempt", "via", "generated", "prompt_version", "model_used", "fallback_used", "score")


async def save_checkpoint(pool, version_id: str, budget: VersionBudget, best: Optional[dict]) -> None:
    """The budget used + the best candidate so far (none before the first candidate), on the still-pending version
    (admin-only generation_meta), so a job retry neither pays a call twice nor writes again. Best effort. Inside
    ``t2_store.use_store`` it goes to the dry store, never the pool."""
    point: dict = {"budget": budget.as_dict()}
    if best is not None:
        point["best"] = {k: best[k] for k in _BEST_KEYS}
    store = active()
    if store is not None:
        store.checkpoints[str(version_id)] = json.loads(json.dumps(point, default=str))
        return
    cp = {"t2_orchestrator": {"version": RECORD_VERSION, "checkpoint": point}}
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE gold_aa_internal.tenant_tour_versions SET generation_meta = $1::jsonb "
                "WHERE id = $2::uuid AND status = 'pending'", json.dumps(cp, default=str), version_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("t2_orchestrator_checkpoint_failed", error=str(e)[:200])


# ── the run ─────────────────────────────────────────────────────────────────────────────────────


@dataclass
class Run:
    bundle: Bundle
    budget: VersionBudget
    pool: Any = None
    version_id: Optional[str] = None
    candidates: list = field(default_factory=list)
    steps: list = field(default_factory=list)
    degraded: list = field(default_factory=list)
    em_dash_fixes: int = 0
    sentence_repairs: int = 0
    meta_tried: set = field(default_factory=set)
    meta_attempts: int = 0
    resumed: bool = False


async def _check(run: Run, result: dict, *, attempt: int, via: str, repaired: bool = False,
                 parent_score: Optional[float] = None) -> dict:
    """Normalize + verify one candidate. Its score is never above main's for the same text lineage: the lower of
    validate's score before and after the code fixes (main scores the writer's draft before T3's fixes), and, for a
    local repair of a draft, never above that draft's (main carries the draft's score onto its repair)."""
    gen = result.get("generated") or {}
    # the writer's own draft as main's graph validates it (generate_node output), else this candidate's text
    raw = result.pop("raw_generated", None) or gen
    pre = validate_score(json.loads(json.dumps(raw, default=str)), run.bundle)
    run.em_dash_fixes += normalize(gen, run.bundle)
    f = await verify(gen, run.bundle, run.degraded)
    f["score"] = min(x for x in (f["score"], pre, parent_score) if x is not None)
    # the text exactly as verify() checked it: the final contract compares the stored text with this copy
    gated = json.loads(json.dumps(gen, default=str))
    cand = {**f, "result": result, "attempt": attempt, "via": via, "repaired": repaired, "gated": gated,
            "codes": enforce_codes(f), "generated": gen, "prompt_version": result.get("prompt_version", ""),
            "model_used": result.get("model_used", ""), "fallback_used": bool(result.get("fallback_used"))}
    run.candidates.append(cand)
    run.steps.append({"attempt": attempt, "via": via, "codes": cand["codes"], "score": cand["score"],
                      "plan": {k: v for k, v in plan(cand["codes"]).items()}})
    return cand


async def _persist(run: Run) -> None:
    """Persist the budget (and the best candidate so far) of a run that has a version to write to."""
    if run.pool is not None and run.version_id:
        best = max(run.candidates, key=best_key) if run.candidates else None
        await save_checkpoint(run.pool, run.version_id, run.budget, best)


async def _reserve(run: Run, kind: str) -> None:
    """Count one billed call of ``kind`` and persist the count BEFORE the call is sent (pessimistic: a call that
    raises, or a crash right after it, is still counted when the job retries)."""
    run.budget.spend(kind)
    await _persist(run)


async def _reserve_n(run: Run, kind: str, n: int) -> None:
    run.budget.spend(kind, n=n)
    await _persist(run)


async def _settle(run: Run, usd: Any) -> None:
    """Add the cost a billed call reported and persist it right after the call returns (before its candidate is
    verified: a crash in verify / Jev / validate never loses a spent call)."""
    run.budget.charge(usd)
    await _persist(run)


async def _first_write(run: Run) -> dict:
    """The first candidate: one write, a second only when the first returned nothing usable (JSON parse failure:
    main's graph retries it too)."""
    while run.budget.can("writer"):
        await _reserve(run, "writer")
        res = await asyncio.to_thread(write, run.bundle)
        await _settle(run, res.get("cost_usd"))
        run.degraded.extend(res.get("degraded") or [])
        if res.get("generated"):
            return res
        logger.warning("t2_orchestrator_write_empty", error=str(res.get("error"))[:200], writer=run.budget.writer)
    raise RuntimeError("rewrite did not succeed: the T2 writer returned no usable draft within its budget")


async def orchestrate(run: Run, resume: Optional[dict] = None, *, pool=None, version_id: Optional[str] = None) -> dict:
    """The candidate loop. Returns the selected candidate."""
    from services.acp_produce import tenant_pipeline as tp
    from services.quality import degraded

    if pool is not None:
        run.pool = pool
    if version_id:
        run.version_id = version_id
    if resume and isinstance(resume.get("best"), dict):
        best = resume["best"]
        result = {"generated": dict(best.get("generated") or {}), "prompt_version": best.get("prompt_version"),
                  "model_used": best.get("model_used"), "fallback_used": best.get("fallback_used"), "cost_usd": 0.0}
        run.resumed = True
        current = await _check(run, result, attempt=int(best.get("attempt") or 0), via="resumed",
                               parent_score=_float(best.get("score")))
    else:
        current = await _check(run, await _first_write(run), attempt=0, via="initial")
    attempt = current["attempt"]
    while True:
        best = max(run.candidates, key=best_key)
        await _persist(run)
        if not best["codes"]:
            return best
        p = plan(best["codes"])
        nxt = None
        meta = best["generated"].get("seo_meta")
        if (p.get("regenerate_section") and isinstance(meta, str) and meta not in run.meta_tried
                and run.budget.can("repair") and run.budget.left("repair") >= META_ATTEMPTS):
            run.meta_tried.add(meta)
            gen = dict(best["generated"])
            new_meta, violated = await _meta_regenerate(run, tp, gen)
            if violated:
                run.degraded = degraded.add(run.degraded, degraded.REPAIR_CONTRACT, detector="t3_meta_regenerate",
                                            detail=", ".join(violated["invariants"]))
            if new_meta:
                run.meta_tried.add(new_meta)
                nxt = ({**best["result"], "generated": {**gen, "seo_meta": new_meta}}, "meta_regenerate", False)
        if nxt is None and p.get("repair_sentence") and not best["repaired"] and run.budget.can("repair"):
            best["repaired"] = True   # one sentence repair per draft
            await _reserve(run, "repair")
            base = {"result": {**best["result"], "generated": best["generated"]}, "grounding": best["grounding"],
                    "attempt": best["attempt"]}
            repaired = await asyncio.to_thread(tp._t3_sentence_repair, base, run.bundle.tour_dict,
                                               run.bundle.source_texts, run.bundle.brand_rules,
                                               run.bundle.tenant_id, _background())
            if repaired is not None:
                run.budget.usd = round(run.budget.usd + float(repaired.get("cost_usd") or 0)
                                       - float(best["result"].get("cost_usd") or 0), 6)
                if repaired.get("t3_contract_reverts"):
                    run.degraded = degraded.add(run.degraded, degraded.REPAIR_CONTRACT,
                                                detector="t3_sentence_repair", count=repaired["t3_contract_reverts"])
                run.sentence_repairs += 1
                nxt = (repaired, "sentence_repair", True)
        if nxt is None and p.get("rewrite") and run.budget.can("writer"):
            feedback = tp.t3_repair_feedback(best["structural"] + best["slots"], best["grounding"])
            await _reserve(run, "writer")
            res = await asyncio.to_thread(write, run.bundle, feedback=feedback, previous_draft=best["generated"])
            await _settle(run, res.get("cost_usd"))
            run.degraded.extend(res.get("degraded") or [])
            if res.get("generated"):
                nxt = (res, "field_regen", False)
        if nxt is None:
            run.steps.append({"stop": "budget_or_no_route", "budget": run.budget.as_dict()})
            return best
        attempt += 1
        await _check(run, nxt[0], attempt=attempt, via=nxt[1], repaired=nxt[2],
                     parent_score=best["score"] if nxt[1] != "field_regen" else None)


async def _meta_regenerate(run: Run, tp, gen: dict) -> tuple:
    """T3's meta regenerate (``_t3_meta_regenerate``: flag_fix's bounded ``_rerepair_meta`` loop, up to
    ``META_ATTEMPTS`` t2_flag_fix calls that the function does not report). Pessimistic: all its attempts are counted
    and persisted before it runs (the caller checks they fit the repair budget), then the count is set to the calls it
    really billed and their cost (``call_log.meter_calls``), and persisted again."""
    from shared.llm_client.call_log import meter_calls
    await _reserve_n(run, "repair", META_ATTEMPTS)
    with meter_calls() as calls:
        try:
            out = await asyncio.to_thread(tp._t3_meta_regenerate, gen, run.bundle.tour_dict, run.bundle.brand_rules,
                                          run.bundle.tenant_id, run.bundle.source_texts)
        finally:
            billed = [c for c in calls if c[0] == "t2_flag_fix"]
            run.budget.repair -= META_ATTEMPTS - min(len(billed), META_ATTEMPTS)
            run.meta_attempts += len(billed)
            await _settle(run, sum(c[1] for c in billed))
    return out


def _float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _background() -> bool:
    from services.content_generation.t2_repair import background_facts_enabled
    return background_facts_enabled()


async def voice_report(run: Run, chosen: dict) -> dict:
    """L3: ONE judge call on the selected draft (report-only under the T2 interim profile; main's ``judge_node``,
    which writes its own llm_call_log rows with the billed fields). Returns the judge_* keys for judge_breakdown.
    The call is counted and persisted before it is sent: a retry after a failed save does not pay it again (the
    voice report is then absent: report-only)."""
    if not run.budget.can("voice"):
        return {}
    from services.content_generation.judge_node import judge_node
    await _reserve(run, "voice")
    state = {**_graph_state(run.bundle), "generated": chosen["generated"], "quality_score": chosen["score"]}
    out = await asyncio.to_thread(judge_node, state)   # a sync model call: off the job worker's event loop
    await _settle(run, out.get("cost_usd"))
    run.degraded.extend(e for e in (out.get("degraded") or []) if e not in run.degraded)
    return {k: out.get(k) for k in ("judge_brand_fit", "judge_cross_brand_distinct", "judge_mission_present",
                                    "judge_feedback", "judge_score", "judge_t2_axes") if k in out}


def brand_report(chosen: dict, bundle: Bundle) -> list[str]:
    """L4: the deterministic brand pre-audit codes of the selected draft (report; main runs them before flag_fix)."""
    from services.content_generation.brand_audit_node import pre_audit_checks
    try:
        return list(pre_audit_checks(chosen["generated"], bundle.tour_dict, tenant_id=bundle.tenant_id,
                                     is_tenant_rewrite=True))
    except Exception as e:  # noqa: BLE001 — report only
        logger.warning("t2_orchestrator_brand_report_failed", error=str(e)[:200])
        return []


def stored_text(gen: dict, tour_dict: dict) -> dict:
    """The ``rewritten_content`` shape of a candidate (main's save builds the same keys)."""
    return {
        "name": gen.get("name", tour_dict["name"]), "subtitle": gen.get("subtitle", ""),
        "summary": gen.get("summary", ""), "highlights": gen.get("highlights", []),
        "itineraries": gen.get("itineraries", tour_dict.get("itineraries", "")),
        "seo_title": gen.get("seo_title", ""), "seo_meta": gen.get("seo_meta", ""),
        "trip_type": gen.get("trip_type", ""), "status": "done",
    }


def record(run: Run, chosen: dict, status: str, brand_codes: list[str]) -> dict:
    """The admin-only replay record of a linear version (generation_meta.t2_orchestrator)."""
    from services.acp_content_writing import route_table as rt
    return {"version": RECORD_VERSION, "route_table": rt.T2_MECHANICS_VERSION, "bundle_sha": run.bundle.sha,
            "fs_sha": run.bundle.fs_sha, "fs_pointer_failures": run.bundle.fs_pointer_failures,
            "slots": run.bundle.slots, "budget": run.budget.as_dict(), "resumed": run.resumed,
            "steps": run.steps, "chosen": {"attempt": chosen["attempt"], "via": chosen["via"],
                                           "codes": chosen["codes"], "score": chosen["score"]},
            "status": status, "brand_precheck": brand_codes, "em_dash_fixes": run.em_dash_fixes,
            "meta_attempts": run.meta_attempts}


async def run_and_save(pool, pt, tour_dict: dict, brand_rules: dict, tenant_id: str, version_id: str,
                       published_tour_id: str) -> dict:
    """The linear T2 run of one version (called by ``_rewrite_and_save`` after the policy route, only in ``linear``
    mode). Same inputs, SEO / writing-memory lookups, progress steps and store columns as main's path."""
    from services.acp_shared.writing_progress import progress_fail, progress_show, progress_step
    from services.jobs import t2_rewrite_job as job
    from services.quality import route_guard
    from services.writing_memory.insertion import (generation_meta as _wm_meta, resolve as _wm_resolve,
                                                   stamp as _wm_stamp, writer_answer as _wm_answer)

    seo_data, diag = await job.t2_seo(pool, pt, tenant_id)
    writing_memory = await _wm_resolve(pool, "t2", tenant_id)
    if writing_memory:
        diag["writing_release"] = _wm_stamp(writing_memory)
    bundle = build_bundle(pt, tour_dict, brand_rules, seo_data, writing_memory, tenant_id)
    resume = await load_checkpoint(pool, version_id)
    run = Run(bundle=bundle, budget=VersionBudget.from_dict((resume or {}).get("budget")), pool=pool,
              version_id=version_id)
    fb_scope = ExitStack()
    fb_notes = fb_scope.enter_context(route_guard.collect_output())
    try:
        chosen = await orchestrate(run, resume, pool=pool, version_id=version_id)
        progress_step("check")
        judge = await voice_report(run, chosen)
        brand_codes = brand_report(chosen, bundle)
        fb_mark = route_guard.t2_mark(fb_notes)
        status = linear_status(chosen["score"], chosen["codes"], bool((fb_mark or {}).get("held")))
        result = {**chosen["result"], **judge, "generated": chosen["generated"], "quality_score": chosen["score"],
                  "prompt_version": chosen["prompt_version"], "model_used": chosen["model_used"],
                  "fallback_used": chosen["fallback_used"]}
        rewritten = stored_text(chosen["generated"], tour_dict)
        passed = not chosen["codes"]
        qa = {"passed": passed, "attempts": len(run.candidates) - 1,
              "structural_issues": [] if passed else list(chosen["structural"]) + list(chosen["slots"]),
              "grounding_issues": [] if passed else list(chosen["grounding"])}
        final_score = chosen["score"] or None
        judge_breakdown = job.t2_judge_breakdown(result, rewritten, tour_dict, brand_rules, final_score=final_score,
                                                 score_source="rewrite" if chosen["score"] else "none",
                                                 chosen_attempt=chosen["attempt"])
        if fb_mark is not None:
            judge_breakdown = {**(judge_breakdown or {}), "fallback_writer": fb_mark}
            diag["fallback_writer"] = fb_mark
        inst_mark = route_guard.t2_instrument_mark(fb_notes)
        if inst_mark is not None:
            judge_breakdown = {**(judge_breakdown or {}), "route_degraded": inst_mark}
            diag["route_degraded"] = inst_mark
        contract = job.final_contract_record(rewritten, tour_dict, pt, exit="done", actual=status, qa=qa,
                                             fallback_held=bool((fb_mark or {}).get("held")), score=chosen["score"],
                                             gated=stored_text(chosen["gated"], tour_dict))
        if contract is not None:
            judge_breakdown = {**(judge_breakdown or {}), "final_contract": contract}
            diag["final_contract"] = {k: contract.get(k) for k in ("verdict", "agrees", "flip", "sha_match")}
        rec = record(run, chosen, status, brand_codes)
        generation_meta = {**(_wm_meta(writing_memory, prompt_version=result.get("prompt_version"),
                                       answer=_wm_answer(result)) or {}), "t2_orchestrator": rec}
        diag["t2_orchestrator"] = {k: rec[k] for k in ("version", "budget", "chosen", "status", "resumed")}
        diag["t3"] = {"chosen_attempt": chosen["attempt"], "chosen_via": chosen["via"],
                      "sentence_repairs": run.sentence_repairs, "degraded": run.degraded}
        progress_show(rewritten)
        progress_step("save")
        await save_version(pool, version_id, rewritten=rewritten, status=status, quality_score=final_score,
                           qa=qa, generation_meta=generation_meta, judge_breakdown=judge_breakdown,
                           final_contract=contract)
        if not passed:
            from services.acp_produce.tenant_pipeline import escalate_t3_failure
            store = active()
            if store is None:
                await escalate_t3_failure(pool, tenant_id, pt["tour_id"], str(version_id), qa["structural_issues"],
                                          qa["grounding_issues"])
            else:
                store.reviews.append({"structural": qa["structural_issues"], "grounding": qa["grounding_issues"]})
        logger.info("t2_orchestrator_done", version_id=str(version_id), status=status, writer=run.budget.writer,
                    repair=run.budget.repair, voice=run.budget.voice, chosen=chosen["via"], resumed=run.resumed)
    except Exception:
        progress_fail()
        raise
    finally:
        fb_scope.close()
    return diag


# ── store seam (the T2 dry store) ───────────────────────────────────────────────────────────────


def active():
    """The dry store of the current replay task (``t2_store.use_store``), None in production."""
    from services.content_generation import t2_store
    return t2_store.active()


async def save_version(pool, version_id: str, *, rewritten: dict, status: str, quality_score, qa: dict,
                       generation_meta: Optional[dict], judge_breakdown: Optional[dict],
                       final_contract: Optional[dict]) -> dict:
    """The linear path's one store step: the same columns as main's save. Not itself a final-contract exit
    (``final_contract.EXITS`` lists its caller ``run_and_save``, which runs the contract on the text and passes the
    record in; the topology test lists this function in NOT_EXITS). Inside ``t2_store.use_store(DryStore())`` it hands
    the row to the dry store instead (no DB).

    It stamps the contract with the sha of the text it writes (``final_contract.stamp_stored``): ``sha_match`` is then
    the text the contract checked against the text stored, so an edit between the contract and the write shows
    (``POST_GATE_EDIT``); an edit between verify and the contract shows as the contract's own POST_GATE_EDIT finding
    (``gated_sha`` = the selected draft exactly as verify checked it)."""
    from services.quality import final_contract as fc
    store = active()
    if final_contract is not None:
        final_contract = fc.stamp_stored(final_contract, fc.tour_text_sha(rewritten))
        judge_breakdown = {**(judge_breakdown or {}), "final_contract": final_contract}
    row = {"version_id": str(version_id), "rewritten": rewritten, "status": status, "quality_score": quality_score,
           "qa_status": "passed" if qa["passed"] else "escalated", "qa_repair_count": qa["attempts"],
           "qa_auto_passed": not qa["passed"], "generation_meta": generation_meta,
           "judge_breakdown": judge_breakdown, "final_contract": final_contract}
    if store is not None:
        return store.save(row)
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE gold_aa_internal.tenant_tour_versions
            SET rewritten_content = $1::jsonb,
                status = $2,
                quality_score = $3,
                qa_status = $4,
                qa_repair_count = $5,
                qa_checked_at = now(),
                qa_auto_passed = $6,
                generation_meta = $8::jsonb,
                judge_breakdown = $9::jsonb
            WHERE id = $7::uuid
        """, json.dumps(rewritten), status, quality_score, row["qa_status"], qa["attempts"], row["qa_auto_passed"],
            version_id, json.dumps(generation_meta, default=str) if generation_meta is not None else None,
            json.dumps(judge_breakdown, default=str) if judge_breakdown is not None else None)
    return row
