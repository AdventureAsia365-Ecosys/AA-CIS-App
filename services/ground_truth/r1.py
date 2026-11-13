"""AA-1048 — R1: judge-only calibration of pinned claim instruments on claim labels (root ADR 0020 D7 / D16; audit W2
"R1 (judge-only, 3 votes, pinned)"; critic F3: "instrument = base model with calibrated connections", F8: "calibrate
entailment and extraction as separate instrument ids").

An instrument is (model key, connection, mode); its id is "<model>@<connection>:<mode>". Two modes:
  entailment  one call per labelled claim: the claim and its aligned source lines (claims.align) -> supported or not;
  extraction  one call per labelled object field: the field text and the full source -> the quotes it calls
              unsupported; code confirms a quote is in the text and maps it onto the labelled claims it overlaps.
Each call is made VOTES times on the pinned model and connection (temperature 0, a fixed seed, max_tokens
MAX_TOKENS >= 8192, migration 382); the verdict is the majority, and the votes give test-retest agreement. A reply from
another model or connection is not a vote (D7: "a miss is unverified").

Split. Only a held-out measurement may set passing classes or a recommendation (audit "30% frozen held-out split";
the panel brief: "measurements that decide a gate read the held-out side"). `items_from` keeps one split (default
held_out; the split comes from claims.split_group of the export line); a train or pooled run is exploration and its
record says so (split, passing_classes empty, recommended None).

Cost. DRY RUN is the default: `plan` counts the calls and estimates the cost from the catalog prices below (expected
reply size plus the expected reasoning tokens of a reasoning model, and the worst case). A live run needs an explicit
flag and a cap <= HARD_CAP_USD, and the cap is HARD (review S272):
  - a call starts only when the spend so far plus that call's WORST case fits the cap. One attempt's worst case is the
    first try billed at its floored ceiling (shared/aa_llm/tokens.floored of MAX_TOKENS: reasoning tokens are billed
    inside it) plus the gateway's one truncation retry at its ceiling (tokens.retry_budget); the call's worst case is
    that times the attempts the request may make (`route_attempts`: model_tier runs one route model, which walks its
    enabled deployments, every one a billed attempt; the pinned connection only goes first);
  - a reply is charged cost_usd + truncation_cut_cost_usd (the cut attempt the retry replaced; worst case when the
    reply says it retried but gives no cut cost) + one attempt's worst case for every earlier attempt its attempt_no
    reports (failed attempts bill their own rows); a missing, 0 or non-finite (NaN / inf) cost_usd is charged the
    call's worst case; a reply whose attempt_no passes the reserved attempts stops the run;
  - a caller exception (route exhausted, throttled, deadline) is charged the call's worst case (every reserved
    attempt), gives no vote, and stops the run cleanly: the votes so far are kept and the report is still written;
  - an optional ledger probe (the sum of shared.llm_call_log.cost_usd on STAGE since the run started) is read before
    every call, and the budget's spend is the larger of the two; a non-finite reading stops the run.
Every reply goes through the Model Gateway (LLMClient.generate) on stage STAGE, role judge, and writes one
shared.llm_call_log row with **billed_fields(resp) (an unusable reply writes its billed_unusable row before the error
is raised). The row names the instrument's prompt version and the hash of its texts (prompt_version column and
quality_signal; services/ground_truth/prompts.py: v1 the S272-S278 prompt and the default, v2 the S279 panel's B1-B5).

Measurement. Precision is the instrument's over EVERY fire on one writer surface (an entailment or extraction
instrument does not know the class at run time: a hold acts on any fire; it does know the writer, and holds act per
writer surface), with its Wilson lower bound. A class passes for a writer only when that writer's precision clears the
bar, that writer has n_pos >= the bar for the class and at least one caught violation of it; passing classes are
listed as
"<writer>:<class>". A verdict needs votes // 2 + 1 real votes that agree (2 of 3); otherwise it is unverified, a miss
on a violation and not cleared on an ok claim (D7: "a miss is unverified"), and the instrument conditions also need
unverified / items <= UNVERIFIED_MAX.

Pooled classes (services/ground_truth/pools.py, tech lead S278: measure a pool first, split later). The record also
measures every declared pool on every writer it names, with the same bar applied to the pool: the writer's precision
(unchanged), the pool's n_pos (violations of all its members) >= the bar's n_pos and at least one caught. A passing
pool is listed as "<writer>:<pool id>" in `passing_pools` and calibrates only its members on that writer
(`calibrated`, pools.calibrated_keys); each member keeps its own n_pos / tp / recall and `split_ready` names the
members that reach the bar alone. The record states the declaration (`pools`, `pools_sha`).
"""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from services.ground_truth import claims as cl
from services.ground_truth import pools as pl
from services.ground_truth import prompts as pr
from services.ground_truth import sample_size as ss
from services.ground_truth.prompts import ENTAILMENT_SYSTEM, EXTRACTION_SYSTEM
from shared.aa_llm import tokens as llm_tokens

STAGE = "adhoc_aa1048"
ROLE = "judge"
HARD_CAP_USD = 2.0
VOTES = 3
MAX_TOKENS = 8192
SEED = 1048
MODES = ("entailment", "extraction")
CHARS_PER_TOKEN = 3.0                    # conservative (English is about 4): over-estimates the prompt side
EXPECTED_OUT = {"entailment": 150, "extraction": 900}
# A reasoning model (shared/aa_llm/tokens.reasoning_capable: GPT-5.x / 6.x, Claude 5.x) bills its thinking inside the
# output: GPT-6 Luna reasons 2-6k tokens at its default effort (token_floor.EFFORT_FLOORS note), so the plan adds the
# top of that range per call. The cap itself never relies on it (it gates on the worst case).
EXPECTED_REASONING = 6000
SAFETY = 2.0                             # the plan fits only when SAFETY x its expected cost <= cap
UNVERIFIED_MAX = 0.05                    # instrument condition: at most this share of items without a verdict
# USD per million tokens (in, out): the catalog prices of the candidates (root docs/generated/llm-routes.md,
# 06/11/2026). A model not listed is refused: without a price the cap cannot be kept.
PRICES = {
    "gpt-6-luna-openai": (0.10, 0.50),
    "llama4-maverick": (0.24, 0.97),
    "gpt-6-luna": (0.10, 0.50),
    "kimi-k3": (3.30, 16.50),
}
# D7: GPT-6 Luna on the OpenAI API plus one acc2 non-Claude model.
DEFAULT_CANDIDATES = (
    ("gpt-6-luna-openai", "openai-direct"),
    ("llama4-maverick", "acc2"),
)
# The instrument bar (root audit README "Gate honesty"; sample_size.BARS[0]) and its extra conditions.
RETEST_BAR = 0.95
OK_CLEARED_BAR = 0.85
HARD_MIN = 10

ENTAILMENT_SCHEMA = {"name": "claim_entailment", "schema": {
    "type": "object", "additionalProperties": False, "required": ["verdict", "quote", "why"],
    "properties": {"verdict": {"type": "string", "enum": ["supported", "unsupported"]},
                   "quote": {"type": "string"}, "why": {"type": "string"}}}}
EXTRACTION_SCHEMA = {"name": "claim_extraction", "schema": {
    "type": "object", "additionalProperties": False, "required": ["claims"],
    "properties": {"claims": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["quote", "class", "why"],
        "properties": {"quote": {"type": "string"}, "class": {"type": "string"}, "why": {"type": "string"}}}}}}}

class R1Refused(ValueError):
    """The run cannot start (price unknown, cap over the hard cap, no items): no call is made."""


@dataclass(frozen=True)
class Instrument:
    """(model, connection, mode, prompt version). A different prompt is a different instrument: its id carries the
    version ("#v2") unless it is the default v1, whose ids stay those of the S272-S278 records."""
    model_key: str
    connection: str
    mode: str
    prompt: str = pr.DEFAULT

    @property
    def id(self) -> str:
        base = f"{self.model_key}@{self.connection}:{self.mode}"
        return base if self.prompt == pr.DEFAULT else f"{base}#{self.prompt}"

    @property
    def prompt_version(self) -> pr.PromptVersion:
        return pr.get(self.prompt)


def instruments(candidates: Iterable[tuple[str, str]] = DEFAULT_CANDIDATES,
                modes: Iterable[str] = MODES, prompt: str = pr.DEFAULT) -> list[Instrument]:
    try:
        pr.get(prompt)
    except pr.UnknownPromptVersion as e:
        raise R1Refused(str(e)) from e
    out = []
    for model, conn in candidates:
        for mode in modes:
            if mode not in MODES:
                raise R1Refused(f"unknown mode {mode!r}")
            out.append(Instrument(model, conn, mode, prompt))
    return out


@dataclass(frozen=True)
class Item:
    """One labelled claim with what the instruments read (from the panel export joined with the final labels)."""
    claim_hash: str
    writer: str
    claim_class: str
    label: str                 # violation | ok (unsure claims are not calibration material)
    stratum: str
    content_sha256: str
    field: str
    claim_text: str
    field_text: str
    source_text: str
    evidence: tuple[str, ...] = ()
    split: str = "train"

    @property
    def object_key(self) -> str:
        return f"{self.content_sha256}:{self.field}"


SPLIT_CHOICES = ("held_out", "train", "all")


def split_of_line(e: dict) -> str:
    """The split of an export line, recomputed from its group key (never trusted from the file)."""
    return cl.split_of(cl.split_group(e.get("refs"), e["content_sha256"]))


def items_from(export_lines: list[dict], finals: dict[str, Any], *, split: str = "held_out") -> list[Item]:
    """Items for every exported claim of `split` ("all" pools both: exploration only) that has a final violation /
    ok label (FinalLabel objects or dicts)."""
    if split not in SPLIT_CHOICES:
        raise R1Refused(f"split must be one of {SPLIT_CHOICES}, got {split!r}")
    out = []
    for e in export_lines:
        line_split = split_of_line(e)
        if split != "all" and line_split != split:
            continue
        f = finals.get(e["claim_hash"])
        if f is None:
            continue
        label = getattr(f, "label", None) or (f.get("label") if isinstance(f, dict) else None)
        cls = getattr(f, "claim_class", None) or (f.get("claim_class") if isinstance(f, dict) else None)
        if label not in ("violation", "ok"):
            continue
        out.append(Item(e["claim_hash"], e["writer"], cls, label, e.get("stratum") or "representative",
                        e["content_sha256"], e["field"], e["claim_text"], e.get("field_text") or e["claim_text"],
                        e.get("source_text") or "",
                        tuple(e.get("evidence") or cl.align(e["claim_text"], e.get("source_text") or "")),
                        line_split))
    return out


# ── prompts ────────────────────────────────────────────────────────────────────────────────────────────────────


def entailment_prompt(item: Item) -> str:
    """The v1 entailment user prompt (services/ground_truth/prompts.py has every version)."""
    return pr.v1_entailment_user(item)


def extraction_prompt(items: list[Item]) -> str:
    return pr.extraction_user(items)


def calls(items: list[Item], inst: Instrument) -> list[tuple[str, list[Item]]]:
    """(user prompt, the items that prompt answers) per call of one vote, in the instrument's prompt version."""
    pv = inst.prompt_version
    if inst.mode == "entailment":
        return [(pv.entailment_user(i), [i]) for i in items]
    groups: dict[str, list[Item]] = defaultdict(list)
    for i in items:
        groups[i.object_key].append(i)
    return [(pv.extraction_user(g), g) for _k, g in sorted(groups.items())]


# ── plan / cost ────────────────────────────────────────────────────────────────────────────────────────────────


def _price(model: str) -> tuple[float, float]:
    if model not in PRICES:
        raise R1Refused(f"no price for {model!r}: add the catalog price to r1.PRICES first")
    return PRICES[model]


def call_cost(inst: Instrument, user_prompt: str, out_tokens: int) -> float:
    pin, pout = _price(inst.model_key)
    system = inst.prompt_version.system(inst.mode)
    tokens_in = (len(system) + len(user_prompt)) / CHARS_PER_TOKEN
    return (tokens_in * pin + out_tokens * pout) / 1e6


def ceiling(model_key: str) -> int:
    """The output ceiling the gateway sends: MAX_TOKENS raised to the model's reasoning floor (never lowered)."""
    return llm_tokens.floored(MAX_TOKENS, model_key)


def retry_ceiling(model_key: str) -> int:
    """The ceiling of the gateway's one truncation retry."""
    return llm_tokens.retry_budget(ceiling(model_key))


def expected_out(inst: Instrument) -> int:
    """Expected output tokens of one reply: the answer plus the reasoning a reasoning model bills inside it."""
    reasoning = EXPECTED_REASONING if llm_tokens.reasoning_capable(inst.model_key) else 0
    return EXPECTED_OUT[inst.mode] + reasoning


def worst_attempt_cost(inst: Instrument, user_prompt: str) -> float:
    """The most one provider attempt of this call can bill (the retry ceiling, every token of it output)."""
    return call_cost(inst, user_prompt, retry_ceiling(inst.model_key))


def worst_call_cost(inst: Instrument, user_prompt: str, attempts: int = 1) -> float:
    """The most one gateway request of this call can bill: per attempt, the first try cut at its floored ceiling plus
    the one truncation retry at the retry ceiling; times the attempts the request may make (route_attempts)."""
    one = (call_cost(inst, user_prompt, ceiling(inst.model_key))
           + call_cost(inst, user_prompt, retry_ceiling(inst.model_key)))
    return max(1, int(attempts)) * one


def route_attempts(inst: Instrument) -> int:
    """The most provider attempts one pinned request of `inst` can make: a model_tier request runs that one model (or
    its alias's models), and the gateway walks every enabled deployment of it in priority order (the pinned
    connection first, the others after it). At least 1 (an empty registry: the gateway's single-provider path)."""
    from shared.llm_client import deployments

    try:
        models = deployments.alias_targets_sync(inst.model_key) or (inst.model_key,)
        n = sum(len(deployments.deployments_for_sync(m)) for m in models)
    except Exception:                                        # noqa: BLE001 — never raises (registry unreadable)
        n = 0
    if n <= 0:
        # Registry unreadable or empty: the gateway may still walk up to its own attempt cap once it reloads, so the
        # gate reserves that cap rather than one attempt (S272 review).
        from shared.llm_client.errors import MAX_CHAIN_ATTEMPTS
        return int(MAX_CHAIN_ATTEMPTS)
    return n


def _num(v: Any) -> Optional[float]:
    """A finite number, else None (a NaN or inf cost is no cost: it takes the worst-case path)."""
    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)):
        return float(v)
    return None


def billed_cost(inst: Instrument, user_prompt: str, resp: Any) -> tuple[float, list[str]]:
    """What one reply is charged against the cap, and the notes of every worst-case stand-in used:
    cost_usd (worst case of the call's attempts when missing, non-finite or <= 0) + truncation_cut_cost_usd when the
    reply is the gateway's retry (the first try's worst case when it gives none, 0 for a provider's refusal to return
    a cut reply, which bills nothing) + one attempt's worst case per earlier attempt its attempt_no reports."""
    notes: list[str] = []
    cost = _num(getattr(resp, "cost_usd", None))
    attempt = getattr(resp, "attempt_no", None)
    earlier = attempt - 1 if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt > 1 else 0
    if cost is None or cost <= 0:
        return worst_call_cost(inst, user_prompt, earlier + 1), ["no_cost_worst_case"]
    total = cost
    cut = _num(getattr(resp, "truncation_cut_cost_usd", None))
    retried = getattr(resp, "truncation_retry_max_tokens", None)
    reason = getattr(resp, "truncation_retry_reason", None)
    if cut is not None and cut > 0:
        total += cut
    elif isinstance(retried, int) and not isinstance(retried, bool) and retried > 0 and reason != "truncated_refusal":
        total += call_cost(inst, user_prompt, ceiling(inst.model_key))
        notes.append("cut_cost_worst_case")
    if earlier:
        total += worst_call_cost(inst, user_prompt, earlier)
        notes.append(f"earlier_attempts_worst_case:{earlier}")
    return total, notes


def plan(items: list[Item], insts: list[Instrument], *, votes: int = VOTES, cap: float = HARD_CAP_USD,
         attempts_of: Optional[Callable[[Instrument], int]] = None) -> dict:
    """The DRY RUN plan: calls and estimated cost per instrument, and whether the whole run fits the cap. The worst
    case reserves `attempts_of(inst)` attempts per call (default 1: a DRY RUN reads no registry; the live run gates on
    route_attempts)."""
    if not items:
        raise R1Refused("no labelled violation / ok claims to calibrate on")
    if not 0 < cap <= HARD_CAP_USD:
        raise R1Refused(f"cap must be in (0, {HARD_CAP_USD}] USD, got {cap}")
    rows = []
    for inst in insts:
        cs = calls(items, inst)
        attempts = attempts_of(inst) if attempts_of else 1
        expected = sum(call_cost(inst, u, expected_out(inst)) for u, _ in cs) * votes
        worst = sum(worst_call_cost(inst, u, attempts) for u, _ in cs) * votes
        rows.append({"instrument": inst.id, "calls": len(cs) * votes, "expected_usd": round(expected, 4),
                     "reserved_attempts": attempts,
                     "expected_out_tokens": expected_out(inst), "ceiling": ceiling(inst.model_key),
                     "retry_ceiling": retry_ceiling(inst.model_key), "worst_case_usd": round(worst, 4)})
    total = round(sum(r["expected_usd"] for r in rows), 4)
    by_class = Counter((i.claim_class, i.label) for i in items)
    return {"stage": STAGE, "role": ROLE, "votes": votes, "max_tokens": MAX_TOKENS, "items": len(items),
            "split": _split_name(items), "splits": dict(Counter(i.split for i in items)),
            "objects": len({i.object_key for i in items}),
            "labels": {f"{c}:{lab}": n for (c, lab), n in sorted(by_class.items())},
            "pool_positives": pl.positives((i.writer, i.claim_class) for i in items if i.label == "violation"),
            "instruments": rows, "expected_total_usd": total, "cap_usd": cap,
            "fits_cap": total * SAFETY <= cap,
            "worst_case_total_usd": round(sum(r["worst_case_usd"] for r in rows), 4)}


# ── live calls ─────────────────────────────────────────────────────────────────────────────────────────────────


class Budget:
    """The hard cap: `charged` = what this run charged itself (billed_cost / worst cases), `ledger` = the last ledger
    probe reading; the spend is the larger. A call is allowed only when spend + its worst case <= cap. A non-finite
    ledger reading (a NaN cost row makes the SUM NaN) closes the budget: no further call."""

    def __init__(self, cap: float):
        if not 0 < cap <= HARD_CAP_USD:
            raise R1Refused(f"cap must be in (0, {HARD_CAP_USD}] USD, got {cap}")
        self.cap, self.charged, self.calls = cap, 0.0, 0
        self.ledger: Optional[float] = None
        self.ledger_unreadable = False

    @property
    def spent(self) -> float:
        return max(self.charged, self.ledger or 0.0)

    def allows(self, worst: float) -> bool:
        return not self.ledger_unreadable and math.isfinite(worst) and self.spent + worst <= self.cap

    def charge(self, amount: Any, worst: float) -> None:
        """Charge one call: `amount`, or the call's `worst` case when the amount is not a finite number."""
        v = _num(amount)
        self.charged += max(0.0, v if v is not None else float(worst))
        self.calls += 1

    def observe_ledger(self, value: Any) -> None:
        if value is None:
            return                                           # the probe could not read the DB: own charges stand
        v = _num(value)
        if v is None:
            self.ledger_unreadable = True                    # NaN / inf / not a number: stop, never ignore
            return
        if v >= 0:
            self.ledger = max(self.ledger or 0.0, v)


def gateway_call(inst: Instrument, system: str, user: str, schema: dict) -> Any:
    """One pinned call through the Model Gateway (the pinned connection first, as eval_judge does)."""
    from shared.llm_client import deployments
    from shared.llm_client.client import LLMClient
    from shared.llm_client.models import LLMRequest

    request = LLMRequest(system_prompt=system, user_prompt=user, stage=STAGE, role=ROLE, model_tier=inst.model_key,
                         max_tokens=MAX_TOKENS, temperature=0.0, seed=SEED, json_schema=schema)
    with deployments.priority_override({inst.model_key: {inst.connection: -1_000_000}}):
        return LLMClient().generate(request)


def _answered_connection(resp: Any) -> Optional[str]:
    """The connection that answered: the vendor connection key (or 'acc2' for a task-role Bedrock deployment), else
    the satellite account (the same reading as the eval judge's pin check; that module stays R2-only)."""
    for attr in ("connection_key", "satellite_account"):
        v = getattr(resp, attr, None)
        if isinstance(v, str) and v:
            return v
    return None


def _identity_ok(inst: Instrument, resp: Any) -> bool:
    from shared.llm_client.family_rule import model_key_of

    return (model_key_of(str(getattr(resp, "model_used", ""))) == model_key_of(inst.model_key)
            and _answered_connection(resp) == inst.connection)


def _log(resp: Any, signal: dict, parsed: Optional[dict], error: Optional[Exception]) -> None:
    from shared.llm_client.call_log import billed_fields, record_call_sync, record_unusable_sync

    if error is not None:
        record_unusable_sync(resp, stage=STAGE, role=ROLE, error=error, quality_signal_extra=signal)
        return
    record_call_sync(
        stage=STAGE, role=ROLE, model=resp.model_used, tokens_in=getattr(resp, "input_tokens", None),
        tokens_out=getattr(resp, "output_tokens", None), cost_usd=getattr(resp, "cost_usd", None),
        quality_signal={**signal, "json_parsed": parsed is not None}, stop_reason=getattr(resp, "stop_reason", None),
        account=getattr(resp, "satellite_account", None), fallback_used=getattr(resp, "fallback_used", None),
        provider=resp.provider if isinstance(getattr(resp, "provider", None), str) else None,
        **billed_fields(resp, prompt_version=signal.get("prompt_version")))


def _overlaps(quote: str, claim: str) -> bool:
    q, c = cl.normalise(quote), cl.normalise(claim)
    if not q or not c:
        return False
    if q in c or c in q:
        return True
    qw, cw = set(q.split()), set(c.split())
    return len(qw & cw) >= max(3, int(0.6 * min(len(qw), len(cw))))


def flags_from_reply(inst: Instrument, parsed: dict, group: list[Item]) -> dict[str, bool]:
    """claim_hash -> flagged by this reply. Extraction: a quote counts only when it is in the field text (confirm in
    code) and it flags the labelled claims it overlaps."""
    if inst.mode == "entailment":
        return {group[0].claim_hash: parsed.get("verdict") == "unsupported"}
    text = cl.normalise(group[0].field_text)
    quotes = [str(c.get("quote") or "") for c in parsed.get("claims") or [] if isinstance(c, dict)]
    confirmed = [q for q in quotes if q.strip() and cl.normalise(q) in text]
    return {i.claim_hash: any(_overlaps(q, i.claim_text) for q in confirmed) for i in group}


Caller = Callable[[Instrument, str, str, dict], Any]


SpendProbe = Callable[[], Optional[float]]

LEDGER_SQL = ("SELECT COALESCE(SUM(cost_usd), 0)::float8 FROM shared.llm_call_log "
              "WHERE stage = $1 AND created_at >= $2")


def ledger_probe(since: Any, stage: str = STAGE) -> SpendProbe:
    """The ledger spend of `stage` since `since` (every row: the replies, the gateway's cut-attempt and failed rung
    rows; another run on the stage at the same time only makes it larger). None when the DB cannot be read: the run
    then keeps its own charges, which already count every attempt at its worst case. (Moved here from
    scripts/aa1048/r1_calibrate.py in S279 so the image, which has no scripts/, can use it.)"""
    def probe() -> Optional[float]:
        import asyncio

        async def _q() -> float:
            import asyncpg

            from shared.secrets import get_database_url

            conn = await asyncpg.connect(get_database_url().strip(), ssl="require")
            try:
                return float(await conn.fetchval(LEDGER_SQL, stage, since))
            finally:
                await conn.close()
        try:
            return asyncio.run(_q())
        except Exception:                                    # noqa: BLE001 — the probe can only raise the spend
            return None
    return probe


def run(items: list[Item], insts: list[Instrument], *, cap: float, votes: int = VOTES, run_id: str,
        caller: Optional[Caller] = None, log: Callable[[Any, dict, Optional[dict], Optional[Exception]], None] = _log,
        spend_probe: Optional[SpendProbe] = None,
        attempts_of: Optional[Callable[[Instrument], int]] = None) -> dict:
    """The live run: votes per item and instrument (None = no vote: identity mismatch, unusable reply, a caller
    error or the cap). Never raises on a caller error: it is charged the worst case and stops the run (the votes so far
    stay, `stopped` says why). `attempts_of` gives the attempts reserved per call (default: route_attempts on the
    Model Gateway, 1 for an injected caller). Returns {"votes": {instrument id: {claim_hash: [True|False|None, ...]}},
    "spent_usd", "charged_usd", "ledger_usd", "calls", "stopped", "identity_mismatches", "caller_errors",
    "worst_case_charges", "reserved_attempts"}."""
    if attempts_of is None:
        attempts_of = route_attempts if caller is None else (lambda _inst: 1)
    caller = caller or gateway_call
    budget = Budget(cap)
    reserved = {inst.id: max(1, int(attempts_of(inst))) for inst in insts}
    out: dict[str, dict[str, list]] = {i.id: defaultdict(list) for i in insts}
    stopped = None
    mismatches = Counter()
    errors: list[dict] = []
    worst_notes = Counter()
    for inst in insts:
        system = inst.prompt_version.system(inst.mode)
        schema = ENTAILMENT_SCHEMA if inst.mode == "entailment" else EXTRACTION_SCHEMA
        for vote in range(votes):
            for user, group in calls(items, inst):
                worst = worst_call_cost(inst, user, reserved[inst.id])
                if not stopped and spend_probe is not None:
                    try:
                        budget.observe_ledger(spend_probe())
                    except Exception:                       # noqa: BLE001 — the probe only ever raises the spend
                        pass
                if not stopped and budget.ledger_unreadable:
                    stopped = f"ledger spend reading is not a finite number, after {budget.calls} calls"
                if stopped or not budget.allows(worst):
                    stopped = stopped or (f"cap {cap} USD: spent {budget.spent:.4f} + the next call's worst case "
                                          f"{worst:.4f} would pass it, after {budget.calls} calls")
                    for i in group:
                        out[inst.id][i.claim_hash].append(None)
                    continue
                try:
                    resp = caller(inst, system, user, schema)
                except Exception as e:                       # noqa: BLE001 — paid attempts may have run: stop clean
                    budget.charge(worst, worst)
                    worst_notes["caller_error_worst_case"] += 1
                    errors.append({"instrument": inst.id, "vote": vote, "error": type(e).__name__,
                                   "detail": str(e)[:200]})
                    stopped = f"caller error after {budget.calls} calls ({type(e).__name__}): stopped, report partial"
                    for i in group:
                        out[inst.id][i.claim_hash].append(None)
                    continue
                amount, notes = billed_cost(inst, user, resp)
                budget.charge(amount, worst)
                worst_notes.update(n.split(":", 1)[0] for n in notes)
                attempt = getattr(resp, "attempt_no", None)
                if (not stopped and isinstance(attempt, int) and not isinstance(attempt, bool)
                        and attempt > reserved[inst.id]):
                    stopped = (f"reply attempt_no {attempt} passed the {reserved[inst.id]} reserved attempt(s) of "
                               f"{inst.id}, after {budget.calls} calls: stopped, report partial")
                signal = {"app": "cis", "issue": "AA-1048", "run_id": run_id, "instrument": inst.id, "vote": vote,
                          "prompt_version": inst.prompt_version.tag, "claims": [i.claim_hash[:16] for i in group][:20]}
                try:
                    parsed = json.loads(getattr(resp, "content", "") or "")
                    if not isinstance(parsed, dict):
                        raise ValueError("reply is not a JSON object")
                except Exception as e:                       # noqa: BLE001 — logged as billed_unusable, no vote
                    log(resp, signal, None, e)
                    for i in group:
                        out[inst.id][i.claim_hash].append(None)
                    continue
                log(resp, signal, parsed, None)
                if not _identity_ok(inst, resp):
                    mismatches[inst.id] += 1
                    for i in group:
                        out[inst.id][i.claim_hash].append(None)
                    continue
                for h, flagged in flags_from_reply(inst, parsed, group).items():
                    out[inst.id][h].append(flagged)
    return {"votes": {k: dict(v) for k, v in out.items()}, "spent_usd": round(budget.spent, 6),
            "charged_usd": round(budget.charged, 6),
            "ledger_usd": round(budget.ledger, 6) if budget.ledger is not None else None,
            "calls": budget.calls, "stopped": stopped, "identity_mismatches": dict(mismatches),
            "caller_errors": errors, "worst_case_charges": dict(worst_notes), "reserved_attempts": reserved}


# ── measurement ────────────────────────────────────────────────────────────────────────────────────────────────


def majority(votes: list) -> Optional[bool]:
    """The verdict: True / False when at least len(votes) // 2 + 1 real votes agree (2 of 3), else None (unverified:
    too few real votes, or a tie such as [True, False, None])."""
    need = len(votes) // 2 + 1
    c = Counter(v for v in votes if v is not None)
    for verdict in (True, False):
        if c[verdict] >= need:
            return verdict
    return None


def _split_name(items: list[Item]) -> str:
    names = {i.split for i in items}
    return names.pop() if len(names) == 1 else "mixed"


def _precision(tp: int, fired: int, bar: Any) -> dict:
    lb = ss.wilson_lower(tp, fired) if fired else None
    return {"fired": fired, "tp": tp, "precision": round(tp / fired, 4) if fired else None,
            "precision_lb": round(lb, 4) if lb is not None else None,
            "precision_passes_bar": bool(fired and tp / fired >= bar.precision and lb is not None
                                         and lb >= bar.lower_bound)}


def _pool_records(pools: dict[str, pl.Pool], writers: dict[str, dict], per_writer: dict, pos: Counter, tp: Counter,
                  bar: Any) -> dict:
    """One entry per declared pool: the pool's n_pos / tp / recall per writer it names (members summed), the
    writers it passes on (writer precision bar, pool n_pos >= bar.n_pos, >= 1 caught), and per member its own
    n_pos / tp per writer and whether it reaches the bar alone (split later)."""
    out = {}
    for pid, p in sorted(pools.items()):
        ws = [w for w in sorted(per_writer) if w in p.writers]
        n_pos = {w: sum(pos[(w, m)] for m in p.members) for w in ws}
        caught = {w: sum(tp[(w, m)] for m in p.members) for w in ws}
        passing = sorted(w for w in ws if writers[w]["precision_passes_bar"] and n_pos[w] >= bar.n_pos
                         and caught[w] >= 1)
        members = {m: {"n_pos_by_writer": {w: pos[(w, m)] for w in ws if pos[(w, m)]},
                       "tp_by_writer": {w: tp[(w, m)] for w in ws if pos[(w, m)]}} for m in sorted(p.members)}
        out[pid] = {**p.declaration(), "n_pos_by_writer": n_pos, "tp_by_writer": caught,
                    "recall_by_writer": {w: round(caught[w] / n_pos[w], 4) for w in ws if n_pos[w]},
                    "passing_writers": passing, "passes_bar": bool(passing),
                    "members_detail": members,
                    "split_ready": sorted(f"{w}:{m}" for m in p.members for w in ws if pos[(w, m)] >= bar.n_pos)}
    return out


def measure(items: list[Item], votes: dict[str, list], *, pools: Optional[dict[str, pl.Pool]] = None) -> dict:
    """The measurement record of one instrument. Precision and its Wilson lower bound are taken over EVERY fire on one
    writer surface (tp = fires on violations of any class of that writer, fired = every fire on it): `writers` holds
    them per writer, the top-level precision pools every writer (reading only). Per class: n_pos / tp / recall (and the
    class-local precision, reading only), and `passing_writers`: the writers whose precision clears the bar, when the
    writer's own n_pos for the class reaches the bar and it has at least one caught violation of it. Also test-retest
    agreement
    (over items with >= 2 real votes), hard-subset recall, ok-cleared rate and the unverified count. An unverified item
    (no agreeing majority of real votes) is a miss on a violation and not cleared on an ok claim. `split` names the
    items' split; only a held-out record lists passing classes ("<writer>:<class>"; a train or mixed one is
    exploration: the bar columns still show, `passing_classes` stays empty). `pools` (default pools.POOLS): the
    declared pools, measured with the same bar (`pools`, `passing_pools`, `calibrated`: see the module doc)."""
    per_class: dict[str, Counter] = defaultdict(Counter)
    per_writer: dict[str, Counter] = defaultdict(Counter)
    tp_writer_class: Counter = Counter()
    pos_writer_class: Counter = Counter()
    retest_n = retest_same = unverified = 0
    unverified_by_label: Counter = Counter()
    hard_pos = hard_hit = ok_n = ok_cleared = 0
    for i in items:
        vs = votes.get(i.claim_hash) or []
        verdict = majority(vs)
        real = [v for v in vs if v is not None]
        if len(real) >= 2:
            retest_n += 1
            retest_same += len(set(real)) == 1
        if verdict is None:
            unverified += 1
            unverified_by_label[i.label] += 1
        flagged = verdict is True
        c = per_class[i.claim_class]
        w = per_writer[i.writer]
        c["n"] += 1
        w["fired"] += flagged
        if i.label == "violation":
            c["n_pos"] += 1
            c["tp"] += flagged
            w["tp"] += flagged
            tp_writer_class[(i.writer, i.claim_class)] += flagged
            pos_writer_class[(i.writer, i.claim_class)] += 1
            if i.stratum == "hard":
                hard_pos += 1
                hard_hit += flagged
        else:
            c["fp"] += flagged
            ok_n += 1
            ok_cleared += verdict is False
    bar = ss.BARS[0]
    tp_all = sum(c["tp"] for c in per_class.values())
    fired_all = tp_all + sum(c["fp"] for c in per_class.values())
    pooled = _precision(tp_all, fired_all, bar)
    writers = {wr: _precision(w["tp"], w["fired"], bar) for wr, w in sorted(per_writer.items())}
    classes = {}
    for cls, c in sorted(per_class.items()):
        fired = c["tp"] + c["fp"]
        lb = ss.wilson_lower(c["tp"], fired) if fired else None
        ok_writers = sorted(wr for wr, rec in writers.items()
                            if rec["precision_passes_bar"] and pos_writer_class[(wr, cls)] >= bar.n_pos
                            and tp_writer_class[(wr, cls)] >= 1)
        classes[cls] = {"n": c["n"], "n_pos": c["n_pos"], "tp": c["tp"], "fp": c["fp"],
                        "precision": round(c["tp"] / fired, 4) if fired else None,
                        "precision_lb": round(lb, 4) if lb is not None else None,
                        "recall": round(c["tp"] / c["n_pos"], 4) if c["n_pos"] else None,
                        "n_pos_by_writer": {wr: pos_writer_class[(wr, cls)] for wr in sorted(per_writer)
                                            if pos_writer_class[(wr, cls)]},
                        "recall_by_writer": {wr: round(tp_writer_class[(wr, cls)] / pos_writer_class[(wr, cls)], 4)
                                             for wr in sorted(per_writer) if pos_writer_class[(wr, cls)]},
                        "passing_writers": ok_writers, "passes_bar": bool(ok_writers)}
    retest = round(retest_same / retest_n, 4) if retest_n else None
    ok_rate = round(ok_cleared / ok_n, 4) if ok_n else None
    hard = {"positives": hard_pos, "caught": hard_hit, "passes": hard_pos >= HARD_MIN and hard_hit == hard_pos}
    passing = sorted(f"{wr}:{k}" for k, v in classes.items() for wr in v["passing_writers"])
    unverified_rate = round(unverified / len(items), 4) if items else None
    instrument_ok = (retest is not None and retest >= RETEST_BAR and ok_rate is not None and ok_rate >= OK_CLEARED_BAR
                     and hard["passes"] and unverified_rate is not None and unverified_rate <= UNVERIFIED_MAX)
    split = _split_name(items)
    declared = pl.declared(pools)
    pool_recs = _pool_records(declared, writers, per_writer, pos_writer_class, tp_writer_class, bar)
    decides = instrument_ok and split == "held_out"
    rec = {"split": split, **pooled, "writers": writers,
           "classes": classes, "retest": retest, "ok_cleared": ok_rate, "hard_subset": hard,
           "unverified": unverified, "unverified_by_label": dict(unverified_by_label),
           "unverified_rate": unverified_rate,
           "passing_classes": passing if decides else [],
           "pools": pool_recs, "pools_sha": pl.pools_sha(declared),
           "passing_pools": sorted(f"{w}:{pid}" for pid, v in pool_recs.items() for w in v["passing_writers"])
           if decides else [],
           "instrument_conditions_met": instrument_ok}
    rec["calibrated"] = pl.calibrated_keys(rec)
    return rec


def report(items: list[Item], run_result: dict, insts: list[Instrument], *,
           pools: Optional[dict[str, pl.Pool]] = None) -> dict:
    """One measurement record per instrument and the recommendation (the instrument that calibrates the most
    "<writer>:<class>" keys on the held-out split: passing classes plus the members of passing pools, then the id);
    the tech lead pins it (D7), this code never does. A train or mixed run recommends nothing."""
    records = {i.id: measure(items, run_result["votes"].get(i.id) or {}, pools=pools) for i in insts}
    best = sorted(records.items(), key=lambda kv: (-len(kv[1]["calibrated"]), kv[0]))
    rec = best[0][0] if best and best[0][1]["calibrated"] else None
    return {"split": _split_name(items), "records": records, "recommended": rec,
            "pools_sha": pl.pools_sha(pools),
            "spent_usd": run_result.get("spent_usd"), "charged_usd": run_result.get("charged_usd"),
            "ledger_usd": run_result.get("ledger_usd"), "partial": bool(run_result.get("stopped")),
            "stopped": run_result.get("stopped"), "identity_mismatches": run_result.get("identity_mismatches"),
            "caller_errors": run_result.get("caller_errors"),
            "worst_case_charges": run_result.get("worst_case_charges"),
            "reserved_attempts": run_result.get("reserved_attempts")}


__all__ = ["DEFAULT_CANDIDATES", "ENTAILMENT_SYSTEM", "EXTRACTION_SYSTEM", "HARD_CAP_USD", "Instrument", "Item",
           "MAX_TOKENS", "MODES", "PRICES", "R1Refused", "SPLIT_CHOICES", "STAGE", "UNVERIFIED_MAX", "VOTES",
           "LEDGER_SQL", "billed_cost", "calls", "ceiling", "expected_out", "flags_from_reply", "instruments",
           "items_from", "ledger_probe",
           "majority", "measure", "plan", "report", "retry_ceiling", "route_attempts", "run", "split_of_line",
           "worst_call_cost"]
