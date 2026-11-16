"""AA-1048 (S282) — the registered step sets of the two-step claim instrument (services/ground_truth/two_step.py) and
the claim-level hold-out of a pre-registration. Pure: no DB, no network, no model call.

A step set is one registered two-step instrument: its step prompt ids (services/ground_truth/prompts.py), its committed
pre-registration file and that file's sha256, pinned here so the registration cannot change after the code that runs it
ships (two_step refuses a live run of a file whose sha differs). A closed set refuses every live run. The stop rule
(one gated run per registration, read from the ledger rows two_step.prior_runs returns) lives here too.
"""
from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from services.ground_truth import instrument_eval as ev


class TwoStepRefused(ev.EvalRefused):
    """The two-step run cannot start (pre-registration, out of sample, inputs): no call is made."""


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
                  "7fc51de0ba7acd5d9d2b52e23420fa79d17a9b372f9746d2a2b2f7950ab348be",
                  closed="no_go in its one gated run c5983e03 (S282: false positives dropped 56 %, bar 60 %); the stop "
                         "rule allows one gated run per pre-registration, so the next run is the v7 step set"),
    "v7": StepSet("v7", {"classify": "v7c", "judge": "v7j"}, _PREREG_DIR / "aa1048_two_step_v7.json",
                  "9806120bf6f396df9d41c2d604866965fc3b9252080fa61deb44dfd2dc7da953"),
}
DEFAULT_STEP_SET = "v7"


def step_set(steps: str) -> StepSet:
    if steps not in STEP_SETS:
        raise TwoStepRefused(f"unknown step set {steps!r} (known: {', '.join(sorted(STEP_SETS))})")
    return STEP_SETS[steps]


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
