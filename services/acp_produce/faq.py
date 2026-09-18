"""
services.acp_produce.faq — N7 E4 (FAQ answers + JSON-LD), AA-371.

STEP 0 (Linear AA-371 comment, 06/08/2026) confirmed the design executed
here:
- Only the ANSWER needs an LLM. The QUESTION already exists —
  `Brief.faq_candidates` (AA-369's `research.py::compile_brief()`) already
  paired each researched PAA question with a real `source_id` atom
  (`FAQCandidate.question` / `.source_id`) before this module ever runs.
  `answer_faq()` never asks the model to invent a question.
- Model is Bedrock satellite acc3 Sonnet (AA-397, acc1 fallback under it), same
  `invoke_claude(..., model="sonnet")` call as E2/E3 — CHỐT, not a proposal (AA-334 Cancelled).
  Palmyra must never appear anywhere in this module.
- Candidates are answered in batches of 2-3/call (`generation.py::
  _batch_sections`, reused directly rather than re-implemented — same
  balanced chunker E2 already uses), not one call per question and not one
  call for all of them.
- E2 (`generation.py::build_outline()`) no longer drafts a "FAQ" section at
  all (AA-371 change) — this module owns the FAQ section of the blog piece
  entirely. `apply_faq()` appends the rendered section to the END of
  `body_tagged`, which reproduces the same position "FAQ" would have
  occupied in the outline (`research.py` always appends it last to
  `required_h2s`).
- JSON-LD is 100% deterministic code (`build_faq_jsonld()`) — no LLM, built
  straight from this module's own already-answered `FAQItem` list. The
  citation tag (`[R:atom_id]`) stays in the rendered `body_tagged` markdown
  (F1 grounding needs it there) but is stripped from the JSON-LD `text`
  field — schema.org's `Answer.text` is reader-facing copy, not a place for
  internal citation markup.
- Not persisted as a new DB column. `Piece.faq_items` / `Piece.faq_jsonld`
  (models.py) are Pydantic-only fields — `acp_deliver.pieces` has no slot for
  either and none is added here; `body_tagged` (already persisted) is the
  one durable rendering, these are computed sidecars for whatever future
  export/publish path needs structured Q/A or schema.org markup.
"""
from __future__ import annotations

import re
import time
from typing import Optional

import structlog

from services.acp_produce.gates import TAG_RE
from services.acp_produce.generation import _batch_sections
from services.acp_produce.models import Brief, FAQCandidate, FAQItem, Piece
from services.content_generation.brand_standards import AA_BRAND_IDENTITY_PROMPT
from shared.llm_client.bedrock_satellite import BedrockInvokeResult, BedrockUnavailable, invoke_claude
from shared.llm_client.role_config import get_stage_config_sync
from shared.llm_client.call_log import record_call_sync
from shared.llm_client.pricing import calc_cost

logger = structlog.get_logger()

_MAX_INVOKE_ATTEMPTS = 3  # same policy as E2/E3
_RETRY_BACKOFF_SECONDS = 2.0
_MIN_MAX_TOKENS = 256
_MAX_MAX_TOKENS = 4096

_FAQ_MARKER_RE = re.compile(r"===FAQ:\d+===\s*\n")

# AA-404 writer-side wire (F9 deep-dive TL;DR #1): this used to be a
# module-level constant built once at import time from the generic
# `AA_BRAND_IDENTITY_PROMPT` — E4 was one of the 4 writer modules still doing
# this while F9's judge (PR #158) had already moved on to a real per-tenant
# rubric (`brand.py::fetch_brand_rubric_text()`). Now built per-call from
# `answer_faq()`'s own `brand_rubric_text` parameter, which defaults to
# `AA_BRAND_IDENTITY_PROMPT` — every pre-AA-404 caller/test that doesn't pass
# it keeps answering against exactly the prompt it always has.
_FAQ_SYSTEM_PROMPT_RULES = (
    "\n\nRULES:\n"
    "- For EACH question below, output a line \"===FAQ:<n>===\" (n = the question's number) followed\n"
    "  by a 1-3 sentence answer, in the exact order given — nothing else on that line.\n"
    "- Use ONLY the fact given for that question. Cite it with [R:atom_id] using the exact id given.\n"
    "- Never invent a fact and never cite an id other than the one given for that question.\n"
    "- If the given fact only partially answers the question, give the closest honest partial\n"
    "  answer and still cite the id — never fabricate a fuller answer than the fact supports.\n"
    "- Do NOT use [F:...] tags anywhere."
)


def _build_faq_system_prompt(brand_rubric_text: str) -> str:
    return (
        "You are answering a set of already-researched reader questions, each backed by exactly one\n"
        "given fact.\n\n"
        + brand_rubric_text.strip() + _FAQ_SYSTEM_PROMPT_RULES
    )


class FAQAnswerFailed(Exception):
    """A batch's Sonnet invoke kept failing after retries, or its response
    didn't parse into one answer per requested question. Same "never guess
    a partial mapping" stance as `generation.DraftGenerationFailed` — raised
    to the caller rather than silently dropping a question."""


# ---------------------------------------------------------------- answer (Sonnet, batched)

def answer_faq(
    faq_candidates: list[FAQCandidate], atom_text_by_id: dict[str, str],
    brand_rubric_text: str = AA_BRAND_IDENTITY_PROMPT,
) -> list[FAQItem]:
    """E4 answer step. Batches `faq_candidates` 2-3/call (reuses
    `generation._batch_sections`) and returns one `FAQItem` per candidate,
    in input order. Empty input returns `[]` with zero Sonnet calls.

    `brand_rubric_text` (AA-404 writer-side wire): the real per-tenant rubric
    F9's judge is scored against (`brand.py::fetch_brand_rubric_text()`),
    threaded down by `slot_runner.py`'s one per-slot fetch — defaults to the
    generic `AA_BRAND_IDENTITY_PROMPT` for any caller/test that doesn't have
    one (unchanged pre-AA-404 behavior)."""
    if not faq_candidates:
        return []

    system_prompt = _build_faq_system_prompt(brand_rubric_text)
    items: list[FAQItem] = []
    for batch in _batch_sections(faq_candidates):
        prompt = _build_batch_prompt(batch, atom_text_by_id)
        max_tokens = min(max(120 * len(batch) + 150, _MIN_MAX_TOKENS), _MAX_MAX_TOKENS)

        result = _invoke_sonnet_with_retry(prompt, max_tokens, system_prompt)
        logger.info("e4_faq_batch_success", model_used=result.model_used,
                     questions=[c.question for c in batch], latency_ms=result.latency_ms, usage=result.usage)

        parsed = _parse_batch_response(result.text, batch)
        _cfg = get_stage_config_sync("n7_faq")
        record_call_sync(
            stage="n7_faq", role="writer", model=result.model_used,
            tokens_in=result.usage.get("input_tokens"), tokens_out=result.usage.get("output_tokens"),
            cost_usd=calc_cost(_cfg.model_id, result.usage.get("input_tokens", 0),
                                result.usage.get("output_tokens", 0)),
            tenant_id=None,
            quality_signal={"items_parsed": len(parsed), "items_requested": len(batch)},
            stop_reason=result.stop_reason,
            account=_cfg.account_route or "acc3", fallback_used=False, provider="bedrock-satellite",
        )
        if len(parsed) != len(batch):
            raise FAQAnswerFailed(
                f"Sonnet response for FAQ batch {[c.question for c in batch]} did not contain "
                f"{len(batch)} parseable ===FAQ:<n>=== block(s) (got {len(parsed)})"
            )
        items.extend(parsed)
    return items


def _build_batch_prompt(batch: list[FAQCandidate], atom_text_by_id: dict[str, str]) -> str:
    lines = ["Answer these questions, in order:"]
    for i, cand in enumerate(batch):
        lines.append(f"\n{i}. QUESTION: {cand.question}")
        lines.append(f"   FACT [{cand.source_id}]: {atom_text_by_id.get(cand.source_id, '')}")
    return "\n".join(lines)


def _invoke_sonnet_with_retry(prompt: str, max_tokens: int, system: str) -> BedrockInvokeResult:
    # AA-518 — "n7_faq" stage config (seeded sonnet/acc3, matching the prior hardcoded literals).
    cfg = get_stage_config_sync("n7_faq")
    last_err: Optional[BedrockUnavailable] = None
    for attempt in range(1, _MAX_INVOKE_ATTEMPTS + 1):
        try:
            return invoke_claude(
                prompt, model=cfg.model_id, max_tokens=max_tokens, system=system,
                account=cfg.account_route or "acc3",
            )
        except BedrockUnavailable as e:
            last_err = e
            logger.warning("e4_faq_sonnet_retry", attempt=attempt, max_attempts=_MAX_INVOKE_ATTEMPTS, error=str(e))
            if attempt < _MAX_INVOKE_ATTEMPTS:
                time.sleep(_RETRY_BACKOFF_SECONDS * attempt)
    raise FAQAnswerFailed(f"Sonnet invoke failed after {_MAX_INVOKE_ATTEMPTS} attempts: {last_err}") from last_err


def _parse_batch_response(raw_text: str, batch: list[FAQCandidate]) -> list[FAQItem]:
    """Positional mapping (chunk i -> batch[i]), same defensive-fallback
    shape as `generation._parse_batch_response()` — returns `[]` (a parse
    failure, handled by the caller) rather than guessing a partial mapping."""
    markers = list(_FAQ_MARKER_RE.finditer(raw_text))
    if len(markers) != len(batch):
        return []
    items = []
    for i, m in enumerate(markers):
        end = markers[i + 1].start() if i + 1 < len(markers) else len(raw_text)
        answer = raw_text[m.end():end].strip()
        items.append(FAQItem(question=batch[i].question, answer=answer, source_id=batch[i].source_id))
    return items


# ---------------------------------------------------------------- render + JSON-LD (deterministic)

def render_faq_section(faq_items: list[FAQItem]) -> str:
    """Code-inserted rendering (never model-written), same "structure comes
    from code" principle as E2's H2-heading insertion. `[R:atom_id]` tags
    stay in this text — F1 grounding runs against `body_tagged` as a whole
    and needs to see them here."""
    if not faq_items:
        return ""
    lines = ["## FAQ", ""]
    for item in faq_items:
        lines.append(f"**Q: {item.question}**")
        lines.append(f"A: {item.answer}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_faq_jsonld(faq_items: list[FAQItem]) -> Optional[dict]:
    """Pure, deterministic — no LLM. `[R:atom_id]`/`[F:fact_id]` citation
    tags are stripped from `Answer.text`: schema.org's FAQPage answer text
    is reader/search-engine-facing copy, not a place for internal
    provenance markup. Returns `None` for an empty list (mirrors
    `Piece.faq_jsonld`'s `Optional[dict] = None` default — "no FAQ" is
    represented as no JSON-LD, never an empty-but-present FAQPage)."""
    if not faq_items:
        return None
    return {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": item.question,
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": TAG_RE.sub("", item.answer).strip(),
                },
            }
            for item in faq_items
        ],
    }


# ---------------------------------------------------------------- orchestration

def apply_faq(
    blog_piece: Piece, brief: Brief, atom_text_by_id: dict[str, str],
    brand_rubric_text: str = AA_BRAND_IDENTITY_PROMPT,
) -> Piece:
    """E4 end-to-end against `blog_piece`: answers `brief.faq_candidates`,
    appends the rendered FAQ section to `body_tagged`, and sets
    `faq_items`/`faq_jsonld`. Mutates and returns the same `Piece` (same
    "mutate the object you were given" convention as `gates.run_gates()`).
    A no-op (returns `blog_piece` unchanged) when there are no candidates —
    matches E1's existing behavior of only ever appending "FAQ" when
    `fw_cfg.get("faq")` produced at least the section, never an empty one.
    Raises `FAQAnswerFailed` (propagated from `answer_faq()`) rather than
    ever splicing in a partial/empty FAQ block.

    `brand_rubric_text` (AA-404 writer-side wire): passed straight through to
    `answer_faq()` — see its docstring."""
    if not brief.faq_candidates:
        return blog_piece

    faq_items = answer_faq(brief.faq_candidates, atom_text_by_id, brand_rubric_text)
    blog_piece.body_tagged = blog_piece.body_tagged.rstrip() + "\n\n" + render_faq_section(faq_items)
    blog_piece.faq_items = faq_items
    blog_piece.faq_jsonld = build_faq_jsonld(faq_items)
    return blog_piece


__all__ = ["answer_faq", "render_faq_section", "build_faq_jsonld", "apply_faq", "FAQAnswerFailed"]
