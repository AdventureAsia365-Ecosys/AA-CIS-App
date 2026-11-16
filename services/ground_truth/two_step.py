"""AA-1048 (S281) — the TWO-STEP claim instrument and its gated, pre-registered evaluation (root ADR 0020 W2;
docs/calibration/aa1048-two-step-preregistration.md).

Why. Three one-prompt rounds were no_go against the bars (false positives dropped >= 60 %, stored violations kept
>= 90 %): v2 48 / 90, v3 68 / 85, v4 48 / 85, and the last two traded one side for the other. One prompt both spots a
new specific and judges its support; v4's guard against new specifics made paraphrase fire again. Two steps:

  step 1  v5c ("classify"): is the sentence a restatement of the source (paraphrase, one-step reading, tone, text about
          the text, background about a place the source names: allowed by the writer source-fidelity policy) or a new
          claim (a new number, name, time, order, service, activity, view, pace ...)? It lists the new specifics.
  step 2  v5j ("judge"): only for a sentence step 1 sent on, is each listed specific supported by the whole source?

A sentence is cleared when the step-1 majority calls it a restatement; otherwise (new claim, or no step-1 majority:
recall first, an unsure step 1 never clears) its verdict is the step-2 majority (unsupported = flagged, no majority =
unverified, D7 "a miss is unverified"). Each step is a registered prompt version (services/ground_truth/prompts.py),
an instrument "<model>@<connection>:entailment#v5c|v5j" with its own tag in every ledger row.

Step sets (S282). v5's one gated run (9b0d46f8) was no_go (false positives dropped 32 %), so v5 is a CLOSED step set
(every live run refused) and v6 (v6c / v6j, docs/calibration/aa1048-two-step-v6-preregistration.md) is the default:
v6c sets aside background, the writer's reading, tone and words naming the reader before it looks the trip specifics
up; v6j first asks whether a listed specific is a trip specific at all. Each step set pins its pre-registration file
and that file's sha256 (STEP_SETS); a live run refuses a file whose sha differs. v6 was shaped on the 25 false
positives of 9b0d46f8 on the S281 fresh pool, so its registration holds them out claim by claim (`held_out`): on that
pool they are dropped before the subset is drawn, and the run counts as kept out; on another pool the manifest must
exclude the run as usual. `--steps v5|v6` picks the set (default v6).

  python -m services.ground_truth.two_step run --pool <pool.jsonl> (--fresh <manifest.json> | --exclude <report> ...)
      [--steps v6] [--candidate <model>@<connection>] [--trial N | --limit N] [--live --confirm-spend] [--cap USD]
      [--measured <two-step report>] [--with-reasons] [--out <json>]
      DRY RUN by default: the subset, the plan and its cost estimate (step 2 priced as if every claim reached it), the
      pre-registration check and the out-of-sample check; no client, no call. A paid run needs --live, --confirm-spend
      and exactly one of --trial / --limit within the settings below, the committed pre-registration matching the
      settings, prompts, candidate and votes in force, and a FRESH pool (--fresh; services/ground_truth/fresh_pool.py)
      that is out of sample for both steps: its manifest matches the pool file, names every run the steps were shaped
      on, shares no piece with them, and excludes every pool the steps were shaped on (the S279 pool; or it is a
      train-split pool: manifest split train and every claim train). --exclude only leaves claims out (DRY RUN reading
      of an older pool); it never makes a live run out of sample.
      Stop rule (in code): a gated run (--limit) refuses when the ledger shows a gated run of the same pre-registration,
      or of the same step-1 prompt on the same pool (every ledger row carries prereg, run_kind and the pool digest),
      or when the ledger cannot be read; a trial (--trial) is free to repeat.
      --measured prices the plan from a finished live two-step run (each step's measured cost per call, scaled by the
      prompt length); the live gate still charges every call's worst case.
  python -m services.ground_truth.two_step score --pool <pool.jsonl> --report <report.json>
      $0: score a finished run again from the step votes in its report (replay).

Settings (Platform > Settings; read after a refresh from the database before a paid run):
  cis.spend.instrument_eval_cap_usd / _max_claims / _trial_claims   the run's hard cap and claim limits (CIS 417)
  cis.quality.instrument_eval_fp_drop_min_pct / _tp_keep_min_pct    the overall go bars (CIS 417)
  cis.quality.claim_two_step_route_tp_min_pct       step 1 must send at least this share of stored violations on
  cis.quality.claim_two_step_judge_tp_keep_min_pct  step 2 must flag at least this share of the violations it judges
  cis.quality.claim_two_step_judge_fp_drop_min_pct  step 2 must clear at least this share of the false positives it
                                                    judges (no false positive reached it: the bar holds)
go needs the overall bars and the three step bars (CIS 423). The subset and the overall score are instrument_eval's.

Calls: every call goes through the Model Gateway (r1.gateway_call: stage r1.STAGE, role judge, the pinned connection
first) and writes one shared.llm_call_log row through r1._log (**billed_fields, the step's prompt version). The cap is
r1's hard cap: one budget for both steps, each call gated on the spend so far plus its worst case, the ledger probe
read before every call. Nothing here writes a label. The report carries counts, groups and hash prefixes (with
--with-reasons also the step-1 specifics and both steps' reasons: model text, keep it in .tmp-session/).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from services.ground_truth import instrument_eval as ev
from services.ground_truth import prompts as pr
from services.ground_truth import r1
from shared import settings

STEPS = ("classify", "judge")
MODE = "entailment"
_PREREG_DIR = Path(__file__).with_name("preregistration")


@dataclass(frozen=True)
class StepSet:
    """One registered two-step instrument: the step prompt ids, its committed pre-registration file and that file's
    sha256 (pinned here, so the registration cannot change after the code that runs it ships). `closed` (a reason)
    refuses every live run of the set (trial or gated); a DRY RUN and a $0 replay stay possible."""
    id: str
    prompts: dict
    preregistration: Path
    prereg_sha256: str
    closed: str = ""


STEP_SETS: dict[str, StepSet] = {
    "v5": StepSet("v5", {"classify": "v5c", "judge": "v5j"}, _PREREG_DIR / "aa1048_two_step_v5.json",
                  "b4c37d8a6cf93194c3da6ff162bfcd2fb8905dc32660e2adfe07c3ba63fe9fe9",
                  closed="no_go in its one gated run 9b0d46f8 (S282); the stop rule allows one gated run per "
                         "pre-registration, so the next run is the v6 step set"),
    "v6": StepSet("v6", {"classify": "v6c", "judge": "v6j"}, _PREREG_DIR / "aa1048_two_step_v6.json",
                  "7fc51de0ba7acd5d9d2b52e23420fa79d17a9b372f9746d2a2b2f7950ab348be"),
}
DEFAULT_STEP_SET = "v6"
# The default set's prompts and registration (the names v5's callers and tests used).
STEP_PROMPT = STEP_SETS[DEFAULT_STEP_SET].prompts
PREREGISTRATION = STEP_SETS[DEFAULT_STEP_SET].preregistration
# The gated runs and trials of the two-step instrument already in the ledger: one row per run and prompt version.
PRIOR_RUNS_SQL = (
    "SELECT quality_signal->>'run_id' AS run_id, quality_signal->>'prereg' AS prereg, "
    "quality_signal->>'pool' AS pool, quality_signal->>'run_kind' AS run_kind, "
    "quality_signal->>'prompt_version' AS prompt_version, MIN(created_at) AS first_at, COUNT(*)::int AS calls "
    "FROM shared.llm_call_log WHERE stage = $1 AND quality_signal->>'issue' = 'AA-1048' "
    "AND quality_signal->>'run_kind' IS NOT NULL GROUP BY 1, 2, 3, 4, 5 ORDER BY first_at")
CLASSIFY_SCHEMA = {"name": "claim_restatement", "schema": {
    "type": "object", "additionalProperties": False, "required": ["kind", "new_specifics", "why"],
    "properties": {"kind": {"type": "string", "enum": ["restatement", "new_claim"]},
                   "new_specifics": {"type": "array", "items": {"type": "string"}},
                   "why": {"type": "string"}}}}
SCHEMA = {"classify": CLASSIFY_SCHEMA, "judge": r1.ENTAILMENT_SCHEMA}
# The reply schema of each step prompt (v6c asks for what it set aside before its decision; pinned in its tag).
PROMPT_SCHEMA = {"v5c": CLASSIFY_SCHEMA, "v5j": r1.ENTAILMENT_SCHEMA, "v6c": pr.V6C_SCHEMA,
                 "v6j": r1.ENTAILMENT_SCHEMA}


def step_set(steps: str) -> StepSet:
    if steps not in STEP_SETS:
        raise TwoStepRefused(f"unknown step set {steps!r} (known: {', '.join(sorted(STEP_SETS))})")
    return STEP_SETS[steps]


def schema_of(inst: r1.Instrument) -> dict:
    return PROMPT_SCHEMA[inst.prompt]


class TwoStepRefused(ev.EvalRefused):
    """The two-step run cannot start (pre-registration, out of sample, inputs): no call is made."""


@dataclass(frozen=True)
class StepLimits:
    route_tp_min: float         # shares, 0..1
    judge_tp_keep_min: float
    judge_fp_drop_min: float


def step_limits() -> StepLimits:
    """The per-step bars from the settings registry (the snapshot; instrument_eval.refresh_settings re-reads it)."""
    return StepLimits(
        route_tp_min=float(settings.get("cis.quality.claim_two_step_route_tp_min_pct")) / 100.0,
        judge_tp_keep_min=float(settings.get("cis.quality.claim_two_step_judge_tp_keep_min_pct")) / 100.0,
        judge_fp_drop_min=float(settings.get("cis.quality.claim_two_step_judge_fp_drop_min_pct")) / 100.0)


def instruments(candidate: str, steps: str = DEFAULT_STEP_SET) -> dict[str, r1.Instrument]:
    """step -> the step's instrument of step set `steps` on `candidate` ("<model>@<connection>"). A model without a
    catalog price is refused (the cap cannot be kept)."""
    prompts = step_set(steps).prompts
    model, _, conn = candidate.partition("@")
    if not model or not conn:
        raise TwoStepRefused(f"a candidate is <model>@<connection>, got {candidate!r}")
    try:
        r1._price(model)
    except r1.R1Refused as e:
        raise TwoStepRefused(str(e)) from e
    return {s: r1.Instrument(model, conn, MODE, prompts[s]) for s in STEPS}


# ── replies ────────────────────────────────────────────────────────────────────────────────────────────────────────


def classify_vote(parsed: dict) -> Optional[bool]:
    """Step 1: True = new claim (sent on), False = restatement (cleared), None = no vote (any other kind)."""
    kind = parsed.get("kind")
    return True if kind == "new_claim" else False if kind == "restatement" else None


def judge_vote(parsed: dict) -> Optional[bool]:
    """Step 2: True = unsupported (flagged), False = supported (cleared), None = no vote (any other verdict)."""
    verdict = parsed.get("verdict")
    return True if verdict == "unsupported" else False if verdict == "supported" else None


def specifics_of(parsed: dict) -> list[str]:
    raw = parsed.get("new_specifics")
    return [str(s).strip()[:r1.REASON_CHARS] for s in raw if str(s).strip()] if isinstance(raw, list) else []


def merge_specifics(lists: Iterable[list[str]]) -> list[str]:
    """The specifics of the step-1 votes that called the sentence a new claim, in order, each once (case and spacing
    ignored)."""
    seen, out = set(), []
    for one in lists:
        for s in one:
            key = " ".join(s.lower().split())
            if key and key not in seen:
                seen.add(key)
                out.append(s)
    return out


def routed(step1_votes: list) -> bool:
    """Step 1 sends a sentence on unless its majority calls it a restatement (an unverified step 1 never clears)."""
    return r1.majority(step1_votes) is not False


def final_votes(items: Iterable[r1.Item], step1: dict[str, list], step2: dict[str, list]) -> dict[str, list]:
    """claim_hash -> [the two-step verdict] (one entry, so r1.majority reads it as is): False when step 1's majority is
    a restatement, else step 2's majority (None when it has none, e.g. the run stopped before step 2)."""
    out = {}
    for i in items:
        h = i.claim_hash
        out[h] = [False] if not routed(step1.get(h) or []) else [r1.majority(step2.get(h) or [])]
    return out


# ── plan ───────────────────────────────────────────────────────────────────────────────────────────────────────────

Expected = Callable[[str, r1.Instrument, str], float]


def step1_user(item: r1.Item, prompt: Optional[str] = None) -> str:
    return pr.get(prompt or STEP_PROMPT["classify"]).entailment_user(item)


def step2_user(item: r1.Item, specifics: Iterable[str] = ()) -> str:
    return pr.v5j_user(item, list(specifics))


def plan(items: list[r1.Item], insts: dict[str, r1.Instrument], *, votes: int = r1.VOTES, cap: float,
         attempts_of: Optional[Callable[[r1.Instrument], int]] = None, expected_of: Optional[Expected] = None) -> dict:
    """The DRY RUN plan of both steps. Step 2 is priced as if every claim reached it (an upper bound: step 1 clears
    the restatements) with no specifics listed. `expected_of(step, inst, user)` replaces the model estimate
    (--measured).
    The run fits when SAFETY x the expected total <= cap. The live gate never reads this: it charges worst cases."""
    if not items:
        raise TwoStepRefused("no claims in the subset")
    if not 0 < cap <= r1.HARD_CAP_USD:
        raise TwoStepRefused(f"cap must be in (0, {r1.HARD_CAP_USD}] USD, got {cap}")
    rows = []
    for step in STEPS:
        inst = insts[step]
        users = [step1_user(i, inst.prompt) if step == "classify" else step2_user(i) for i in items]
        attempts = attempts_of(inst) if attempts_of else 1
        one = (lambda u, s=step, i=inst: expected_of(s, i, u)) if expected_of else (
            lambda u, i=inst: r1.call_cost(i, u, r1.expected_out(i)))
        expected = sum(one(u) for u in users) * votes
        worst = [r1.worst_call_cost(inst, u, attempts) for u in users]
        rows.append({"step": step, "instrument": inst.id, "prompt_version": inst.prompt_version.tag,
                     "calls_max": len(users) * votes, "expected_usd_max": round(expected, 4),
                     "expected_from": "measured" if expected_of else "model", "reserved_attempts": attempts,
                     "worst_call_usd_max": round(max(worst), 4), "worst_case_usd": round(sum(worst) * votes, 4)})
    total = round(sum(r["expected_usd_max"] for r in rows), 4)
    return {"stage": r1.STAGE, "role": r1.ROLE, "votes": votes, "items": len(items), "steps": rows,
            "expected_total_usd_max": total, "cap_usd": cap, "fits_cap": total * r1.SAFETY <= cap,
            "worst_case_total_usd": round(sum(r["worst_case_usd"] for r in rows), 4),
            "note": "step 2 priced as if every claim reached it"}


def _chars(inst: r1.Instrument, user: str) -> int:
    return len(inst.prompt_version.system(MODE)) + len(user)


def measured_expectation(report: dict, insts: dict[str, r1.Instrument]) -> tuple[Expected, dict]:
    """One call's expected cost per step from a finished live two-step run on the same model, connection and prompt
    texts: that step's measured cost per call, scaled by this call's prompt length over that run's mean."""
    if report.get("mode") != "live" or report.get("stopped") or not report.get("calls"):
        raise TwoStepRefused("--measured needs a finished live two-step run report (mode live, not stopped)")
    by_step = report.get("by_step") or {}
    per_call, mean = {}, {}
    for step in STEPS:
        rec = by_step.get(step) or {}
        if rec.get("instrument") != insts[step].id or rec.get("prompt_version") != insts[step].prompt_version.tag:
            raise TwoStepRefused(f"--measured: the report's {step} step is another instrument or prompt text")
        calls, spent, chars = rec.get("calls"), rec.get("charged_usd"), rec.get("mean_prompt_chars")
        if not (isinstance(calls, int) and calls > 0 and isinstance(spent, (int, float)) and spent > 0
                and isinstance(chars, (int, float)) and chars > 0):
            raise TwoStepRefused(f"--measured: the report's {step} step has no positive calls, cost and prompt size")
        per_call[step], mean[step] = float(spent) / calls, float(chars)

    def expected(step: str, inst: r1.Instrument, user: str) -> float:
        return per_call[step] * _chars(inst, user) / mean[step]
    return expected, {"run": str(report.get("run_id", ""))[:8],
                      "usd_per_call": {s: round(v, 6) for s, v in per_call.items()}}


# ── pre-registration and out of sample ($0) ────────────────────────────────────────────────────────────────────────


def load_preregistration(path: Path = PREREGISTRATION) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _pct(share: float) -> float:
    return round(share * 100.0, 4)


def preregistration_check(prereg: dict, lim: ev.Limits, slim: StepLimits, insts: dict[str, r1.Instrument],
                          votes: int) -> dict:
    """Whether the run in force is the pre-registered one: the bars in the settings, the step prompt tags (texts), the
    candidate, the votes and the stage. A live run refuses on any difference (the bars were fixed before any data)."""
    bars = prereg.get("bars") or {}
    now = {"fp_dropped_min_pct": _pct(lim.fp_drop_min), "tp_kept_min_pct": _pct(lim.tp_keep_min),
           "route_tp_min_pct": _pct(slim.route_tp_min), "judge_tp_kept_min_pct": _pct(slim.judge_tp_keep_min),
           "judge_fp_dropped_min_pct": _pct(slim.judge_fp_drop_min)}
    diffs = [f"bar {k}: registered {bars.get(k)!r}, settings {v!r}" for k, v in now.items()
             if not isinstance(bars.get(k), (int, float)) or float(bars[k]) != v]
    for step in STEPS:
        tag = (prereg.get("prompt_versions") or {}).get(step)
        if tag != insts[step].prompt_version.tag:
            diffs.append(f"{step} prompt: registered {tag!r}, code {insts[step].prompt_version.tag!r}")
    for key, have in (("shaped_on", shaped_on(insts)), ("shaped_on_pools", shaped_on_pools(insts))):
        if sorted(str(x) for x in prereg.get(key) or []) != have:
            diffs.append(f"{key}: registered {prereg.get(key)!r}, code {have!r}")
    cand = f"{insts['classify'].model_key}@{insts['classify'].connection}"
    for key, have in (("candidate", cand), ("votes", votes), ("stage", r1.STAGE), ("role", r1.ROLE)):
        if prereg.get(key) != have:
            diffs.append(f"{key}: registered {prereg.get(key)!r}, run {have!r}")
    return {"id": prereg.get("id"), "registered_on": prereg.get("registered_on"), "bars": bars, "in_force": now,
            "differences": diffs, "matches": not diffs}


def shaped_on(insts: dict[str, r1.Instrument]) -> list[str]:
    return sorted({r for i in insts.values() for r in i.prompt_version.shaped_on})


def shaped_on_pools(insts: dict[str, r1.Instrument]) -> list[str]:
    return sorted({p for i in insts.values() for p in i.prompt_version.shaped_on_pools})


def pool_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def held_out(prereg: dict, pool: list[ev.PoolItem], pool_digest: str) -> tuple[list[ev.PoolItem], Optional[dict]]:
    """The claims a step set was shaped on inside the pool it is then evaluated on, held out claim by claim: the
    pre-registration's `held_out` names the shaping run, that run's pool digest (16 hex) and the hash prefixes of the
    claims whose votes, specifics and reasons were read. On that pool they are dropped before the subset is drawn and
    the run counts as kept out (fresh_check `covered`); on another pool nothing is dropped and the run is not covered.
    None when the registration holds no claim-level hold-out (v5)."""
    ho = prereg.get("held_out")
    if not ho:
        return pool, None
    claims = {str(c).lower() for c in ho.get("claims") or []}
    bad = sorted(c for c in claims if len(c) < 16 or any(ch not in "0123456789abcdef" for ch in c))
    run, digest = str(ho.get("run") or "")[:8].lower(), str(ho.get("pool") or "").lower()
    if not claims or bad or len(run) != 8 or len(digest) != 16:
        raise TwoStepRefused("the pre-registration's held_out needs a run id, a 16-hex pool digest and claim prefixes "
                             f"of 16+ hex characters (bad: {bad[:3]})")
    rec = {"run": run, "pool": digest, "claims": len(claims), "applies": digest == pool_digest.lower(),
           "dropped": 0, "not_in_pool": None, "by_group": {}, "tp_side": ho.get("tp_side")}
    if not rec["applies"]:
        return pool, rec
    kept, dropped, found = [], Counter(), set()
    for p in pool:
        hit = next((c for c in claims if p.item.claim_hash.lower().startswith(c)), None)
        if hit:
            dropped[p.group] += 1
            found.add(hit)
        else:
            kept.append(p)
    rec.update(dropped=sum(dropped.values()), not_in_pool=len(claims - found), by_group=dict(sorted(dropped.items())))
    return kept, rec


def covered_runs(hold: Optional[dict]) -> list[str]:
    """The shaping runs a claim-level hold-out keeps out of this pool (its run, when it applies and every listed
    claim was found)."""
    return [hold["run"]] if hold and hold["applies"] and hold["not_in_pool"] == 0 else []


def fresh_check(manifest: dict, pool_text: str, pool: list[ev.PoolItem], shaped: list[str],
                shaped_pools: Iterable[str] = (), covered: Iterable[str] = ()) -> dict:
    """A fresh pool is out of sample by construction (fresh_pool.py): its manifest is the one of this pool file, it
    names every run the steps were shaped on as excluded, no claim of the pool sits on an excluded piece, and the
    pools the steps were shaped on are kept out: each is in the manifest's excluded_pools, or the pool is a train-split
    pool (manifest split train and every claim train: the split is frozen per tour, the shaping pools are held-out).
    `covered`: shaping runs that a claim-level hold-out of the pre-registration keeps out of this pool (held_out)."""
    excluded_runs = sorted({str(r)[:8].lower() for r in manifest.get("excluded_runs") or []})
    held = set(covered)
    pieces = {str(p)[:16].lower() for p in manifest.get("excluded_pieces") or []}
    overlap = sorted({p.item.content_sha256[:16] for p in pool if p.item.content_sha256[:16].lower() in pieces})
    excluded_pools = {str(p)[:16].lower() for p in manifest.get("excluded_pools") or []}
    pools_missing = [p for p in sorted(shaped_pools) if p.lower() not in excluded_pools]
    splits = Counter(p.item.split for p in pool)
    train_only = manifest.get("split") == "train" and bool(pool) and set(splits) == {"train"}
    return {"kind": "fresh", "pool_sha_matches": manifest.get("pool_sha256") == pool_sha256(pool_text),
            "shaped_on": shaped, "excluded_runs": excluded_runs,
            "missing": [r for r in shaped if r not in excluded_runs and r not in held],
            "covered_by_held_out": sorted(held), "excluded_pieces": len(pieces),
            "pieces_on_excluded": len(overlap), "shaped_on_pools": sorted(shaped_pools),
            "excluded_pools": sorted(excluded_pools), "pools_missing": pools_missing,
            "split": manifest.get("split"), "pool_splits": dict(sorted(splits.items())),
            "pool_side": "excluded" if not pools_missing else "train_split" if train_only else None}


def oos_refusal(oos: dict) -> Optional[str]:
    if oos.get("kind") != "fresh":
        return ("a live two-step run needs --fresh <manifest> (fresh_pool.py): the steps were shaped on runs and on "
                "pools that --exclude cannot leave out (it drops claims of run reports, not the pool the panel read)")
    hold = oos.get("held_out")
    if hold and hold.get("applies") and hold.get("not_in_pool"):
        return (f"{hold['not_in_pool']} held-out claim(s) of the pre-registration are not in this pool: it is not the "
                "pool the hold-out was registered on")
    if oos["missing"]:
        return f"the steps were shaped on run(s) {', '.join(oos['missing'])}: the pool is in-sample for them"
    if oos.get("kind") == "fresh" and not oos["pool_sha_matches"]:
        return "the fresh manifest is not the manifest of this pool file (sha256 differs)"
    if oos.get("pieces_on_excluded"):
        return f"{oos['pieces_on_excluded']} piece(s) of the pool were excluded as in-sample"
    if oos.get("pool_side") is None:
        return (f"the steps were shaped on pool(s) {', '.join(oos['pools_missing'])}: the fresh manifest must exclude "
                "each (fresh_pool eligible --exclude-pool) or be a train-split pool (split train, every claim train)")
    return None


def prior_runs(stage: str = r1.STAGE) -> Optional[list[dict]]:
    """The two-step runs already in the ledger (PRIOR_RUNS_SQL): run id, pre-registration id, pool digest, run kind,
    prompt version, first call and calls. None when the ledger cannot be read (the stop rule then refuses a gated
    run)."""
    import asyncio

    async def _q() -> list[dict]:
        import asyncpg

        from shared.secrets import get_database_url

        conn = await asyncpg.connect(get_database_url().strip(), ssl="require")
        try:
            return [dict(r) for r in await conn.fetch(PRIOR_RUNS_SQL, stage)]
        finally:
            await conn.close()
    try:
        rows = asyncio.run(_q())
    except Exception:                                        # noqa: BLE001 — unreadable: the caller refuses
        return None
    return [{**r, "first_at": r["first_at"].isoformat() if hasattr(r.get("first_at"), "isoformat")
             else r.get("first_at")} for r in rows]


def stop_rule(prior: Optional[list[dict]], *, kind: str, prereg_id: str, pool: str, classify_tag: str) -> dict:
    """The pre-registered stop rule, in code: one gated run. A gated ("full") run is refused when the ledger already
    holds a gated run of this pre-registration (any pool), or of the same step-1 prompt on the same pool (a re-roll
    under a new id), or when the ledger cannot be read (a gated run that cannot prove it is the first). A trial is not
    gated (its verdict is always "trial") and may repeat; a stopped gated run counts (its votes were seen)."""
    read = prior is not None if kind == "full" else None    # a trial does not read the ledger
    rec: dict[str, Any] = {"kind": kind, "prereg": prereg_id, "pool": pool, "ledger_read": read,
                           "prior_gated_runs": [], "prior_trials": 0, "refused": None}
    if prior is None:
        if kind == "full":
            rec["refused"] = ("the ledger cannot be read, so this gated run cannot prove it is the first of its "
                              "pre-registration (stop rule)")
        return rec
    gated = {}
    for r in prior:
        rid = str(r.get("run_id") or "")[:8]
        if r.get("run_kind") == "trial":
            continue
        same_id = r.get("prereg") == prereg_id
        reroll = r.get("pool") == pool and r.get("prompt_version") == classify_tag
        if same_id or reroll:
            gated.setdefault(rid, {"run": rid, "prereg": r.get("prereg"), "pool": r.get("pool"),
                                   "first_at": r.get("first_at")})
    rec["prior_trials"] = len({str(r.get("run_id") or "")[:8] for r in prior
                               if r.get("run_kind") == "trial" and r.get("prereg") == prereg_id})
    rec["prior_gated_runs"] = sorted(gated.values(), key=lambda g: str(g["first_at"]))
    if kind == "full" and gated:
        rec["refused"] = (f"the stop rule allows one gated run: the ledger already holds gated run(s) "
                          f"{', '.join(sorted(gated))} of pre-registration {prereg_id} or of the same step-1 prompt "
                          "on this pool; a no_go is read at $0, a new run needs a new pre-registration")
    return rec


# ── live ───────────────────────────────────────────────────────────────────────────────────────────────────────────


class _Run:
    """The state of one live two-step run: one hard budget for both steps, the per-step bookkeeping, the stop
    reason and the counters `run` returns."""

    def __init__(self, insts: dict[str, r1.Instrument], *, cap: float, run_id: str, caller: r1.Caller,
                 log: Callable[[Any, dict, Optional[dict], Optional[Exception]], None],
                 spend_probe: Optional[r1.SpendProbe], reserved: dict[str, int],
                 signal_extra: Optional[dict]) -> None:
        self.insts, self.cap, self.run_id = insts, cap, run_id
        self.caller, self.log, self.spend_probe = caller, log, spend_probe
        self.reserved, self.signal_extra = reserved, signal_extra
        self.budget = r1.Budget(cap)
        self.by_step = {s: {"instrument": insts[s].id, "prompt_version": insts[s].prompt_version.tag, "calls": 0,
                            "charged_usd": 0.0, "prompt_chars": 0} for s in STEPS}
        self.stopped: Optional[str] = None
        self.mismatches: Counter = Counter()
        self.errors: list[dict] = []
        self.worst_notes: Counter = Counter()

    def _blocked(self, worst: float) -> bool:
        """The budget/stop gate: True (and the stop reason set) when the next call may not start."""
        budget = self.budget
        if not self.stopped and self.spend_probe is not None:
            try:
                budget.observe_ledger(self.spend_probe())
            except Exception:                               # noqa: BLE001 — the probe only ever raises the spend
                pass
        if not self.stopped and budget.ledger_unreadable:
            self.stopped = f"ledger spend reading is not a finite number, after {budget.calls} calls"
        if self.stopped or not budget.allows(worst):
            self.stopped = self.stopped or (
                f"cap {self.cap} USD: spent {budget.spent:.4f} + the next call's worst case {worst:.4f} would pass "
                f"it, after {budget.calls} calls")
            return True
        return False

    def _charge(self, step: str, inst: r1.Instrument, user: str, amount: float, worst: float) -> None:
        self.budget.charge(amount, worst)
        rec = self.by_step[step]
        rec["calls"] += 1
        rec["charged_usd"] += amount
        rec["prompt_chars"] += _chars(inst, user)

    def _caller_error(self, step: str, vote: int, inst: r1.Instrument, user: str, worst: float,
                      e: Exception) -> None:
        """Paid attempts may have run: charge the worst case and stop clean (the votes so far stay)."""
        self._charge(step, inst, user, worst, worst)
        self.worst_notes["caller_error_worst_case"] += 1
        self.errors.append({"step": step, "vote": vote, "error": type(e).__name__, "detail": str(e)[:200]})
        self.stopped = (f"caller error after {self.budget.calls} calls ({type(e).__name__}): stopped, report "
                        "partial")

    def _check_attempt(self, inst: r1.Instrument, resp: Any) -> None:
        attempt = getattr(resp, "attempt_no", None)
        if (not self.stopped and isinstance(attempt, int) and not isinstance(attempt, bool)
                and attempt > self.reserved[inst.id]):
            self.stopped = (f"reply attempt_no {attempt} passed the {self.reserved[inst.id]} reserved attempt(s) "
                            f"of {inst.id}, after {self.budget.calls} calls: stopped, report partial")

    def _parse(self, inst: r1.Instrument, resp: Any, signal: dict) -> Optional[dict]:
        """The parsed reply when it is a JSON object of the pinned model, else None (no vote); always logged."""
        try:
            parsed = json.loads(getattr(resp, "content", "") or "")
            if not isinstance(parsed, dict):
                raise ValueError("reply is not a JSON object")
        except Exception as e:                               # noqa: BLE001 — logged as billed_unusable, no vote
            self.log(resp, signal, None, e)
            return None
        self.log(resp, signal, parsed, None)
        if not r1._identity_ok(inst, resp):
            self.mismatches[inst.id] += 1
            return None
        return parsed

    def one(self, step: str, vote: int, item: r1.Item, user: str) -> Optional[dict]:
        """One call; the parsed reply when it is a vote of the pinned model, else None (no vote)."""
        inst = self.insts[step]
        worst = r1.worst_call_cost(inst, user, self.reserved[inst.id])
        if self._blocked(worst):
            return None
        try:
            resp = self.caller(inst, inst.prompt_version.system(MODE), user, schema_of(inst))
        except Exception as e:                               # noqa: BLE001 — paid attempts may have run: stop clean
            self._caller_error(step, vote, inst, user, worst, e)
            return None
        amount, notes = r1.billed_cost(inst, user, resp)
        self._charge(step, inst, user, amount, worst)
        self.worst_notes.update(n.split(":", 1)[0] for n in notes)
        self._check_attempt(inst, resp)
        signal = {"app": "cis", "issue": "AA-1048", "run_id": self.run_id, "instrument": inst.id, "step": step,
                  "vote": vote, "prompt_version": inst.prompt_version.tag, "claims": [item.claim_hash[:16]],
                  **(self.signal_extra or {})}
        return self._parse(inst, resp, signal)

    def finish_by_step(self) -> dict:
        for rec in self.by_step.values():
            rec["mean_prompt_chars"] = round(rec["prompt_chars"] / rec["calls"], 1) if rec["calls"] else None
            rec["charged_usd"] = round(rec["charged_usd"], 6)
        return self.by_step


def _step1_votes(state: _Run, items: list[r1.Item], votes: int, step_votes: dict, reasons: dict,
                 spec_votes: dict) -> None:
    for vote in range(votes):
        for item in items:
            parsed = state.one("classify", vote, item, step1_user(item, state.insts["classify"].prompt))
            v = classify_vote(parsed) if parsed is not None else None
            step_votes["classify"][item.claim_hash].append(v)
            reasons["classify"][item.claim_hash].append(str((parsed or {}).get("why") or "")[:r1.REASON_CHARS])
            spec_votes[item.claim_hash].append((v, specifics_of(parsed) if parsed is not None else []))


def _step2_votes(state: _Run, sent: list[r1.Item], votes: int, specifics: dict, step_votes: dict,
                 reasons: dict) -> None:
    for vote in range(votes):
        for item in sent:
            parsed = state.one("judge", vote, item, step2_user(item, specifics[item.claim_hash]))
            step_votes["judge"][item.claim_hash].append(judge_vote(parsed) if parsed is not None else None)
            reasons["judge"][item.claim_hash].append(str((parsed or {}).get("why") or "")[:r1.REASON_CHARS])


def run(items: list[r1.Item], insts: dict[str, r1.Instrument], *, cap: float, votes: int = r1.VOTES, run_id: str,
        caller: Optional[r1.Caller] = None,
        log: Callable[[Any, dict, Optional[dict], Optional[Exception]], None] = r1._log,
        spend_probe: Optional[r1.SpendProbe] = None,
        attempts_of: Optional[Callable[[r1.Instrument], int]] = None,
        signal_extra: Optional[dict] = None) -> dict:
    """Both steps, live: every step-1 vote of every claim, then every step-2 vote of the claims step 1 sent on. One
    hard budget (r1.Budget) for both: a call starts only when the spend so far plus its worst case fits the cap; a
    caller error is charged the worst case and stops the run (the votes so far stay). Never raises on a caller
    error. `signal_extra` (prereg, run_kind, pool) goes into every ledger row's signal: the stop rule reads it back.
    Returns the step votes (None = no vote), the merged specifics, spend per step and the stop reason."""
    if attempts_of is None:
        attempts_of = r1.route_attempts if caller is None else (lambda _inst: 1)
    caller = caller or r1.gateway_call
    reserved = {inst.id: max(1, int(attempts_of(inst))) for inst in insts.values()}
    state = _Run(insts, cap=cap, run_id=run_id, caller=caller, log=log, spend_probe=spend_probe, reserved=reserved,
                 signal_extra=signal_extra)
    step_votes: dict[str, dict[str, list]] = {s: defaultdict(list) for s in STEPS}
    reasons: dict[str, dict[str, list]] = {s: defaultdict(list) for s in STEPS}
    spec_votes: dict[str, list[tuple[Optional[bool], list[str]]]] = defaultdict(list)
    _step1_votes(state, items, votes, step_votes, reasons, spec_votes)
    sent = [i for i in items if routed(step_votes["classify"][i.claim_hash])]
    specifics = {i.claim_hash: merge_specifics(s for v, s in spec_votes[i.claim_hash] if v is True) for i in sent}
    _step2_votes(state, sent, votes, specifics, step_votes, reasons)
    by_step = state.finish_by_step()
    budget = state.budget
    return {"votes": {s: dict(v) for s, v in step_votes.items()}, "specifics": specifics,
            "reasons": {s: dict(v) for s, v in reasons.items()}, "by_step": by_step,
            "spent_usd": round(budget.spent, 6), "charged_usd": round(budget.charged, 6),
            "ledger_usd": round(budget.ledger, 6) if budget.ledger is not None else None, "calls": budget.calls,
            "stopped": state.stopped, "identity_mismatches": dict(state.mismatches), "caller_errors": state.errors,
            "worst_case_charges": dict(state.worst_notes), "reserved_attempts": reserved}


# ── score ──────────────────────────────────────────────────────────────────────────────────────────────────────────


def _share(n: int, d: int) -> Optional[float]:
    return round(n / d, 4) if d else None


def step_record(subset: list[ev.PoolItem], step1: dict[str, list], step2: dict[str, list], slim: StepLimits,
                gold: Optional[dict[str, str]] = None) -> dict:
    """Per group: step 1 restated / sent on (and how many of those were unverified), step 2 flagged / cleared /
    unverified among the claims it judged; the three step bars on the stored violations (tp) and the false positives.
    `gold` (hash prefix -> the panel's step-1 answer, fresh_pool import) adds step 1 against the panel (read only)."""
    s1: dict[str, Counter] = defaultdict(Counter)
    s2: dict[str, Counter] = defaultdict(Counter)
    vs_panel: Counter = Counter()
    for p in subset:
        h, g = p.item.claim_hash, p.group
        m1 = r1.majority(step1.get(h) or [])
        if gold and h[:16] in gold:
            vs_panel[f"{gold[h[:16]]}->{'restatement' if m1 is False else 'new_claim' if m1 else 'unverified'}"] += 1
        if m1 is False:
            s1[g]["restated"] += 1
            continue
        s1[g]["sent_on"] += 1
        s1[g]["sent_on_unverified"] += m1 is None
        m2 = r1.majority(step2.get(h) or [])
        s2[g]["flagged" if m2 is True else "cleared" if m2 is False else "unverified"] += 1
    fp_groups = [g for g in set(s1) if g not in ev.TP_GROUPS]
    tp1, tp2 = s1.get("tp", Counter()), s2.get("tp", Counter())
    fp2 = sum((s2[g] for g in fp_groups), Counter())
    route_tp = _share(tp1["sent_on"], tp1["sent_on"] + tp1["restated"])
    judge_tp = _share(tp2["flagged"], sum(tp2.values()))
    judge_fp = _share(fp2["cleared"], sum(fp2.values()))
    passes = {"route_tp": route_tp is not None and route_tp >= slim.route_tp_min,
              "judge_tp_kept": judge_tp is not None and judge_tp >= slim.judge_tp_keep_min,
              # no false positive reached step 2: step 1 cleared them all, the bar holds
              "judge_fp_dropped": judge_fp is None or judge_fp >= slim.judge_fp_drop_min}
    return {"step1": {g: dict(c) for g, c in sorted(s1.items())}, "step2": {g: dict(c) for g, c in sorted(s2.items())},
            "route_tp_share": route_tp, "judge_tp_kept_share": judge_tp, "judge_fp_dropped_share": judge_fp,
            "bars": {"route_tp_min": slim.route_tp_min, "judge_tp_kept_min": slim.judge_tp_keep_min,
                     "judge_fp_dropped_min": slim.judge_fp_drop_min, "on": ["tp"]},
            "passes": passes, "step1_vs_panel": dict(sorted(vs_panel.items()))}


def score(subset: list[ev.PoolItem], step1: dict[str, list], step2: dict[str, list], lim: ev.Limits,
          slim: StepLimits, *, trial: bool = False, stopped: Optional[str] = None,
          gold: Optional[dict[str, str]] = None) -> dict:
    """instrument_eval's score of the two-step verdicts (overall bars) plus the step record; go only when the overall
    verdict is go AND every step bar holds (pre-registration)."""
    overall = ev.score(subset, final_votes([p.item for p in subset], step1, step2), lim, trial=trial,
                       stopped=stopped)
    steps = step_record(subset, step1, step2, slim, gold)
    verdict = overall["verdict"]
    if verdict == "go" and not all(steps["passes"].values()):
        verdict = "no_go"
    return {**overall, "overall_verdict": overall["verdict"], "steps": steps, "verdict": verdict}


def replay(pool: list[ev.PoolItem], report: dict, lim: ev.Limits, slim: StepLimits) -> dict:
    votes = report.get("votes") or {}
    if not isinstance(votes.get("classify"), dict) or not votes["classify"]:
        raise TwoStepRefused("the report has no step votes (only a live two-step report carries them)")
    hashes = [p.item.claim_hash for p in pool]
    step1 = ev.by_prefix(hashes, votes["classify"])
    step2 = ev.by_prefix(hashes, votes.get("judge") or {}) if votes.get("judge") else {}
    subset = [p for p in pool if p.item.claim_hash in step1]
    if len(subset) != len(votes["classify"]):
        raise TwoStepRefused(f"{len(votes['classify']) - len(subset)} voted claims of the report are not in the pool")
    stored = (report.get("score") or {}).get("verdict")
    sc = score(subset, step1, step2, lim, slim, trial=stored == "trial", stopped=report.get("stopped"))
    return {"replayed_run": str(report.get("run_id", ""))[:8], "subset": ev.composition(subset), "score": sc,
            "stored_verdict": stored, "matches_stored": stored == sc["verdict"]}


# ── CLI ────────────────────────────────────────────────────────────────────────────────────────────────────────────

_caller: Optional[r1.Caller] = None                     # tests put a fake here; None = the Model Gateway
_probe: Callable[[Any], r1.SpendProbe] = r1.ledger_probe
_prior_runs: Callable[[], Optional[list[dict]]] = prior_runs   # tests put a fake here


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _subset(pool: list[ev.PoolItem], a: argparse.Namespace, n: int, lim: ev.Limits) -> list[ev.PoolItem]:
    return ev.select(pool, lim.max_claims)[:n] if a.trial is not None else ev.select(pool, n)


def _h16(votes: dict[str, list]) -> dict[str, list]:
    return {h[:16]: v for h, v in sorted(votes.items())}


def cmd_run(a: argparse.Namespace) -> tuple[int, dict]:
    if a.live and not a.confirm_spend:
        raise TwoStepRefused("--live is paid: add --confirm-spend")
    sset = step_set(a.steps)
    if a.live and sset.closed:
        raise TwoStepRefused(f"step set {sset.id} is closed: {sset.closed}")
    fresh = ev.refresh_settings() if a.live else None
    lim, slim = ev.limits(), step_limits()
    n = ev.gate(a, lim)
    insts = instruments(a.candidate, sset.id)
    pool_text = _read(a.pool)
    pool_digest = pool_sha256(pool_text)[:16]
    prereg_path = Path(a.preregistration) if a.preregistration else sset.preregistration
    prereg_file = load_preregistration(prereg_path)
    full, hold = held_out(prereg_file, ev.read_pool(pool_text), pool_digest)
    texts = [_read(f) for f in a.exclude]
    pool, excluded = (ev.exclude(full, set().union(*(ev.prefixes_from(t) for t in texts))) if texts else (full, None))
    shaped = shaped_on(insts)
    runs = sorted({r for r in map(ev.run_of, texts) if r})
    manifest = json.loads(_read(a.fresh)) if a.fresh else {}
    if a.fresh:
        # the manifest alone proves the pool is out of sample (piece level); --exclude only drops claims
        oos = {**fresh_check(manifest, pool_text, pool, shaped, shaped_on_pools(insts), covered_runs(hold)),
               "cli_excluded_runs": runs, "held_out": hold}
    else:
        oos = {"kind": "exclude", "shaped_on": shaped, "excluded_runs": runs,
               "missing": [r for r in shaped if r not in runs], "shaped_on_pools": shaped_on_pools(insts),
               "held_out": hold}
    sha = file_sha256(prereg_path)
    prereg = {**preregistration_check(prereg_file, lim, slim, insts, a.votes), "file_sha256": sha,
              "pinned_sha256": sset.prereg_sha256, "sha_matches": sha == sset.prereg_sha256}
    kind = "trial" if a.trial is not None else "full"
    stop = None
    if a.live:
        why = oos_refusal(oos)
        if why:
            raise TwoStepRefused(why)
        if prereg_path.resolve() != sset.preregistration.resolve():
            raise TwoStepRefused("a live run reads the committed pre-registration only (drop --preregistration)")
        if not prereg["sha_matches"]:
            raise TwoStepRefused(f"the pre-registration file of step set {sset.id} changed after it was pinned "
                                 f"(sha256 {sha[:12]}, pinned {sset.prereg_sha256[:12]})")
        if not prereg["matches"]:
            raise TwoStepRefused("the run is not the pre-registered one: " + "; ".join(prereg["differences"]))
        stop = stop_rule(_prior_runs() if kind == "full" else None, kind=kind, prereg_id=str(prereg_file.get("id")),
                         pool=pool_digest, classify_tag=insts["classify"].prompt_version.tag)
        if stop["refused"]:
            raise TwoStepRefused(stop["refused"])
    expected_of, measured = (measured_expectation(json.loads(_read(a.measured)), insts) if a.measured
                             else (None, None))
    subset = _subset(pool, a, n, lim)
    cap = a.cap if a.cap is not None else lim.cap_usd
    the_plan = plan([p.item for p in subset], insts, votes=a.votes, cap=cap, expected_of=expected_of)
    rep = {"mode": "live" if a.live else "dry_run", "steps": sset.id,
           "instruments": {s: i.id for s, i in insts.items()},
           "prompt_versions": {s: i.prompt_version.tag for s, i in insts.items()}, "settings_refreshed": fresh,
           "limits": asdict(lim), "step_limits": asdict(slim), "preregistration": prereg, "excluded": excluded,
           "out_of_sample": oos, "run_kind": kind, "pool_sha256": pool_digest, "stop_rule": stop,
           "measured": measured, "subset": ev.composition(subset), "plan": the_plan}
    if not the_plan["fits_cap"]:
        return 3, {"ok": False, "refused": "the plan does not fit the cap", **rep}
    if not a.live:
        from services.golden_set import llm_guard

        llm_guard.install("aa1048_two_step_dry_run")
        return 0, {"ok": True, **rep}
    run_id = str(uuid.uuid4())
    res = run([p.item for p in subset], insts, cap=cap, votes=a.votes, run_id=run_id, caller=_caller,
              spend_probe=_probe(datetime.now(timezone.utc)),
              signal_extra={"prereg": str(prereg_file.get("id")), "run_kind": kind, "pool": pool_digest})
    live = {"run_id": run_id, "spent_usd": res["spent_usd"], "ledger_usd": res["ledger_usd"], "calls": res["calls"],
            "stopped": res["stopped"], "caller_errors": res["caller_errors"],
            "identity_mismatches": res["identity_mismatches"], "reserved_attempts": res["reserved_attempts"],
            "worst_case_charges": res["worst_case_charges"], "by_step": res["by_step"],
            "score": score(subset, res["votes"]["classify"], res["votes"]["judge"], lim, slim,
                           trial=a.trial is not None, stopped=res["stopped"], gold=manifest.get("step1_gold")),
            "votes": {s: _h16(v) for s, v in res["votes"].items()},
            **({"specifics": _h16(res["specifics"]), "reasons": {s: _h16(v) for s, v in res["reasons"].items()}}
               if a.with_reasons else {})}
    return (0 if not res["stopped"] else 3), {"ok": not res["stopped"], **rep, **live}


def cmd_score(a: argparse.Namespace) -> dict:
    return {"ok": True, **replay(ev.read_pool(_read(a.pool)), json.loads(_read(a.report)), ev.limits(),
                                 step_limits())}


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m services.ground_truth.two_step", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="DRY RUN by default; --live --confirm-spend with --trial N or --limit N is paid")
    r.add_argument("--pool", required=True)
    r.add_argument("--fresh", help="the fresh pool's manifest (fresh_pool.py import): out of sample by construction")
    r.add_argument("--exclude", action="append", default=[],
                   help="leave out the claims of this run report (an older pool): out of sample for those runs")
    r.add_argument("--candidate", default=ev.DEFAULT_CANDIDATE)
    r.add_argument("--steps", default=DEFAULT_STEP_SET, choices=sorted(STEP_SETS),
                   help=f"the step set (prompts + pre-registration); default {DEFAULT_STEP_SET}")
    r.add_argument("--votes", type=int, default=r1.VOTES)
    r.add_argument("--trial", type=int)
    r.add_argument("--limit", type=int)
    r.add_argument("--cap", type=float)
    r.add_argument("--preregistration", help="DRY RUN only: check against this file instead of the step set's")
    r.add_argument("--measured", help="expected cost from this finished live two-step run report")
    r.add_argument("--dry-run", action="store_true", help="the default; never paid")
    r.add_argument("--live", action="store_true", help="PAID: make the calls")
    r.add_argument("--confirm-spend", action="store_true", help="required with --live")
    r.add_argument("--with-reasons", action="store_true",
                   help="add step-1 specifics and both steps' reasons (model text: keep the report in .tmp-session/)")
    r.add_argument("--out", help="write the report here too")
    s = sub.add_parser("score", help="$0: score a finished run again from the step votes in its report")
    s.add_argument("--pool", required=True)
    s.add_argument("--report", required=True)
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    a = parser().parse_args(argv)
    if a.cmd == "run" and a.dry_run and a.live:
        a.live = False                                      # --dry-run wins: never pay by accident
    try:
        rc, rep = (0, cmd_score(a)) if a.cmd == "score" else cmd_run(a)
    except (ev.EvalRefused, r1.R1Refused) as e:
        print(json.dumps({"ok": False, "refused": str(e)}))
        return 2
    if getattr(a, "out", None) and a.cmd == "run":
        try:
            out = ev._check_out(a.out)                      # never inside a git work tree unless ignored there
        except ev.EvalRefused as e:
            print(json.dumps({"ok": False, "refused": str(e)}))
            return 2
        with open(out, "w", encoding="utf-8") as f:
            f.write(json.dumps(rep, indent=1, default=str))
    print(json.dumps(rep, indent=1, default=str))
    return rc


if __name__ == "__main__":
    sys.exit(main())
