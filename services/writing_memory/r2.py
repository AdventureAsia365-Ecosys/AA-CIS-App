"""AA-919 WM-19 — the R2 runner: a candidate release (or today's writers, a code-train run) against the golden set.

One run = one `shared.prompt_eval_runs` row (pipeline writing_memory, run_kind r2 | reverify) + one
`shared.eval_item_result` row per (item, arm, mode). The Releases tab attaches the run as a release's `eval_run_id`
(the API and the CIS 287 trigger accept it only as a finished r2 run of the same writer with verdict pass).

Sequence (WM-19 scope 1, cost-cuts #3):
  0. calibrate  two things, both with the current judge (`writing_memory_eval`, ~$0.002 a call), both stored as
                mode `rejudge` rows (scored_result_id = the stored result the text came from), one row per item:
                (a) CALIBRATION, every run, whatever supplies the control: the FROZEN labelled output of every labelled
                    non-trap item of the writer and owner (not only the planned ones, so a cheap `--limit` run is
                    calibrated too), scored against the baseline_import run's score_only row. Cohen's kappa of these
                    verdicts vs the golden labels decides whether the judge COUNTS (kappa >= 0.6 on >= 10 pairs,
                    scope 4); otherwise the run is informative only: it stops after tune (no holdout money) and
                    cannot pass. The same pairs are also scored per stratum of the golden set (channel,
                    tenant, tier, split; `details.kappa_strata`, S257) and the measurable strata under the
                    threshold are named (`details.kappa_strata_below`): reported, the gate stays the overall one.
                (b) the CONTROL of every planned item: the stored output of the owner's active release (its eval
                    run's candidate output; release 0 = the AA-729 baseline's score_only row, i.e. the frozen fixture
                    output, so (a) and (b) are the same judge call). Never regenerated outside a `reverify` run (scope
                    2; the CIS 284 trigger refuses it too).
                When (a) and (b) are different texts the row keeps the control's verdict and the calibration verdict
                goes in `details.calibration`; an item with no planned control keeps a calibration-only row.
  1. probe      <= 3 items, the injection traps first (filled up with tune items).
  2. tune       the tune split (70%) of the affected tiers (A1 / T2 always add fact_dense + long_9d, scope 7).
  3. holdout    the holdout split (30%) only when tune passed.
  Per candidate item: regenerate through the writer with the candidate view, judge, then the deterministic checks:
  QA artefacts (hard stop 3), isolation probe against every other tenant of the golden set (hard stop 2), trap
  defence. Stop at once on a hard stop (1 = a NEW hard fact class vs the item's control, counted only with a
  calibrated judge) or at the end of tune on futility. Never stop early on success.

Money (E4 / D3): a HARD cap per run, <= $10 (`cfg.R2_RUN_CAP_USD`), configurable lower, read from llm_call_log: every
model call of the run (writer, its own judges and Jev, the eval judge) is written under stage `writing_memory_eval`
with `quality_signal.eval_run = <run id>` (call_log.bind_eval_stage), so `run_spend` is the run's real logged cost.
AA-900: a run started for a prompt / model / route change PR passes `--stage adhoc_aa900` (`log_stage`): the same run
under the ad-hoc label the handbook asks for (§5.7); `run_spend` and the month cap read that label too.
Before each item the runner reserves the item's estimate and stops (`budget_cap`, verdict invalid) when logged spend +
estimate would pass the cap; after each item it stops when the logged spend has passed it. The $25 month cap over every
writing-memory stage applies the same way (budget.py).

`goal`: `non_inferior` (an empty release or a code-train run: new failures - fixed <= 0 and new <= noise_k) or
`improve` (a release with rules: fixed > new and new <= noise_k), quality-system design §4.2.

`--seed-fault` (operator check of the stop, WM-19 Done-when "a seeded failing candidate stops at tune"): the candidate
of each item is its stored control text with an injected fault (an unsourced inclusion claim, or a QA trace), no writer
call (judge cost only), traps skipped; never with a release, and never verdict `pass` (a fault the judge misses ends
`informative`); the release evidence check also requires the run's release_id to be the release's (releases.py, CIS
292), so a seeded run can never become evidence.

`goal` `non_inferior` is refused for a rules / repair_hint_only release (they must improve, §4.2).

CLI (inside the api/worker container; DRY RUN prints the plan and the estimate, $0):
  python -m services.writing_memory.r2 --writer a1 [--release <id>] [--tiers fact_dense,long_9d] [--limit 8]
         [--cap-usd 2] [--goal non_inferior] [--seed-fault qa_trace] [--stage adhoc_aa900] [--enqueue]
`--enqueue` queues the job `writing_memory_r2` on the worker (the run itself is never done in an exec session: a long
paid run dies with the session, lessons S234).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional, Protocol

import structlog

from services.writing_memory import config as cfg
from services.writing_memory import eval_checks as ec
from shared.llm_client import family_rule

logger = structlog.get_logger()

GOLDEN_SET = "aa729"
RUN_KINDS = ("r2", "reverify")
GOALS = ("non_inferior", "improve")
SEED_FAULTS = {
    "unsourced_claim": (" This trip also includes a complimentary private helicopter transfer and a five-star spa"
                        " night."),
    "qa_trace": " [src:1] rule_ids",            # a citation tag and a memory key: hard stop 3 (eval_checks)
}
_TEXT_FIELDS = ("content_text", "description", "summary", "subtitle", "name")


class R2Refused(ValueError):
    """The run cannot start (bad arguments, no items, a release in the wrong state): nothing was spent."""


# ── the store (Postgres in production, a fake in the unit tests) ───────────────────────────────────────────────────

class Store(Protocol):
    async def get_release(self, release_id: str) -> Optional[dict]: ...
    async def active_parts(self, writer: str, tenant_id: Optional[str]) -> dict: ...
    async def golden_set(self) -> Optional[dict]: ...
    async def items(self, set_id: str, writer: str, tenant_id: Optional[str]) -> list[dict]: ...
    async def tenant_packs(self, set_id: str) -> dict[str, dict]: ...
    async def baseline_run(self, set_id: str, writer: str) -> Optional[str]: ...
    async def control_sources(self, run_id: str, item_ids: list[str]) -> dict[str, dict]: ...
    async def calibration_sources(self, run_id: str, item_ids: list[str]) -> dict[str, dict]: ...
    async def rule_texts(self, rule_ids: list[str]) -> list[tuple[str, str]]: ...
    async def fingerprint(self, writer: str) -> dict: ...
    async def create_run(self, row: dict) -> str: ...
    async def insert_result(self, row: dict) -> str: ...
    async def finish_run(self, run_id: str, row: dict) -> None: ...
    async def run_spend(self, run_id: str, since: datetime) -> float: ...
    async def month_spend(self) -> float: ...


@dataclass
class R2Config:
    writer: str
    release_id: Optional[str] = None
    run_kind: str = "r2"
    tiers: Optional[list[str]] = None
    limit: Optional[int] = None
    cap_usd: float = cfg.R2_RUN_CAP_USD
    goal: Optional[str] = None
    seed_fault: Optional[str] = None
    triggered_by: str = "manual"
    log_stage: str = cfg.EVAL_STAGE    # AA-900: the llm_call_log label of the run (cfg.EVAL_LOG_STAGES)

    def validate(self) -> None:
        if self.writer not in ("a1", "t2", "t9_blog", "t9_social"):
            raise R2Refused(f"unknown writer {self.writer!r}")
        if self.run_kind not in RUN_KINDS:
            raise R2Refused(f"run kind must be one of {RUN_KINDS}")
        if not (0 < float(self.cap_usd) <= cfg.R2_RUN_CAP_USD):
            raise R2Refused(f"cap_usd must be > 0 and <= {cfg.R2_RUN_CAP_USD} (E4); a bigger run needs its own "
                            "approval")
        if self.goal is not None and self.goal not in GOALS:
            raise R2Refused(f"goal must be one of {GOALS}")
        if self.seed_fault is not None:
            if self.seed_fault not in SEED_FAULTS:
                raise R2Refused(f"seed_fault must be one of {sorted(SEED_FAULTS)}")
            if self.release_id:
                raise R2Refused("a seeded-fault run takes no release (it must never become a release's evidence)")
            if self.run_kind != "r2":
                raise R2Refused("a seeded-fault run is an r2 run")
        if self.limit is not None and self.limit < 1:
            raise R2Refused("limit must be >= 1")
        if self.log_stage not in cfg.EVAL_LOG_STAGES:
            raise R2Refused(f"log_stage must be one of {cfg.EVAL_LOG_STAGES}")
        if self.release_id and self.log_stage != cfg.EVAL_STAGE:
            raise R2Refused(f"a release run logs under {cfg.EVAL_STAGE} (a {self.log_stage} change eval must never "
                            "become a release's evidence)")
        if self.tiers is not None:
            bad = set(self.tiers) - set(_TIERS)
            if bad:
                raise R2Refused(f"unknown tiers {sorted(bad)}")


_TIERS = ("cycle_bike", "trek_hike", "water", "wildlife", "culinary", "long_9d", "long_18d", "poor_raw",
          "known_failure", "held", "fact_dense", "prompt_injection", "post_edit")


@dataclass
class Outcome:
    run_id: Optional[str] = None
    verdict: Optional[str] = None
    stop_reason: Optional[str] = None
    stopped_phase: Optional[str] = None
    cost_usd: float = 0.0
    judge_kappa: Optional[float] = None
    judge_counts: bool = False
    counts: dict = field(default_factory=dict)
    kappa_strata_below: list = field(default_factory=list)   # S257: measurable strata with kappa < MIN_KAPPA


WriterFn = Callable[..., Awaitable[dict]]
JudgeFn = Callable[..., Awaitable[Any]]


def sha256_json(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def seed(output: dict, fault: str) -> dict:
    """The control output with the fault appended to its first reader text field (operator check only)."""
    out = dict(output or {})
    for k in _TEXT_FIELDS:
        if isinstance(out.get(k), str) and out[k].strip():
            out[k] = out[k].rstrip() + SEED_FAULTS[fault]
            return out
    out["content_text"] = SEED_FAULTS[fault].strip()
    return out


def _part(row: Optional[dict]) -> Optional[dict]:
    """A view part of a release row: the insertion path's own constructor (AA-920: with the rule items when the store
    loaded them, so the candidate is written with its rules through the same builder as production)."""
    from services.writing_memory.insertion import part_of
    return part_of(row)


def build_views(writer: str, active: dict, candidate: Optional[dict]) -> tuple[Optional[dict], Optional[dict]]:
    """(control view = the owner's active pointers, candidate view = the same with the candidate's scope replaced)."""
    control = {"writer": writer, "platform": _part(active.get("platform")), "tenant": _part(active.get("tenant"))}
    cand = dict(control)
    if candidate is not None:
        cand["platform" if candidate["scope"] == "platform" else "tenant"] = _part(candidate)

    def _norm(v):
        return v if (v.get("platform") or v.get("tenant")) else None
    return _norm(control), _norm(cand)


def default_goal(release: Optional[dict]) -> str:
    return "improve" if release and release.get("content_class") in ("rules", "repair_hint_only") else "non_inferior"


def control_text(src: Optional[dict], item: dict) -> tuple[Optional[dict], bool]:
    """(the control's output, whether it is the frozen labelled fixture output). A human-edited frozen output
    (post_edit) is never a writer control (critic 10): None."""
    if src is None:
        return None, False
    if src.get("output") is not None:
        return src["output"], False
    if item.get("post_edit"):
        return None, False
    return item.get("control_output"), True


def calibration_items(items: list[dict]) -> list[dict]:
    """The items whose FROZEN output calibrates the judge: labelled pass / fail, not a trap, not human-edited
    (post_edit), with a frozen output."""
    return [it for it in items if not it.get("trap") and not it.get("post_edit")
            and it.get("label_verdict") in ("pass", "fail") and it.get("control_output") is not None]


def _release_prompt(release: Optional[dict]) -> Optional[str]:
    """The release's prompt register (`fingerprint.prompt_sha256`, recorded by an R0 replay), when known."""
    fp = (release or {}).get("fingerprint")
    if isinstance(fp, str):
        try:
            fp = json.loads(fp)
        except ValueError:
            fp = None
    return fp.get("prompt_sha256") if isinstance(fp, dict) else None


def prompt_version(writer: str, writer_prompt: Optional[str]) -> str:
    """`prompt_eval_runs.prompt_version` of an R2 run: the writer prompt register when the release names one, else
    the eval judge's version (`<writer>:judge-<12 hex>`): a writer's system prompt is built per input (brand, channel,
    tour) and is measured by the R0 replay, not here."""
    if writer_prompt:
        return str(writer_prompt)[:64]
    from services.writing_memory.eval_judge import judge_version
    return f"{writer}:judge-{judge_version()}"[:64]


def _claims_of(v) -> dict:
    """{"judge_claims": [...]} when the verdict quotes the phrases behind its classes (eval_judge, S256), else {}."""
    claims = list(getattr(v, "claims", None) or [])[:8]
    return {"judge_claims": claims} if claims else {}


def _with_claims(det: dict, v) -> dict:
    return {**det, **_claims_of(v)} if v is not None else det


class _Stop(Exception):
    def __init__(self, reason: str, phase: str):
        super().__init__(reason, phase)
        self.reason, self.phase = reason, phase


async def run_r2(store: Store, conf: R2Config, *, writer_fn: WriterFn, judge_fn: JudgeFn,
                 flush: Callable[[], Awaitable[None]], pool=None,
                 progress: Optional[Callable[..., Awaitable[None]]] = None,
                 family_models: Optional[list] = None) -> Outcome:
    """The run. `writer_fn(writer, input, view=, shared_inputs=)` -> output dict; `judge_fn(pool, writer=,
    source_text=, output_text=, run_id=, item_id=, arm=)` -> eval_judge.Verdict; `flush()` waits for the background
    llm_call_log writes, so `run_spend` sees the item just done.

    AA-1024 A4 (ADR 0009 decision 12): every regenerated output is judged only by a model of another family than the
    models that wrote it (`_judge_family`; `family_models` = the catalog rows, None = the catalog cache). A same-family
    verdict is an `error` row, never a verdict (a writer route may end on the judge's maker, migration 327)."""
    from services.writing_memory import eval_writers as ew
    from services.writing_memory.eval_judge import judge_source_text
    from shared.llm_client.call_log import bind_eval_stage

    conf.validate()
    writer = conf.writer
    release = None
    if conf.release_id:
        release = await store.get_release(conf.release_id)
        if release is None:
            raise R2Refused("unknown release")
        if release["writer"] != writer:
            raise R2Refused(f"the release is a {release['writer']} release, not {writer}")
        if release["status"] not in ("draft", "evaluated"):
            raise R2Refused(f"a {release['status']} release keeps its evidence (draft / evaluated only)")
    owner_tenant = release["tenant_id"] if release and release["scope"] == "tenant" else None
    goal = conf.goal or default_goal(release)
    if goal == "non_inferior" and release and release["content_class"] in ("rules", "repair_hint_only"):
        raise R2Refused(f"a {release['content_class']} release must improve (goal improve, §4.2); non_inferior is for "
                        "an empty release or a code train")
    gs = await store.golden_set()
    if gs is None:
        raise R2Refused(f"no active golden set {GOLDEN_SET!r}")
    items = await store.items(gs["set_id"], writer, owner_tenant)
    if not items:
        raise R2Refused("no golden items for this writer and owner")
    active = await store.active_parts(writer, owner_tenant)
    ctl_mode = ec.control_mode(conf.run_kind)
    baseline = await store.baseline_run(gs["set_id"], writer)
    control_run = None
    for scope in ("tenant", "platform"):
        part = active.get(scope)
        if part and part.get("eval_run_id"):
            control_run = part["eval_run_id"]
            break
    control_run = control_run or baseline
    if ctl_mode == "rejudge" and control_run is None:
        raise R2Refused("no control run (no active release with an eval run, no baseline import)")

    tiers = ec.selected_tiers(writer, conf.tiers)
    plan = ec.plan_phases(items, tiers=tiers, limit=conf.limit)
    if conf.seed_fault:
        plan["probe"] = [it for it in plan["probe"] if it.get("split") != "trap"]
    planned = plan["probe"] + plan["tune"] + plan["holdout"]
    planned_ids = {it["item_id"] for it in planned}
    sources = await store.control_sources(control_run, [it["item_id"] for it in planned]) if control_run else {}
    calib = calibration_items(items)
    frozen_src = await store.calibration_sources(baseline, [it["item_id"] for it in calib]) if baseline else {}
    calib_ids = {it["item_id"] for it in calib}
    rule_texts = await store.rule_texts(list((release or {}).get("rule_ids") or []))
    # AA-921: the candidate's pinned examples (attached with its items), for the carry-over check
    exemplar_texts = [(str(e["exemplar_id"]), e.get("masked_text") or "")
                      for e in (release or {}).get("exemplars") or []]
    packs = await store.tenant_packs(gs["set_id"])
    shared_inputs = gs.get("shared_inputs") or {}
    fp = await store.fingerprint(writer)
    month_start = await store.month_spend()
    started = datetime.now(timezone.utc)
    t0 = time.monotonic()
    writer_prompt = _release_prompt(release)
    details: dict[str, Any] = {
        "control_run_id": control_run, "calibration_run_id": baseline, "goal": goal, "control_mode": ctl_mode,
        "cap_usd": conf.cap_usd, "planned": {k: len(v) for k, v in plan.items()}, "calibration_items": len(calib),
        "limit": conf.limit, "seeded_fault": conf.seed_fault,
        "candidate": "release" if release else "current", "t9_first_draft_only": writer in ew.T9_WRITERS,
        "golden_set": f"{gs['name']}@{gs['version']}", "writer_prompt_sha256": writer_prompt,
        "log_stage": conf.log_stage,
        "views": "per item owner (active_parts(writer, item tenant))"}
    run_id = await store.create_run({
        "prompt_version": prompt_version(writer, writer_prompt),
        "triggered_by": conf.triggered_by[:128], "run_kind": conf.run_kind, "writer": writer,
        "release_id": conf.release_id, "golden_set_id": gs["set_id"], "tiers": sorted(tiers) if tiers else [],
        "cost_cap_usd": conf.cap_usd, "fingerprint": fp, "details": details})
    out = Outcome(run_id=run_id)
    counters = {"attempted": 0, "errors": 0, "no_control": 0, "skipped_post_edit": 0, "hard_stops": {},
                "controls_judged": 0, "control_errors": 0, "calibration_judged": 0, "calibration_errors": 0}
    compared: dict[str, list[dict]] = {"probe": [], "tune": [], "holdout": []}
    controls: dict[str, dict] = {}
    pairs: list[tuple[str, str]] = []
    cal_recs: list[dict] = []          # S257: the same pairs with the item's strata (channel, tenant, tier, split)
    view_cache: dict[Optional[str], tuple[Optional[dict], Optional[dict]]] = {owner_tenant: build_views(
        writer, active, release)}

    async def views_for(tenant_id: Optional[str]) -> tuple[Optional[dict], Optional[dict]]:
        """(control view, candidate view) of an item's OWNER: a platform run over tenant items writes each with its
        tenant's active tenant release, as production (and the shadow) do."""
        key = owner_tenant if owner_tenant else tenant_id
        if key not in view_cache:
            view_cache[key] = build_views(writer, await store.active_parts(writer, key), release)
        return view_cache[key]

    async def spent() -> float:
        await flush()
        return await store.run_spend(run_id, started)

    async def reserve(estimate: float, phase: str) -> None:
        s = await spent()
        if s + estimate > conf.cap_usd:
            raise _Stop("budget_cap", phase)
        if month_start + s + estimate > cfg.MONTH_CAP_USD:
            details["month_cap_hit"] = True
            raise _Stop("budget_cap", phase)

    async def after(phase: str) -> None:
        if await spent() > conf.cap_usd:
            raise _Stop("budget_cap", phase)

    async def judge(item: dict, output: dict, arm: str):
        # S256 source parity: the judge reads what the AA-729 panel read (eval_judge.judge_source_text), not the
        # isolation probe's shorter `ew.source_text`.
        return await judge_fn(pool, writer=writer, source_text=judge_source_text(writer, item["input"], shared_inputs),
                              output_text=ew.output_text(writer, output), run_id=run_id, item_id=item["item_id"],
                              arm=arm)

    def checks(item: dict, output: dict) -> dict:
        text = ew.output_text(writer, output)
        own = item.get("tenant_id")
        foreign = {tid: ec.tenant_fingerprint(p) for tid, p in packs.items() if tid != own}
        iso = ec.isolation_probe(text, foreign=foreign,
                                 allowed_text=ew.allowed_text(writer, item["input"], shared_inputs))
        return {"qa_artefacts": ec.qa_artefacts(text, rule_texts=rule_texts, exemplars=exemplar_texts),
                "isolation": iso,
                "injection": ec.injection_violations(text, item.get("injection_spec")) if item.get("trap") else []}

    def result_row(item: dict, *, arm: str, mode: str, verdict: str, output: Optional[dict] = None,
                   score=None, codes=(), hard_stop=False, judge=None, scored=None, cost=0.0, det=None) -> dict:
        return {"run_id": run_id, "item_id": item["item_id"], "writer": writer, "scope": item["scope"],
                "tenant_id": item.get("tenant_id"), "arm": arm, "mode": mode, "phase": ec.result_phase(item),
                "scored_result_id": scored, "output": output, "output_sha256": sha256_json(output) if output else None,
                "verdict": verdict, "score": score, "failure_codes": list(codes)[:50], "hard_stop": bool(hard_stop),
                "judge_model": getattr(judge, "model", None), "judge_version": getattr(judge, "judge_version", None),
                "cost_usd": round(max(cost, 0.0), 6), "details": _with_claims(det or {}, judge)}

    async def judged(it: dict, text_out: dict, err_key: str):
        await reserve(cfg.JUDGE_CALL_ESTIMATE_USD, "calibrate")
        try:
            return await judge(it, text_out, "control")
        except Exception as e:  # noqa: BLE001 — a judge failure is a counted error, never a verdict
            counters[err_key] += 1
            logger.warning("aa919_r2_control_judge_failed", run_id=run_id, item_id=it["item_id"], what=err_key,
                           error=f"{type(e).__name__}: {str(e)[:200]}")
            return None

    stop_reason, stopped_phase = None, None
    tune_result = None
    try:
        with bind_eval_stage(conf.log_stage, run_id):
            # 0. calibrate (every run) + re-judge the stored controls (rejudge mode; reverify regenerates them below)
            for it in [x for x in items if not x.get("trap") and (x["item_id"] in planned_ids
                                                                   or x["item_id"] in calib_ids)]:
                iid = it["item_id"]
                fsrc = frozen_src.get(iid) if iid in calib_ids else None
                csrc, ctl_text, is_frozen = None, None, False
                if ctl_mode == "rejudge" and iid in planned_ids:
                    csrc = sources.get(iid)
                    ctl_text, is_frozen = control_text(csrc, it)
                    if csrc is None:
                        counters["no_control"] += 1
                    elif ctl_text is None:
                        counters["skipped_post_edit"] += 1
                same = (fsrc is not None and csrc is not None and ctl_text is not None and is_frozen
                        and csrc["result_id"] == fsrc["result_id"])
                before = await spent()
                cal_v = ctl_v = None
                if fsrc is not None:
                    cal_v = await judged(it, it["control_output"], "calibration_errors")
                    if cal_v is not None:
                        counters["calibration_judged"] += 1
                        pairs.append((it["label_verdict"], cal_v.verdict))
                        cal_recs.append({"label": it["label_verdict"], "judge": cal_v.verdict,
                                         "strata": ec.item_strata(it)})
                        if same:
                            ctl_v = cal_v
                if ctl_text is not None and ctl_v is None and not (same and cal_v is None):
                    ctl_v = await judged(it, ctl_text, "control_errors")
                elif same and cal_v is None:
                    counters["control_errors"] += 1
                if ctl_v is None and cal_v is None:
                    continue
                cost = await spent() - before
                if ctl_v is not None:
                    cal = "same_row" if same else (None if cal_v is None else {
                        "scored_result_id": fsrc["result_id"], "verdict": cal_v.verdict, "score": cal_v.score,
                        "failure_codes": list(cal_v.failure_codes)[:50], "label": it.get("label_verdict"),
                        **_claims_of(cal_v)})
                    row = result_row(it, arm="control", mode="rejudge", verdict=ctl_v.verdict, score=ctl_v.score,
                                     codes=ctl_v.failure_codes, judge=ctl_v, scored=csrc["result_id"], cost=cost,
                                     det={"source_mode": csrc.get("mode"), "frozen": is_frozen, "calibration": cal})
                    counters["controls_judged"] += 1
                    controls[iid] = {"verdict": ctl_v.verdict, "failure_codes": ctl_v.failure_codes,
                                     "output": ctl_text}
                else:
                    row = result_row(it, arm="control", mode="rejudge", verdict=cal_v.verdict, score=cal_v.score,
                                     codes=cal_v.failure_codes, judge=cal_v, scored=fsrc["result_id"], cost=cost,
                                     det={"source_mode": "score_only", "frozen": True, "calibration": "only",
                                          "label": it.get("label_verdict")})
                await store.insert_result(row)
                await after("calibrate")
            out.judge_kappa = ec.cohen_kappa(pairs)
            details["kappa_labels"] = ec.label_counts(pairs)
            out.judge_counts = ec.judge_counts(out.judge_kappa, len(pairs), details["kappa_labels"])
            # S257: per-stratum agreement, reported with the overall row; the gate above is unchanged
            details["kappa_strata"] = ec.kappa_by_stratum(cal_recs)
            details["kappa_strata_below"] = ec.strata_below(details["kappa_strata"])
            out.kappa_strata_below = list(details["kappa_strata_below"])
            if progress:
                await progress(phase="calibrated", kappa=out.judge_kappa, pairs=len(pairs),
                               strata_below=",".join(out.kappa_strata_below) or None)

            for phase in ("probe", "tune", "holdout"):
                if phase == "holdout" and tune_result != "pass":
                    break
                for it in plan[phase]:
                    await reserve(cfg.R2_ITEM_ESTIMATE_USD[writer] + 2 * cfg.JUDGE_CALL_ESTIMATE_USD, phase)
                    counters["attempted"] += 1
                    before = await spent()
                    control_view, cand_view = await views_for(it.get("tenant_id"))
                    if ctl_mode == "regenerate" and not it.get("trap"):
                        ctl = await _control_regenerate(it, writer_fn=writer_fn, writer=writer, view=control_view,
                                                        shared_inputs=shared_inputs, judge=judge,
                                                        insert=store.insert_result, row=result_row,
                                                        family_models=family_models)
                        if ctl is not None:
                            controls[it["item_id"]] = ctl
                    row, cmp = await _candidate(it, conf=conf, writer=writer, view=cand_view, writer_fn=writer_fn,
                                                shared_inputs=shared_inputs, judge=judge, checks=checks,
                                                control=controls.get(it["item_id"]), counts=out.judge_counts,
                                                row=result_row, family_models=family_models)
                    row["cost_usd"] = round(max(await spent() - before, 0.0), 6)
                    await store.insert_result(row)
                    if row["verdict"] == "error":
                        counters["errors"] += 1
                    compared[phase if not (phase == "probe" and it.get("split") == "tune") else "tune"].append(cmp)
                    if progress:
                        await progress(phase=phase, done=counters["attempted"], total=len(planned))
                    await after(phase)
                    if cmp["hard_stop"] is not None:
                        key = str(cmp["hard_stop"])
                        counters["hard_stops"][key] = counters["hard_stops"].get(key, 0) + 1
                        eff = ec.result_phase(it)      # a tune item run in the probe is a tune item
                        raise _Stop("probe_fail" if eff == "probe" else "hard_stop", eff)
                if ec.too_many_errors(counters["errors"], counters["attempted"]):
                    raise _Stop("error", phase)
                if phase == "tune":
                    stats = ec.paired_stats(compared["tune"])
                    tune_result = ec.tune_decision(stats, goal=goal, counts=out.judge_counts)
                    details["tune"] = {**stats, "decision": tune_result}
                    if tune_result == "futility":
                        raise _Stop("futility", "tune")
                    if tune_result == "informative":
                        stop_reason, stopped_phase = "completed", "tune"
            if stop_reason is None:
                stop_reason, stopped_phase = "completed", "holdout" if plan["holdout"] else "tune"
    except _Stop as s:
        stop_reason, stopped_phase = s.reason, s.phase
    except Exception as e:  # noqa: BLE001 — the run row is always finished, then the error is re-raised
        stop_reason, stopped_phase = "error", "error"
        details["error"] = f"{type(e).__name__}: {str(e)[:300]}"
        await _finish(store, out, run_id, conf, stop_reason, stopped_phase, details, counters, compared, pairs,
                      tune_result, plan, spent_fn=spent, t0=t0)
        raise
    await _finish(store, out, run_id, conf, stop_reason, stopped_phase, details, counters, compared, pairs,
                  tune_result, plan, spent_fn=spent, t0=t0)
    return out


async def _judge_family(writer_labels: list[str], verdict, models: Optional[list]) -> None:
    """AA-1024 A4: SameFamilyJudge when the model that judged an output shares a family with a model that wrote it.
    The catalog lookup may read the database once (a thread, so the loop is never blocked)."""
    judge_label = getattr(verdict, "model", None)
    if not writer_labels or not judge_label:
        return
    await asyncio.to_thread(family_rule.check_pair, writer_labels, [judge_label], models=models,
                            what="the R2 judge")


async def _written(writer_fn, writer, inp, *, view, shared_inputs) -> tuple[dict, list[str]]:
    """One write, with the writer-role models that served it (family_rule.trace_models)."""
    with family_rule.trace_models() as seen:
        output = await writer_fn(writer, inp, view=view, shared_inputs=shared_inputs)
    return output, family_rule.labels(seen, "writer")


async def _control_regenerate(it, *, writer_fn, writer, view, shared_inputs, judge, insert, row, family_models=None):
    """reverify only: a fresh control output with the active view (ec.control_mode allows nothing else)."""
    try:
        output, wrote = await _written(writer_fn, writer, it["input"], view=view, shared_inputs=shared_inputs)
        v = await judge(it, output, "control")
        await _judge_family(wrote, v, family_models)
    except Exception as e:  # noqa: BLE001
        logger.warning("aa919_r2_control_regenerate_failed", item_id=it["item_id"], error=str(e)[:200])
        return None
    await insert(row(it, arm="control", mode="regenerate", verdict=v.verdict, output=output, score=v.score,
                     codes=v.failure_codes, judge=v))
    return {"verdict": v.verdict, "failure_codes": v.failure_codes, "output": output}


async def _candidate(it, *, conf, writer, view, writer_fn, shared_inputs, judge, checks, control, counts, row,
                     family_models=None):
    """One candidate item: (the result row, the comparison). Errors become an `error` row, never a verdict."""
    trap = bool(it.get("trap"))
    wrote: list[str] = []
    try:
        if conf.seed_fault:
            if control is None:
                raise RuntimeError("seeded fault needs the item's control output")
            output = seed(control["output"], conf.seed_fault)
        else:
            output, wrote = await _written(writer_fn, writer, it["input"], view=view, shared_inputs=shared_inputs)
    except Exception as e:  # noqa: BLE001
        logger.warning("aa919_r2_writer_failed", item_id=it["item_id"], error=f"{type(e).__name__}: {str(e)[:200]}")
        return (row(it, arm="candidate", mode="regenerate", verdict="error",
                    det={"error": type(e).__name__}),
                {"hard_stop": None, "paired": False, "new_fail": False, "fixed": False, "new_hard": []})
    det = checks(it, output)
    try:
        v = await judge(it, output, "candidate")
        await _judge_family(wrote, v, family_models)      # AA-1024 A4: never a same-family verdict
        cand = {"verdict": v.verdict, "failure_codes": v.failure_codes, **det}
    except Exception as e:  # noqa: BLE001 — judge down: deterministic stops still apply, no verdict
        logger.warning("aa919_r2_judge_failed", item_id=it["item_id"], error=f"{type(e).__name__}: {str(e)[:200]}")
        v = None
        cand = {"verdict": "error", "failure_codes": [], **det}
        if isinstance(e, family_rule.SameFamilyJudge):
            det = {**det, "same_family": str(e)[:300]}
    cmp = ec.compare_item(control=control, candidate=cand, counts=counts, trap=trap)
    if v is None and (det["qa_artefacts"] or det["isolation"]["leak"] or (trap and det["injection"])):
        cmp = ec.compare_item(control=None, candidate={**cand, "verdict": "fail"}, counts=counts, trap=trap)
    verdict = "fail" if cmp["hard_stop"] is not None else cand["verdict"]
    info = {"qa_artefacts": det["qa_artefacts"][:20], "isolation": det["isolation"],
            "injection": det["injection"], "new_hard": cmp["new_hard"], "hard_stop": cmp["hard_stop"],
            "paired": cmp["paired"], "new_fail": cmp["new_fail"], "fixed": cmp["fixed"]}
    if det.get("same_family"):
        info["same_family"] = det["same_family"]
    if conf.seed_fault:
        info["seeded_fault"] = conf.seed_fault
    return (row(it, arm="candidate", mode="regenerate", verdict=verdict, output=output,
                score=getattr(v, "score", None), codes=(v.failure_codes if v else []) + det["qa_artefacts"][:10],
                hard_stop=cmp["hard_stop"] is not None, judge=v, det=info), cmp)


async def _finish(store, out: Outcome, run_id, conf, stop_reason, stopped_phase, details, counters, compared, pairs,
                  tune_result, plan, *, spent_fn, t0) -> None:
    holdout_stats = ec.paired_stats(compared["holdout"])
    goal = details["goal"]
    truncated = conf.limit is not None
    if stop_reason in ("hard_stop", "probe_fail", "futility"):
        verdict = "fail"
    elif stop_reason in ("budget_cap", "error"):
        verdict = "invalid"
    elif not out.judge_counts or tune_result != "pass" or not plan["holdout"] or truncated or conf.seed_fault:
        verdict = "informative"          # a seeded-fault run is an operator check: never an attachable pass
    else:
        verdict = "pass" if ec.criterion_met(holdout_stats, goal) else "fail"
    try:
        cost = await spent_fn()
    except Exception:  # noqa: BLE001
        cost = 0.0
    details.update({"counts": counters, "probe": ec.paired_stats(compared["probe"]),
                    "holdout": holdout_stats, "kappa_pairs": len(pairs), "stopped_phase": stopped_phase,
                    "truncated": truncated, "seconds": round(time.monotonic() - t0, 1)})
    out.verdict, out.stop_reason, out.stopped_phase, out.cost_usd = verdict, stop_reason, stopped_phase, cost
    out.counts = counters
    await store.finish_run(run_id, {
        "tour_count": counters["attempted"], "cost_usd": round(cost, 6), "regression_detected": verdict == "fail",
        "details": details, "stop_reason": stop_reason, "verdict": verdict,
        "judge_kappa": out.judge_kappa, "judge_counts": bool(out.judge_counts)})
    logger.info("aa919_r2_done", run_id=run_id, writer=conf.writer, verdict=verdict, stop_reason=stop_reason,
                stopped_phase=stopped_phase, cost_usd=round(cost, 4), kappa=out.judge_kappa,
                strata_below=details.get("kappa_strata_below"), attempted=counters["attempted"])


# ── Postgres store ─────────────────────────────────────────────────────────────────────────────────────────────────

def _j(v):
    return json.loads(v) if isinstance(v, str) else v


class PgStore:
    """The production store: cis_app on the shared RDS (every tenant read filters tenant_id explicitly)."""

    def __init__(self, pool, *, stages: tuple[str, ...] = (cfg.EVAL_STAGE,)):
        self.pool = pool
        self.stages = stages

    async def get_release(self, release_id):
        from services.writing_memory import insertion, releases
        async with self.pool.acquire() as conn:
            rel = await releases.get_release(conn, release_id)
            # AA-920: the candidate's checked rule items (insertion.load_items), so its view renders its rules
            return await insertion.attach_items(conn, rel, rel["writer"]) if rel else rel

    async def active_parts(self, writer, tenant_id):
        from services.writing_memory import insertion
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT release_id::text AS release_id, scope, tenant_id::text AS tenant_id, release_no,
                          content_class, rule_ids::text[] AS rule_ids, items_sha256, eval_run_id::text AS eval_run_id
                     FROM shared.writing_memory_release
                    WHERE writer = $1 AND status = 'active'
                      AND (scope = 'platform' OR (scope = 'tenant' AND tenant_id = $2::uuid))""", writer, tenant_id)
            out = {"platform": None, "tenant": None}
            for r in rows:
                out[r["scope"]] = await insertion.attach_items(conn, dict(r), writer)
        return out

    async def golden_set(self):
        async with self.pool.acquire() as conn:
            r = await conn.fetchrow("SELECT set_id::text AS set_id, name, version, shared_inputs "
                                    "FROM shared.eval_golden_set WHERE name = $1 AND status = 'active'", GOLDEN_SET)
        return None if r is None else {**dict(r), "shared_inputs": _j(r["shared_inputs"]) or {}}

    async def items(self, set_id, writer, tenant_id):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT item_id::text AS item_id, sample_id, writer, channel, scope, tenant_id::text AS tenant_id,
                          split, trap, tiers, input, control_output, post_edit, label_verdict, injection_spec
                     FROM shared.eval_golden_item
                    WHERE set_id = $1::uuid AND writer = $2 AND ($3::uuid IS NULL OR tenant_id = $3::uuid)
                    ORDER BY sample_id""", set_id, writer, tenant_id)
        return [{**dict(r), "tiers": list(r["tiers"] or []), "input": _j(r["input"]),
                 "control_output": _j(r["control_output"]), "injection_spec": _j(r["injection_spec"])} for r in rows]

    async def tenant_packs(self, set_id):
        from services.writing_memory.eval_writers import own_pack
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT DISTINCT ON (tenant_id, writer) tenant_id::text AS tenant_id, writer, input
                     FROM shared.eval_golden_item WHERE set_id = $1::uuid AND scope = 'tenant'
                    ORDER BY tenant_id, writer, sample_id""", set_id)
        packs: dict[str, dict] = {}
        for r in rows:
            p = own_pack(r["writer"], _j(r["input"]) or {})
            merged = packs.setdefault(r["tenant_id"], {})
            for k, v in p.items():
                merged.setdefault(k, v)
        return packs

    async def baseline_run(self, set_id, writer):
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """SELECT id::text FROM shared.prompt_eval_runs
                    WHERE pipeline = 'writing_memory' AND run_kind = 'baseline_import' AND writer = $2
                      AND golden_set_id = $1::uuid ORDER BY created_at DESC LIMIT 1""", set_id, writer)

    async def control_sources(self, run_id, item_ids):
        """The stored output each item's control re-judges. An r2 run: its candidate output. The baseline_import run
        (release 0): the frozen fixture text (control / score_only, output NULL) before any AA-729 regenerate score
        row, so release 0's control is the frozen labelled output."""
        if not run_id or not item_ids:
            return {}
        async with self.pool.acquire() as conn:
            kind = await conn.fetchval("SELECT run_kind FROM shared.prompt_eval_runs WHERE id = $1::uuid", run_id)
            rows = await conn.fetch(
                """SELECT result_id::text AS result_id, item_id::text AS item_id, arm, mode, output
                     FROM shared.eval_item_result
                    WHERE run_id = $1::uuid AND item_id = ANY($2::uuid[]) AND verdict <> 'error'
                      AND mode IN ('score_only', 'regenerate')""", run_id, item_ids)
        if kind == "baseline_import":
            rank = {("control", "score_only"): 0, ("control", "regenerate"): 1}
        else:
            rank = {("candidate", "regenerate"): 0, ("control", "regenerate"): 1, ("control", "score_only"): 2}
        out: dict[str, dict] = {}
        for r in sorted(rows, key=lambda r: rank.get((r["arm"], r["mode"]), 9)):
            if (r["arm"], r["mode"]) in rank:
                out.setdefault(r["item_id"], {**dict(r), "output": _j(r["output"])})
        return out

    async def calibration_sources(self, run_id, item_ids):
        """The baseline import's control / score_only rows (the frozen labelled text): what calibration re-judges."""
        if not run_id or not item_ids:
            return {}
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT result_id::text AS result_id, item_id::text AS item_id, arm, mode
                     FROM shared.eval_item_result
                    WHERE run_id = $1::uuid AND item_id = ANY($2::uuid[]) AND arm = 'control'
                      AND mode = 'score_only' AND verdict <> 'error'""", run_id, item_ids)
        return {r["item_id"]: {**dict(r), "output": None} for r in rows}

    async def rule_texts(self, rule_ids):
        if not rule_ids:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT rule_id::text AS rule_id, rule_text, replacement FROM shared.writing_rule "
                                    "WHERE rule_id = ANY($1::uuid[])", rule_ids)
        return [(r["rule_id"], r["rule_text"]) for r in rows]

    async def fingerprint(self, writer):
        from services.writing_memory import releases
        async with self.pool.acquire() as conn:
            return await releases.current_fingerprint(conn, writer)

    async def create_run(self, row):
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """INSERT INTO shared.prompt_eval_runs
                       (pipeline, prompt_version, tour_count, cost_usd, regression_detected, details, triggered_by,
                        run_kind, writer, release_id, golden_set_id, tiers, cost_cap_usd, fingerprint)
                   VALUES ('writing_memory', $1, 0, 0, false, $2::jsonb, $3, $4, $5, $6::uuid, $7::uuid, $8::text[], $9,
                           $10::jsonb)
                   RETURNING id::text""",
                row["prompt_version"], json.dumps(row["details"], default=str), row["triggered_by"], row["run_kind"],
                row["writer"], row["release_id"], row["golden_set_id"], row["tiers"], row["cost_cap_usd"],
                json.dumps(row["fingerprint"], default=str))

    async def insert_result(self, row):
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """INSERT INTO shared.eval_item_result
                       (run_id, item_id, writer, scope, tenant_id, arm, mode, phase, scored_result_id, output,
                        output_sha256, verdict, score, failure_codes, hard_stop, judge_model, judge_version, cost_usd,
                        details)
                   VALUES ($1::uuid, $2::uuid, $3, $4, $5::uuid, $6, $7, $8, $9::uuid, $10::jsonb, $11, $12, $13,
                           $14::text[], $15, $16, $17, $18, $19::jsonb)
                   RETURNING result_id::text""",
                row["run_id"], row["item_id"], row["writer"], row["scope"], row["tenant_id"], row["arm"], row["mode"],
                row["phase"], row["scored_result_id"],
                None if row["output"] is None else json.dumps(row["output"], ensure_ascii=False, default=str),
                row["output_sha256"], row["verdict"], row["score"], row["failure_codes"], row["hard_stop"],
                row["judge_model"], row["judge_version"], row["cost_usd"], json.dumps(row["details"], default=str))

    async def finish_run(self, run_id, row):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """UPDATE shared.prompt_eval_runs
                      SET tour_count = $2, cost_usd = $3, regression_detected = $4, details = $5::jsonb,
                          stop_reason = $6, verdict = $7, judge_kappa = $8, judge_counts = $9, finished_at = now()
                    WHERE id = $1::uuid""",
                run_id, row["tour_count"], row["cost_usd"], row["regression_detected"],
                json.dumps(row["details"], default=str), row["stop_reason"], row["verdict"], row["judge_kappa"],
                row["judge_counts"])

    async def run_spend(self, run_id, since):
        # the run id filter is exact; `since` (minus a margin: a row's created_at is its transaction's start) only lets
        # the (stage, created_at) index narrow the scan
        since = since - timedelta(minutes=10)
        async with self.pool.acquire() as conn:
            return float(await conn.fetchval(
                """SELECT coalesce(sum(cost_usd), 0)::float FROM shared.llm_call_log
                    WHERE stage = ANY($1::text[]) AND created_at >= $2 AND quality_signal->>'eval_run' = $3""",
                list(self.stages), since, run_id) or 0.0)

    async def month_spend(self):
        from services.writing_memory.budget import month_spend
        async with self.pool.acquire() as conn:
            return await month_spend(conn)


async def flush_call_log() -> None:
    from shared.llm_client.call_log import wait_for_pending_writes
    await asyncio.to_thread(wait_for_pending_writes, 15.0)


async def check_judge_family(writer: str) -> list:
    """AA-1024 A4 (ADR 0009 decision 12), before any money is spent: R2Refused when a model of the judge route
    (`writing_memory_eval`, primary and fallbacks) is of the writer stage's primary family. Returns the catalog rows
    (the call-time check reuses them). An unreadable route or catalog refuses the run: it cannot be checked."""
    from services.writing_memory.releases import WRITER_STAGES
    from shared.llm_client.catalog import list_models
    try:
        models = await list_models()
        conflicts = await family_rule.stage_pair_conflicts(WRITER_STAGES.get(writer), [cfg.EVAL_STAGE],
                                                           models=models)
    except Exception as e:  # noqa: BLE001
        raise R2Refused(f"the judge / writer family rule could not be checked: {type(e).__name__}: {str(e)[:200]}")
    if conflicts:
        raise R2Refused("the judge route has a model of the writer's family (ADR 0009 decision 12): "
                        + "; ".join(conflicts))
    return models


async def run_with_pool(pool, conf: R2Config, *, progress=None) -> Outcome:
    from services.writing_memory.eval_judge import judge_output
    from services.writing_memory.eval_writers import regenerate
    conf.validate()
    models = await check_judge_family(conf.writer)
    # AA-900: run_spend reads the run's own label, so the per-run cap holds under either stage
    return await run_r2(PgStore(pool, stages=(conf.log_stage,)), conf, writer_fn=regenerate, judge_fn=judge_output,
                        flush=flush_call_log,
                        pool=pool, progress=progress, family_models=models)


async def estimate(pool, conf: R2Config) -> dict:
    """The DRY RUN: the plan and its estimated cost, $0 (reads only)."""
    conf.validate()
    try:                                        # AA-1024 A4: what the run would refuse, reported ($0, read only)
        await check_judge_family(conf.writer)
        family_check = "ok"
    except R2Refused as e:
        family_check = str(e)
    store = PgStore(pool)
    release = await store.get_release(conf.release_id) if conf.release_id else None
    owner = release["tenant_id"] if release and release["scope"] == "tenant" else None
    gs = await store.golden_set()
    if gs is None:
        raise R2Refused("no active golden set")
    items = await store.items(gs["set_id"], conf.writer, owner)
    plan = ec.plan_phases(items, tiers=ec.selected_tiers(conf.writer, conf.tiers), limit=conf.limit)
    if conf.seed_fault:
        plan["probe"] = [it for it in plan["probe"] if it.get("split") != "trap"]
    n = {k: len(v) for k, v in plan.items()}
    calib = calibration_items(items)
    calib_labels = {v: sum(1 for it in calib if it["label_verdict"] == v) for v in ("pass", "fail")}
    per = cfg.R2_ITEM_ESTIMATE_USD[conf.writer] + cfg.JUDGE_CALL_ESTIMATE_USD
    # calibration (every labelled item) + the planned controls (the same call for release 0's frozen controls)
    judge_only = (len(calib) + sum(n.values())) * cfg.JUDGE_CALL_ESTIMATE_USD
    gen = sum(n.values()) * (cfg.JUDGE_CALL_ESTIMATE_USD if conf.seed_fault else per)
    return {"writer": conf.writer, "release": conf.release_id, "items": len(items), "plan": n,
            "calibration_items": len(calib), "calibration_labels": calib_labels,
            "calibrated_possible": (len(calib) >= cfg.MIN_KAPPA_PAIRS
                                    and min(calib_labels.values()) >= cfg.MIN_KAPPA_PER_LABEL),
            "calibration_strata": ec.strata_label_counts(calib),
            "estimated_usd_max": round(judge_only + gen, 3), "cap_usd": conf.cap_usd, "log_stage": conf.log_stage,
            "month_spend_usd": round(await store.month_spend(), 4), "month_cap_usd": cfg.MONTH_CAP_USD,
            "judge_family_check": family_check}


def _parse(argv=None) -> tuple[R2Config, argparse.Namespace]:
    ap = argparse.ArgumentParser(description="AA-919 WM-19 R2 runner (DRY RUN by default: plan + estimate, $0)")
    ap.add_argument("--writer", required=True, choices=("a1", "t2", "t9_blog", "t9_social"))
    ap.add_argument("--release")
    ap.add_argument("--run-kind", default="r2", choices=RUN_KINDS)
    ap.add_argument("--tiers", help="comma-separated affected tiers (default: every tier)")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--cap-usd", type=float, default=cfg.R2_RUN_CAP_USD)
    ap.add_argument("--goal", choices=GOALS)
    ap.add_argument("--seed-fault", choices=sorted(SEED_FAULTS))
    ap.add_argument("--requested-by", default="cli")
    ap.add_argument("--stage", default=cfg.EVAL_STAGE, choices=cfg.EVAL_LOG_STAGES,
                    help="llm_call_log label of the run: adhoc_aa900 for a prompt / model / route change (AA-900)")
    ap.add_argument("--enqueue", action="store_true", help="queue the writing_memory_r2 job (the paid run)")
    a = ap.parse_args(argv)
    conf = R2Config(writer=a.writer, release_id=a.release, run_kind=a.run_kind,
                    tiers=[t for t in (a.tiers or "").split(",") if t] or None, limit=a.limit, cap_usd=a.cap_usd,
                    goal=a.goal, seed_fault=a.seed_fault, triggered_by=f"cli:{a.requested_by}", log_stage=a.stage)
    return conf, a


def payload_of(conf: R2Config) -> dict:
    p = asdict(conf)
    return {k: v for k, v in p.items() if v is not None}


def config_from_payload(payload: dict) -> R2Config:
    known = {f for f in R2Config.__dataclass_fields__}
    return R2Config(**{k: v for k, v in (payload or {}).items() if k in known})


async def _main(argv=None) -> int:
    import asyncpg

    from shared.secrets import get_database_url
    conf, a = _parse(argv)
    pool = await asyncpg.create_pool(get_database_url().strip(), ssl="require", min_size=1, max_size=2)
    try:
        print(json.dumps(await estimate(pool, conf), indent=1, default=str))
        if a.enqueue:
            from services.jobs.writing_memory_job import R2
            from shared.jobs.registry import enqueue
            import services.jobs.writing_memory_job  # noqa: F401 — registers the kinds
            job_id, created = await enqueue(pool, R2, payload_of(conf), created_by=conf.triggered_by)
            print(json.dumps({"enqueued": R2, "job_id": job_id, "created": created}))
    finally:
        await pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
