"""AA-921 WM-28 — tenant preference summaries (CIPHER-style, <= 5 sentences), gated like rules (app ADR 0010).

With few edits per tenant a short summary of the tenant's voice learns faster than examples. It can still smuggle in
an unsourced promise ("always mention the free hotel pick-up"), so it goes through the same gates as a rule: drafted
on the judge route from that tenant's own `pass` signals only (masked text), linted (shape: <= 5 sentences / 125
words; constraint_only; `unsourced_claim`, below; no other tenant's brand; PREDICT), stored as a `preference_summary`
rule of the tenant (`shared.writing_rule`, CIS 280: tenant scope only, <= 250 tokens with the tenant's other rules,
trigger writing_rule_token_cap) and reaching a writer only inside an approved tenant release (R0 -> R2 -> shadow ->
one click), where insertion.rule_line renders it as "- Voice: ...".

  unsourced_claim   the quarantine check of WM-28: a summary fails (stored `rejected` by the lint, `quarantine:
                    true`) when it names a promise word (fact_filter.PROMISE_WORDS: free, included, guaranteed ...),
                    a specific (a digit, or a mask token [N] / [PRICE] / [TIME] / [URL] / [NAME]) or an add-verb
                    (lint.constraint_only). A voice summary describes tone and form; it never states a fact.

The LLM path is OFF by default (PREF_ENV unset): `run_preference` then makes no call and returns
`state: needs_llm_path`, and the admin route answers 409 `needs_llm_path`. Switch it on with
WRITING_MEMORY_PREF_SUMMARY=true on the worker once the judge route answers again (S260: every LLM path is failing).
The calls go through the Model Gateway (stage `writing_memory_mine`, the judge route of CIS 280), under the same run
($1) and month ($25) caps as the miner, and each one writes its llm_call_log row.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Optional

import structlog

from services.writing_memory import config as cfg
from services.writing_memory import lint as lint_mod
from services.writing_memory.masking import excerpt

logger = structlog.get_logger()

PREF_ENV = "WRITING_MEMORY_PREF_SUMMARY"
KIND = "preference_summary"
WRITERS = ("t2", "t9_blog", "t9_social")
MIN_SIGNALS = 4                  # >= 4 human pass signals ...
MIN_PIECES = 2                   # ... on >= 2 pieces of one tenant and writer
MAX_EXAMPLES = 10
MAX_TOKENS = 400
STATE_OFF = "needs_llm_path"
_SPECIFIC_RE = re.compile(r"\d|\[(?:N|PRICE|TIME|URL|NAME)\]")

SYSTEM_PROMPT = """You summarise how ONE travel brand edits the drafts a writer produced for it, as a short voice guide.

The examples show text the writer produced (BEFORE) and what the brand changed it to (AFTER). Specific facts are
masked: [NAME] = a place, operator or brand name, [N] = a number, [PRICE], [TIME], [URL].

Write at most 5 short sentences about TONE and FORM only: register, sentence length, person, rhythm, words the brand
cuts or avoids, formatting. Hard limits:
- Never tell the writer to add, mention, include, promise or highlight anything; never name a service, inclusion,
  place, time, price, number, discount or offer; no masked token.
- Plain English, imperative or descriptive ("Keep sentences short.", "Avoid exclamation marks.").
- match_phrases: 1 to 5 short phrases, exactly as they appear in the BEFORE texts, that the brand removed.
If the examples show no consistent voice preference, return summary "NONE" and match_phrases ["NONE"]."""

SUMMARY_SCHEMA = {
    "name": "preference_summary",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["summary", "match_phrases"],
        "properties": {"summary": {"type": "string"},
                       "match_phrases": {"type": "array", "items": {"type": "string"}}},
    },
}


def enabled() -> bool:
    """True only when the worker env opts in (default off: the LLM path is not available)."""
    return os.getenv(PREF_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def state() -> dict:
    return {"enabled": enabled(), "state": "ready" if enabled() else STATE_OFF, "env": PREF_ENV}


@dataclass
class Group:
    tenant_id: str
    writer: str
    signals: list

    @property
    def cluster_key(self) -> str:
        return f"{self.writer}|pref:voice"

    def support(self) -> dict:
        return {"signals": len(self.signals), "pieces": len({s["source_id"] for s in self.signals}), "tenants": 0,
                "reviewers": 0}

    def channels(self) -> list[str]:
        return sorted({s["channel"] for s in self.signals if s.get("channel")})[:8]


def build_groups(signals: Iterable[dict]) -> list[Group]:
    """Tenant human edits with text that passed the fact filter, per (tenant, writer), over the thresholds. Never
    another tenant's signal in a group; deterministic order (tenant, writer, signal id)."""
    by: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for s in signals:
        if (s.get("scope") != "tenant" or not s.get("tenant_id") or s.get("writer") not in WRITERS
                or s.get("fact_status") != "pass" or s.get("actor_type") not in ("admin", "tenant")
                or s.get("after_text") is None or not (s.get("before_text") or "").strip()):
            continue
        by[(s["tenant_id"], s["writer"])].append(s)
    out = []
    for (tid, writer), sigs in sorted(by.items()):
        sigs = sorted(sigs, key=lambda x: x["signal_id"])
        if len(sigs) >= MIN_SIGNALS and len({x["source_id"] for x in sigs}) >= MIN_PIECES:
            out.append(Group(tid, writer, sigs))
    return out


def split(group: Group) -> tuple[list[dict], list[dict]]:
    """(shown, held out for PREDICT): always >= PREDICT_MIN_EXPLAINED held out."""
    n = len(group.signals)
    shown = max(1, min(MAX_EXAMPLES, n - cfg.PREDICT_MIN_EXPLAINED))
    return group.signals[:shown], group.signals[shown:]


def build_user_prompt(group: Group, shown: list[dict]) -> str:
    lines = [f"Writer: {group.writer} (channels: {', '.join(group.channels()) or 'any'}).", "", "Examples:"]
    for i, s in enumerate(shown, 1):
        before, after = excerpt(s.get("before_text"), s.get("after_text"))
        lines.append(f"{i}. BEFORE: {before or '(none)'}")
        lines.append(f"   AFTER: {after or '(cut)'}")
    return "\n".join(lines)


def parse_summary(content: str) -> dict:
    obj = json.loads(content)
    if not isinstance(obj, dict) or not isinstance(obj.get("summary"), str) or not obj["summary"].strip():
        raise ValueError("reply has no summary")
    phrases = obj.get("match_phrases")
    if not isinstance(phrases, list) or not all(isinstance(p, str) for p in phrases):
        raise ValueError("match_phrases is not a list of strings")
    return {"summary": " ".join(obj["summary"].split()),
            "match_phrases": [p.strip() for p in phrases if p.strip()][: cfg.MAX_MATCH_PHRASES]}


def unsourced_claim(summary: str) -> dict:
    """The WM-28 quarantine check: no promise word, no specific, no add-verb (see the module docstring)."""
    from services.writing_memory.fact_filter import added_promises
    found = [f"promise '{w}'" for w in added_promises("", summary or "")]
    found += [f"specific '{m}'" for m in sorted(set(_SPECIFIC_RE.findall(summary or "")))]
    adds = lint_mod.constraint_only(summary or "")
    if not adds["ok"]:
        found.append(adds["detail"])
    ok = not found
    return {"ok": ok, "detail": "; ".join(found[:3]), **({} if ok else {"quarantine": True})}


def lint_summary(summary: str, match_phrases: list[str], *, held_out: list[dict], other_names: Iterable[str]) -> dict:
    """Every check of a drafted summary; `ok` only when all pass, `quarantine` when unsourced_claim failed."""
    out = {
        "shape": lint_mod.shape(KIND, summary, None, match_phrases),
        "constraint_only": lint_mod.constraint_only(summary),
        "unsourced_claim": unsourced_claim(summary),
        "five_gram_overlap": lint_mod.five_gram_overlap([summary, *match_phrases], set(), other_names),
        "predict": lint_mod.predict(match_phrases, held_out),
    }
    out["ok"] = all(v["ok"] for v in out.values() if isinstance(v, dict))
    out["scope"] = "tenant"
    out["basis"] = "tenant_preference"
    if not out["unsourced_claim"]["ok"]:
        out["quarantine"] = True
    return out


def _generate_sync(user_prompt: str):
    from shared.llm_client.client import LLMClient
    from shared.llm_client.models import LLMRequest
    return LLMClient().generate(LLMRequest(system_prompt=SYSTEM_PROMPT, user_prompt=user_prompt, stage=cfg.MINE_STAGE,
                                           max_tokens=MAX_TOKENS, temperature=0.0, json_schema=SUMMARY_SCHEMA))


async def draft_summary(pool, *, user_prompt: str, cluster_key: str, tenant_id: str) -> tuple[dict, str, float]:
    """One judge-route call through the Model Gateway; its llm_call_log row (or billed_unusable row) is written before
    it returns / raises. (parsed reply, model, cost)."""
    from shared.llm_client.call_log import billed_fields, record_call_with_pool, record_unusable
    resp = await asyncio.to_thread(_generate_sync, user_prompt)
    signal = {"app": "cis", "issue": "AA-921", "cluster_key": cluster_key[:200], "kind": KIND}
    try:
        d = parse_summary(resp.content)
    except Exception as e:
        await record_unusable(resp, stage=cfg.MINE_STAGE, role=cfg.MINE_ROLE, error=e, quality_signal_extra=signal,
                              tenant_id=tenant_id)
        raise
    await record_call_with_pool(
        pool, stage=cfg.MINE_STAGE, role=cfg.MINE_ROLE, model=resp.model_used,
        tokens_in=getattr(resp, "input_tokens", None), tokens_out=getattr(resp, "output_tokens", None),
        cost_usd=resp.cost_usd, quality_signal={**signal, "json_parsed": True,
                                                "none": d["summary"].upper() == "NONE"},
        stop_reason=getattr(resp, "stop_reason", None), account=getattr(resp, "satellite_account", None),
        fallback_used=getattr(resp, "fallback_used", None), provider=getattr(resp, "provider", None),
        tenant_id=tenant_id, **billed_fields(resp))
    return d, resp.model_used, float(resp.cost_usd or 0.0)


_SIGNALS_SQL = """
    SELECT signal_id::text AS signal_id, writer, channel, scope, tenant_id::text AS tenant_id, before_text, after_text,
           actor_type, fact_status, source_id
      FROM shared.writing_signal
     WHERE scope = 'tenant' AND fact_status = 'pass' AND writer IN ('t2', 't9_blog', 't9_social')
       AND created_at >= now() - make_interval(days => $1)
     ORDER BY signal_id
"""


async def run_preference(pool, ctx, *, max_calls: int = cfg.MINE_MAX_CALLS) -> dict:
    """One preference-summary run (the writing_memory_mine job with mode preference_summary). Off: no call, the
    `needs_llm_path` state. On: one draft per tenant and writer over threshold that has no open summary, linted and
    stored (candidate, or rejected by the lint with `quarantine` when it made a claim)."""
    if not enabled():
        return {"ran": False, "mode": KIND, "state": STATE_OFF, "calls": 0, "cost_usd": 0.0}
    from shared.cost_guard import load_run_budget
    from services.writing_memory import rules as rules_mod
    from services.writing_memory.budget import load_month_budget
    from services.writing_memory.mining import _corpora

    async with pool.acquire() as conn:
        sigs = [dict(r) for r in await conn.fetch(_SIGNALS_SQL, cfg.SIGNAL_WINDOW_DAYS)]
        open_keys = await rules_mod.open_cluster_keys(conn)
        month = await load_month_budget(conn)
        _, names, missing = await _corpora(conn)
    run_budget = await load_run_budget(pool, "bedrock", cfg.MINE_STAGE, day_scope="kind")
    groups = build_groups(sigs)
    result: dict = {"ran": True, "mode": KIND, "state": "ready", "groups": len(groups), "skipped_open": 0,
                    "candidates": 0, "quarantined": 0, "lint_rejected": 0, "none": 0, "unusable": 0, "calls": 0,
                    "cost_usd": 0.0, "stopped": None, "rules": []}
    for g in groups:
        if (g.writer, "tenant", g.tenant_id, g.cluster_key) in open_keys:
            result["skipped_open"] += 1
            continue
        if result["calls"] >= max_calls:
            result["stopped"] = "max_calls"
            break
        try:
            month.check(cfg.MINE_CALL_ESTIMATE_USD)
            run_budget.check(cfg.MINE_CALL_ESTIMATE_USD)
        except Exception as e:  # noqa: BLE001 — BudgetExceeded: stop cleanly
            result["stopped"] = f"budget: {e}"
            break
        shown, held = split(g)
        result["calls"] += 1
        try:
            d, model, cost = await draft_summary(pool, user_prompt=build_user_prompt(g, shown),
                                                 cluster_key=g.cluster_key, tenant_id=g.tenant_id)
        except Exception as e:  # noqa: BLE001 — one bad reply skips one group (its row is already logged)
            result["unusable"] += 1
            logger.warning("aa921_pref_draft_failed", writer=g.writer, error=f"{type(e).__name__}: {str(e)[:200]}")
            continue
        month.charge(cost)
        run_budget.charge(cost)
        result["cost_usd"] = round(result["cost_usd"] + cost, 6)
        if ctx is not None:
            ctx.add_cost(cost)
            await ctx.progress(calls=result["calls"], cost_usd=result["cost_usd"])
        if d["summary"].upper() == "NONE":
            result["none"] += 1
            continue
        others = [n for t, v in names.items() if t != g.tenant_id for n in v]
        lint = lint_summary(d["summary"], d["match_phrases"], other_names=others,
                            held_out=[{"before_text": s.get("before_text"), "after_text": s.get("after_text")}
                                      for s in held])
        if "names" in missing or "forbidden" in missing:
            lint["five_gram_overlap"] = {"ok": False, "detail": "tenant names unavailable"}
            lint["ok"] = False
        async with pool.acquire() as conn:
            rid = await rules_mod.insert_candidate(
                conn, kind=KIND, scope="tenant", tenant_id=g.tenant_id, writer=g.writer, cluster_key=g.cluster_key,
                rule_text=d["summary"], match_phrases=d["match_phrases"], applies_to=g.channels(),
                signal_ids=[s["signal_id"] for s in g.signals], support=g.support(), lint=lint, drafted_by=model,
                mine_job_id=getattr(ctx, "job_id", None))
        if rid:
            key = "candidates" if lint["ok"] else ("quarantined" if lint.get("quarantine") else "lint_rejected")
            result[key] += 1
            result["rules"].append({"rule_id": rid, "writer": g.writer, "tenant_id": g.tenant_id, "ok": lint["ok"]})
    result["month"] = month.summary()
    result["run_budget"] = run_budget.summary()
    return result


__all__ = ["KIND", "PREF_ENV", "STATE_OFF", "SUMMARY_SCHEMA", "SYSTEM_PROMPT", "build_groups", "build_user_prompt",
           "draft_summary", "enabled", "lint_summary", "parse_summary", "run_preference", "split", "state",
           "unsourced_claim"]
