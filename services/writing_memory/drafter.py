"""AA-917 WM-12 scope 3 — the rule drafter: one Model Gateway call per cluster on stage `writing_memory_mine`.

The stage is a `judge` stage on the non-Anthropic judge route (CIS 280; app ADR 0010 decision 5, ADR 0006 §5): writers
stay Anthropic, rules are drafted by another vendor. The input is entity-masked text only (masking.py), never raw
multi-tenant text. The reply is one JSON object (structured output): kind, rule_text, replacement, match_phrases.

llm_call_log, AA-927 canonical shape: a usable reply writes one row (`json_parsed: true`); a billed reply that cannot
be used writes one `billed_unusable` row and the caller skips the cluster. The job id comes from the worker's
`call_log.bind_job`.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Optional

from shared.llm_client.call_log import billed_fields, record_call_with_pool, record_unusable
from shared.llm_client.client import LLMClient
from shared.llm_client.models import LLMRequest
from services.writing_memory import config as cfg

MAX_TOKENS = 400

SYSTEM_PROMPT = """You turn repeated corrections of a travel-content writer into ONE short writing rule.

The examples show text the writer produced (BEFORE) and what a reviewer changed it to (AFTER), or a quality-gate
failure of the writer's text. Specific facts are masked: [NAME] = a place, operator or brand name, [N] = a number,
[PRICE], [TIME], [URL].

Write one rule that would have prevented most of these corrections. Hard limits:
- The rule may only CUT, BAN, REPHRASE or FORMAT. It must never tell the writer to add, mention, include, promise or
  highlight anything: no facts, services, inclusions, places, times or prices.
- At most 25 words, plain English, imperative ("Do not open with ...", "Cut ...", "Replace ... with ...").
- No masked token, name, number or brand in the rule, unless the rule is about the token itself.
- kind: "lexicon" when the rule bans or replaces a word or phrase (rule_text = the phrase itself; replacement = a plain
  neutral rewording that adds nothing, or "" to ban it); "repair_hint" when it tells a repair pass how to fix one
  gate failure; otherwise "writer_instruction". replacement is "" for every kind but a lexicon "replace" rule.
- match_phrases: 1 to 5 short phrases, exactly as they appear in the BEFORE texts, that the rule targets.
If no safe constraint rule explains the examples, return kind "writer_instruction", rule_text "NONE" and
match_phrases ["NONE"]."""

RULE_SCHEMA = {
    "name": "writing_rule",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["kind", "rule_text", "replacement", "match_phrases"],
        "properties": {
            "kind": {"type": "string", "enum": ["lexicon", "writer_instruction", "repair_hint"]},
            "rule_text": {"type": "string"},
            "replacement": {"type": "string"},
            "match_phrases": {"type": "array", "items": {"type": "string"}},
        },
    },
}


@dataclass(frozen=True)
class Draft:
    kind: str
    rule_text: str
    replacement: Optional[str]
    match_phrases: list[str]
    model: str
    cost_usd: float

    @property
    def is_none(self) -> bool:
        return self.rule_text.strip().upper() == "NONE"


def build_user_prompt(*, writer: str, channels: list[str], label: str, label_meaning: str, scope: str,
                      examples: list[dict]) -> str:
    """The cluster as the drafter sees it. `examples` are already masked excerpts: {before, after, problem}."""
    lines = [f"Writer: {writer} (channels: {', '.join(channels) or 'any'}); rule scope: {scope}.",
             f"What the corrections have in common: {label_meaning} ({label}).", "", "Examples:"]
    for i, ex in enumerate(examples, 1):
        lines.append(f"{i}. BEFORE: {ex.get('before') or '(none)'}")
        if ex.get("after") is not None:
            lines.append(f"   AFTER: {ex.get('after') or '(cut)'}")
        if ex.get("problem"):
            lines.append(f"   GATE SAID: {ex['problem']}")
    return "\n".join(lines)


def parse_draft(content: str) -> dict:
    """The reply as {kind, rule_text, replacement, match_phrases}; ValueError when it is not that object."""
    obj = json.loads(content)
    if not isinstance(obj, dict):
        raise ValueError("reply is not an object")
    kind, text = obj.get("kind"), obj.get("rule_text")
    phrases = obj.get("match_phrases")
    if kind not in ("lexicon", "writer_instruction", "repair_hint") or not isinstance(text, str) or not text.strip():
        raise ValueError(f"bad kind / rule_text: {kind!r}")
    if not isinstance(phrases, list) or not all(isinstance(p, str) for p in phrases):
        raise ValueError("match_phrases is not a list of strings")
    rep = obj.get("replacement")
    if rep is not None and not isinstance(rep, str):
        raise ValueError("replacement is not a string")
    rep = (rep or "").strip() or None if kind == "lexicon" else None     # only a lexicon rule replaces
    return {"kind": kind, "rule_text": text.strip(), "replacement": rep,
            "match_phrases": [p.strip() for p in phrases if p.strip()][: cfg.MAX_MATCH_PHRASES]}


def _generate_sync(user_prompt: str):
    # temperature 0: the same cluster should give the same rule
    return LLMClient().generate(LLMRequest(system_prompt=SYSTEM_PROMPT, user_prompt=user_prompt, stage=cfg.MINE_STAGE,
                                           max_tokens=MAX_TOKENS, temperature=0.0, json_schema=RULE_SCHEMA))


async def draft_rule(pool, *, user_prompt: str, cluster_key: str, tenant_id: Optional[str]) -> Draft:
    """One drafter call; the llm_call_log row (or the billed_unusable row) is written before it returns / raises."""
    resp = await asyncio.to_thread(_generate_sync, user_prompt)
    signal = {"app": "cis", "issue": "AA-917", "cluster_key": cluster_key[:200]}
    try:
        d = parse_draft(resp.content)
    except Exception as e:
        # AA-927: the reply was billed; its billed_unusable row (no ok-rate key), then the caller skips the cluster.
        await record_unusable(resp, stage=cfg.MINE_STAGE, role=cfg.MINE_ROLE, error=e, quality_signal_extra=signal,
                              tenant_id=tenant_id)
        raise
    await record_call_with_pool(
        pool, stage=cfg.MINE_STAGE, role=cfg.MINE_ROLE, model=resp.model_used,
        tokens_in=getattr(resp, "input_tokens", None), tokens_out=getattr(resp, "output_tokens", None),
        cost_usd=resp.cost_usd, quality_signal={**signal, "json_parsed": True, "kind": d["kind"],
                                                "none": d["rule_text"].upper() == "NONE"},
        stop_reason=getattr(resp, "stop_reason", None), account=getattr(resp, "satellite_account", None),
        fallback_used=getattr(resp, "fallback_used", None), provider=getattr(resp, "provider", None),
        tenant_id=tenant_id, **billed_fields(resp))
    return Draft(kind=d["kind"], rule_text=d["rule_text"], replacement=d["replacement"],
                 match_phrases=d["match_phrases"], model=resp.model_used, cost_usd=float(resp.cost_usd or 0.0))


__all__ = ["Draft", "RULE_SCHEMA", "SYSTEM_PROMPT", "build_user_prompt", "draft_rule", "parse_draft"]
