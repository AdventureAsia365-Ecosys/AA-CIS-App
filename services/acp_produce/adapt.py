"""
services.acp_produce.adapt — N7 E3 (Adapt channel), AA-371.

STEP 0 (Linear AA-371 comment, 06/08/2026) confirmed the design executed
here:
- 1 Piece = 1 channel — already the real DB shape (`acp_deliver.pieces.
  channel`, migration 094, scalar TEXT NOT NULL, no list/JSONB). `Piece`
  itself gained the `channel` field in this same issue (models.py).
  `adapt_channels()` returns up to 2 NEW `Piece` objects (channel="facebook",
  channel="tiktok"), never mutates the blog `Piece` it reads from.
- Runs in the same E1-E5 generation pass as E2, BEFORE any F-gate — matches
  the original AA-298 order (`brief compile -> apply_output_rules() -> E1-E5
  generation -> F1-F9 gates`, pipeline.py's own docstring). Each returned
  channel Piece still goes through the same `run_piece_through_produce_gates
  ()` (F1/F8/F9) as the blog piece once a real slot-runner exists — nothing
  here special-cases channel for gating, that stays pipeline.py's job.
- Input is the blog Piece's OWN already-tagged body — atom ids come from
  what it actually cites (`atom_usage.py::atom_ids_cited()`, the same
  `[R:atom_id]`-only convention F1/N8 usage-logging already rely on), never
  re-derived from the atom pack. This is a hard requirement from the issue
  text ("tái sử dụng cùng atom_ids đã cite trong blog gốc, không re-derive
  từ atom pack lại từ đầu — tránh lệch nội dung giữa các kênh") and is also
  why E3 doesn't need to wait for E4: it deliberately never reads the blog's
  FAQ content (E4, faq.py, owns that section and appends it separately).
  **Caller ordering note** (no real slot-runner exists yet to enforce this,
  so it's a call-order contract, not a code guard): call `adapt_channels()`
  on the blog `Piece` BEFORE `faq.apply_faq()` mutates it. Calling after
  would let `atom_ids_cited()` also pick up atoms cited only inside the
  appended FAQ section — not unsafe (they'd still be real, already-grounded
  atoms) but it drifts from "adapt the blog content", the scope this issue
  actually asked for.
- Model is Bedrock satellite acc3 Sonnet (AA-397, acc1 fallback under it), same
  `invoke_claude(..., model="sonnet")` call as E2 (generation.py) — CHỐT, not a proposal
  (AA-334 Cancelled, see generation.py's own docstring). Palmyra must never
  appear anywhere in this module.
- Two independent Sonnet calls (facebook, tiktok), not one combined call —
  so a failure on one channel never blocks the other. A failed channel is
  logged (`e3_adapt_channel_failed`) and simply omitted from the returned
  list; `adapt_channels()` itself never raises for a single-channel failure
  (only `_invoke_channel_with_retry()` raises, and that's caught internally).
"""
from __future__ import annotations

import time
from typing import Literal

import structlog

from services.acp_produce.atom_usage import atom_ids_cited
from services.acp_produce.models import Piece
from services.content_generation.brand_standards import AA_BRAND_IDENTITY_PROMPT
from shared.llm_client.bedrock_satellite import BedrockInvokeResult, BedrockUnavailable, invoke_claude
from shared.llm_client.role_config import get_stage_config_sync
from shared.llm_client.call_log import record_call_sync
from shared.llm_client.pricing import calc_cost

logger = structlog.get_logger()

_MAX_INVOKE_ATTEMPTS = 3  # 1 original + 2 retries, same policy as generation.py's E2
_RETRY_BACKOFF_SECONDS = 2.0
_MAX_TOKENS = 1024  # both channel outputs are short-form, well under E2's per-batch ceiling

ChannelName = Literal["facebook", "tiktok"]

# AA-404 writer-side wire (F9 deep-dive TL;DR #1): this used to be a
# module-level constant built once at import time from the generic
# `AA_BRAND_IDENTITY_PROMPT` — E3 was one of the 4 writer modules still doing
# this while F9's judge (PR #158) had already moved on to a real per-tenant
# rubric (`brand.py::fetch_brand_rubric_text()`). Now built per-call from
# `adapt_channels()`'s own `brand_rubric_text` parameter, which defaults to
# `AA_BRAND_IDENTITY_PROMPT` — every pre-AA-404 caller/test that doesn't pass
# it keeps adapting against exactly the prompt it always has.
_ADAPT_SYSTEM_PROMPT_RULES = (
    "\n\nHARD RULES FOR THIS ADAPT TASK:\n"
    "- You may ONLY cite [R:atom_id] ids from the ATOM LIST given below — never invent a new id,\n"
    "  never cite an id not in that list.\n"
    "- Do not add any factual claim that isn't already in the blog text given to you.\n"
    "- Do NOT use [F:...] tags anywhere.\n"
    "- Even under this channel's tight word budget, naturally work in at least one of the REQUIRED\n"
    "  language words above (Design / Curated / Refined / Tailored / Journey) — never drop brand\n"
    "  voice just to save words. A short piece still has to sound like this brand, not a generic\n"
    "  travel caption that happens to be about this trip.\n"
)


def _build_adapt_system_prompt_base(brand_rubric_text: str) -> str:
    return (
        "You are adapting an ALREADY-WRITTEN blog piece into a shorter channel-specific format.\n\n"
        + brand_rubric_text.strip() + _ADAPT_SYSTEM_PROMPT_RULES
    )

_FACEBOOK_INSTRUCTIONS = (
    "\nFACEBOOK CAPTION FORMAT — this is the hook_story_cta framework (hook, then story/atom\n"
    "detail, then a call to action) — output ONLY this, nothing else:\n"
    "- One short paragraph, 50-120 words, brand voice, citing the ONE atom given to you below\n"
    "  (see \"ATOMS YOU MAY CITE\") with [R:atom_id].\n"
    "- Build the ENTIRE caption around that one atom/moment — one experience, one emotion. Do not\n"
    "  reference other experiences, shift between locations, or try to summarize the whole trip;\n"
    "  hook_story_cta is a single-atom format, not a highlights reel.\n"
    "- The paragraph's FIRST sentence must be a hook — a striking detail or question that earns\n"
    "  the next sentence, not a generic opener.\n"
    "- The paragraph's LAST sentence must invite the reader to \"Design This Journey\" (the exact\n"
    "  brand CTA phrase — never \"Book Now\", never a markdown link or bare URL, written as\n"
    "  conversational prose the way a real Facebook caption ends).\n"
    "- Then, on the line immediately after (no blank line before it), a line starting with\n"
    "  \"HASHTAGS:\" followed by 3-5 relevant hashtags.\n"
)

_TIKTOK_INSTRUCTIONS = (
    "\nTIKTOK KIT FORMAT — output ONLY these three labeled blocks, in order:\n"
    "\"HOOK:\" one line, <=15 words, no citation needed — must be strong enough on its own to\n"
    "  stop a scroll in the first second (a specific, surprising detail or a real question,\n"
    "  never a generic opener).\n"
    "\"SCRIPT:\" 100-150 words, spoken/conversational, structured as TIMED BEATS — 2-3 distinct\n"
    "  moments in sequence (not one flat paragraph), each building on the last, with at least\n"
    "  one [R:atom_id] citation carried over from the blog text. The FINAL beat must be a clear\n"
    "  PAYOFF — the moment that delivers on what the HOOK promised (a concrete reveal, a\n"
    "  satisfying detail, the reason this was worth watching) — never a trailing summary or a\n"
    "  generic sign-off.\n"
    "\"VISUAL:\" 1-2 lines of shot/visual direction notes (no video/image is generated — this is\n"
    "  text guidance only for whoever films it).\n"
    "- \"Spoken/conversational\" means natural sentence rhythm and contractions, not casual slang —\n"
    "  the brand voice (calm, assured, selective, precise, curated, composed, premium) still has to\n"
    "  come through when SCRIPT is read aloud. Write it the way a calm, well-traveled person would\n"
    "  actually talk about this trip, not a hype-y ad read.\n"
)

_CHANNEL_INSTRUCTIONS: dict[ChannelName, str] = {
    "facebook": _FACEBOOK_INSTRUCTIONS,
    "tiktok": _TIKTOK_INSTRUCTIONS,
}

# Substrings that MUST be present for a response to be considered parseable —
# same "unparseable == invoke failure, never guess a partial result" stance
# generation.py's _parse_batch_response() already established for E2.
_CHANNEL_REQUIRED_MARKERS: dict[ChannelName, tuple[str, ...]] = {
    "facebook": ("HASHTAGS:",),
    "tiktok": ("HOOK:", "SCRIPT:", "VISUAL:"),
}


class AdaptChannelFailed(Exception):
    """One channel's Sonnet invoke kept failing after retries, or its
    response didn't contain the required format markers. Raised internally
    by `_invoke_channel_with_retry()` and always caught by
    `adapt_channels()` — never propagates out of this module."""


def adapt_channels(
    blog_piece: Piece, atom_text_by_id: dict[str, str], brand_rubric_text: str = AA_BRAND_IDENTITY_PROMPT,
) -> list[Piece]:
    """E3. Returns 0, 1, or 2 new `Piece` objects (channel="facebook" /
    "tiktok") adapted from `blog_piece.body_tagged`. A channel that fails
    after retries is logged and omitted — never raises, never blocks the
    other channel.

    `brand_rubric_text` (AA-404 writer-side wire): the real per-tenant rubric
    F9's judge is scored against (`brand.py::fetch_brand_rubric_text()`),
    threaded down by `slot_runner.py`'s one per-slot fetch — defaults to the
    generic `AA_BRAND_IDENTITY_PROMPT` for any caller/test that doesn't have
    one (unchanged pre-AA-404 behavior)."""
    cited_ids = atom_ids_cited(blog_piece)
    cited_atom_text = {aid: atom_text_by_id[aid] for aid in cited_ids if aid in atom_text_by_id}

    pieces: list[Piece] = []
    for channel in ("facebook", "tiktok"):
        channel_atom_text = _select_atoms_for_channel(cited_atom_text, channel)
        try:
            body = _invoke_channel_with_retry(blog_piece.body_tagged, channel_atom_text, channel, brand_rubric_text)
        except AdaptChannelFailed as e:
            logger.error("e3_adapt_channel_failed", channel=channel,
                         blog_piece_id=blog_piece.piece_id, error=str(e))
            continue
        pieces.append(Piece(piece_id=f"{blog_piece.piece_id}#{channel}", body_tagged=body, channel=channel))
    return pieces


def _select_atoms_for_channel(cited_atom_text: dict[str, str], channel: ChannelName) -> dict[str, str]:
    """AA-404 Part 2: hook_story_cta's "one atom, one emotion" F8 criterion
    (facebook only) fails in real production data because the writer was
    handed EVERY atom the blog cited, with `_FACEBOOK_INSTRUCTIONS` giving
    only a citation FLOOR ("at least one") — never a ceiling
    (docs/implementation-notes/AA-404.md §1/§4, a held piece cited 4 distinct
    atoms in what's supposed to be a single-atom post). Fixed at the DATA
    layer, not just the prompt: facebook only ever SEES one atom, so it
    cannot cite more even if the wording above were ignored. tiktok keeps
    every cited atom unchanged — hook_beats_payoff (its framework) has no
    single-atom constraint, and its SCRIPT format legitimately spans
    multiple timed beats.

    Atom choice is deterministic: `cited_atom_text` iterates in
    `atom_ids_cited()`'s first-seen-in-body order (atom_usage.py's own
    docstring), so this picks the FIRST atom the blog piece cites — the
    theory being that whatever the writer led with is the moment most
    load-bearing for the piece, not an arbitrary pick."""
    if channel != "facebook":
        return cited_atom_text
    if not cited_atom_text:
        return {}
    first_id = next(iter(cited_atom_text))
    return {first_id: cited_atom_text[first_id]}


def _build_prompt(blog_body: str, cited_atom_text: dict[str, str], channel: ChannelName) -> str:
    lines = [
        "BLOG TEXT (adapt from this, do not add facts beyond it):",
        blog_body,
        "",
        "ATOMS YOU MAY CITE (only these ids, exactly as given):",
    ]
    lines += [f"- {aid}: {text}" for aid, text in cited_atom_text.items()] or ["- (none cited in the blog text)"]
    return "\n".join(lines)


def _invoke_channel_with_retry(
    blog_body: str, cited_atom_text: dict[str, str], channel: ChannelName, brand_rubric_text: str,
) -> str:
    system = _build_adapt_system_prompt_base(brand_rubric_text) + _CHANNEL_INSTRUCTIONS[channel]
    prompt = _build_prompt(blog_body, cited_atom_text, channel)
    required_markers = _CHANNEL_REQUIRED_MARKERS[channel]
    # AA-518 — "n7_adapt" stage config (seeded sonnet/acc3, matching the prior hardcoded literals).
    cfg = get_stage_config_sync("n7_adapt")

    last_err: Exception | None = None
    for attempt in range(1, _MAX_INVOKE_ATTEMPTS + 1):
        try:
            result: BedrockInvokeResult = invoke_claude(
                prompt, model=cfg.model_id, max_tokens=_MAX_TOKENS, system=system,
                account=cfg.account_route or "acc3",
            )
        except BedrockUnavailable as e:
            last_err = e
            logger.warning("e3_adapt_sonnet_retry", channel=channel, attempt=attempt,
                            max_attempts=_MAX_INVOKE_ATTEMPTS, error=str(e))
            if attempt < _MAX_INVOKE_ATTEMPTS:
                time.sleep(_RETRY_BACKOFF_SECONDS * attempt)
            continue

        markers_ok = all(marker in result.text for marker in required_markers)
        record_call_sync(
            stage="n7_adapt", role="writer", model=result.model_used,
            tokens_in=result.usage.get("input_tokens"), tokens_out=result.usage.get("output_tokens"),
            cost_usd=calc_cost(cfg.model_id, result.usage.get("input_tokens", 0),
                                result.usage.get("output_tokens", 0)),
            tenant_id=None,
            quality_signal={"channel": channel, "required_markers_present": markers_ok},
            stop_reason=result.stop_reason,
            account=cfg.account_route or "acc3", fallback_used=False, provider="bedrock-satellite",
        )
        if not markers_ok:
            last_err = AdaptChannelFailed(
                f"response missing required marker(s) {required_markers} for channel={channel}"
            )
            logger.warning("e3_adapt_unparseable_retry", channel=channel, attempt=attempt,
                            max_attempts=_MAX_INVOKE_ATTEMPTS)
            if attempt < _MAX_INVOKE_ATTEMPTS:
                time.sleep(_RETRY_BACKOFF_SECONDS * attempt)
            continue

        logger.info("e3_adapt_channel_success", channel=channel, model_used=result.model_used,
                     latency_ms=result.latency_ms, usage=result.usage)
        return result.text.strip()

    raise AdaptChannelFailed(
        f"channel={channel} failed after {_MAX_INVOKE_ATTEMPTS} attempts: {last_err}"
    ) from last_err


__all__ = ["adapt_channels", "AdaptChannelFailed"]
