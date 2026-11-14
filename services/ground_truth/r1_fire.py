"""AA-1048 (S281 lane l13) — the R1 FIRING run on UNLABELLED eligible claims, for the fresh pool of the two-step claim
instrument (services/ground_truth/fresh_pool.py, two_step.py).

Why. The fresh pool labels the claims R1 fired on. scripts/aa1048/r1_calibrate.py fires only on labelled claims
(r1.items_from), and the train split holds 126 of them with 8 violations: too few for the 20 violations and 25 false
positives a 50-claim subset needs. This run fires the same instrument (gpt-6-luna-openai@openai-direct, extraction,
prompt v1: fresh_pool.FIRE_CANDIDATE) on a capped sample of the eligible claims whatever their label, and writes a fire
report that `fresh_pool sample --votes` reads, so the agent panel labels only what R1 fired.

  python -m services.ground_truth.r1_fire --eligible <eligible.jsonl> --manifest <eligible-manifest.json>
      [--size N] [--trial N] [--expect-sample <digest>] [--measured <fire report>] [--cap USD]
      [--live --confirm-spend] [--out <report.json>]

DRY RUN by default: the out-of-sample check, the sample and the plan (calls, the model estimate, or a measured estimate
from --measured: a finished live fire report of the same instrument and prompt, its cost per call scaled by the prompt
length); no client, no call. A live run is PAID and needs --live and --confirm-spend; it re-reads the settings from the
database first.

Out of sample (refused in every mode). The eligible manifest (fresh_pool eligible) must be the manifest of this file
(sha256), name every run the two-step prompts were shaped on as excluded, keep every pool they were shaped on out
(listed in excluded_pools, or a train-split manifest whose every claim is train), and no claim may sit on an excluded
piece or off the manifest's split.

Sample. At most N claims (default and ceiling: the setting cis.quality.claim_fire_sample_max, CIS 426), spread over the
writers in proportion and ordered by sha256(salt + claim hash) (fresh_pool.pick), with the salt "aa1048-fire:" + the
eligible file's sha256 prefix: deterministic, unrelated to the text, and not the salt fresh_pool sample orders the fired
claims by. The digest of the drawn hashes is printed; --expect-sample refuses a run whose sample differs (the settings
of a live run are re-read, so a changed size shows here). --trial N fires only the first N claims of the sample (at
most cis.spend.instrument_eval_trial_claims): a trial report prices the full run (--measured); fresh_pool sample
refuses it.

Calls. Extraction makes one call per object (piece + field) per vote, r1.VOTES votes, through the Model Gateway
(r1.gateway_call: stage r1.STAGE, role judge, the pinned connection first); every reply writes one shared.llm_call_log
row through r1._log (**billed_fields, the prompt version). The cap is r1's hard cap (r1.Budget): --cap, default and
ceiling cis.spend.instrument_eval_cap_usd (CIS 417, never above r1.HARD_CAP_USD); each call is gated on the spend so far
plus its worst case, the ledger probe is read before every call, and a caller error stops the run cleanly (the votes so
far are kept and the report is written). Each ledger row's quality_signal carries the run id and a call id
("<run_id>:<n>", purpose "fresh_pool_fire"); the report lists, per call, its call id, the provider request id, the
object key, the charge and the outcome, and after the run it counts the run's ledger rows (read only).

Report (no text: claim hashes, booleans, ids, counts): kind "r1_fire", the sample record, the plan, and for a live run
"votes" ({instrument id: {claim hash: [true|false|null, ...]}}, the shape fresh_pool.read_fire_votes reads), "fired"
(the majority per claim), "ledger" (the calls) and "ledger_rows".
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
from typing import Any, Callable, Optional

from services.ground_truth import fresh_pool as fp
from services.ground_truth import instrument_eval as ev
from services.ground_truth import panel as pn
from services.ground_truth import r1
from services.ground_truth import two_step as ts
from shared import settings

KIND = fp.FIRE_REPORT_KIND
SALT = "aa1048-fire:"
PURPOSE = "fresh_pool_fire"
FIRE_KINDS = ("trial", "full")
LEDGER_ROWS_SQL = ("SELECT count(*)::int AS n FROM shared.llm_call_log "
                   "WHERE stage = $1 AND quality_signal->>'run_id' = $2")


class FireRefused(ev.EvalRefused):
    """The firing run cannot start (inputs, out of sample, settings, cap): no call is made."""


@dataclass(frozen=True)
class FireLimits:
    cap_usd: float          # the run's hard cap (cis.spend.instrument_eval_cap_usd, <= r1.HARD_CAP_USD)
    sample_max: int         # the most claims one sample may draw (cis.quality.claim_fire_sample_max)
    trial_max: int          # the most claims a trial may fire (cis.spend.instrument_eval_trial_claims)


def limits() -> FireLimits:
    """The run's limits from the settings registry (the snapshot; instrument_eval.refresh_settings re-reads it)."""
    lim = ev.limits()
    return FireLimits(cap_usd=lim.cap_usd, sample_max=int(settings.get("cis.quality.claim_fire_sample_max")),
                      trial_max=lim.trial_claims)


def instrument() -> r1.Instrument:
    """The fire instrument: the R1 extraction instrument whose false positives the fresh pool measures."""
    return r1.instruments([fp.FIRE_CANDIDATE], ["extraction"])[0]


def item_of(line: dict) -> r1.Item:
    """An r1.Item of an eligible line whatever its label (the run never reads the label; "unlabelled" says so)."""
    return r1.Item(line["claim_hash"], line["writer"], line.get("claim_class") or "unlabelled", "unlabelled",
                   line.get("stratum") or "representative", line["content_sha256"], line["field"], line["claim_text"],
                   line.get("field_text") or line["claim_text"], line.get("source_text") or "",
                   tuple(line.get("evidence") or ()), r1.split_of_line(line))


# ── inputs and out of sample ($0) ──────────────────────────────────────────────────────────────────────────────────


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def load(eligible_path: str, manifest_path: str) -> tuple[list[dict], dict]:
    """The eligible lines and their manifest; refused unless the manifest is fresh_pool's for this very file."""
    text = _read(eligible_path)
    manifest = json.loads(_read(manifest_path))
    if not isinstance(manifest, dict) or manifest.get("kind") != "fresh_eligible":
        raise FireRefused("--manifest must be the eligible manifest of fresh_pool eligible (kind fresh_eligible)")
    if manifest.get("eligible_sha256") != fp._sha(text):
        raise FireRefused("the eligible manifest is not the manifest of this eligible file (sha256 differs)")
    lines = pn.read_jsonl(text)
    if not lines:
        raise FireRefused("the eligible file holds no claim")
    return lines, manifest


def _short(values: Any, n: int) -> set[str]:
    return {str(v)[:n].lower() for v in values or []}


def out_of_sample(manifest: dict, lines: list[dict]) -> dict:
    """What the manifest keeps out against what the two-step prompts were shaped on, and where the lines sit."""
    steps = ts.instruments(ev.DEFAULT_CANDIDATE)
    shaped, shaped_pools = ts.shaped_on(steps), ts.shaped_on_pools(steps)
    runs, pieces, pools = (_short(manifest.get("excluded_runs"), 8), _short(manifest.get("excluded_pieces"), 16),
                           _short(manifest.get("excluded_pools"), 16))
    split = manifest.get("split")
    splits = Counter(r1.split_of_line(e) for e in lines)
    pools_missing = [p for p in shaped_pools if p.lower() not in pools]
    train_only = split == "train" and set(splits) == {"train"}
    return {"shaped_on": shaped, "excluded_runs": sorted(runs), "missing": [r for r in shaped if r not in runs],
            "excluded_pieces": len(pieces),
            "pieces_on_excluded": len({fp.piece(e)[:16] for e in lines if fp.piece(e)[:16] in pieces}),
            "shaped_on_pools": shaped_pools, "excluded_pools": sorted(pools), "pools_missing": pools_missing,
            "split": split, "splits": dict(sorted(splits.items())),
            "off_split": sum(n for s, n in splits.items() if split not in ("all", s)),
            "pool_side": "excluded" if not pools_missing else "train_split" if train_only else None}


def oos_refusal(oos: dict) -> Optional[str]:
    if oos["missing"]:
        return (f"the two-step prompts were shaped on run(s) {', '.join(oos['missing'])}: the eligible manifest must "
                "exclude them (fresh_pool eligible --exclude-run)")
    if oos["pieces_on_excluded"]:
        return f"{oos['pieces_on_excluded']} piece(s) of the eligible file were excluded as in-sample"
    if oos["off_split"]:
        return f"{oos['off_split']} claim(s) are not on the manifest's split {oos['split']!r}"
    if oos["pool_side"] is None:
        return (f"the two-step prompts were shaped on pool(s) {', '.join(oos['pools_missing'])}: the manifest must "
                "exclude each (--exclude-pool) or be a train-split manifest whose every claim is train")
    return None


# ── sample ($0) ────────────────────────────────────────────────────────────────────────────────────────────────────


def salt_of(manifest: dict) -> str:
    return SALT + str(manifest.get("eligible_sha256", ""))[:16]


def digest(chosen: list[dict]) -> str:
    """The sample's identity: sha256 of its sorted claim hashes (16 hex)."""
    return hashlib.sha256("\n".join(sorted(e["claim_hash"] for e in chosen)).encode("utf-8")).hexdigest()[:16]


def objects(lines: list[dict]) -> int:
    return len({(fp.piece(e), e["field"]) for e in lines})


def draw(lines: list[dict], size: int, salt: str) -> list[dict]:
    """At most `size` claims, writer-proportional, in salted-hash order (fresh_pool.pick)."""
    return fp.pick(lines, size, salt)


def sample_record(lines: list[dict], chosen: list[dict], size: int, salt: str) -> dict:
    return {"salt": salt, "size": size, "eligible_claims": len(lines), "claims": len(chosen),
            "objects": objects(chosen), "pieces": len({fp.piece(e) for e in chosen}),
            "by_writer": dict(sorted(Counter(e["writer"] for e in chosen).items())), "digest": digest(chosen)}


def _size(a: argparse.Namespace, lim: FireLimits) -> int:
    size = a.size if a.size is not None else lim.sample_max
    if not 0 < size <= lim.sample_max:
        raise FireRefused(f"--size must be 1..{lim.sample_max} (cis.quality.claim_fire_sample_max)")
    if a.trial is not None and not 0 < a.trial <= min(lim.trial_max, size):
        raise FireRefused(f"--trial must be 1..{min(lim.trial_max, size)} "
                          "(cis.spend.instrument_eval_trial_claims, and at most the sample size)")
    return size


def _cap(a: argparse.Namespace, lim: FireLimits) -> float:
    cap = a.cap if a.cap is not None else lim.cap_usd
    if not 0 < cap <= lim.cap_usd:
        raise FireRefused(f"--cap must be in (0, {lim.cap_usd}] USD (cis.spend.instrument_eval_cap_usd)")
    return cap


# ── plan ($0) ──────────────────────────────────────────────────────────────────────────────────────────────────────


def _chars(inst: r1.Instrument, user: str) -> int:
    return len(inst.prompt_version.system(inst.mode)) + len(user)


def measured_expectation(report: dict, inst: r1.Instrument) -> tuple[Callable[[r1.Instrument, str], float], dict]:
    """One call's expected cost from a finished live fire report of the same instrument and prompt texts: its charged
    cost per call, scaled by this call's prompt length over that run's mean. The live gate still charges worst cases."""
    if report.get("kind") != KIND or report.get("mode") != "live" or report.get("stopped") or not report.get("calls"):
        raise FireRefused("--measured needs a finished live fire report (kind r1_fire, mode live, not stopped)")
    if report.get("instrument") != inst.id or report.get("prompt_version") != inst.prompt_version.tag:
        raise FireRefused("--measured: the report fired another instrument or prompt text")
    calls, spent, chars = report.get("calls"), report.get("charged_usd"), report.get("mean_prompt_chars")
    if not (isinstance(calls, int) and calls > 0 and isinstance(spent, (int, float)) and spent > 0
            and isinstance(chars, (int, float)) and chars > 0):
        raise FireRefused("--measured: the report has no positive calls, charged cost and prompt size")
    per_call = float(spent) / calls

    def expected(i: r1.Instrument, user: str) -> float:
        return per_call * _chars(i, user) / float(chars)
    return expected, {"run": str(report.get("run_id", ""))[:8], "usd_per_call": round(per_call, 6), "calls": calls}


def plan(items: list[r1.Item], inst: r1.Instrument, *, cap: float,
         expected_of: Optional[Callable[[r1.Instrument, str], float]] = None) -> dict:
    """r1's plan of the run (calls = objects x votes), without its label columns (the claims are unlabelled)."""
    try:
        p = r1.plan(items, [inst], votes=r1.VOTES, cap=cap, expected_of=expected_of)
    except r1.R1Refused as e:
        raise FireRefused(str(e)) from e
    return {k: v for k, v in p.items() if k not in ("labels", "pool_positives")}


# ── live ───────────────────────────────────────────────────────────────────────────────────────────────────────────


class _Fire:
    """One live firing run: the hard budget, the votes, the call ledger and the stop reason."""

    def __init__(self, inst: r1.Instrument, *, cap: float, run_id: str, fire_kind: str, caller: r1.Caller,
                 log: Callable[[Any, dict, Optional[dict], Optional[Exception]], None],
                 spend_probe: Optional[r1.SpendProbe], reserved: int) -> None:
        self.inst, self.cap, self.run_id, self.fire_kind = inst, cap, run_id, fire_kind
        self.caller, self.log, self.spend_probe, self.reserved = caller, log, spend_probe, reserved
        self.budget = r1.Budget(cap)
        self.votes: dict[str, list] = defaultdict(list)
        self.ledger: list[dict] = []
        self.stopped: Optional[str] = None
        self.mismatches = 0
        self.errors: list[dict] = []
        self.worst_notes: Counter = Counter()
        self.prompt_chars = 0

    def _blocked(self, worst: float) -> bool:
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

    def _no_vote(self, group: list[r1.Item]) -> None:
        for i in group:
            self.votes[i.claim_hash].append(None)

    def _entry(self, vote: int, group: list[r1.Item], resp: Any, amount: float, outcome: str) -> None:
        request_id = getattr(resp, "request_id", None)
        self.ledger.append({"call_id": f"{self.run_id}:{self.budget.calls}", "vote": vote,
                            "object": f"{group[0].content_sha256[:16]}:{group[0].field}", "claims": len(group),
                            "request_id": request_id if isinstance(request_id, str) and request_id else None,
                            "charged_usd": round(amount, 6), "outcome": outcome})

    def _charge(self, user: str, amount: float, worst: float) -> None:
        self.budget.charge(amount, worst)
        self.prompt_chars += _chars(self.inst, user)

    def _check_attempt(self, resp: Any) -> None:
        attempt = getattr(resp, "attempt_no", None)
        if (not self.stopped and isinstance(attempt, int) and not isinstance(attempt, bool)
                and attempt > self.reserved):
            self.stopped = (f"reply attempt_no {attempt} passed the {self.reserved} reserved attempt(s) of "
                            f"{self.inst.id}, after {self.budget.calls} calls: stopped, report partial")

    def _parse(self, resp: Any, signal: dict) -> tuple[Optional[dict], str]:
        try:
            parsed = json.loads(getattr(resp, "content", "") or "")
            if not isinstance(parsed, dict):
                raise ValueError("reply is not a JSON object")
        except Exception as e:                               # noqa: BLE001 — logged as billed_unusable, no vote
            self.log(resp, signal, None, e)
            return None, "unusable"
        self.log(resp, signal, parsed, None)
        if not r1._identity_ok(self.inst, resp):
            self.mismatches += 1
            return None, "identity_mismatch"
        return parsed, "vote"

    def one(self, vote: int, user: str, group: list[r1.Item]) -> None:
        inst = self.inst
        worst = r1.worst_call_cost(inst, user, self.reserved)
        if self._blocked(worst):
            self._no_vote(group)
            return
        try:
            resp = self.caller(inst, inst.prompt_version.system(inst.mode), user, r1.EXTRACTION_SCHEMA)
        except Exception as e:                               # noqa: BLE001 — paid attempts may have run: stop clean
            self._charge(user, worst, worst)
            self.worst_notes["caller_error_worst_case"] += 1
            self.errors.append({"vote": vote, "error": type(e).__name__, "detail": str(e)[:200]})
            self.stopped = f"caller error after {self.budget.calls} calls ({type(e).__name__}): stopped, report partial"
            self._entry(vote, group, None, worst, "caller_error")
            self._no_vote(group)
            return
        amount, notes = r1.billed_cost(inst, user, resp)
        self._charge(user, amount, worst)
        self.worst_notes.update(n.split(":", 1)[0] for n in notes)
        self._check_attempt(resp)
        signal = {"app": "cis", "issue": "AA-1048", "run_id": self.run_id, "instrument": inst.id, "vote": vote,
                  "prompt_version": inst.prompt_version.tag, "purpose": PURPOSE, "fire_kind": self.fire_kind,
                  "call_id": f"{self.run_id}:{self.budget.calls}", "claims": [i.claim_hash[:16] for i in group][:20]}
        parsed, outcome = self._parse(resp, signal)
        self._entry(vote, group, resp, amount, outcome)
        if parsed is None:
            self._no_vote(group)
            return
        for h, flagged in r1.flags_from_reply(inst, parsed, group).items():
            self.votes[h].append(flagged)

    def result(self) -> dict:
        b = self.budget
        return {"votes": dict(self.votes), "ledger": self.ledger, "spent_usd": round(b.spent, 6),
                "charged_usd": round(b.charged, 6), "ledger_usd": round(b.ledger, 6) if b.ledger is not None else None,
                "calls": b.calls, "mean_prompt_chars": round(self.prompt_chars / b.calls, 1) if b.calls else None,
                "stopped": self.stopped, "identity_mismatches": self.mismatches, "caller_errors": self.errors,
                "worst_case_charges": dict(self.worst_notes), "reserved_attempts": self.reserved}


def fire(items: list[r1.Item], inst: r1.Instrument, *, cap: float, run_id: str, fire_kind: str = "full",
         votes: int = r1.VOTES, caller: Optional[r1.Caller] = None,
         log: Callable[[Any, dict, Optional[dict], Optional[Exception]], None] = r1._log,
         spend_probe: Optional[r1.SpendProbe] = None,
         attempts_of: Optional[Callable[[r1.Instrument], int]] = None) -> dict:
    """The live run: `votes` rounds of one extraction call per object of `items` (r1.calls). Never raises on a caller
    error (charged its worst case, the run stops, the votes so far stay). `attempts_of` gives the attempts reserved per
    call (default: r1.route_attempts on the Model Gateway, 1 for an injected caller)."""
    if fire_kind not in FIRE_KINDS:
        raise FireRefused(f"fire_kind must be one of {FIRE_KINDS}")
    if attempts_of is None:
        attempts_of = r1.route_attempts if caller is None else (lambda _inst: 1)
    state = _Fire(inst, cap=cap, run_id=run_id, fire_kind=fire_kind, caller=caller or r1.gateway_call, log=log,
                  spend_probe=spend_probe, reserved=max(1, int(attempts_of(inst))))
    calls = r1.calls(items, inst)
    for vote in range(votes):
        for user, group in calls:
            state.one(vote, user, group)
    return state.result()


def ledger_rows(run_id: str, stage: str = r1.STAGE) -> Optional[int]:
    """The run's rows in shared.llm_call_log (read only); None when the ledger cannot be read."""
    import asyncio

    async def _q() -> int:
        import asyncpg

        from shared.secrets import get_database_url

        conn = await asyncpg.connect(get_database_url().strip(), ssl="require")
        try:
            return int(await conn.fetchval(LEDGER_ROWS_SQL, stage, run_id))
        finally:
            await conn.close()
    try:
        return asyncio.run(_q())
    except Exception:                                        # noqa: BLE001 — a count for the report only
        return None


def fired_of(votes: dict[str, list]) -> dict[str, Optional[bool]]:
    return {h: r1.majority(v) for h, v in sorted(votes.items())}


# ── CLI ────────────────────────────────────────────────────────────────────────────────────────────────────────────

_caller: Optional[r1.Caller] = None                     # tests put a fake here; None = the Model Gateway
_probe: Callable[[Any], r1.SpendProbe] = r1.ledger_probe
_ledger_rows: Callable[[str], Optional[int]] = ledger_rows


def _prepare(a: argparse.Namespace) -> tuple[dict, list[r1.Item], r1.Instrument, float]:
    """Everything a run reads before a call: the checks, the sample, the plan. Refuses on any input problem."""
    if a.live and not a.confirm_spend:
        raise FireRefused("--live is paid: add --confirm-spend")
    refreshed = ev.refresh_settings() if a.live else None
    lim = limits()
    lines, manifest = load(a.eligible, a.manifest)
    oos = out_of_sample(manifest, lines)
    why = oos_refusal(oos)
    if why:
        raise FireRefused(why)
    size, cap = _size(a, lim), _cap(a, lim)
    salt = salt_of(manifest)
    chosen = draw(lines, size, salt)
    sample = sample_record(lines, chosen, size, salt)
    if a.expect_sample and a.expect_sample != sample["digest"]:
        raise FireRefused(f"the sample is {sample['digest']}, not the expected {a.expect_sample} (size or input "
                          "changed)")
    fired_lines = chosen[:a.trial] if a.trial is not None else chosen
    inst = instrument()
    expected_of, measured = (measured_expectation(json.loads(_read(a.measured)), inst) if a.measured
                             else (None, None))
    items = [item_of(e) for e in fired_lines]
    rep = {"kind": KIND, "issue": "AA-1048", "mode": "live" if a.live else "dry_run",
           "fire_kind": "trial" if a.trial is not None else "full", "instrument": inst.id,
           "prompt_version": inst.prompt_version.tag, "eligible_sha256": manifest["eligible_sha256"],
           "settings_refreshed": refreshed, "limits": asdict(lim), "out_of_sample": oos, "sample": sample,
           "firing": {"claims": len(items), "objects": objects(fired_lines)}, "measured": measured,
           "plan": plan(items, inst, cap=cap, expected_of=expected_of)}
    return rep, items, inst, cap


def cmd_fire(a: argparse.Namespace) -> tuple[int, dict]:
    rep, items, inst, cap = _prepare(a)
    if not rep["plan"]["fits_cap"]:
        return 3, {"ok": False, "refused": "the plan does not fit the cap (price it with --measured <trial report>)",
                   **rep}
    if not a.live:
        from services.golden_set import llm_guard

        llm_guard.install("aa1048_r1_fire_dry_run")
        return 0, {"ok": True, **rep}
    run_id = str(uuid.uuid4())
    res = fire(items, inst, cap=cap, run_id=run_id, fire_kind=rep["fire_kind"], caller=_caller,
               spend_probe=_probe(datetime.now(timezone.utc)))
    fired = fired_of(res["votes"])
    live = {"run_id": run_id, **{k: v for k, v in res.items() if k != "votes"},
            "ledger_rows": _ledger_rows(run_id), "fired_count": dict(Counter(str(v).lower() for v in fired.values())),
            "votes": {inst.id: dict(sorted(res["votes"].items()))}, "fired": fired}
    return (0 if not res["stopped"] else 3), {"ok": not res["stopped"], **rep, **live}


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m services.ground_truth.r1_fire", description=__doc__.split("\n")[0])
    ap.add_argument("--eligible", required=True, help="fresh_pool eligible --out")
    ap.add_argument("--manifest", required=True, help="fresh_pool eligible --manifest")
    ap.add_argument("--size", type=int, help="claims to draw (default and ceiling: cis.quality.claim_fire_sample_max)")
    ap.add_argument("--trial", type=int, help="fire only the first N claims of the sample")
    ap.add_argument("--expect-sample", help="refuse unless the sample digest is this one")
    ap.add_argument("--measured", help="price the plan from this finished live fire report")
    ap.add_argument("--cap", type=float,
                    help="hard cap in USD (default and ceiling: cis.spend.instrument_eval_cap_usd)")
    ap.add_argument("--live", action="store_true", help="PAID: make the calls")
    ap.add_argument("--confirm-spend", action="store_true", help="required with --live")
    ap.add_argument("--out", help="write the report here too")
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    a = parser().parse_args(argv)
    try:
        rc, rep = cmd_fire(a)
    except (ev.EvalRefused, r1.R1Refused) as e:
        print(json.dumps({"ok": False, "refused": str(e)}))
        return 2
    if a.out:
        try:
            out = ev._check_out(a.out)
        except ev.EvalRefused as e:
            print(json.dumps({"ok": False, "refused": str(e)}))
            return 2
        with open(out, "w", encoding="utf-8") as f:
            f.write(json.dumps(rep, indent=1, default=str))
    print(json.dumps(rep, indent=1, default=str))
    return rc


if __name__ == "__main__":
    sys.exit(main())
