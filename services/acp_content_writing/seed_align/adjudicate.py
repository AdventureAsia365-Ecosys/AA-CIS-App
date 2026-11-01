"""AA-1000 — T10 tier 2: an LLM adjudicator for the forms seed alignment (tier 1, AA-789) cannot reach.

Root plan docs/audits/2026-10-29-S257-aa789-design/PLAN.md, "Tier 2". Four classes, each under the gate tier 1 uses for
it (no new gate, S246 freeze):

- `naming_note_leak` (F13_leak): a paraphrased naming note ("a name locals find unfortunate");
- `paraphrased_repeat` (F13_repeat): a sentence with an implicit subject that retells an earlier one of its section;
- `faq_non_answer` (F13_repeat): the first sentence of an answer to an open WHAT / WHY / HOW question that does not
  address it;
- `scopeless_superlative` (F18_rarity): hype the seed does not make ("hard to overstate", "few places can offer").

How a sentence gets here. Tier 1 records what it found, not what it abstained on, so tier 2 re-reads the piece with a
deterministic trigger per class (regex + `SeedGraph`, no spaCy) and keeps only the sentences tier 1 left alone: tier 1
must have run this attempt (`status == "ok"`), must have run the class when it has a check for it, and must have no
candidate on the sentence. Each kept sentence goes to the model with a few seed lines (the excerpt).

The model (stage `t10_adjudicate`, role `judge`, through `LLMClient`, one `shared.llm_call_log` row per reply) answers
VIOLATION or OK per item, with two verbatim quotes: words of the sentence and words of a seed line. A VIOLATION is
dropped unless both quotes are found (piece quote in the sentence, seed quote in the excerpt it was sent and in the
seed). Any model error, an unusable reply or the deadline → nothing changes (fail open).

It never holds a piece. Modes, per class (env `T10_ADJ_MODE`, `T10_ADJ_MODE_<CLASS>`), default off:
- `off`: no trigger, no call; the gate ledger and the outcome are what they were.
- `shadow`: the call runs; verified findings are recorded only (`writer_input.seed_align[*].tier2`, next to tier 1's
  record of the same attempt), never in the ledger.
- `repair`: at most ONE verified finding per attempt becomes `sa:t2.<class>: <reason>: '<sentence>'` under its gate, so
  that gate's one-sentence repair rewrites or cuts that sentence. The `sa:` prefix sends it through tier 1's never-hold
  rule (`modes.finding_may_hold`): a `t2.` class is never hold-capable, whatever the env says.
Any other value (`hold` included) reads as off.

Imports nothing from quality_gates (invariant I1): this layer cannot build a gate result.
"""
from __future__ import annotations

import contextvars
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

import structlog

from services.acp_content_writing.hygiene import fold, sentence_spans, split_seed
from services.acp_content_writing.seed_align import checks as sa_checks
from services.acp_content_writing.seed_align import modes as sa_modes
from services.acp_content_writing.seed_align import seed_graph
from services.quality import registry as qr
from shared.llm_client.call_log import billed_fields, record_call_sync, record_unusable_sync
from shared.llm_client.json_reply import JsonReplyError, first_json_value
from shared.llm_client.models import LLMRequest

logger = structlog.get_logger()

STAGE = "t10_adjudicate"
ROLE = "judge"
MODE_ENV = "T10_ADJ_MODE"
OFF, SHADOW, REPAIR = "off", "shadow", "repair"
MODES = (OFF, SHADOW, REPAIR)
# A tier-2 finding reads `sa:t2.<class>: …`: the `sa:` prefix is tier 1's routing and never-hold path.
CLASS_TAG = "t2."

NAMING = sa_checks.NAMING_NOTE_LEAK
REPEAT = sa_checks.PARAPHRASED_REPEAT
FAQ = sa_checks.FAQ_NON_ANSWER
HYPE = sa_checks.SCOPELESS_SUPERLATIVE
CLASSES = (NAMING, REPEAT, FAQ, HYPE)
CLASS_GATE = {NAMING: qr.F13_LEAK, REPEAT: qr.F13_REPEAT, FAQ: qr.F13_REPEAT, HYPE: qr.F18_RARITY}
# The classes tier 1 has a check for: tier 2 looks at them only when tier 1 ran that class this attempt.
TIER1_BUILT = frozenset(c for c in CLASSES if c in sa_checks.BUILT_CLASSES)
# The reason the repair reads. Fixed text (never the model's words), with no ": '" in it (the finding is parsed as
# "<problem>: '<sentence>'").
REASONS = {
    NAMING: "the seed's naming note told as copy (tier 2) — use the name plainly without the note, or cut the sentence",
    REPEAT: "retells an earlier sentence of this section in other words (tier 2) — give one new source fact or cut it",
    FAQ: "does not answer its FAQ question (tier 2) — answer what the question asks from the source facts",
    HYPE: "a rarity or hype claim the source does not make (tier 2) — state the source fact plainly or cut the claim",
}
# The check name the model sees per class.
CHECK_NAME = {NAMING: "naming_note", REPEAT: "repeat", FAQ: "faq_answer", HYPE: "hype"}

MAX_ITEMS = 6           # sentences sent per attempt (one call); the rest wait for the next attempt or are dropped
MAX_TOKENS = 4000       # Luna reasons inside max_tokens (S246: an empty reply at 1500)
TIMEOUT_S = 60.0        # the whole call; on timeout nothing changes
MIN_QUOTE_WORDS = 3
SEED_LINES = 3          # seed lines per item (plus the Naming lines for a naming item)
SEED_LINE_CHARS = 400
SENTENCE_CHARS = 400
REPEAT_WINDOW = 6       # earlier body sentences of the same section a repeat is compared with

VIOLATION, OK_VERDICT = "VIOLATION", "OK"
ACTIONS = ("repair", "cut")


# ---------------------------------------------------------------- modes

def class_mode(cls: str) -> str:
    """The class's own env, else the global one, else off; anything but off / shadow / repair reads as off."""
    raw = os.environ.get(f"{MODE_ENV}_{cls.upper()}")
    if raw is None or not raw.strip():
        raw = os.environ.get(MODE_ENV, "")
    mode = raw.strip().lower()
    return mode if mode in MODES and cls in CLASSES else OFF


def active_modes() -> dict[str, str]:
    return {c: m for c in CLASSES if (m := class_mode(c)) != OFF}


def finding_text(cls: str, sentence: str) -> str:
    return f"{sa_modes.FINDING_PREFIX}{CLASS_TAG}{cls}: {REASONS[cls]}: '{sentence[:300]}'"


def tier2_only_failure(g: dict) -> bool:
    """Is legacy result `g` failed and blocking ONLY because of tier-2 findings? True when it carries an
    `sa:t2.…` finding, no tier-1 `sa:` finding, and its own findings (if any) did not block before `apply`
    (seed_align.modes.BASE_BLOCKING_KEY). The AA-528 regression guard leaves such a result out (service.py): tier 2
    is re-asked every attempt (not sticky) and defers triggers past MAX_ITEMS, so a finding that first shows on a
    later attempt is not a sign the rewrite broke anything — and the revert it would trigger could keep an earlier
    attempt's product-truth failure. The finding itself still gets its sentence repair; it never holds."""
    if g.get("passed", True) or not g.get("blocking", True):
        return False
    t2_prefix = f"{sa_modes.FINDING_PREFIX}{CLASS_TAG}"
    vs = [str(v) for v in g.get("violations") or []]
    if not any(v.startswith(t2_prefix) for v in vs):
        return False
    rest = [v for v in vs if not v.startswith(t2_prefix)]
    if any(v.startswith(sa_modes.FINDING_PREFIX) for v in rest):
        return False  # a tier-1 finding blocks it too
    return not (rest and sa_modes.base_blocking(g))


# ---------------------------------------------------------------- triggers (deterministic, no spaCy)

@dataclass(frozen=True)
class Trigger:
    cls: str
    sentence: str
    start: int
    context: str = ""               # the earlier sentence (repeat) or the question (FAQ)
    seed_lines: tuple = ()          # the seed excerpt sent with it
    # False for the only sentence of an FAQ answer: a repair that fails is cut in code on the last attempt, and an
    # emptied answer could drop the FAQ under its pair minimum (F16_faq holds). Such a finding is recorded, never live.
    cuttable: bool = True


_WORD_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ’'-]+")
_STOP = frozenset({
    "this", "that", "with", "from", "into", "they", "them", "their", "there", "here", "have", "been", "were", "what",
    "when", "where", "which", "while", "your", "about", "also", "more", "most", "than", "then", "only", "just", "each",
    "every", "over", "after", "before", "through", "along", "will", "would", "could", "should", "because", "between",
    "other", "some", "such", "very", "even", "much", "many", "these", "those", "it's", "does", "make", "makes", "made",
    "come", "comes", "take", "takes", "place", "places", "trip", "tour", "visit", "visitors", "travellers", "travelers",
})
_BRIEF_RE = re.compile(r"^\s*(?:Thin moment\b|SCOPE\b|Brand pack\b|Angle brief\b|System prompt\b|AUDIENCE\b)"
                       r"|writer instruction", re.I)
_NAME_CUE_RE = re.compile(
    r"\b(?:names?|named|naming|call(?:s|ed)?|spell\w*|pronounc\w*|sounds?|sounded|translat\w*|transliterat\w*"
    r"|romani[sz]\w*|connotation\w*|english|ears?|meaning|means|approximation|variants?|unfortunate|awkward)\b", re.I)
_PRONOUNCE_Q_RE = re.compile(r"\b(?:pronounce|say|spell|spelled|spelt|written|write|name|called)\b", re.I)
_NAMING_GENERIC = frozenset({"name", "also", "use", "write", "know", "city", "call", "place", "town", "naming"})
_HYPE_RE = re.compile(
    r"\b(?:(?:hard|impossible|difficult) to overstate|can(?:not|'t|’t) be overstated|few (?:other )?places\b[^.]{0,40}?"
    r"\b(?:can|could|offer|match|rival|compare)\w*|nowhere else|like no other|unlike any(?:where| other)"
    r"|second to none|without (?:equal|parallel|rival)|unrivall?ed|unparall?ell?ed|unmatched|one of a kind"
    r"|in a class of its own|rarely (?:matched|equall?ed)|nothing (?:here |else )?(?:quite )?(?:like it|compares"
    r"|resembles)|(?:world|planet|earth)(?:'s|’s) (?:finest|greatest|best)|once[- ]in[- ]a[- ]lifetime"
    r"|unlike (?:almost |virtually |practically )?(?:anything|anywhere)|(?:almost|virtually) nowhere"
    r"|(?:anything|anywhere) else (?:on earth|in the world)|on (?:the )?(?:earth|planet)"
    r"|(?:among|one of) the (?:most|least) \w+"
    r"|(?:most|few) (?:other )?(?:family )?(?:travel|travellers|travelers|visitors|trips) (?:never|rarely) \w+)\b",
    re.I)
_OPEN_Q_RE = re.compile(r"^\s*(?:what|why|how)\b", re.I)
_MEASURABLE_HOW_RE = re.compile(r"^\s*how\s+(?:many|much|long|far|old|high|big|tall|deep|often|early|late|cold|hot"
                                r"|warm|difficult|hard|fit|steep|large|wide|expensive|soon)\b", re.I)
# A body sentence that opens without naming its subject: a pronoun / demonstrative, a connective, a participle.
# (A sentence that opens on a definite noun the earlier sentence already has is a repeat candidate too, see below.)
_IMPLICIT_SUBJECT_RE = re.compile(r"^(?:it|its|this|that|these|those|they|there|here|another|also|again|and|but|so"
                                  r"|which|such|the same|both|all of (?:it|this|them))\b|^[a-z]+(?:ing|ed)\b", re.I)
# 'The dzong …', 'Their camps …': the noun is checked against the earlier sentence's words.
_DEFINITE_OPENER_RE = re.compile(r"^(?:the|their|its|this|that)\s+([A-Za-zÀ-ÖØ-öø-ÿ'’-]+)"
                                 r"(?:\s+([A-Za-zÀ-ÖØ-öø-ÿ'’-]+))?", re.I)


def _stems(text: str) -> set[str]:
    return {seed_graph.norm(w) for w in _WORD_RE.findall(text or "")
            if len(w.strip("'’-")) >= 4 and fold(w) not in _STOP}


def _seed_lines(seed: str) -> tuple[list[str], list[str]]:
    """(source lines, Naming lines) of the moment's own seed; writer-instruction lines are never sent."""
    own, _sib = split_seed(seed)
    src, naming = [], []
    for raw in own.splitlines():
        line = raw.strip().lstrip("- ").strip()
        if not line or _BRIEF_RE.search(line):
            continue
        (naming if line.startswith("Naming") else src).append(line[:SEED_LINE_CHARS])
    return src, naming


def _excerpt(lines: Sequence[str], text: str, n: int = SEED_LINES, need: int = 1) -> list[str]:
    """The `n` seed lines that share most content words with `text` (at least `need`), in seed order."""
    words = _stems(text)
    scored = [(len(words & _stems(ln)), i) for i, ln in enumerate(lines)]
    top = sorted((s for s in scored if s[0] >= need), key=lambda s: (-s[0], s[1]))[:n]
    return [lines[i] for _, i in sorted(top, key=lambda s: s[1])]


def _names_a_place(sentence: str, names: frozenset) -> bool:
    return any(fold(w).strip("'’-") in names for w in _WORD_RE.findall(sentence))


def _question_text(clean: str) -> str:
    q = clean.strip().strip("*").strip()
    return re.sub(r"^Q:\s*", "", q).strip().strip("*").strip()


def tier1_skip_set(tier1_record: Optional[dict]) -> Optional[set[str]]:
    """The sentences tier 1 already has a candidate on, or None when tier 1 did not run cleanly this attempt (then
    tier 2 looks at nothing)."""
    if not isinstance(tier1_record, dict) or tier1_record.get("status") != "ok":
        return None
    return {str(c.get("sentence") or "")[:300] for c in tier1_record.get("candidates") or [] if isinstance(c, dict)}


def find_triggers(text: str, seed: Optional[str], tier1_record: Optional[dict],
                  classes: Iterable[str] = CLASSES, brief_texts: Optional[Iterable[str]] = None) -> list[Trigger]:
    """The sentences of `text` tier 2 should look at: a class trigger fires and tier 1 left the sentence alone (see the
    module docstring). In text order, one per sentence (the first class that fires)."""
    skip = tier1_skip_set(tier1_record)
    if skip is None or not seed or not (text or "").strip():
        return []
    ran = set((tier1_record or {}).get("modes") or {})
    on = {c for c in classes if c in CLASSES and (c not in TIER1_BUILT or c in ran)}
    if not on:
        return []
    g = seed_graph.build(seed, list(brief_texts or []))
    src_lines, naming_lines = _seed_lines(seed)
    seed_fold = fold(seed)
    spans = sentence_spans(text)
    out: list[Trigger] = []
    taken: set[int] = set()

    def add(t: Trigger) -> None:
        if t.start not in taken and t.sentence[:300] not in skip:
            taken.add(t.start)
            out.append(t)

    # The FAQ answers: question → its answer sentences. The only sentence of an answer is never cuttable.
    answers: list[tuple[str, list[dict]]] = []
    for i, sp in enumerate(spans):
        if sp["kind"] == "faq_q":
            block = []
            for nxt in spans[i + 1:]:
                if nxt["kind"] != "faq_a":
                    break
                block.append(nxt)
            answers.append((_question_text(sp["clean"]), block))
    lone = {b[0]["start"] for _q, b in answers if len(b) == 1}

    # FAQ: the first sentence of the answer to an open WHAT / WHY / HOW question (a measurable "how many / long / far"
    # is tier 1's future type check, plan S7).
    if FAQ in on:
        for q, block in answers:
            if block and _OPEN_Q_RE.match(q) and not _MEASURABLE_HOW_RE.match(q):
                first = block[0]
                add(Trigger(FAQ, first["clean"], first["start"], context=q, cuttable=first["start"] not in lone,
                            seed_lines=tuple(_excerpt(src_lines, q + " " + first["clean"]))))

    question = ""
    section: list[dict] = []
    for sp in spans:
        kind, clean = sp["kind"], sp["clean"]
        if kind == "heading":
            section, question = [], ""
            continue
        if kind == "faq_q":
            question = _question_text(clean)
            continue
        if kind not in ("prose", "bullet", "faq_a") or not clean.strip():
            continue
        if HYPE in on:
            m = _HYPE_RE.search(clean)
            if m and fold(m.group(0)) not in seed_fold:
                add(Trigger(HYPE, clean, sp["start"], seed_lines=tuple(_excerpt(src_lines, clean)),
                            cuttable=sp["start"] not in lone))
        if NAMING in on and g.naming_words and naming_lines and _names_a_place(clean, g.place_names) and not (
                kind == "faq_a" and _PRONOUNCE_Q_RE.search(question)):
            note = {seed_graph.norm(w) for w in _WORD_RE.findall(clean) if len(w) > 4} & set(g.naming_words)
            note -= _NAMING_GENERIC | {fold(w) for w in g.place_names}
            if _NAME_CUE_RE.search(clean) or note:
                lines = naming_lines + [ln for ln in _excerpt(src_lines, clean) if ln not in naming_lines]
                add(Trigger(NAMING, clean, sp["start"], seed_lines=tuple(lines[:SEED_LINES + len(naming_lines)]),
                            cuttable=sp["start"] not in lone))
        if REPEAT in on and kind in ("prose", "bullet") and not sp.get("in_faq"):
            mine = _stems(clean)
            implicit = bool(_IMPLICIT_SUBJECT_RE.match(clean))
            m_open = _DEFINITE_OPENER_RE.match(clean)
            opener = _stems(" ".join(w for w in m_open.groups() if w)) if m_open else set()
            for prev in reversed(section[-REPEAT_WINDOW:]):
                theirs = _stems(prev["clean"])
                if not implicit and not opener & theirs:
                    continue
                shared = mine & theirs
                if len(shared) < 3 or len(shared) / max(1, min(len(mine), len(theirs))) < 0.4:
                    continue
                # the seed anchor: a seed line both sentences draw on (plan B's same-seed-line pairing)
                anchor = [ln for ln in src_lines if len(_stems(ln) & mine) >= 2 and len(_stems(ln) & theirs) >= 2]
                if anchor:
                    add(Trigger(REPEAT, clean, sp["start"], context=prev["clean"],
                                seed_lines=tuple(anchor[:SEED_LINES])))
                    break
        if kind in ("prose", "bullet") and not sp.get("in_faq"):
            section.append(sp)
    return sorted(out, key=lambda t: t.start)


# ---------------------------------------------------------------- the model call

SYSTEM_PROMPT = """You review single sentences from a travel article against the source facts (SEED) its writer \
was given. Each numbered item names one CHECK, the SENTENCE, sometimes a CONTEXT (an earlier sentence, or the FAQ \
question the sentence answers) and some SEED lines. Decide for each item whether the sentence has the problem its \
CHECK describes. Do not judge style, tone or length, and do not use outside knowledge.

Checks:
- naming_note: the sentence turns a note about the place's name (how it is spelled, pronounced or translated, or how \
it sounds to foreign readers) into copy. A plain use of the name, a real spelling or pronunciation tip the reader \
needs, or "also known as" is OK.
- repeat: the sentence retells, in other words, what the CONTEXT sentence already said and adds no new fact. Two \
different facts in a similar frame are OK.
- faq_answer: the sentence opens the answer to the CONTEXT question and does not address what the question asks (it \
talks around it). A short or partial answer is OK.
- hype: the sentence makes a rarity, uniqueness or "cannot be overstated" claim the SEED does not make. A claim the \
SEED makes at the same scope is OK.

Quote, never paraphrase. piece_quote: at least 3 words of the SENTENCE that carry the problem, copied character for \
character. seed_quote: at least 3 words of one SEED line your verdict rests on, copied character for character. If \
you cannot quote both, answer OK. When unsure, answer OK: a sentence is changed only on a clear problem. action: \
"cut" when nothing worth keeping is left once the problem is removed, else "repair".

Answer with JSON only: {"items": [{"n": <item number>, "verdict": "VIOLATION"|"OK", "piece_quote": "...", \
"seed_quote": "...", "action": "repair"|"cut"}]}, one entry per item, in the given order."""

SCHEMA = {"name": "t10_adjudicate", "schema": {
    "type": "object", "additionalProperties": False, "required": ["items"],
    "properties": {"items": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["n", "verdict", "piece_quote", "seed_quote", "action"],
        "properties": {"n": {"type": "integer"}, "verdict": {"type": "string", "enum": [VIOLATION, OK_VERDICT]},
                       "piece_quote": {"type": "string"}, "seed_quote": {"type": "string"},
                       "action": {"type": "string", "enum": list(ACTIONS)}}}}}}}


class AdjudicateReplyError(ValueError):
    """The model replied (and was billed) but the reply has no usable verdict for every item."""


def build_user_prompt(items: Sequence[Trigger]) -> str:
    out = ["ITEMS:"]
    for n, t in enumerate(items, 1):
        out.append(f"{n}. CHECK: {CHECK_NAME[t.cls]}")
        out.append(f"   SENTENCE: {t.sentence[:SENTENCE_CHARS]}")
        if t.context:
            out.append(f"   CONTEXT: {t.context[:SENTENCE_CHARS]}")
        out.append("   SEED:")
        out += [f"   - {ln}" for ln in t.seed_lines] or ["   - (no seed line shares its words)"]
    return "\n".join(out)


def build_request(items: Sequence[Trigger], *, stage: str = STAGE, model_tier: Optional[str] = None) -> LLMRequest:
    return LLMRequest(system_prompt=SYSTEM_PROMPT, user_prompt=build_user_prompt(items), max_tokens=MAX_TOKENS,
                      temperature=0.0, stage=stage, model_tier=model_tier, json_schema=SCHEMA)


def parse_verdicts(content: str, n: int) -> list[dict]:
    """One {verdict, piece_quote, seed_quote, action} per item 1..n, or AdjudicateReplyError (never guessed). The
    reply's first complete JSON value is read (text after it ignored), the {"items": [...]} object or the bare items
    list (AA-1026, as faq_filter.parse_verdicts)."""
    try:
        obj = first_json_value(content, kinds=(dict, list))
    except JsonReplyError as e:
        raise AdjudicateReplyError(str(e)) from e
    items = obj.get("items") if isinstance(obj, dict) else obj
    if not isinstance(items, list):
        raise AdjudicateReplyError("no items list")
    by_n: dict[int, dict] = {}
    for it in items:
        if not isinstance(it, dict):
            raise AdjudicateReplyError(f"bad item {str(it)[:80]}")
        verdict = str(it.get("verdict") or "").strip().upper()
        if verdict not in (VIOLATION, OK_VERDICT):
            raise AdjudicateReplyError(f"bad verdict {str(it)[:80]}")
        try:
            k = int(it.get("n"))
        except (TypeError, ValueError) as e:
            raise AdjudicateReplyError(f"bad item number {it.get('n')!r}") from e
        if not 1 <= k <= n:
            raise AdjudicateReplyError(f"item n={k} out of range 1..{n}")
        action = str(it.get("action") or "repair").strip().lower()
        by_n[k] = {"verdict": verdict, "piece_quote": str(it.get("piece_quote") or "")[:400],
                   "seed_quote": str(it.get("seed_quote") or "")[:400],
                   "action": action if action in ACTIONS else "repair"}
    missing = [k for k in range(1, n + 1) if k not in by_n]
    if missing:
        raise AdjudicateReplyError(f"no verdict for item(s) {missing}")
    return [by_n[k] for k in range(1, n + 1)]


_QUOTE_CHARS = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-"})


def _norm_text(s: str) -> str:
    return " ".join(fold(s or "").translate(_QUOTE_CHARS).split())


def _norm_quote(s: str) -> str:
    return _norm_text(s).strip(" .,;:!?\"'()*")


def quote_in(quote: str, *haystacks: str) -> bool:
    """`quote` (≥ MIN_QUOTE_WORDS words; case, accents, quote marks and spacing normalised) is in every haystack."""
    q = _norm_quote(quote)
    if len(q.split()) < MIN_QUOTE_WORDS:
        return False
    return all(q in _norm_text(h) for h in haystacks)


def verify(item: Trigger, verdict: dict, seed: str) -> Optional[str]:
    """Why a VIOLATION is dropped, or None when both quotes hold."""
    if not quote_in(verdict.get("piece_quote", ""), item.sentence):
        return "piece_quote_not_in_sentence"
    if not item.seed_lines or not quote_in(verdict.get("seed_quote", ""), "\n".join(item.seed_lines), seed):
        return "seed_quote_not_in_seed"
    return None


@dataclass
class Adjudication:
    findings: list = field(default_factory=list)   # [(Trigger, verdict)] that passed both quote checks
    dropped: list = field(default_factory=list)    # [{"cls", "why"}]
    model: Optional[str] = None
    cost_usd: float = 0.0


def _call(items: Sequence[Trigger], *, client, stage: str, role: str, model_tier: Optional[str]) -> tuple[list, object]:
    """One gateway call and its llm_call_log row (AA-927 shape): a usable reply → one row; a billed reply that does not
    parse → one billed_unusable row, then the error is raised (the caller fails open)."""
    resp = client.generate(build_request(items, stage=stage, model_tier=model_tier))
    signal = {"issue": "AA-1000", "n_items": len(items), "classes": sorted({t.cls for t in items})}
    try:
        verdicts = parse_verdicts(resp.content, len(items))
    except Exception as e:
        record_unusable_sync(resp, stage=stage, role=role, error=e, quality_signal_extra=signal)
        raise
    record_call_sync(
        stage=stage, role=role, model=resp.model_used,
        tokens_in=getattr(resp, "input_tokens", None), tokens_out=getattr(resp, "output_tokens", None),
        cost_usd=resp.cost_usd,
        quality_signal=signal | {"json_parsed": True, "violations": sum(v["verdict"] == VIOLATION for v in verdicts)},
        stop_reason=getattr(resp, "stop_reason", None), account=getattr(resp, "satellite_account", None),
        fallback_used=getattr(resp, "fallback_used", None), provider=getattr(resp, "provider", None),
        **billed_fields(resp),
    )
    return verdicts, resp


def _with_deadline(fn, timeout_s: Optional[float]):
    """`fn()` in a worker thread (a copy of the caller's contextvars: job and tenant ids for the log row); raises
    TimeoutError past `timeout_s`. A call still running then finishes in the background and writes its own row."""
    if timeout_s is None:
        return fn()
    ctx = contextvars.copy_context()
    ex = ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(ctx.run, fn).result(timeout=timeout_s)
    finally:
        ex.shutdown(wait=False)


def adjudicate(items: Sequence[Trigger], seed: str, *, client, stage: str = STAGE, role: str = ROLE,
               model_tier: Optional[str] = None, timeout_s: Optional[float] = TIMEOUT_S) -> Adjudication:
    """One call over `items` (at most MAX_ITEMS); raises on any model error, unusable reply or the deadline. `stage` /
    `model_tier` are overridable for an ad-hoc calibration (stage `adhoc_aa1000`, no route row → `model_tier`)."""
    items = list(items)[:MAX_ITEMS]
    out = Adjudication()
    if not items:
        return out
    verdicts, resp = _with_deadline(
        lambda: _call(items, client=client, stage=stage, role=role, model_tier=model_tier), timeout_s)
    out.model = getattr(resp, "model_used", None)
    out.cost_usd = float(getattr(resp, "cost_usd", 0.0) or 0.0)
    for item, v in zip(items, verdicts):
        if v["verdict"] != VIOLATION:
            continue
        why = verify(item, v, seed)
        if why:
            out.dropped.append({"cls": item.cls, "why": why})
        else:
            out.findings.append((item, v))
    return out


# ---------------------------------------------------------------- one attempt

def _default_client():
    from shared.llm_client.client import LLMClient  # noqa: PLC0415 — built only when a call is due
    return LLMClient()


def run(content_text: str, *, tier1=None, trip_ctx=None, hygiene_ctx=None, brief_texts: Optional[dict] = None,
        modes: Optional[dict[str, str]] = None, client=None, replay: bool = False) -> Optional[sa_modes.SeedAlignRun]:
    """Tier 2 for one attempt, or None when every class is off (then nothing is called, recorded or changed).

    `tier1` is this attempt's `seed_align.modes.run` result (None when tier 1 is off). `replay` (a $0 replay of stored
    verdicts) records and calls nothing new. Returns a SeedAlignRun whose `live` holds at most one finding (repair
    mode) for `seed_align.modes.apply`, and whose `record` is what writer_input keeps. Fails open on every error."""
    modes = active_modes() if modes is None else {c: m for c, m in modes.items() if m in (SHADOW, REPAIR)}
    if not modes:
        return None
    started = time.monotonic()
    out = sa_modes.SeedAlignRun(record={"modes": dict(sorted(modes.items())), "findings": []})
    rec = out.record
    if replay:
        rec["status"] = "replay"
        return out
    t1 = getattr(tier1, "record", None)
    if tier1_skip_set(t1) is None:
        rec["status"] = "tier1_not_ok"  # tier 1 off, erroring or without spaCy: nothing was skipped by it
        return out
    seed = sa_modes._seed_of(trip_ctx, hygiene_ctx)
    try:
        briefs = sa_modes._brief_lines(brief_texts, trip_ctx, hygiene_ctx)
        triggers = find_triggers(content_text, seed, t1, classes=modes.keys(), brief_texts=briefs)
    except Exception as exc:  # fail open
        logger.warning("t10_adjudicate_trigger_failed", error=str(exc)[:300])
        rec.update(status="error", error=str(exc)[:300])
        return out
    rec["triggers"] = len(triggers)
    if not triggers:
        rec["status"] = "no_trigger"
        return out
    items = triggers[:MAX_ITEMS]
    rec["sent"] = len(items)
    try:
        result = adjudicate(items, seed or "", client=client or _default_client(), timeout_s=TIMEOUT_S)
    except Exception as exc:  # model error, unusable reply, deadline: nothing changes
        logger.warning("t10_adjudicate_failed", error=str(exc)[:300], kind=type(exc).__name__)
        rec.update(status="timeout" if isinstance(exc, TimeoutError) else "error", error=str(exc)[:300])
        return out
    rec.update(status="ok", model=result.model, cost_usd=round(result.cost_usd, 6),
               ms=int((time.monotonic() - started) * 1000), dropped=result.dropped)
    for item, v in result.findings:
        rec["findings"].append({"cls": item.cls, "mode": modes.get(item.cls, OFF), "gate": CLASS_GATE[item.cls],
                                "action": v["action"], "sentence": item.sentence[:300],
                                "piece_quote": v["piece_quote"][:200], "seed_quote": v["seed_quote"][:200],
                                **({} if item.cuttable else {"live_blocked": "only_sentence_of_faq_answer"})})
    # repair: the first verified finding in text order of a class in repair mode whose sentence may be cut, and only
    # that one
    live = next(((item, v) for item, v in result.findings if modes.get(item.cls) == REPAIR and item.cuttable), None)
    if live is not None:
        item, _v = live
        out.live = {CLASS_GATE[item.cls]: [finding_text(item.cls, item.sentence)]}
        rec["live"] = {"cls": item.cls, "gate": CLASS_GATE[item.cls]}
    return out


__all__ = ["STAGE", "ROLE", "MODE_ENV", "OFF", "SHADOW", "REPAIR", "MODES", "CLASS_TAG", "CLASSES", "CLASS_GATE",
           "REASONS", "MAX_ITEMS", "Trigger", "class_mode", "active_modes", "finding_text", "tier2_only_failure",
           "tier1_skip_set",
           "find_triggers", "SYSTEM_PROMPT", "SCHEMA", "build_user_prompt", "build_request", "parse_verdicts",
           "AdjudicateReplyError", "quote_in", "verify", "Adjudication", "adjudicate", "run"]
