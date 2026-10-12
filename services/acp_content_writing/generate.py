"""
services.acp_content_writing.generate — the T9 write call (SKILL_v2.md workflow step 9) and the
attempt-2 rewrite call (feedback-driven, not a fresh write).

LLM layer: shared.llm_client.client.LLMClient (model_tier="sonnet") — the same "exclusive layer"
T7/T8 already use, confirmed by STEP0 (docs/claude_audit/AA-450-00-...md §6) using the exact
3-way reasoning services/acp_angle_gate/generate.py's own docstring already worked through for
T8's angle-generation call: T9 writes ONE short single-channel piece (same job shape as the old,
not-reused acp_s4_social/writer.py), not the multi-H2 long-form draft
services/acp_produce/generation.py's Bedrock-satellite-acc1-Sonnet-only path is locked to
(AA-334), and not a cross-vendor quality judge (judge_client.py's Nova Pro, reserved for
quality_gates.py's F8/F9-equivalent gates — a writer must never share a model with the judge
scoring it, ADR-2026-014/027).

Deliberately SYNCHRONOUS functions, not `async def` — same "wrap at the async/sync boundary, not
inside every helper" decision AA-416 already made and documented
(docs/implementation-notes/AA-416-fix-event-loop-blocking.md) for the exact same class of
blocking Bedrock call. service.py (this package's async orchestrator) wraps every call here in
`await asyncio.to_thread(...)` — built in from the start per the build task's own explicit
instruction, not patched in later after a production incident the way N7's own fix was.
"""
from __future__ import annotations

import json
import re

from json_repair import repair_json

from services.acp_angle_gate.brand_audience import BrandAudience
from services.acp_angle_gate.channel_style import ChannelStyle
from services.acp_angle_gate.goals import Goal
from services.acp_content_writing.prompts import PLAIN_TEXT_CHANNELS, SYSTEM_PROMPT, build_user_prompt
from services.acp_content_writing.quality_gates import strip_citation_tags
from shared.llm_client.client import LLMClient
from shared.llm_client.models import LLMRequest
from shared.llm_client.call_log import record_call_sync

# AA-768 (S233) — output ceiling per channel. A single 2048 cap cut 20/92 t9_write calls in the
# S232 runs (stop_reason=max_tokens), all of them blogs: the blog JSON envelope + a 4,700-13,000
# char body + citation tags needs far more. Short channels keep head-room above their own bands.
_MAX_TOKENS_BY_CHANNEL = {"blog": 8000}
_MAX_TOKENS_DEFAULT = 3000
# One retry with more room when the model still stops on the ceiling (rare after the raise).
_TRUNCATION_RETRY_FACTOR = 1.5
_TRUNCATION_RETRY_CAP = 16000
# Anthropic/Converse report "max_tokens"; OpenAI-compatible routes report "length".
_TRUNCATED_STOP_REASONS = {"max_tokens", "length"}
_MAX_TOKENS = 2048  # sentence repair + SEO fit: short JSON replies


def max_tokens_for(channel: str) -> int:
    return _MAX_TOKENS_BY_CHANNEL.get(channel, _MAX_TOKENS_DEFAULT)


def _generate_full(request: LLMRequest, channel: str, *, attempt: int, tenant_id,
                   angle_gate_request_id):
    """Run the writer call; when it stops on the token ceiling, log that call and run it once
    more with a larger ceiling. Returns (resp, cost of every call made). A reply that is still
    cut after the retry is returned as-is — the F0_complete gate then reports it."""
    client = LLMClient()
    resp = client.generate(request)
    _record_t9_write_call(resp, channel, attempt=attempt,
                           tenant_id=tenant_id, angle_gate_request_id=angle_gate_request_id)
    cost = resp.cost_usd
    if getattr(resp, "stop_reason", None) in _TRUNCATED_STOP_REASONS:
        bigger = min(int(request.max_tokens * _TRUNCATION_RETRY_FACTOR), _TRUNCATION_RETRY_CAP)
        retry = LLMRequest(system_prompt=request.system_prompt, user_prompt=request.user_prompt,
                           stage=request.stage, max_tokens=bigger)
        resp = client.generate(retry)
        _record_t9_write_call(resp, channel, attempt=attempt, tenant_id=tenant_id,
                               angle_gate_request_id=angle_gate_request_id, truncation_retry=True)
        cost += resp.cost_usd
    return resp, cost


class SeoEnvelopeError(Exception):
    """AA-514 — the blog channel's JSON envelope ({"seo_title","meta_description","slug","body"})
    couldn't be parsed even after json-repair salvage. Raised rather than silently falling back
    to treating the raw response as plain body text — a malformed envelope on the ONE channel
    that's supposed to produce one is a real write failure (same "never silently persist a
    malformed result" precedent services/acp_angle_gate/generate.py::AngleGenerationError
    already sets for T8's own JSON contract), not a shape to guess at."""


def _parse_blog_envelope(raw: str) -> tuple[str, dict, str | None]:
    """Returns (body, seo_meta, summary) — seo_meta = {"seo_title", "meta_description", "slug"},
    each `None` if the key was missing or not a string (a real parse gap, not "legitimately
    empty" — quality_gates.py::gate_seo_surface() reports a missing field as its own violation
    either way, so no information is lost by not distinguishing the two here). `summary` (AA-498
    Decision 4) is the SAME soft-fail treatment — a missing/non-string "summary" key is `None`,
    never a reason to raise SeoEnvelopeError; the piece itself must not fail over a missing
    summary."""
    text = _strip_fences(raw)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = repair_json(text, return_objects=True)
    if not isinstance(data, dict) or not isinstance(data.get("body"), str) or not data["body"].strip():
        raise SeoEnvelopeError(f"Could not parse blog JSON envelope (missing/empty 'body'): {raw[:300]!r}")
    seo_meta = {
        key: data.get(key) if isinstance(data.get(key), str) else None
        for key in ("seo_title", "meta_description", "slug")
    }
    summary = data.get("summary")
    summary = summary.strip() if isinstance(summary, str) and summary.strip() else None
    return data["body"], seo_meta, summary


_SUMMARY_MARKER = "===SUMMARY==="


def _extract_summary(text: str) -> tuple[str, str | None]:
    """AA-498 (Decision 4) — the 7 non-blog channels return plain text with a trailing
    `===SUMMARY===` marker line (see prompts.py::_SUMMARY_INSTRUCTIONS). Splits it out and
    returns (content_without_marker, summary_or_None). Soft-fail: a response with no marker at
    all (model didn't follow the instruction) returns the text unchanged and `None` — never
    raises, the same "missing summary is not a write failure" contract _parse_blog_envelope()
    follows for the blog channel."""
    if _SUMMARY_MARKER not in text:
        return text, None
    content, _, tail = text.partition(_SUMMARY_MARKER)
    content = content.rstrip()
    summary = tail.strip() or None
    return content, summary


def _strip_fences(raw: str) -> str:
    """Same defensive strip content_generation/graph.py::generate_node() and
    services/acp_angle_gate/generate.py both already use — the system prompt asks for plain text,
    not JSON, but a model wrapping its answer in a markdown fence anyway is a real, seen failure
    mode worth guarding against cheaply."""
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        if len(parts) >= 2:
            raw = parts[1]
            # drop a leading language tag line if present (e.g. "```text\n...")
            first_line, _, rest = raw.partition("\n")
            if rest and len(first_line.split()) <= 1:
                raw = rest
        raw = raw.strip()
    return raw


_MD_BOLD_RE = re.compile(r"(\*\*|__)(.+?)\1", re.DOTALL)
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def strip_markdown(text: str) -> str:
    """AA-768 (S233) — plain-text channels: drop **bold**/__bold__, "# " heading markers and
    [text](url) links (→ "text url"). Hashtags (#Bhutan, no space) are kept."""
    text = _MD_BOLD_RE.sub(r"\2", text)
    text = _MD_HEADING_RE.sub("", text)
    return _MD_LINK_RE.sub(r"\1 \2", text)


def write_content(
    *, content_seed: str, goal: Goal, channel_style: ChannelStyle,
    brand_audience: BrandAudience, angle: dict, cta: str,
    destination: str | None = None, trip_name: str | None = None, atom_id: str | None = None,
    route_segments: list[tuple[str, str]] | None = None, keyword: str | None = None,
    tenant_id: str | None = None, angle_gate_request_id: str | None = None,  # AA-505, optional
    facts_text: str | None = None,  # AA-529, optional
    scope: str | None = None,  # S233 — which part of the trip the piece covers
) -> tuple[str, float, dict, str | None]:
    """Attempt 1 — SKILL_v2.md workflow step 9, fresh write. Returns (content_text, cost_usd,
    seo_meta, summary) — `seo_meta` is `{"seo_title": None, "meta_description": None, "slug":
    None}` for every non-blog channel (the writer's output contract for those 7 is UNCHANGED,
    still plain text — see build_user_prompt()'s own docstring); for blog, the response is a
    JSON envelope (AA-514) parsed by `_parse_blog_envelope()`. `summary` (AA-498 Decision 4) is
    a 1-2 sentence internal-only summary from the SAME call (near-zero marginal cost, migration
    124's own header) — `None` if the model didn't produce one, never a write failure either way.

    `atom_id` (AA-452, defaults to `None` so every pre-AA-452 caller/test is unaffected): passed
    straight through to `build_user_prompt()` — only consumed there, and only for `channel=='blog'`
    (see that function's own docstring).

    `route_segments` (AA-513, defaults to `None` so every pre-AA-513 caller/test is unaffected):
    a Route/Blog pick's own (atom_id, text) pairs in day order — passed straight through, see
    build_user_prompt()'s own docstring.

    `keyword` (AA-514, defaults to `None`): the SEO keyword gate_seo_surface() checks title/meta
    against — passed straight through, only used for `channel=='blog'`.

    `facts_text` (AA-529, defaults to `None`): a pre-formatted `[Fact id=<id>]`-labeled block
    (`services/acp_content_writing/facts.py::format_facts_block()`) — passed straight through to
    `build_user_prompt()`, which appends it to the CONTENT SEED regardless of channel/route
    branch. `None`/empty (no Facts Entry exists yet for this tenant+platform) is a no-op."""
    user_prompt = build_user_prompt(
        content_seed=content_seed, goal=goal, channel_style=channel_style,
        brand_audience=brand_audience, angle=angle, cta=cta,
        destination=destination, trip_name=trip_name, atom_id=atom_id,
        route_segments=route_segments, keyword=keyword, facts_text=facts_text, scope=scope,
    )
    channel = channel_style["channel"]
    request = LLMRequest(system_prompt=SYSTEM_PROMPT, user_prompt=user_prompt,
                          stage="t9_write", max_tokens=max_tokens_for(channel))
    resp, cost = _generate_full(request, channel, attempt=1,
                                tenant_id=tenant_id, angle_gate_request_id=angle_gate_request_id)
    if channel == "blog":
        body, seo_meta, summary = _parse_blog_envelope(resp.content)
        return body, cost, seo_meta, summary
    content, summary = _extract_summary(_strip_fences(resp.content))
    if channel in PLAIN_TEXT_CHANNELS:
        content = strip_markdown(content)
    return content, cost, {"seo_title": None, "meta_description": None, "slug": None}, summary


def _record_t9_write_call(resp, channel: str, *, attempt: int, tenant_id, angle_gate_request_id,
                          truncation_retry: bool = False) -> None:
    """AA-505 — shared by write_content()/rewrite_with_feedback(). Lightweight, real, immediate
    heuristic (output length is non-trivial) — the MEANINGFUL quality signal for this piece
    (whether it actually passed T10) is logged separately by quality_gates.py's own "t10_judge"
    rows, right after run_quality_gates() runs on this exact output — deliberately not deferred
    here."""
    record_call_sync(
        stage="t9_write", role="writer", model=resp.model_used,
        tokens_in=getattr(resp, "input_tokens", None), tokens_out=getattr(resp, "output_tokens", None),
        cost_usd=resp.cost_usd,
        tenant_id=tenant_id, angle_gate_request_id=angle_gate_request_id,
        quality_signal={"channel": channel, "attempt": attempt, "output_len_chars": len(resp.content),
                        **({"truncation_retry": True} if truncation_retry else {})},
        stop_reason=getattr(resp, "stop_reason", None),
        account=getattr(resp, "satellite_account", None),
        fallback_used=getattr(resp, "fallback_used", None),
    )


def rewrite_with_feedback(
    *, content_seed: str, goal: Goal, channel_style: ChannelStyle,
    brand_audience: BrandAudience, angle: dict, cta: str, revision_feedback: list[str],
    destination: str | None = None, trip_name: str | None = None, atom_id: str | None = None,
    route_segments: list[tuple[str, str]] | None = None, keyword: str | None = None,
    tenant_id: str | None = None, angle_gate_request_id: str | None = None,  # AA-505, optional
    facts_text: str | None = None,  # AA-529, optional
    scope: str | None = None,  # S233 — which part of the trip the piece covers
) -> tuple[str, float, dict, str | None]:
    """Attempt 2 (the only retry — Phase 1's confirmed cap of 2 total attempts) — the SAME
    write call, with `revision_feedback` (the specific gate/violation strings T10 failed on,
    Phase 1 §2a's confirmed "specific, not generic" feedback shape) appended to the prompt. A
    fresh full write from the same brief, not a diff/patch — same "return the full corrected
    text, never a partial section" contract N7's own repair.py documents, kept here because it's
    the right contract, not because it's ported code (this module imports nothing from
    services.acp_produce.repair). Returns (content_text, cost_usd, seo_meta, summary) — see
    write_content()'s own docstring for the shape.

    `facts_text` (AA-529, defaults to `None`): same as `write_content()`'s own — passed straight
    through, so a rewrite attempt sees the exact same Facts as the first attempt did."""
    user_prompt = build_user_prompt(
        content_seed=content_seed, goal=goal, channel_style=channel_style,
        brand_audience=brand_audience, angle=angle, cta=cta,
        destination=destination, trip_name=trip_name, revision_feedback=revision_feedback,
        atom_id=atom_id, route_segments=route_segments, keyword=keyword, facts_text=facts_text, scope=scope,
    )
    channel = channel_style["channel"]
    request = LLMRequest(system_prompt=SYSTEM_PROMPT, user_prompt=user_prompt,
                          stage="t9_write", max_tokens=max_tokens_for(channel))
    resp, cost = _generate_full(request, channel, attempt=2,
                                tenant_id=tenant_id, angle_gate_request_id=angle_gate_request_id)
    if channel == "blog":
        body, seo_meta, summary = _parse_blog_envelope(resp.content)
        return body, cost, seo_meta, summary
    content, summary = _extract_summary(_strip_fences(resp.content))
    if channel in PLAIN_TEXT_CHANNELS:
        content = strip_markdown(content)
    return content, cost, {"seo_title": None, "meta_description": None, "slug": None}, summary


_REPAIR_SYSTEM = """You fix single sentences inside a finished piece of travel content.
For each numbered sentence you get the problem a checker found. Return a replacement that keeps
the sentence's role, tone and flow in the piece but removes the problem — the unsupported claim
or number, the word the brand forbids, or the wrong brand name (use the BRAND named in the SOURCE,
or drop the self-reference). Use only what the SOURCE states, or widely known background about a
place it names. Never add a new claim, number, place, scene detail or promise. If nothing true is
left to say, return "" to delete the sentence. Return ONLY JSON: {"replacements": ["...", "..."]}
— one string per numbered sentence, in the same order."""


def repair_sentences(
    *, sentences: list[dict], content_seed: str, facts_text: str | None = None,
    channel: str, tenant_id: str | None = None, angle_gate_request_id: str | None = None,
) -> tuple[list[str], float]:
    """AA-768 — attempt-2 sentence-level repair (see sentence_repair.py). `sentences` items carry
    "sentence" and "problem". Returns (replacements in the same order, cost). Raises
    SentenceRepairError when the reply cannot be parsed into exactly one string per sentence —
    the caller then falls back to the full rewrite."""
    source = content_seed + (f"\n\nFACTS:\n{facts_text}" if facts_text else "")
    numbered = "\n".join(
        f"{i}. SENTENCE: {it['sentence']}\n   PROBLEM: {it['problem']}" for i, it in enumerate(sentences, 1)
    )
    request = LLMRequest(system_prompt=_REPAIR_SYSTEM,
                         user_prompt=f"SOURCE:\n{source}\n\nSENTENCES TO FIX:\n{numbered}",
                         stage="t9_write", max_tokens=_MAX_TOKENS)
    resp = LLMClient().generate(request)
    record_call_sync(
        stage="t9_write", role="writer", model=resp.model_used,
        tokens_in=getattr(resp, "input_tokens", None), tokens_out=getattr(resp, "output_tokens", None),
        cost_usd=resp.cost_usd, tenant_id=tenant_id, angle_gate_request_id=angle_gate_request_id,
        quality_signal={"channel": channel, "attempt": 2, "mode": "sentence_repair",
                        "sentences": len(sentences)},
        stop_reason=getattr(resp, "stop_reason", None),
        account=getattr(resp, "satellite_account", None),
        fallback_used=getattr(resp, "fallback_used", None),
    )
    text = _strip_fences(resp.content)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = repair_json(text, return_objects=True)
    reps = data.get("replacements") if isinstance(data, dict) else None
    if not isinstance(reps, list) or len(reps) != len(sentences) or not all(isinstance(r, str) for r in reps):
        raise SentenceRepairError(f"bad sentence-repair reply: {resp.content[:200]!r}")
    return reps, resp.cost_usd


class SentenceRepairError(Exception):
    """The sentence-repair reply was not one string per sentence — fall back to the full rewrite."""


_SEO_FIT_SYSTEM = """You write the search-result title and meta description for a finished blog post.
Rules:
- Title: 45-60 characters, contains the target keyword verbatim, names the place the post is about,
  reads as one natural headline (never a fragment cut at a colon).
- Meta description: 140-155 characters, ONE complete sentence ending in '.', contains the target
  keyword verbatim, says what the reader gets from the post.
- Use only places and facts that appear in the post. No new claims, numbers or promises.
Return ONLY JSON: {"seo_title": "...", "meta_description": "..."}"""


def fit_seo_fields(
    *, body: str, seo_title: str | None, meta_description: str | None, keyword: str | None,
    brand_name: str | None = None, tenant_id: str | None = None,
    angle_gate_request_id: str | None = None,
) -> tuple[dict, float]:
    """AA-768 (S233) — regenerate an out-of-band blog SEO title/meta with one short call instead
    of cutting the string (PR #670 cut "Bhutan's Craft Bazaar: Thimphu's Weekend Market…" at the
    colon → a 21-char stub on 6/20 blogs). Returns ({"seo_title", "meta_description"} — a key is
    None when the reply did not give a usable string) and the cost. The caller validates the
    lengths and keeps the writer's own value when the new one is not inside the band."""
    parts = [f"TARGET SEO KEYWORD: {keyword}" if keyword else "No target keyword — write naturally."]
    if brand_name:
        parts.append(f"BRAND (never name another brand): {brand_name}")
    parts.append(f"CURRENT TITLE ({len(seo_title or '')} chars): {seo_title or '(none)'}")
    parts.append(f"CURRENT META ({len(meta_description or '')} chars): {meta_description or '(none)'}")
    parts.append(f"POST:\n{strip_citation_tags(body)[:6000]}")
    request = LLMRequest(system_prompt=_SEO_FIT_SYSTEM, user_prompt="\n\n".join(parts),
                         stage="t9_write", max_tokens=_MAX_TOKENS)
    resp = LLMClient().generate(request)
    record_call_sync(
        stage="t9_write", role="writer", model=resp.model_used,
        tokens_in=getattr(resp, "input_tokens", None), tokens_out=getattr(resp, "output_tokens", None),
        cost_usd=resp.cost_usd, tenant_id=tenant_id, angle_gate_request_id=angle_gate_request_id,
        quality_signal={"channel": "blog", "mode": "seo_fit"},
        stop_reason=getattr(resp, "stop_reason", None),
        account=getattr(resp, "satellite_account", None),
        fallback_used=getattr(resp, "fallback_used", None),
    )
    text = _strip_fences(resp.content)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = repair_json(text, return_objects=True)
    if not isinstance(data, dict):
        data = {}
    out = {}
    for key in ("seo_title", "meta_description"):
        val = data.get(key)
        out[key] = " ".join(val.split()) if isinstance(val, str) and val.strip() else None
    return out, resp.cost_usd


__all__ = ["SeoEnvelopeError", "fit_seo_fields", "max_tokens_for", "SentenceRepairError", "write_content",
           "rewrite_with_feedback",
           "repair_sentences"]
