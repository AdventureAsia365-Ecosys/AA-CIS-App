"""services.acp_content_writing.faq_filter — AA-779 (S247) optional LLM filter for the blog FAQ questions (T9).

A blog's FAQ may answer real search questions (People Also Ask and the piece's Segment / place questions,
services/acp_shared/real_questions.py). The deterministic scope filter (quality_gates.paa_for_scope) kept
questions at precision 0.64 on the S246 labelled set (200 questions). A blind Claude Haiku classifier
scored precision 0.987, recall 0.724 on the same set (S247), and Nghiệp's hand check of the 30
disagreements agreed with the label panel 30/30 (ADR 0011).

This module puts that classifier behind a flag, as a question FILTER (it only chooses which real questions
the writer is given; it is not a gate and never holds a piece):

    T9_FAQ_LLM_FILTER = off     (default) the deterministic list, no model call;
                        shadow  call the model, record its verdicts, keep the deterministic list;
                        on      the model's KEEP list (in candidate order, at most PAA_MAX_QUESTIONS).

One call per piece (all candidates in one numbered list) through the Model Gateway, stage
`t9_faq_filter`, role `validate` (ADR 0008: a classification nobody scores; a `judge` stage must be
non-Anthropic), one `shared.llm_call_log` row per reply. Any error, unusable reply or timeout falls back to
the deterministic list: the filter never blocks or fails T9.
"""
from __future__ import annotations

import asyncio
import collections
import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional, Sequence

import structlog

from services.acp_content_writing.hygiene import fold
from services.acp_content_writing.quality_gates import PAA_MAX_QUESTIONS, normalize_paa_question, paa_for_scope
from shared.llm_client.call_log import record_call_sync
from shared.llm_client.client import LLMClient
from shared.llm_client.models import LLMRequest

logger = structlog.get_logger()

FAQ_FILTER_STAGE = "t9_faq_filter"
FAQ_FILTER_ROLE = "validate"
FAQ_FILTER_ENV = "T9_FAQ_LLM_FILTER"
MODE_OFF, MODE_SHADOW, MODE_ON = "off", "shadow", "on"
FAQ_FILTER_MODES = (MODE_OFF, MODE_SHADOW, MODE_ON)

LLM_MAX_QUESTIONS = 30   # = real_questions.CHAIN_MAX: every raw question of one piece fits in one call
SOURCE_MAX = 3000        # chars of the moment's own seed + Facts sent as the source text
PLACES_MAX = 20
OUTSIDE_MAX = 30
MAX_TOKENS = 3000        # ~45 output tokens per question x 30 + the JSON envelope
TIMEOUT_S = 60.0         # the whole step; on timeout the writer gets the deterministic list

KEEP, DROP = "KEEP", "DROP"
REASON_CODES = ("on_subject", "off_scope_homonym", "off_scope_other_place", "outside_piece_scope", "unanswerable",
                "sensitive_no_travel_use", "commercial_junk", "word_meaning", "non_english", "other")
CONFIDENCES = ("high", "medium", "low")
_ON_ALIASES = ("1", "true", "yes", MODE_ON)


def faq_filter_mode() -> str:
    """The flag (worker task def env). Unknown values read as `off`; '1' / 'true' / 'yes' read as `on`."""
    raw = os.environ.get(FAQ_FILTER_ENV, "").strip().lower()
    if raw in _ON_ALIASES:
        return MODE_ON
    return raw if raw in FAQ_FILTER_MODES else MODE_OFF


# The S247 trial prompt (blind classifier, root audit 2026-10-22-S247-aa779-llm) with a calibration folded into
# its rules: 29 of the trial's 30 disagreements were answerable on-subject questions the model dropped as
# "unanswerable" / "outside_piece_scope" / "off_scope_homonym"; one was a country-trivia false keep. The added
# clauses: country-as-destination topics (KEEP 1, outside_piece_scope), general travel facts and "an empty
# source is no reason to drop" (KEEP 2), local food is not a homonym, visitor safety "right now" and hazards
# near a site (sensitive_no_travel_use), country trivia that answers with another place (off_scope_other_place).
# Its examples are NOT questions of the 200-question labelled set (keep it that way, or the re-measure on that
# set stops being a fair test). Kept short on purpose: the system prompt is most of the input tokens.
SYSTEM_PROMPT = """You decide which real search questions (Google "People Also Ask") may appear in the \
customer-facing FAQ of one travel blog post. The post covers ONE scope: a moment, a few days of a trip, or a tour. \
You get the scope (what the post covers, its places, the trip's other places that are NOT in this post, and the \
source text) and a numbered list of questions. Judge each question on its own.

KEEP a question only if BOTH are true:
1. It is about this post's subject: a place, attraction or activity in the scope, or the scope's own \
country/region as a travel destination. Practical travel questions about the scope's own country count (safety for \
visitors, visas, weather and best season, money and costs, local food, what to pack, dress code and etiquette, \
souvenirs, getting there and around, what the country is known for, its landscape).
2. A truthful 40-60 word answer can be written from the source text plus general, well-known background facts \
about the named places (history, height, location, what a place is known for, distances and travel times between \
them, how long visitors usually spend there, whether to book ahead, the wildlife a park is known for, what a \
monument enshrines). The answer must not need to promise anything the tour itself does (an activity, a service, a \
time, a view, a private option) that the source does not state. A short or empty source text is no reason to drop.

DROP the question if ANY of these is true (use the matching reason):
- off_scope_homonym: it is about something else that only shares a word with the scope: a dish, product, brand, \
breed, person, film, sports team or a different place with a similar name (e.g. "Turkish delight" on a Turkey \
trip, "Georgia" the US state on a trip to the country Georgia, the programming language on a Java island moment). \
Local food of the scope's own country is not a homonym.
- off_scope_other_place: it is about a different place or country than the scope (a comparison is DROP unless the \
answer stays about the scope place), or country trivia whose answer is another specific place (the country's \
largest lake on a moment elsewhere).
- outside_piece_scope: it is about a place on the same trip that this post does not cover (listed under "Trip \
places outside this post"), or about a part of a big place that this post does not visit. Never for a question \
about the scope's own country as a destination.
- unanswerable: it can only be answered by promising tour activities, services, times, prices or views that the \
source does not support, or it needs live data that no general answer can cover (today's prices, opening today).
- sensitive_no_travel_use: politics, war casualties, crime, disasters, disease or diet/health advice, economics or \
statistics, with no practical use for a visitor. Visitor safety in the scope place or country stays KEEP, even \
asked "right now" (a general answer plus "check current advisories" is truthful), and so do hazards near a site \
(unexploded ordnance, scams, strong currents).
- commercial_junk: coupons, deals, "where can I watch/buy", brand shopping, jobs, real estate.
- word_meaning: about a place NAME as a word (meaning, spelling, translation, an insult it sounds like), not about \
the place.
- non_english: not written in English.
- other: anything else that would look wrong in this post's FAQ (say why in "note").

Rules:
- The scope text is authoritative. A post about one moment is narrower than its trip.
- Background about a named place in scope is fine even if the source does not mention it (e.g. "How old is the \
Hagia Sophia?" on an Istanbul old-town moment = KEEP).
- What a local kind of landmark is or means, or what a temple/monastery name in scope means, is background, not \
word_meaning (e.g. "What is a medrese?" or "What does Süleymaniye mean?" on an Istanbul old-town moment = KEEP).
- A question about the scope's country as a whole is KEEP only when it is a practical travel question or plainly \
about the country as a destination ("Is Peru worth visiting?" or "Is $50 a day enough in Peru?" on a Machu Picchu \
moment = KEEP; "What is Peru's GDP?" = DROP).
- Do not drop a question for being a duplicate of another question in the list; judge each one on relevance only.
- Elephant riding or animal-welfare questions: judge them as usual and write "elephant" in note.
- When unsure, choose DROP and set confidence "low": the FAQ is customer-facing, precision comes first.

Answer with JSON only: {"items": [{"n": <question number>, "label": "KEEP"|"DROP", "reason_code": "on_subject" for \
KEEP or one DROP reason above, "confidence": "high"|"medium"|"low", "note": "<= 10 words"}]}, one entry per \
question, in the given order."""


@dataclass(frozen=True)
class FaqScope:
    """What the classifier sees about one piece (the trial's blind scope view)."""
    country: Optional[str]
    trip: Optional[str]
    scope_text: str
    places: tuple = ()
    trip_places_outside: tuple = ()
    source_text: str = ""


@dataclass
class FilterOutcome:
    kept: list
    verdicts: list = field(default_factory=list)  # [{q, label, reason_code, confidence, note}], candidate order
    model: Optional[str] = None
    cost_usd: float = 0.0


class FaqFilterReplyError(ValueError):
    """The model replied (and was billed) but the reply has no usable verdict for every question."""


_LOCATION_RE = re.compile(r"^\s*Location:\s*(.+)$", re.M)


def _seed_places(own_seed: str) -> list[str]:
    """The place names of the seed's 'Location:' lines (all parts before the country), in order."""
    out: list[str] = []
    for m in _LOCATION_RE.finditer(own_seed or ""):
        parts = [p.strip() for p in re.sub(r"\(.*?\)", " ", m.group(1)).split(",")]
        for p in parts[:-1] if len(parts) > 1 else parts:
            if p and p not in out:
                out.append(p)
    return out


def scope_from_context(context: dict, own_seed: str, facts_text: Optional[str] = None) -> FaqScope:
    """The scope view for a T9 piece: `own_seed` = the moment's own lines (hyg.lexicon.own_text)."""
    first = next((ln.strip() for ln in (own_seed or "").splitlines() if ln.strip()), "")
    scope_text = (context.get("scope") or "").strip() or (f"This piece covers one moment: {first}." if first else "")
    source = (own_seed or "").strip()
    if facts_text:
        source = f"{source}\n{facts_text.strip()}".strip()
    return FaqScope(country=context.get("destination"), trip=context.get("trip_name"), scope_text=scope_text,
                    places=tuple(_seed_places(own_seed)[:PLACES_MAX]),
                    trip_places_outside=tuple(list(context.get("trip_places") or ())[:OUTSIDE_MAX]),
                    source_text=source[:SOURCE_MAX])


def candidate_questions(questions, limit: int = LLM_MAX_QUESTIONS) -> list[str]:
    """The questions the model judges: cleaned like paa_for_scope (normalize_paa_question), folded duplicates
    dropped, order kept, at most `limit`."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in questions or []:
        q = normalize_paa_question(raw)
        if q is None or fold(q) in seen:
            continue
        seen.add(fold(q))
        out.append(q)
        if len(out) >= limit:
            break
    return out


def build_user_prompt(scope: FaqScope, questions: Sequence[str]) -> str:
    """Same layout as the S247 trial's user prompt."""
    lines = [f"COUNTRY: {scope.country or '-'}"]
    if scope.trip:
        lines.append(f"TRIP: {scope.trip}")
    lines.append(f"SCOPE: {scope.scope_text}")
    if scope.places:
        lines.append("Places in this post: " + "; ".join(scope.places))
    if scope.trip_places_outside:
        lines.append("Trip places outside this post (not covered): " + "; ".join(scope.trip_places_outside))
    lines.append("SOURCE TEXT:\n" + (scope.source_text or "(none: only the scope line above)"))
    lines.append("\nQUESTIONS:")
    lines += [f"{n}. {q}" for n, q in enumerate(questions, 1)]
    return "\n".join(lines)


def parse_verdicts(content: str, n: int) -> list[dict]:
    """One verdict per question 1..n, or FaqFilterReplyError. An unknown reason code reads as 'other', an
    unknown confidence as 'low'; a missing answer or label is an error (never guessed)."""
    m = re.search(r"\{.*\}", (content or "").strip(), re.S)
    if not m:
        raise FaqFilterReplyError("no JSON object in the reply")
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise FaqFilterReplyError(f"invalid JSON: {e}") from e
    items = obj.get("items") if isinstance(obj, dict) else None
    if not isinstance(items, list):
        raise FaqFilterReplyError("no items list")
    by_n: dict[int, dict] = {}
    for it in items:
        if not isinstance(it, dict) or it.get("label") not in (KEEP, DROP):
            raise FaqFilterReplyError(f"bad item {str(it)[:80]}")
        try:
            k = int(it.get("n"))
        except (TypeError, ValueError) as e:
            raise FaqFilterReplyError(f"bad item number {it.get('n')!r}") from e
        if not 1 <= k <= n:
            raise FaqFilterReplyError(f"item n={k} out of range 1..{n}")
        reason = it.get("reason_code") if it.get("reason_code") in REASON_CODES else "other"
        by_n[k] = {"label": it["label"], "reason_code": reason,
                   "confidence": it.get("confidence") if it.get("confidence") in CONFIDENCES else "low",
                   "note": str(it.get("note") or "")[:200]}
    missing = [k for k in range(1, n + 1) if k not in by_n]
    if missing:
        raise FaqFilterReplyError(f"no verdict for question(s) {missing}")
    return [by_n[k] for k in range(1, n + 1)]


def llm_filter_questions(questions, scope: FaqScope, *, limit: int = PAA_MAX_QUESTIONS,
                         stage: str = FAQ_FILTER_STAGE, role: str = FAQ_FILTER_ROLE,
                         model_tier: Optional[str] = None, tenant_id=None, content_piece_id=None,
                         angle_gate_request_id=None, quality_signal: Optional[dict] = None,
                         client: Optional[LLMClient] = None) -> FilterOutcome:
    """Sync. One gateway call for every candidate of `questions`; the KEEP ones in candidate order, at most
    `limit`. Writes one llm_call_log row per reply (also an unusable one, `outcome: unusable`). Raises on a
    gateway error or an unusable reply: the caller falls back. `stage` / `model_tier` are overridable for an
    ad-hoc measurement (stage `adhoc_aa779`, no route row → `model_tier` names the model)."""
    cands = candidate_questions(questions)
    if not cands:
        return FilterOutcome(kept=[])
    request = LLMRequest(system_prompt=SYSTEM_PROMPT, user_prompt=build_user_prompt(scope, cands),
                         max_tokens=MAX_TOKENS, temperature=0.0, stage=stage, model_tier=model_tier)
    resp = (client or LLMClient()).generate(request)
    verdicts, error = None, None
    try:
        verdicts = parse_verdicts(resp.content, len(cands))
    except FaqFilterReplyError as e:
        error = str(e)
    signal = {"issue": "AA-779", "n_questions": len(cands), **(quality_signal or {})}
    if verdicts is None:
        signal |= {"outcome": "unusable", "error": error[:200]}
    else:
        keeps = sum(1 for v in verdicts if v["label"] == KEEP)
        signal |= {"outcome": "ok", "keep": keeps,
                   "drop_reasons": dict(collections.Counter(v["reason_code"] for v in verdicts
                                                            if v["label"] == DROP))}
    record_call_sync(
        stage=stage, role=role, model=resp.model_used,
        tokens_in=getattr(resp, "input_tokens", None), tokens_out=getattr(resp, "output_tokens", None),
        cost_usd=resp.cost_usd, tenant_id=tenant_id, content_piece_id=content_piece_id,
        angle_gate_request_id=angle_gate_request_id, quality_signal=signal,
        stop_reason=getattr(resp, "stop_reason", None), account=getattr(resp, "satellite_account", None),
        fallback_used=getattr(resp, "fallback_used", None), provider=getattr(resp, "provider", None),
    )
    if verdicts is None:
        raise FaqFilterReplyError(error)
    rows = [{"q": q, **v} for q, v in zip(cands, verdicts)]
    kept = [r["q"] for r in rows if r["label"] == KEEP][:limit]
    return FilterOutcome(kept=kept, verdicts=rows, model=resp.model_used, cost_usd=float(resp.cost_usd or 0.0))


async def select_faq_questions(questions, *, own_seed: str, facts_text: Optional[str], out_of_scope,
                               context: dict, piece_id=None, request_id=None,
                               mode: Optional[str] = None) -> tuple[list[str], dict]:
    """The real questions a blog's FAQ may use, and the admin record of how they were chosen ({} when the
    flag is off). The deterministic list (paa_for_scope) is always computed first: it is the result for
    `off` and `shadow`, and the fallback for `on` when the model call fails, times out or is unusable."""
    deterministic = paa_for_scope(questions, own_seed, facts_text, out_of_scope)
    mode = mode or faq_filter_mode()
    if mode == MODE_OFF:
        return deterministic, {}
    record: dict = {"mode": mode, "stage": FAQ_FILTER_STAGE, "deterministic": deterministic}
    scope = scope_from_context(context, own_seed, facts_text)
    try:
        outcome = await asyncio.wait_for(asyncio.to_thread(
            llm_filter_questions, questions, scope, tenant_id=context.get("tenant_id"),
            content_piece_id=str(piece_id) if piece_id else None,
            angle_gate_request_id=str(request_id) if request_id else None,
            quality_signal={"mode": mode}), timeout=TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 — any failure: the writer gets the deterministic list
        logger.warning("t9_faq_filter_fallback", mode=mode, error=f"{type(e).__name__}: {str(e)[:200]}",
                       request_id=str(request_id) if request_id else None)
        return deterministic, record | {"fallback": True, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    record |= {"fallback": False, "model": outcome.model, "cost_usd": round(outcome.cost_usd, 6),
               "llm_kept": outcome.kept, "verdicts": outcome.verdicts}
    return (outcome.kept if mode == MODE_ON else deterministic), record
