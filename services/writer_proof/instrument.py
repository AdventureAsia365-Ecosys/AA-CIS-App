"""AA-1051 (S283) — the W5 judge on the pinned AA-1048 claim instrument (R1 fire, then the two-step v7), ADVISORY.

Why. Root ADR 0020 D10: the eval and production share one claim engine, and the W5 null run's judge must be the
instrument W2 pins (D7). W2 (AA-1048) closed Done-with-scope with the two-step instrument v7 pinned (registration
`aa1048-two-step-v7`, step tags aa1048-r1-v7c / aa1048-r1-v7j, gpt-6-luna-openai@openai-direct, 3 votes): on the
claims the R1 extraction instrument FIRED on, v7 dropped 68 % of the false positives and kept 88 % of the stored
violations (precision 0.73, recall 0.88), no_go by one stored violation against the 90 % bar. The tech lead allowed it
(S283) as an ADVISORY W5 judge under the null run cap: its verdicts inform, they do not calibrate a gate, and a null run
judged with it only opens later proofs judged with the same instrument (runner.same_instrument).

The chain is the one v7 was measured on, nothing more:
  claims   every sentence of >= 3 words of each labelled OUTPUT field (claims.sentence_spans, the AA-1048 claim unit),
           the field labels mapped back to the output keys the AA-1048 claims carry (BODY -> content_text, ...), the
           same sentence twice in one field judged once;
  fire     the R1 extraction instrument the fresh pools were fired with (fresh_pool.FIRE_CANDIDATE, prompt v1), one
           call per field per vote: a claim is fired unless the vote majority clears it (an unsure fire never clears,
           recall first as in two_step.routed). A cleared claim is supported and costs no further call;
  step 1   v7c, `votes` calls per fired claim: restatement of the SOURCE or new claim (the specifics listed);
  step 2   v7j, `votes` calls per claim step 1 sent on (two_step.routed);
  verdict  per claim as two_step.final_votes: cleared when step 1's majority is a restatement, else step 2's majority
           (unsupported = flagged, no majority = unverified, D7 "a miss is unverified").
The item verdict is `fail` when any claim is flagged, else `pass`; `claims` lists every flagged claim (class
CLAIM_CLASS: the entailment instrument names no policy class, so `failure_codes` stays empty and hard stop 1 cannot
fire, root ADR 0020 amendment S278 item 3), so decision.metric_of's `fail_claims_per_item` counts them all; score = 10 x
the share of claims not flagged. R2 runs it without its item-kappa calibration block (a claim instrument's calibration
is its AA-1048 record, not the item kappa against the golden pass / fail labels: r2.calibration_items is empty and
r2.calibration_ready is satisfied when `judge_instrument` is set, `judge_counts` stays false, so R2's own verdict is
informative and the pre-registered decision is decision.py's).

Calls. Each call is the instrument's own request (r1.gateway_call: the adhoc_aa1048 request settings, role judge, the
pinned connection first, temperature 0, seed), so the W5 judge is the calibrated chain byte for byte. Each call writes
one shared.llm_call_log row (**billed_fields, the step's prompt tag in quality_signal.prompt_version); inside R2 the
eval label relabels it to the run's stage (adhoc_aa1051) with quality_signal.eval_run, so the run's hard cap and cost
report read it. A reply from another model or connection is logged and then refused (JudgeIdentityMismatch: the item
is an error row, never a verdict). The run cap holds per call: before each call the run's ledger spend (read once per
output, after the background writes are flushed) + this output's charges + the worst case of every call in flight +
this call's worst case must fit `cap_usd`, else the output stops (InstrumentBudgetStop, an error row) and R2's next
reservation ends the run at its cap.
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from services.ground_truth import claims as cl

# The pinned claim instruments a W5 pre-registration may name: id (its AA-1048 registration id) -> the two_step step
# set that runs it. A new pinned instrument is a reviewed code change here, never a value in a file.
PINNED = {"aa1048-two-step-v7": "v7"}
# Why an instrument is advisory only (its AA-1048 record did not pass every bar): a pre-registration naming it must
# say `advisory: true`, and the runner's report carries the reason.
ADVISORY = {"aa1048-two-step-v7": ("AA-1048 v7 gated run 46d645c4 was no_go by one stored violation (false positives "
                                   "dropped 68 %, stored violations kept 88 % against the 90 % bar; precision 0.73, "
                                   "recall 0.88); the tech lead allowed it as an advisory W5 judge (S283)")}
# Planning only (price.py): the share of claims the R1 fire fired per writer, measured on the v7 fresh pool fire
# (run 5b24602e, S282: 62 of 223 t9_blog, 72 of 187 t9_social, 16 of 71 t2 claims; run 6797de2f, S285: a1 400 of 1669
# claims on the w5-s1 GATE controls). A measurement, not a policy value:
# it sizes the estimate; the run itself is held by the per-call cap. A writer without a measurement plans every claim.
FIRE_SHARE = {"aa1048-two-step-v7": {"t9_blog": 0.278, "t9_social": 0.385, "t2": 0.225, "a1": 0.24}}
FIRE_SHARE_SOURCE = ("R1 fire 5b24602e on the AA-1048 v7 fresh pool (S282); a1: R1 fire 6797de2f on the w5-s1 GATE "
                     "controls, aa729@1.7.0 (S285)")
CLAIM_CLASS = "claim.unsupported"
STEPS = ("fire", "classify", "judge")
CONCURRENCY = 10            # technical-constant: calls in flight per output (provider latency, not policy)
SPEND_LOOKBACK_H = 24       # technical-constant: the ledger window of the run's spend read (index narrowing only)
_LABEL_RE = re.compile(r"^[A-Z0-9][A-Z0-9 _/&-]*:$")


class InstrumentRefused(ValueError):
    """The named instrument is not a pinned one (unknown, closed, or its registration changed): nothing is called."""


class InstrumentBudgetStop(RuntimeError):
    """The next call's worst case would pass the run cap: the output stops (an error row), R2 ends the run."""


@dataclass(frozen=True)
class Pinned:
    id: str
    step_set: str
    model_key: str
    connection: str
    votes: int
    tags: dict                  # step (STEPS) -> prompt version tag
    registration_sha256: str

    @property
    def candidate(self) -> str:
        return f"{self.model_key}@{self.connection}"

    @property
    def judge_version(self) -> str:
        """`<id>:<12 hex>` of the step tags and the votes: stored on every result row (r2.row_judge_version)."""
        raw = "|".join([*(self.tags[s] for s in STEPS), str(self.votes)])
        return f"{self.id}:{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]}"


def fire_instrument():
    """The R1 extraction instrument the AA-1048 fresh pools were fired with (fresh_pool.FIRE_CANDIDATE, prompt v1)."""
    from services.ground_truth import fresh_pool as fp
    from services.ground_truth import r1
    return r1.instruments([fp.FIRE_CANDIDATE], ["extraction"])[0]


def instruments(p: "Pinned") -> dict:
    """step -> r1.Instrument of the pinned chain (fire, classify, judge)."""
    from services.ground_truth import two_step as ts
    return {"fire": fire_instrument(), **ts.instruments(p.candidate, p.step_set)}


def _problems(instrument_id: str, reg: dict, tags: dict, fire) -> list[str]:
    from services.ground_truth import two_step as ts
    out = [f"registration id {reg.get('id')!r}" for _ in [0] if reg.get("id") != instrument_id]
    out += [f"{s} tag registered {(reg.get('prompt_versions') or {}).get(s)!r}, code {tags[s]!r}"
            for s in ts.STEPS if (reg.get("prompt_versions") or {}).get(s) != tags[s]]
    votes = reg.get("votes")
    model, _, conn = str(reg.get("candidate") or "").partition("@")
    if not (isinstance(votes, int) and not isinstance(votes, bool) and votes >= 1) or not model or not conn:
        out.append("the registration needs a candidate <model>@<connection> and votes >= 1")
    if (fire.model_key, fire.connection) != (model, conn):
        out.append(f"the fire instrument {fire.id} is not on the registered candidate {reg.get('candidate')}")
    return out


@functools.lru_cache(maxsize=8)
def pinned(instrument_id: str) -> Pinned:
    """The pinned instrument, read from its AA-1048 registration file (whose sha256 two_step_sets pins) and checked
    against the code: an open step set, the registration id, the step prompt tags in force, the fire on the same
    candidate. InstrumentRefused else."""
    from services.ground_truth import two_step as ts
    if instrument_id not in PINNED:
        raise InstrumentRefused(f"unknown claim instrument {instrument_id!r} (pinned: {', '.join(sorted(PINNED))})")
    try:
        sset = ts.step_set(PINNED[instrument_id])
    except ts.TwoStepRefused as e:
        raise InstrumentRefused(str(e)) from e
    if sset.closed:
        raise InstrumentRefused(f"{instrument_id}: step set {sset.id} is closed ({sset.closed})")
    if ts.file_sha256(sset.preregistration) != sset.prereg_sha256:
        raise InstrumentRefused(f"{instrument_id}: the registration file changed after it was pinned")
    reg = json.loads(sset.preregistration.read_text(encoding="utf-8"))
    try:
        steps = ts.instruments(reg["candidate"], sset.id)
        fire = fire_instrument()
    except Exception as e:  # noqa: BLE001 — an unpriced or malformed candidate: refused, nothing called
        raise InstrumentRefused(f"{instrument_id}: {e}") from e
    tags = {s: steps[s].prompt_version.tag for s in ts.STEPS}
    tags["fire"] = fire.prompt_version.tag
    problems = _problems(instrument_id, reg, tags, fire)
    if problems:
        raise InstrumentRefused(f"{instrument_id}: " + "; ".join(problems))
    model, _, conn = reg["candidate"].partition("@")
    return Pinned(instrument_id, sset.id, model, conn, int(reg["votes"]), tags, sset.prereg_sha256)


def judge_problems(judge: Any) -> list[str]:
    """Why a pre-registration's judge block may not name its instrument ([] without one). Reads the pinned
    registration file; otherwise pure."""
    if not isinstance(judge, dict) or judge.get("instrument") is None:
        if isinstance(judge, dict) and judge.get("advisory") is not None:
            return ["judge.advisory is only for a judge.instrument"]
        return []
    iid = judge.get("instrument")
    try:
        p = pinned(str(iid))
    except InstrumentRefused as e:
        return [f"judge.instrument: {e}"]
    out = []
    if (judge.get("model_key"), judge.get("connection")) != (p.model_key, p.connection):
        out.append(f"judge.instrument {iid} runs on {p.candidate}: judge.model_key / connection must name it")
    if iid in ADVISORY and judge.get("advisory") is not True:
        out.append(f"judge.instrument {iid} is advisory only (judge.advisory must be true): {ADVISORY[iid]}")
    return out


# ── the claims of one labelled OUTPUT ──────────────────────────────────────────────────────────────────────────────

@functools.lru_cache(maxsize=1)
def _label_keys() -> dict[str, str]:
    """"<LABEL>:" -> the output key it labels (eval_judge.OUTPUT_FIELDS_*; the first key wins for a shared label)."""
    from services.writing_memory import eval_judge as ej
    out: dict[str, str] = {}
    for key, label in ej.OUTPUT_FIELDS_FIRST + ej.OUTPUT_FIELDS_LAST:
        out.setdefault(f"{label}:", key)
    return out


def _field_name(label_line: str) -> str:
    known = _label_keys().get(label_line)
    if known:
        return known
    return re.sub(r"[^a-z0-9]+", "_", label_line.split("(")[0].rstrip(":").strip().lower()).strip("_")[:40] or "body"


def output_fields(output_text: str) -> list[tuple[str, str]]:
    """[(field, text)] of eval_judge.judge_output_text's labelled OUTPUT ("SEO TITLE:\\n...\\n\\nBODY:\\n..."): a
    line that is a field label opens the next field (named by its output key: BODY -> content_text); text before any
    label is field `content_text`. Pure."""
    labels = _label_keys()
    out: list[tuple[str, list[str]]] = []
    for line in (output_text or "").splitlines():
        s = line.strip()
        if s in labels or _LABEL_RE.match(s):
            out.append((_field_name(s), []))
        else:
            if not out:
                out.append(("content_text", []))
            out[-1][1].append(line)
    return [(f, "\n".join(lines).strip()) for f, lines in out if "\n".join(lines).strip()]


def output_claims(writer: str, source_text: str, output_text: str) -> list:
    """The r1.Items of one output: one per distinct sentence claim (>= 3 words) of each field, in reading order, with
    the field text and the SOURCE the fire and step prompts read. Pure."""
    from services.ground_truth import r1
    sha = cl.sha256_text(output_text or "")
    seen: set[str] = set()
    items = []
    for fld, text in output_fields(output_text):
        for s, e in cl.sentence_spans(text):
            span = text[s:e]
            h = cl.claim_hash(sha, fld, span)
            if h in seen:
                continue
            seen.add(h)
            items.append(r1.Item(h, writer, "", "", "w5", sha, fld, span, text, source_text or ""))
    return items


def output_shape(writer: str, output_text: str) -> dict:
    """{fields, claims} the instrument judges in this output ($0: the planning counts of price.py). Pure."""
    items = output_claims(writer, "", output_text)
    return {"fields": len({i.field for i in items}), "claims": len(items)}


# ── one output ─────────────────────────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class InstrumentVerdict:
    """The R2 verdict shape (eval_judge.Verdict's attributes, read by getattr in r2.py) of an instrument judgement."""
    verdict: str
    score: float
    failure_codes: list
    model: str
    cost_usd: float
    judge_version: str
    claims: tuple = ()
    model_key: Optional[str] = None
    connection: Optional[str] = None
    provider_model: Optional[str] = None
    evidence_sha: Optional[str] = None
    max_claims: int = 0                     # r2._claims_of keeps this many quoted claims (every flagged one)
    counts: dict = field(default_factory=dict)


class _Output:
    """The calls of one output: the run-cap gate (spend + in-flight worst cases), the votes, the identity."""

    def __init__(self, p: Pinned, insts: dict, *, cap_usd: float, base_spend: float, attempts: int,
                 caller: Callable, log: Callable, signal: dict) -> None:
        self.p, self.insts, self.cap, self.base = p, insts, float(cap_usd), float(base_spend)
        self.attempts, self.caller, self.log, self.signal = attempts, caller, log, signal
        self.charged, self.in_flight, self.calls = 0.0, 0.0, 0
        self.stopped: Optional[str] = None
        self.mismatch: Optional[str] = None
        self.answered: Optional[tuple[str, Optional[str], Optional[str]]] = None
        self.lock = asyncio.Lock()
        self.sem = asyncio.Semaphore(CONCURRENCY)

    async def _reserve(self, worst: float) -> bool:
        async with self.lock:
            if self.stopped or self.mismatch:
                return False
            if self.base + self.charged + self.in_flight + worst > self.cap + 1e-9:
                self.stopped = (f"run cap {self.cap} USD: spent {self.base + self.charged:.4f} + in flight "
                                f"{self.in_flight:.4f} + the next call's worst case {worst:.4f} would pass it")
                return False
            self.in_flight += worst
            return True

    async def call(self, step: str, vote: int, group: list, user: str) -> Optional[dict]:
        """One call of `step` about the claims in `group`; the parsed reply of the pinned model, else None (no vote).
        Sets `stopped` at the cap or on a caller error."""
        from services.ground_truth import r1
        from services.ground_truth import two_step as ts
        inst = self.insts[step]
        worst = r1.worst_call_cost(inst, user, self.attempts)
        async with self.sem:
            if not await self._reserve(worst):
                return None
            resp = None
            schema = r1.EXTRACTION_SCHEMA if step == "fire" else ts.schema_of(inst)
            try:
                resp = await asyncio.to_thread(self.caller, inst, inst.prompt_version.system(inst.mode), user, schema)
                amount = r1.billed_cost(inst, user, resp)[0]
            except Exception as e:  # noqa: BLE001 — paid attempts may have run: charge the worst case and stop clean
                amount = worst
                self.stopped = self.stopped or f"caller error ({type(e).__name__}): output stopped"
            async with self.lock:
                self.in_flight -= worst
                self.charged += amount
                self.calls += 1
        if resp is None:
            return None
        return await self._parsed(inst, step, vote, group, resp)

    async def _parsed(self, inst, step: str, vote: int, group: list, resp) -> Optional[dict]:
        from services.ground_truth import r1
        signal = {**self.signal, "instrument": inst.id, "step": step, "vote": vote,
                  "prompt_version": inst.prompt_version.tag, "claims": [i.claim_hash[:16] for i in group][:20]}
        try:
            parsed = json.loads(getattr(resp, "content", "") or "")
            if not isinstance(parsed, dict):
                raise ValueError("reply is not a JSON object")
        except Exception as e:  # noqa: BLE001 — logged as billed_unusable, no vote
            await self.log(resp, inst, signal, None, e)
            return None
        await self.log(resp, inst, signal, parsed, None)
        if not r1._identity_ok(inst, resp):
            self.mismatch = (f"the instrument is pinned to {inst.model_key}@{inst.connection} but "
                             f"{getattr(resp, 'model_used', None)} answered from another model or connection")
            return None
        self.answered = (str(resp.model_used)[:128], r1._answered_connection(resp),
                         _text(getattr(resp, "provider_model", None)) or _text(getattr(resp, "deployment_wire_id",
                                                                                         None)))
        return parsed


def _text(v) -> Optional[str]:
    return v if isinstance(v, str) and v else None


async def _fire(out: _Output, items: list) -> dict[str, list]:
    """The fire votes of every claim of the output: claim_hash -> [True | False | None] per vote (r1.calls groups the
    claims by field: one extraction call per field per vote)."""
    from services.ground_truth import r1
    inst = out.insts["fire"]
    groups = r1.calls(items, inst)
    votes: dict[str, list] = defaultdict(list)
    for vote in range(out.p.votes):
        replies = await asyncio.gather(*(out.call("fire", vote, g, user) for user, g in groups))
        for (_user, g), parsed in zip(groups, replies):
            flags = r1.flags_from_reply(inst, parsed, g) if parsed is not None else {}
            for i in g:
                votes[i.claim_hash].append(flags.get(i.claim_hash))
    return votes


async def _claim(out: _Output, item) -> dict:
    """Both two-step steps of one fired claim: {"final": True / False / None, "routed", "why": step 2's reason}."""
    from services.ground_truth import r1
    from services.ground_truth import two_step as ts
    step1, specs = [], []
    for vote in range(out.p.votes):
        parsed = await out.call("classify", vote, [item], ts.step1_user(item, out.insts["classify"].prompt))
        v = ts.classify_vote(parsed) if parsed is not None else None
        step1.append(v)
        specs.append(ts.specifics_of(parsed) if v is True else [])
    if not ts.routed(step1):
        return {"final": False, "routed": False, "why": ""}
    specifics = ts.merge_specifics(specs)
    step2, why = [], ""
    for vote in range(out.p.votes):
        parsed = await out.call("judge", vote, [item], ts.step2_user(item, specifics))
        step2.append(ts.judge_vote(parsed) if parsed is not None else None)
        why = why or str((parsed or {}).get("why") or "")
    return {"final": r1.majority(step2), "routed": True, "why": why}


async def _log_row(pool, resp, inst, signal: dict, parsed: Optional[dict], error: Optional[Exception]) -> None:
    from services.ground_truth import r1
    from shared.llm_client.call_log import billed_fields, record_call_with_pool, record_unusable
    if error is not None:
        await record_unusable(resp, stage=r1.STAGE, role=r1.ROLE, error=error, quality_signal_extra=signal)
        return
    await record_call_with_pool(
        pool, stage=r1.STAGE, role=r1.ROLE, model=resp.model_used, tokens_in=getattr(resp, "input_tokens", None),
        tokens_out=getattr(resp, "output_tokens", None), cost_usd=getattr(resp, "cost_usd", None),
        quality_signal={**signal, "json_parsed": parsed is not None}, stop_reason=getattr(resp, "stop_reason", None),
        account=getattr(resp, "satellite_account", None), fallback_used=getattr(resp, "fallback_used", None),
        provider=resp.provider if isinstance(getattr(resp, "provider", None), str) else None,
        **billed_fields(resp, prompt_version=signal.get("prompt_version")))


async def run_spend(pool, run_id: str, stages: tuple[str, ...]) -> float:
    """The run's ledger spend so far (R2's own reading, after the background writes are flushed)."""
    from services.writing_memory.r2 import PgStore, flush_call_log
    await flush_call_log()
    since = datetime.now(timezone.utc) - timedelta(hours=SPEND_LOOKBACK_H)
    return await PgStore(pool, stages=stages).run_spend(run_id, since)


async def _attempts(insts: dict, caller: Optional[Callable]) -> int:
    from services.ground_truth import r1
    if caller is not None:
        return 1
    return max([await asyncio.to_thread(r1.route_attempts, i) for i in insts.values()])


async def judge_output(pool, *, writer: str, source_text: str, output_text: str, run_id: str, item_id: str,
                       arm: str, instrument: str, cap_usd: float, stages: tuple[str, ...],
                       caller: Optional[Callable] = None, spend: Optional[Callable] = None,
                       log: Optional[Callable] = None) -> InstrumentVerdict:
    """One output judged by the pinned chain (the R2 judge_fn shape plus the binding judge_fn() adds).
    `caller(inst, system, user, schema)`, `spend(pool, run_id, stages)` and `log(resp, inst, signal, parsed, error)`
    are injectable for tests. Raises InstrumentBudgetStop at the cap and JudgeIdentityMismatch on another answerer."""
    from services.ground_truth import r1
    from services.writing_memory import eval_judge as ej
    p = pinned(instrument)
    insts = instruments(p)
    items = output_claims(writer, source_text, output_text)
    ev_sha = ej.evidence_sha((source_text or "").strip(), (output_text or "").strip())
    base = await (spend or run_spend)(pool, run_id, stages)
    signal = {"app": "cis", "issue": "AA-1051", "run_id": run_id, "item_id": item_id, "arm": arm,
              "w5_instrument": p.id, "judge_version": p.judge_version, "evidence_sha": ev_sha,
              "judge_pin": p.candidate}
    out = _Output(p, insts, cap_usd=cap_usd, base_spend=base, attempts=await _attempts(insts, caller),
                  caller=caller or r1.gateway_call, log=log or functools.partial(_log_row, pool), signal=signal)
    fire = await _fire(out, items) if items else {}
    fired = [i for i in items if r1.majority(fire.get(i.claim_hash) or []) is not False]
    results = dict(zip([i.claim_hash for i in fired], await asyncio.gather(*(_claim(out, i) for i in fired))))
    if out.mismatch:
        raise ej.JudgeIdentityMismatch(out.mismatch)
    if out.stopped:
        raise InstrumentBudgetStop(out.stopped)
    return verdict_of(p, items, fire, results, out, ev_sha)


def verdict_of(p: Pinned, items: list, fire: dict[str, list], results: dict[str, dict], out: Any,
               ev_sha: str) -> InstrumentVerdict:
    """The item verdict from the claim verdicts (see the module docstring). `results`: claim_hash -> _claim's record
    for every fired claim. Pure."""
    from services.ground_truth import r1
    flagged = [(i, results[i.claim_hash]) for i in items if (results.get(i.claim_hash) or {}).get("final") is True]
    claims = tuple({"class": CLAIM_CLASS, "claim": " ".join(i.claim_text.split())[:240],
                    "why": " ".join(r["why"].split())[:240]} for i, r in flagged)
    n = len(items)
    majorities = [r1.majority(fire.get(i.claim_hash) or []) for i in items]
    counts = {"claims": n, "fired": sum(1 for m in majorities if m is True),
              "fire_unverified": sum(1 for m in majorities if m is None),
              "routed": sum(1 for r in results.values() if r["routed"]), "flagged": len(flagged),
              "unverified": sum(1 for r in results.values() if r["routed"] and r["final"] is None), "calls": out.calls}
    model, conn, provider_model = out.answered or (p.model_key, None, None)
    return InstrumentVerdict(
        verdict="fail" if flagged else "pass", score=round(10.0 * (1 - len(flagged) / n), 3) if n else 10.0,
        failure_codes=[], model=model, cost_usd=round(out.charged, 6), judge_version=p.judge_version, claims=claims,
        model_key=p.model_key, connection=conn, provider_model=provider_model, evidence_sha=ev_sha,
        max_claims=max(1, len(claims)), counts=counts)


def judge_fn(conf) -> Callable:
    """The R2 judge_fn of a run that names a claim instrument (r2.judge_fn_for). R2Refused at $0 when the run is not a
    pre-registered W5 run, the instrument is not pinned, or the run's judge pin is not the instrument's candidate."""
    from services.writing_memory.r2 import R2Refused
    if not conf.prereg_id:
        raise R2Refused("a claim instrument judges pre-registered W5 runs only (services/writer_proof)")
    try:
        p = pinned(conf.judge_instrument)
    except InstrumentRefused as e:
        raise R2Refused(str(e)) from e
    if (conf.judge_model, conf.judge_connection) != (p.model_key, p.connection):
        raise R2Refused(f"the instrument {p.id} runs on {p.candidate}: pin judge_model / judge_connection to it")
    return functools.partial(judge_output, instrument=p.id, cap_usd=float(conf.cap_usd),
                             stages=(conf.log_stage,))


__all__ = ["ADVISORY", "CLAIM_CLASS", "FIRE_SHARE", "FIRE_SHARE_SOURCE", "InstrumentBudgetStop", "InstrumentRefused",
           "InstrumentVerdict", "PINNED", "Pinned", "STEPS", "fire_instrument", "instruments", "judge_fn",
           "judge_output", "judge_problems", "output_claims", "output_fields", "output_shape", "pinned", "run_spend",
           "verdict_of"]
