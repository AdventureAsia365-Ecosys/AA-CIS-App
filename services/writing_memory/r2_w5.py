"""AA-1051 — the W5 (writer proof) helpers of the R2 runner (services/writing_memory/r2.py), kept out of r2.py so it
stays within its code-health size: the null-run check, kappa per eval side, and (S283) the claim instrument of a
pre-registered run and its tune step. r2.py re-exports every public name here, so callers keep `r2.<name>`."""
from __future__ import annotations

from typing import Any, Optional

from services.writing_memory import eval_checks as ec

PREREG_FULL_SAMPLE = "prereg_full_sample"     # AA-1051 S283: tune_step of a pre-registered run


def _instrument_check(conf: Any) -> None:
    """AA-1051 S283: a claim instrument (services/writer_proof/instrument.py) judges a pre-registered run only, pinned
    to the instrument's own model and connection; an unknown or closed instrument is refused at $0."""
    if conf.judge_instrument is None:
        return
    from services.writer_proof import instrument as wi
    from services.writing_memory.r2 import R2Refused
    if not conf.prereg_id:
        raise R2Refused("a claim instrument judges pre-registered W5 runs only (services/writer_proof)")
    try:
        p = wi.pinned(str(conf.judge_instrument))
    except wi.InstrumentRefused as e:
        raise R2Refused(str(e)) from e
    if (conf.judge_model, conf.judge_connection) != (p.model_key, p.connection):
        raise R2Refused(f"the instrument {p.id} runs on {p.candidate}: pin judge_model / judge_connection to it")


def _instrument_version(instrument: Optional[str]) -> Optional[str]:
    from services.writer_proof import instrument as wi
    return wi.pinned(instrument).judge_version if instrument else None


def _max_claims(v) -> int:
    """8 quoted claims (eval_judge's MAX_CLAIMS), or every flagged claim of a claim instrument (its max_claims)."""
    n = getattr(v, "max_claims", None)
    return max(8, n) if isinstance(n, int) and not isinstance(n, bool) else 8


def full_sample(conf: Any) -> bool:
    """AA-1051: the run must reach every planned item. A pre-registered run (S283: its decision reads the whole fixed
    sample, writer_proof.decision paired_all), and (S284) a reverify of a fixed sample: it writes the controls a later
    null run pairs on, so a sequential stop after tune would leave the holdout items without a control."""
    return bool(conf.prereg_id) or (conf.run_kind == "reverify" and bool(conf.sample_ids))


def tune_step(conf: Any, stats: dict, goal: str, counts: bool) -> str:
    """The decision after tune. AA-1051 S283: a full-sample run (`full_sample`) always goes on (PREREG_FULL_SAMPLE):
    a sequential stop would pay for tune and leave its purpose unmet. R2's own verdict of such a run stays informative
    (_finish). Hard stops, the cap and the error share still stop it."""
    return PREREG_FULL_SAMPLE if full_sample(conf) else ec.tune_decision(stats, goal=goal, counts=counts)


def writes_per_item(run_kind: str) -> int:
    """AA-1051 S284: writer calls one planned item costs. A reverify regenerates the control (_control_regenerate) and
    then the candidate, so its estimate and its per-item reservation count two writes (they counted one, so a
    reverify's estimate was about half its spend and the cap stopped it part way)."""
    return 2 if run_kind == "reverify" else 1


# AA-1051 S284: the newest finished reverify run of a writer on one golden set version: the control of a null run.
# Only a reverify of no release or of one of the owner's active releases ($3): a reverify of another release wrote
# its candidates with that release's view (S284 review).
REVERIFY_CONTROL_SQL = """
    SELECT id::text AS run_id FROM shared.prompt_eval_runs
     WHERE pipeline = 'writing_memory' AND run_kind = 'reverify' AND writer = $1 AND golden_set_id = $2::uuid
       AND finished_at IS NOT NULL AND (release_id IS NULL OR release_id = ANY($3::uuid[]))
     ORDER BY finished_at DESC, id DESC
     LIMIT 1
"""
# AA-1051 S284 review: the only stored row of a reverify run a null run pairs on, its REGENERATED CONTROL (written
# with the active view of its day, the configuration its stored fingerprint names). Its candidate row is never a
# fallback: a candidate may be written with a release's view, so B == A would no longer hold by configuration. An
# item whose control regenerate failed or was judged `error` has no row, the control plan reports it missing
# (controls found < planned) and the run is refused at $0.
NULL_CONTROL_RANKS: dict[tuple[str, str], int] = {("control", "regenerate"): 0}


def active_release_ids(active: dict) -> list[str]:
    """The release ids of the owner's active parts (platform and tenant), for REVERIFY_CONTROL_SQL."""
    return sorted({str(p["release_id"]) for p in (active.get("platform"), active.get("tenant"))
                   if p and p.get("release_id")})


async def null_control(store: Any, set_id: str, writer: str, active: dict) -> Optional[str]:
    """AA-1051 S284: the control run of a null run, the newest finished reverify run of the writer on this golden set
    version, of no release or an active one (it regenerated the controls under the configuration of its day;
    `null_config_problems` then requires that configuration to be today's). None when there is none: the release /
    baseline control applies, and the null check refuses a baseline import at $0 as before. A reverify that does not
    hold a regenerated control for every planned item is caught by the control plan (`controls found < planned`, only
    NULL_CONTROL_RANKS rows pair), never paid for."""
    return await store.reverify_control_run(set_id, writer, active_release_ids(active))


def null_control_sources(sources: dict[str, dict]) -> dict[str, dict]:
    """The sources of a null run's reverify control that may pair: regenerated controls only (NULL_CONTROL_RANKS),
    whatever a store returned."""
    return {iid: src for iid, src in sources.items() if (src.get("arm"), src.get("mode")) in NULL_CONTROL_RANKS}


# AA-1051 S284 review: the logged cost of the writer's last candidate items, ONE write each. A reverify candidate row
# carries the item's whole spend (the control regenerate, then the candidate: r2._control_regenerate writes no cost),
# so where the same run holds the item's regenerated control the cost is halved; a trap item (no control) is one write.
ITEM_COST_SAMPLES_SQL = """
    SELECT (r.cost_usd / CASE WHEN p.run_kind = 'reverify' AND EXISTS (
                SELECT 1 FROM shared.eval_item_result c
                 WHERE c.run_id = r.run_id AND c.item_id = r.item_id AND c.arm = 'control' AND c.mode = 'regenerate')
            THEN 2 ELSE 1 END)::float AS cost_usd
      FROM shared.eval_item_result r
      JOIN shared.prompt_eval_runs p ON p.id = r.run_id
     WHERE p.pipeline = 'writing_memory' AND p.run_kind IN ('r2', 'reverify') AND p.writer = $1
       AND p.finished_at IS NOT NULL AND (p.details->>'seeded_fault') IS NULL
       AND r.arm = 'candidate' AND r.mode = 'regenerate' AND r.verdict <> 'error' AND r.cost_usd > 0
     ORDER BY r.created_at DESC LIMIT $2
"""


def null_config_problems(control_fp: Optional[dict], now_fp: Optional[dict]) -> list[str]:
    """AA-1051: why a W5 null run (prereg workflow NULL_WORKFLOW) would not be null by construction, [] when it is.
    The null run regenerates with today's writers (B) against the control's stored outputs (A): B == A only when the
    control was written under the same configuration (the audit: "A = stored finals at matching config_fp"). Stricter
    than `releases.reverify_reasons`: a control without a config_fp (a baseline import of frozen text, a run before
    W0), or a component missing / unavailable on either side, is a problem, never "no change". mode_flags is compared
    the reverify way (only by the worker against a worker record: an exec session does not know the worker env).
    Pure."""
    from services.writing_memory import config_fp as cf
    if not isinstance(control_fp, dict) or not isinstance(control_fp.get("config_fp"), dict):
        return ["the control run records no writer configuration (config_fp): a baseline import of frozen text or a "
                "run from before W0. A null run needs controls written under the current configuration: run a "
                "reverify of the writer's active release on this golden set version first"]
    now_fp = now_fp or {}
    out = [f"{k} differs from the control's" for k in ("golden_set", "writer_route", "gate_taxonomy")
           if control_fp.get(k) != now_fp.get(k)]
    old = control_fp["config_fp"].get("parts") or {}
    new = (now_fp.get("config_fp") or {}).get("parts") or {}
    for c in cf.COMPONENTS:
        if c == "mode_flags":
            continue
        if c not in old or c not in new or cf.UNAVAILABLE in (old[c], new[c]):
            out.append(f"config_fp {c} cannot be compared (missing or unavailable)")
        elif old[c] != new[c]:
            out.append(f"config_fp {c} differs from the control's")
    if "mode_flags" in cf.reverify_components(control_fp, now_fp):
        out.append("config_fp mode_flags differs from the control's")
    return out


def kappa_by_side(records: list[dict], side: Optional[str]) -> Optional[dict]:
    """AA-1051: kappa of the CAL pairs and of the planned side's pairs (records carry `eval_split`), and their gap
    (audit target: GATE within 0.1 of CAL). None without a side."""
    if not side:
        return None
    out: dict[str, Any] = {}
    for name in sorted({"cal", side}):
        pairs = [(r["label"], r["judge"]) for r in records if r.get("eval_split") == name]
        out[name] = {"pairs": len(pairs), "kappa": ec.cohen_kappa(pairs)}
    k_cal, k_side = out["cal"]["kappa"], out[side]["kappa"]
    out["gap"] = round(abs(k_cal - k_side), 3) if (k_cal is not None and k_side is not None) else None
    return out
