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

Cost. DRY RUN is the default: `plan` counts the calls and estimates the cost from the catalog prices below (expected
reply size and the worst case at MAX_TOKENS). A live run needs an explicit flag and a cap <= HARD_CAP_USD; it stops
before a call when the spend so far plus twice that call's expected cost would pass the cap. Every reply goes through
the Model Gateway (LLMClient.generate) on stage STAGE, role judge, and writes one shared.llm_call_log row with
**billed_fields(resp) (an unusable reply writes its billed_unusable row before the error is raised).
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from services.ground_truth import claims as cl
from services.ground_truth import policy
from services.ground_truth import sample_size as ss

STAGE = "adhoc_aa1048"
ROLE = "judge"
HARD_CAP_USD = 2.0
VOTES = 3
MAX_TOKENS = 8192
SEED = 1048
MODES = ("entailment", "extraction")
CHARS_PER_TOKEN = 3.0                    # conservative (English is about 4): over-estimates the prompt side
EXPECTED_OUT = {"entailment": 150, "extraction": 900}
SAFETY = 2.0                             # a call is started only when spent + SAFETY x its expected cost <= cap
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

_RULES = (
    "A claim about THIS trip (an activity, a service, a meal, a time, a stay, a view, a number, a name, a day) must be "
    "stated by the source; otherwise it is unsupported.",
    "Background about a place the source names (history, geography, culture) is allowed, but it never promises the "
    "trip's activities, services, times or views.",
    "Reflection or general feeling that states no fact about the trip is voice, not a claim.",
    "Hypothetical framing ('you might', 'imagine') that names a concrete trip activity, service, time or view is a "
    "promise and needs the source.",
    "Suitability, safety, pace, rarity or uniqueness of the trip only as the source states it.",
)
ENTAILMENT_SYSTEM = (
    "You check ONE claim from a travel text against the SOURCE LINES the trip is built from.\n"
    + "\n".join(f"- {r}" for r in _RULES)
    + "\nAnswer 'supported' only when the source lines state what the claim says about the trip. Quote the source "
      "line you relied on (empty when unsupported). Reply with the JSON object only.")
EXTRACTION_SYSTEM = (
    "You read a travel TEXT and the SOURCE it was written from, and list every claim in the TEXT that the SOURCE "
    "does not support.\n" + "\n".join(f"- {r}" for r in _RULES)
    + "\nQuote each claim exactly as it appears in the TEXT (a sentence or a clause), name its class from the list "
      "given, and say why in one sentence. List nothing that the source supports. Reply with the JSON object only.")


class R1Refused(ValueError):
    """The run cannot start (price unknown, cap over the hard cap, no items): no call is made."""


@dataclass(frozen=True)
class Instrument:
    model_key: str
    connection: str
    mode: str

    @property
    def id(self) -> str:
        return f"{self.model_key}@{self.connection}:{self.mode}"


def instruments(candidates: Iterable[tuple[str, str]] = DEFAULT_CANDIDATES,
                modes: Iterable[str] = MODES) -> list[Instrument]:
    out = []
    for model, conn in candidates:
        for mode in modes:
            if mode not in MODES:
                raise R1Refused(f"unknown mode {mode!r}")
            out.append(Instrument(model, conn, mode))
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

    @property
    def object_key(self) -> str:
        return f"{self.content_sha256}:{self.field}"


def items_from(export_lines: list[dict], finals: dict[str, Any]) -> list[Item]:
    """Items for every exported claim that has a final violation / ok label (FinalLabel objects or dicts)."""
    out = []
    for e in export_lines:
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
                        tuple(e.get("evidence") or cl.align(e["claim_text"], e.get("source_text") or ""))))
    return out


# ── prompts ────────────────────────────────────────────────────────────────────────────────────────────────────


def entailment_prompt(item: Item) -> str:
    lines = "\n".join(f"- {ln}" for ln in item.evidence) or "- (no source line shares a word with the claim)"
    return f"SOURCE LINES:\n{lines}\n\nCLAIM ({item.field}):\n{item.claim_text}"


def extraction_prompt(items: list[Item]) -> str:
    first = items[0]
    classes = sorted({c.id for c in policy.policy_classes().values() if first.writer in c.writers})
    return (f"CLASSES: {', '.join(classes)}\n\nSOURCE:\n{first.source_text}\n\nTEXT ({first.field}):\n"
            f"{first.field_text}")


def calls(items: list[Item], inst: Instrument) -> list[tuple[str, list[Item]]]:
    """(user prompt, the items that prompt answers) per call of one vote."""
    if inst.mode == "entailment":
        return [(entailment_prompt(i), [i]) for i in items]
    groups: dict[str, list[Item]] = defaultdict(list)
    for i in items:
        groups[i.object_key].append(i)
    return [(extraction_prompt(g), g) for _k, g in sorted(groups.items())]


# ── plan / cost ────────────────────────────────────────────────────────────────────────────────────────────────


def _price(model: str) -> tuple[float, float]:
    if model not in PRICES:
        raise R1Refused(f"no price for {model!r}: add the catalog price to r1.PRICES first")
    return PRICES[model]


def call_cost(inst: Instrument, user_prompt: str, out_tokens: int) -> float:
    pin, pout = _price(inst.model_key)
    system = ENTAILMENT_SYSTEM if inst.mode == "entailment" else EXTRACTION_SYSTEM
    tokens_in = (len(system) + len(user_prompt)) / CHARS_PER_TOKEN
    return (tokens_in * pin + out_tokens * pout) / 1e6


def plan(items: list[Item], insts: list[Instrument], *, votes: int = VOTES, cap: float = HARD_CAP_USD) -> dict:
    """The DRY RUN plan: calls and estimated cost per instrument, and whether the whole run fits the cap."""
    if not items:
        raise R1Refused("no labelled violation / ok claims to calibrate on")
    if not 0 < cap <= HARD_CAP_USD:
        raise R1Refused(f"cap must be in (0, {HARD_CAP_USD}] USD, got {cap}")
    rows = []
    for inst in insts:
        cs = calls(items, inst)
        expected = sum(call_cost(inst, u, EXPECTED_OUT[inst.mode]) for u, _ in cs) * votes
        worst = sum(call_cost(inst, u, MAX_TOKENS) for u, _ in cs) * votes
        rows.append({"instrument": inst.id, "calls": len(cs) * votes, "expected_usd": round(expected, 4),
                     "worst_case_usd": round(worst, 4)})
    total = round(sum(r["expected_usd"] for r in rows), 4)
    by_class = Counter((i.claim_class, i.label) for i in items)
    return {"stage": STAGE, "role": ROLE, "votes": votes, "max_tokens": MAX_TOKENS, "items": len(items),
            "objects": len({i.object_key for i in items}),
            "labels": {f"{c}:{lab}": n for (c, lab), n in sorted(by_class.items())},
            "instruments": rows, "expected_total_usd": total, "cap_usd": cap,
            "fits_cap": total * SAFETY <= cap,
            "worst_case_total_usd": round(sum(r["worst_case_usd"] for r in rows), 4)}


# ── live calls ─────────────────────────────────────────────────────────────────────────────────────────────────


class Budget:
    def __init__(self, cap: float):
        if not 0 < cap <= HARD_CAP_USD:
            raise R1Refused(f"cap must be in (0, {HARD_CAP_USD}] USD, got {cap}")
        self.cap, self.spent, self.calls = cap, 0.0, 0

    def allows(self, expected: float) -> bool:
        return self.spent + SAFETY * expected <= self.cap

    def charge(self, cost: Optional[float], expected: float) -> None:
        self.spent += cost if isinstance(cost, (int, float)) and cost > 0 else expected
        self.calls += 1


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
        provider=resp.provider if isinstance(getattr(resp, "provider", None), str) else None, **billed_fields(resp))


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


def run(items: list[Item], insts: list[Instrument], *, cap: float, votes: int = VOTES, run_id: str,
        caller: Optional[Caller] = None, log: Callable[[Any, dict, Optional[dict], Optional[Exception]], None] = _log
        ) -> dict:
    """The live run: votes per item and instrument (None = no vote: identity mismatch, unusable reply or the cap).
    Returns {"votes": {instrument id: {claim_hash: [True|False|None, ...]}}, "spent_usd", "calls", "stopped"}."""
    caller = caller or gateway_call
    budget = Budget(cap)
    out: dict[str, dict[str, list]] = {i.id: defaultdict(list) for i in insts}
    stopped = None
    mismatches = Counter()
    for inst in insts:
        system = ENTAILMENT_SYSTEM if inst.mode == "entailment" else EXTRACTION_SYSTEM
        schema = ENTAILMENT_SCHEMA if inst.mode == "entailment" else EXTRACTION_SCHEMA
        for vote in range(votes):
            for user, group in calls(items, inst):
                expected = call_cost(inst, user, EXPECTED_OUT[inst.mode])
                if stopped or not budget.allows(expected):
                    stopped = stopped or f"cap {cap} USD reached after {budget.calls} calls"
                    for i in group:
                        out[inst.id][i.claim_hash].append(None)
                    continue
                resp = caller(inst, system, user, schema)
                budget.charge(getattr(resp, "cost_usd", None), expected)
                signal = {"app": "cis", "issue": "AA-1048", "run_id": run_id, "instrument": inst.id, "vote": vote,
                          "claims": [i.claim_hash[:16] for i in group][:20]}
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
            "calls": budget.calls, "stopped": stopped, "identity_mismatches": dict(mismatches)}


# ── measurement ────────────────────────────────────────────────────────────────────────────────────────────────


def majority(votes: list) -> Optional[bool]:
    real = [v for v in votes if v is not None]
    if len(real) < (len(votes) // 2 + 1):
        return None                                          # too few real votes: unverified
    c = Counter(real)
    return c[True] > c[False]


def measure(items: list[Item], votes: dict[str, list]) -> dict:
    """The measurement record of one instrument: per class precision / Wilson lower bound / recall / n_pos and the
    bar, test-retest agreement, hard-subset recall, ok-cleared rate and the unverified count."""
    per_class: dict[str, Counter] = defaultdict(Counter)
    retest_n = retest_same = unverified = 0
    hard_pos = hard_hit = ok_n = ok_cleared = 0
    for i in items:
        vs = votes.get(i.claim_hash) or []
        verdict = majority(vs)
        real = [v for v in vs if v is not None]
        if len(real) == len(vs) and vs:
            retest_n += 1
            retest_same += len(set(real)) == 1
        if verdict is None:
            unverified += 1
            continue
        c = per_class[i.claim_class]
        c["n"] += 1
        if i.label == "violation":
            c["n_pos"] += 1
            c["tp"] += verdict
            if i.stratum == "hard":
                hard_pos += 1
                hard_hit += verdict
        else:
            c["fp"] += verdict
            ok_n += 1
            ok_cleared += not verdict
    bar = ss.BARS[0]
    classes = {}
    for cls, c in sorted(per_class.items()):
        fired = c["tp"] + c["fp"]
        lb = ss.wilson_lower(c["tp"], fired) if fired else None
        classes[cls] = {"n": c["n"], "n_pos": c["n_pos"], "tp": c["tp"], "fp": c["fp"],
                        "precision": round(c["tp"] / fired, 4) if fired else None,
                        "precision_lb": round(lb, 4) if lb is not None else None,
                        "recall": round(c["tp"] / c["n_pos"], 4) if c["n_pos"] else None,
                        "passes_bar": ss.passes(c["tp"], fired, c["n_pos"], bar)}
    retest = round(retest_same / retest_n, 4) if retest_n else None
    ok_rate = round(ok_cleared / ok_n, 4) if ok_n else None
    hard = {"positives": hard_pos, "caught": hard_hit, "passes": hard_pos >= HARD_MIN and hard_hit == hard_pos}
    passing = sorted(k for k, v in classes.items() if v["passes_bar"])
    instrument_ok = (retest is not None and retest >= RETEST_BAR and ok_rate is not None and ok_rate >= OK_CLEARED_BAR
                     and hard["passes"])
    return {"classes": classes, "retest": retest, "ok_cleared": ok_rate, "hard_subset": hard,
            "unverified": unverified, "passing_classes": passing if instrument_ok else [],
            "instrument_conditions_met": instrument_ok}


def report(items: list[Item], run_result: dict, insts: list[Instrument]) -> dict:
    """One measurement record per instrument and the recommendation (the instrument with the most passing classes,
    then the lower spend); the tech lead pins it (D7), this code never does."""
    records = {i.id: measure(items, run_result["votes"].get(i.id) or {}) for i in insts}
    best = sorted(records.items(), key=lambda kv: (-len(kv[1]["passing_classes"]), kv[0]))
    rec = best[0][0] if best and best[0][1]["passing_classes"] else None
    return {"records": records, "recommended": rec, "spent_usd": run_result.get("spent_usd"),
            "stopped": run_result.get("stopped"), "identity_mismatches": run_result.get("identity_mismatches")}


__all__ = ["DEFAULT_CANDIDATES", "ENTAILMENT_SYSTEM", "EXTRACTION_SYSTEM", "HARD_CAP_USD", "Instrument", "Item",
           "MAX_TOKENS", "MODES", "PRICES", "R1Refused", "STAGE", "VOTES", "calls", "flags_from_reply",
           "instruments", "items_from", "majority", "measure", "plan", "report", "run"]
