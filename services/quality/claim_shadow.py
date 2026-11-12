"""AA-1050 W4 (root ADR 0020 D6 / D7, audit arch.roadmap[4]) — the pinned claim instrument in SHADOW next to the live
T9 F9 and T2 decisions.

What it does. After a T9 piece or a T2 tenant version is saved (its status already decided and written), and on the
replay corpus (``claim_shadow_replay``), the instrument calibrated in W2 (``services/ground_truth/r1.py``, extraction
mode: the field text and the full source -> the quotes it calls unsupported; code confirms each quote is in the text)
reads the object and its verdict is stored in ``shared.claim_shadow_verdict`` (CIS 391) next to the live decision it
shadows:

  T9  baseline = the final ``F9_cta_fact`` entry of the gate ledger: pass | flag (blocking) | warn (non-blocking) |
      unavailable (the judge outage marker) | none;
  T2  baseline = the deterministic brand-claim check of the saved version (``judge_breakdown.brand_claims``,
      AA-795): flag when it left an unsupported claim, else pass (none when it did not run).

The shadow NEVER changes a status: it runs after the save, writes only its own table and the ledger, and every error
is swallowed (logged). Rules it keeps:

- **Switch**: ``CLAIM_SHADOW=on`` (ECS env, read per call); anything else, and the default, is off: no read, no call,
  no row.
- **Pinned instrument, no fallback (D7)**: ``CLAIM_SHADOW_INSTRUMENT`` (``<model>@<connection>:extraction``, default
  the D7 primary candidate). Each call is made ``VOTES`` times on the pinned model and connection (temperature 0, the
  R1 seed, the R1 prompts byte for byte); a reply from another model or connection is no vote; a sentence's verdict
  needs ``VOTES // 2 + 1`` agreeing real votes. A miss, a refusal, an unusable reply, a caller error or the spend
  guard make it ``unverified`` with a typed reason, never a pass.
- **Enforce map per (writer, class)**: ``mode_for`` is ``warn`` for every class. A class may be ``enforce`` only when
  it is listed in ``ENFORCE_OVERRIDES`` AND in the pinned instrument's passing record (``CALIBRATED``); both are empty
  until W2 measures a class. ``shadow_action`` records what the map would do (hold / review / warn); nothing acts on
  it.
- **Tenant-authored spans are never cut (D6)**: a flagged sentence that carries the tenant's own CTA or brand name is
  marked ``tenant_span`` and never counts towards the verdict or an action.
- **Cache first**: the cache key hashes the instrument, the votes, the prompt sha and every field's user prompt; the
  newest definitive verdict of the key is reused (a cached row: 0 calls, cost 0) before any call.
- **Hard spend guard**: a call starts only when the shadow stage's ledger spend over the window
  (``CLAIM_SHADOW_WINDOW_DAYS``, default 30) plus this run's charges plus the call's WORST case
  (``r1.worst_call_cost``: every reserved attempt at its retry ceiling, all output) fits ``CLAIM_SHADOW_CAP_USD``
  (default 0.30, at most ``HARD_CAP_USD``). An unreadable ledger closes the guard (no call). A reply is charged
  ``r1.billed_cost`` (worst case when its cost is missing); a caller error is charged the call's worst case and ends
  the run.
- **Ledger**: every billed reply writes one ``shared.llm_call_log`` row on stage ``claim_shadow`` (role judge,
  ``{"shadow": true}`` so ``is_shadow``, ``**billed_fields(resp)``); an unusable billed reply its billed_unusable row.
  ``claim_shadow`` is a platform stage: no tenant budget stops it, and the tenant's bill excludes shadow rows.

No text is stored: the verdict row holds spans, claim hashes, classes and vote patterns.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import structlog

from services.ground_truth import claims as cl
from services.ground_truth import policy
from services.ground_truth import r1

logger = structlog.get_logger()

SHADOW_STAGE = "claim_shadow"
ROLE = "judge"
SWITCH_ENV = "CLAIM_SHADOW"
ON = "on"
INSTRUMENT_ENV = "CLAIM_SHADOW_INSTRUMENT"
CAP_ENV = "CLAIM_SHADOW_CAP_USD"
WINDOW_ENV = "CLAIM_SHADOW_WINDOW_DAYS"
DEFAULT_CAP_USD = 0.30                  # Done-when: shadow spend < $0.30
HARD_CAP_USD = 0.50                     # Done-when: total W4 spend < $0.50; no env value goes above it
DEFAULT_WINDOW_DAYS = 30
VOTES = r1.VOTES
MODE = "extraction"                     # live objects have no labelled claim: one call per field, as R1 extraction
DEFAULT_INSTRUMENT = r1.Instrument("gpt-6-luna-openai", "openai-direct", MODE)
SURFACES = ("t9_blog", "t9_social", "t2")
WARN, ENFORCE = "warn", "enforce"
# The fields the shadow reads, in reading order (claims.FIELD_ORDER names). T2: the prose fields a reader sees
# (t2_brand_claims._FIELDS minus the name and the SEO title, labels rather than claims); T9: the body and the blog's
# search title and meta.
T9_FIELDS = ("seo_title", "content_text", "meta_description")
T2_FIELDS = ("subtitle", "summary", "highlights", "itineraries", "seo_meta")
REFUSAL_STOPS = frozenset({"refusal", "content_filter", "safety", "refused"})
# Cause precedence of an unverified verdict (the first one seen in this order is the reason).
UNVERIFIED_CAUSES = ("budget", "error", "identity", "refused", "miss", "split")

# W2 measurement records: instrument id -> the "<writer>:<class>" it calibrated on the held-out side (r1.measure
# `calibrated` = pools.calibrated_keys: its passing classes plus the members of its passing pools, never a class
# outside a pool's members). Empty: no R1 record has passed yet.
CALIBRATED: dict[str, frozenset[str]] = {}
# "<writer>:<class>" -> "enforce". Empty in W4 shadow: every class warns (the flip is a later, measured PR).
ENFORCE_OVERRIDES: dict[str, str] = {}


# ── config ─────────────────────────────────────────────────────────────────────────────────────────────────────────


def enabled() -> bool:
    return (os.environ.get(SWITCH_ENV) or "").strip().lower() == ON


def instrument() -> r1.Instrument:
    """The pinned instrument: ``CLAIM_SHADOW_INSTRUMENT`` when it parses, names extraction and has a price (the guard
    needs one), else the default."""
    raw = (os.environ.get(INSTRUMENT_ENV) or "").strip()
    if not raw:
        return DEFAULT_INSTRUMENT
    try:
        model_conn, mode = raw.rsplit(":", 1)
        model, conn = model_conn.split("@", 1)
        if mode != MODE or not model or not conn or model not in r1.PRICES:
            raise ValueError(raw)
        return r1.Instrument(model, conn, mode)
    except ValueError:
        logger.warning("claim_shadow_bad_instrument", value=raw[:120], used=DEFAULT_INSTRUMENT.id)
        return DEFAULT_INSTRUMENT


def _env_float(name: str, default: float, low: float, high: float) -> float:
    try:
        v = float(os.environ.get(name) or default)
    except ValueError:
        return default
    return v if math.isfinite(v) and low < v <= high else default


def cap_usd() -> float:
    return _env_float(CAP_ENV, DEFAULT_CAP_USD, 0.0, HARD_CAP_USD)


def window_days() -> int:
    return int(_env_float(WINDOW_ENV, DEFAULT_WINDOW_DAYS, 0.0, 366.0))


def mode_for(writer: str, claim_class: Optional[str], inst: Optional[r1.Instrument] = None) -> str:
    """warn | enforce for one (writer, class). Enforce needs the override AND the pinned instrument's passing record:
    a class W2 never measured can never hold, whatever the override says."""
    key = f"{writer}:{claim_class}"
    inst = inst or instrument()
    if ENFORCE_OVERRIDES.get(key) == ENFORCE and key in CALIBRATED.get(inst.id, frozenset()):
        return ENFORCE
    return WARN


def enforce_map(writer: str, inst: Optional[r1.Instrument] = None) -> dict[str, str]:
    """Every policy class of the writer -> its mode (the admin summary shows it)."""
    return {c.id: mode_for(writer, c.id, inst) for c in policy.policy_classes().values() if writer in c.writers}


def prompt_sha() -> str:
    return hashlib.sha256((r1.EXTRACTION_SYSTEM + json.dumps(r1.EXTRACTION_SCHEMA, sort_keys=True))
                          .encode("utf-8")).hexdigest()


# ── objects ────────────────────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ShadowObject:
    """One saved object the shadow reads. ``fields``: (field, text) in reading order, empty fields left out."""
    surface: str
    fields: tuple[tuple[str, str], ...]
    source_text: str
    baseline: str
    baseline_status: Optional[str] = None
    tenant_id: Optional[str] = None
    piece_id: Optional[str] = None
    version_id: Optional[str] = None
    job_id: Optional[str] = None
    tenant_spans: tuple[str, ...] = ()
    baseline_detail: dict = field(default_factory=dict)

    @property
    def writer(self) -> str:
        return self.surface

    @property
    def content_sha256(self) -> str:
        return cl.content_sha({f: t for f, t in self.fields})

    @property
    def object_id(self) -> Optional[str]:
        return self.version_id if self.surface == "t2" else self.piece_id


def t9_surface(channel: Optional[str]) -> str:
    return "t9_blog" if (channel or "") == "blog" else "t9_social"


def baseline_t9(gate_ledger: Optional[list]) -> tuple[str, dict]:
    """The live F9 decision of a T9 piece from its final ledger: pass | flag | warn | unavailable | none."""
    from services.quality import registry as qr

    f9 = next((g for g in gate_ledger or [] if isinstance(g, dict) and g.get("gate") == qr.F9_CTA_FACT), None)
    if f9 is None:
        return "none", {}
    detail = {"violations": len(f9.get("violations") or [])}
    if f9.get("degraded"):
        return "unavailable", detail
    if f9.get("passed"):
        return "pass", detail
    return ("flag" if f9.get("blocking") else "warn"), detail


def baseline_t2(judge_breakdown: Optional[dict]) -> tuple[str, dict]:
    """The T2 claim decision of a saved version: the brand-claim check (report-only, AA-795) flagged or not."""
    claims = (judge_breakdown or {}).get("brand_claims") if isinstance(judge_breakdown, dict) else None
    if not isinstance(claims, dict):
        return "none", {}
    unsupported = claims.get("unsupported") or []
    return ("flag" if unsupported else "pass"), {"unsupported": len(unsupported)}


def t9_object(*, piece_id, tenant_id, job_id, channel, content_text, seo_title, meta_description, source_text, cta,
              gate_ledger, status) -> ShadowObject:
    values = {"seo_title": seo_title, "content_text": cl_strip_tags(content_text or ""),
              "meta_description": meta_description}
    base, detail = baseline_t9(gate_ledger)
    return ShadowObject(
        surface=t9_surface(channel), fields=tuple((f, str(values[f])) for f in T9_FIELDS
                                                  if isinstance(values.get(f), str) and values[f].strip()),
        source_text=source_text or "", baseline=base, baseline_status=_status(status), tenant_id=_id(tenant_id),
        piece_id=_id(piece_id), job_id=_id(job_id), tenant_spans=tuple(s for s in (cta,) if s and str(s).strip()),
        baseline_detail=detail)


def t2_object(*, version_id, tenant_id, job_id, rewritten: dict, status, judge_breakdown, tour_dict: dict,
              brand_rules: Optional[dict]) -> ShadowObject:
    from services.content_generation import t2_brand_claims

    out = []
    for f, text in cl.field_texts({k: (rewritten or {}).get(k) for k in T2_FIELDS if (rewritten or {}).get(k)}):
        out.append((f, text))
    base, detail = baseline_t2(judge_breakdown)
    spans = tuple(str(v) for v in ((brand_rules or {}).get("default_cta"), (brand_rules or {}).get("brand_name"))
                  if v and str(v).strip())
    return ShadowObject(
        surface="t2", fields=tuple(out), source_text=t2_brand_claims.source_text(tour_dict), baseline=base,
        baseline_status=_status(status), tenant_id=_id(tenant_id), version_id=_id(version_id), job_id=_id(job_id),
        tenant_spans=spans, baseline_detail=detail)


def cl_strip_tags(text: str) -> str:
    """The text the reader sees (F9 judges it without the citation tags too)."""
    from services.acp_content_writing.quality_gates import strip_citation_tags

    return strip_citation_tags(text)


def _id(v: Any) -> Optional[str]:
    """A UUID as text, else None (the row's id columns are uuid; a test double or a slug is no id)."""
    import uuid

    try:
        return str(uuid.UUID(str(v))) if v not in (None, "") else None
    except ValueError:
        return None


def _status(v: Any) -> Optional[str]:
    s = str(v or "").strip().lower()
    return s[:32] if s and s.replace("_", "").isalpha() else None


def user_prompts(obj: ShadowObject, inst: r1.Instrument) -> list[tuple[str, str, str]]:
    """(field, field text, user prompt) per call of one vote: the R1 extraction prompt, byte for byte."""
    out = []
    for f, text in obj.fields:
        item = r1.Item(claim_hash="0" * 64, writer=obj.writer, claim_class="", label="ok", stratum="representative",
                       content_sha256=obj.content_sha256, field=f, claim_text=text, field_text=text,
                       source_text=obj.source_text)
        out.append((f, text, r1.extraction_prompt([item])))
    return out


def cache_key(inst: r1.Instrument, prompts: list[tuple[str, str, str]], votes: int = VOTES) -> str:
    payload = json.dumps([inst.id, votes, prompt_sha(), [[f, hashlib.sha256(u.encode("utf-8")).hexdigest()]
                                                        for f, _t, u in prompts]], separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ── tally (pure) ───────────────────────────────────────────────────────────────────────────────────────────────────


def _class_of(raw: Any, writer: str) -> str:
    cid = str(raw or "").strip()
    pc = policy.policy_class(cid)
    return cid if pc is not None and writer in pc.writers else "unclassified"


def _in_tenant_span(sentence: str, spans: tuple[str, ...]) -> bool:
    s = cl.normalise(sentence)
    for span in spans:
        t = cl.normalise(span)
        if t and len(t.split()) >= 2 and (t in s or s in t):
            return True
    return False


def tally(obj: ShadowObject, votes_by_field: dict[str, list[Optional[dict]]], causes: Counter,
          inst: Optional[r1.Instrument] = None) -> dict:
    """The object's verdict from its replies. ``votes_by_field[field]``: one entry per vote, the parsed reply or None
    (no vote: ``causes`` says why). Returns verdict, reason, shadow_action and the text-free details."""
    inst = inst or instrument()
    sha = obj.content_sha256
    fields_out = []
    flagged_classes: list[str] = []
    unverified = False
    action = "none"
    for f, text in obj.fields:
        replies = votes_by_field.get(f) or []
        spans = cl.sentence_spans(text)
        sentence_votes: list[list[Optional[bool]]] = [[] for _ in spans]
        sentence_classes: list[Counter] = [Counter() for _ in spans]
        unmapped = 0
        norm_text = cl.normalise(text)
        for reply in replies:
            if reply is None:
                for v in sentence_votes:
                    v.append(None)
                continue
            quotes = [(str(c.get("quote") or ""), c.get("class")) for c in reply.get("claims") or []
                      if isinstance(c, dict)]
            confirmed = [(q, k) for q, k in quotes if q.strip() and cl.normalise(q) in norm_text]
            hit_any = [False] * len(confirmed)
            for i, (s, e) in enumerate(spans):
                sentence = text[s:e]
                hits = [(j, k) for j, (q, k) in enumerate(confirmed) if r1._overlaps(q, sentence)]
                for j, k in hits:
                    hit_any[j] = True
                    sentence_classes[i][_class_of(k, obj.writer)] += 1
                sentence_votes[i].append(bool(hits))
            unmapped += sum(1 for h in hit_any if not h)
        if not replies:
            sentence_votes = [[None] * VOTES for _ in spans]
        flagged = []
        for i, (s, e) in enumerate(spans):
            verdict = r1.majority(sentence_votes[i]) if sentence_votes[i] else None
            if verdict is False:
                continue
            sentence = text[s:e]
            cls = sentence_classes[i].most_common(1)[0][0] if sentence_classes[i] else None
            tenant = _in_tenant_span(sentence, obj.tenant_spans)
            entry = {"claim_hash": cl.claim_hash(sha, f, sentence), "span": [s, e],
                     "votes": [None if v is None else int(v) for v in sentence_votes[i]],
                     "verdict": "unsupported" if verdict else "unverified", "class": cls,
                     "tenant_span": tenant}
            if tenant:
                pass                                   # D6: the tenant's own words never count, never cut
            elif verdict is None:
                unverified = True
                if "split" not in causes and any(v is not None for v in sentence_votes[i]):
                    causes["split"] += 1
            elif not tenant:
                mode = mode_for(obj.writer, cls, inst)
                entry["mode"] = mode
                entry["cut_eligible"] = False      # W4 shadow never cuts; a later flip cuts enforce classes only
                flagged_classes.append(cls or "unclassified")
                if mode == ENFORCE:
                    action = "hold"
            flagged.append(entry)
        fields_out.append({"field": f, "sentences": len(spans), "votes": len(replies),
                           "real_votes": sum(1 for r in replies if r is not None), "unmapped_quotes": unmapped,
                           "flagged": flagged[:200]})
    if flagged_classes:
        top = Counter(flagged_classes).most_common(1)[0][0]
        verdict, reason = "unsupported", f"unsupported:{top}"
    elif unverified:
        cause = next((c for c in UNVERIFIED_CAUSES if causes.get(c)), "miss")
        verdict, reason = "unverified", f"unverified:{cause}"
    else:
        verdict, reason = "supported", "supported:clear"
    if action != "hold":
        if verdict == "unverified" and any(m == ENFORCE for m in enforce_map(obj.writer, inst).values()):
            action = "review"                         # D7: a miss in an enforce class is never a pass
        elif verdict in ("unsupported", "unverified"):
            action = "warn"
    return {"verdict": verdict, "reason": reason, "shadow_action": action,
            "details": {"fields": fields_out, "causes": dict(causes), "votes": VOTES, "prompt_sha": prompt_sha(),
                        "baseline": obj.baseline_detail}}


def agreement(baseline: str, verdict: str) -> str:
    if verdict == "unverified":
        return "unverified"
    if baseline in ("unavailable", "none"):
        return "no_baseline"
    flagged = baseline == "flag"
    return "agree" if flagged == (verdict == "unsupported") else "disagree"


# ── spend guard ────────────────────────────────────────────────────────────────────────────────────────────────────


class SpendGuard:
    """The hard cap over the shadow stage's ledger window. ``observe`` takes a ledger reading (the first one is the
    base); spent = max(base + this run's charges, the newest reading). A call is allowed only when spent + its worst
    case fits ``cap`` (and this run's charges + the worst case fit ``run_cap`` when set). No reading, or a reading
    that is not a finite number, closes the guard: no call."""

    def __init__(self, cap: float, *, run_cap: Optional[float] = None):
        if not (isinstance(cap, (int, float)) and 0 < cap <= HARD_CAP_USD):
            raise ValueError(f"cap must be in (0, {HARD_CAP_USD}] USD, got {cap}")
        if run_cap is not None and not 0 < run_cap <= cap:
            raise ValueError(f"run cap must be in (0, {cap}] USD, got {run_cap}")
        self.cap, self.run_cap = float(cap), run_cap
        self.base: Optional[float] = None
        self.ledger: Optional[float] = None
        self.charged = 0.0
        self.calls = 0
        self.closed = False

    def observe(self, value: Any) -> None:
        v = r1._num(value)
        if v is None or v < 0:
            self.closed = True
            return
        if self.base is None:
            self.base = v
        self.ledger = max(self.ledger or 0.0, v)

    @property
    def spent(self) -> float:
        return max((self.base or 0.0) + self.charged, self.ledger or 0.0)

    def allows(self, worst: float) -> bool:
        if self.closed or self.base is None or not math.isfinite(worst):
            return False
        if self.run_cap is not None and self.charged + worst > self.run_cap:
            return False
        return self.spent + worst <= self.cap

    def charge(self, amount: Any, worst: float) -> None:
        v = r1._num(amount)
        self.charged += max(0.0, v if v is not None else float(worst))
        self.calls += 1


# ── the gateway call ───────────────────────────────────────────────────────────────────────────────────────────────


def gateway_call(inst: r1.Instrument, system: str, user: str, schema: dict) -> Any:
    """One pinned call through the Model Gateway on the shadow stage (the pinned connection first, as R1)."""
    from shared.llm_client import deployments
    from shared.llm_client.client import LLMClient
    from shared.llm_client.models import LLMRequest

    request = LLMRequest(system_prompt=system, user_prompt=user, stage=SHADOW_STAGE, role=ROLE,
                         model_tier=inst.model_key, max_tokens=r1.MAX_TOKENS, temperature=0.0, seed=r1.SEED,
                         json_schema=schema)
    with deployments.priority_override({inst.model_key: {inst.connection: -1_000_000}}):
        return LLMClient().generate(request)


async def _log(pool, resp: Any, signal: dict, parsed: Optional[dict], error: Optional[Exception], *,
               obj: ShadowObject) -> None:
    """The ledger row of one billed reply (``**billed_fields``), or its billed_unusable row."""
    from shared.llm_client.call_log import billed_fields, record_call_with_pool, record_unusable

    ids = {"tenant_id": obj.tenant_id, "content_piece_id": obj.piece_id}
    if error is not None:
        await record_unusable(resp, stage=SHADOW_STAGE, role=ROLE, error=error, quality_signal_extra=signal, **ids)
        return
    await record_call_with_pool(
        pool, stage=SHADOW_STAGE, role=ROLE, model=resp.model_used, tokens_in=getattr(resp, "input_tokens", None),
        tokens_out=getattr(resp, "output_tokens", None), cost_usd=getattr(resp, "cost_usd", None),
        quality_signal={**signal, "json_parsed": parsed is not None}, stop_reason=getattr(resp, "stop_reason", None),
        account=getattr(resp, "satellite_account", None), fallback_used=getattr(resp, "fallback_used", None),
        provider=resp.provider if isinstance(getattr(resp, "provider", None), str) else None, **ids,
        **billed_fields(resp))


Caller = Callable[[r1.Instrument, str, str, dict], Any]
Logger = Callable[..., Awaitable[None]]
Probe = Callable[[], Awaitable[Optional[float]]]


async def run_object(obj: ShadowObject, inst: r1.Instrument, guard: SpendGuard, *, probe: Probe, caller: Caller,
                     log: Logger, run_kind: str, run_id: str, key: str,
                     attempts: Optional[int] = None) -> dict:
    """Make the object's calls (VOTES per field) under the guard and tally them. Never raises on a caller error: it
    is charged the call's worst case, gives no vote and stops the remaining calls (``stopped``)."""
    prompts = user_prompts(obj, inst)
    reserved = max(1, int(attempts if attempts is not None else r1.route_attempts(inst)))
    votes: dict[str, list[Optional[dict]]] = {f: [] for f, _t, _u in prompts}
    causes: Counter = Counter()
    stopped: Optional[str] = None
    cost = 0.0
    models: Counter = Counter()
    max_rung: Optional[int] = None
    pinned = True
    calls = 0
    for vote in range(VOTES):
        for f, _text, user in prompts:
            worst = r1.worst_call_cost(inst, user, reserved)
            if not stopped:
                try:
                    guard.observe(await probe())
                except Exception:                       # noqa: BLE001 — an unreadable ledger closes the guard
                    guard.observe(None)
                if not guard.allows(worst):
                    stopped = "budget"
            if stopped:
                votes[f].append(None)
                causes["budget" if stopped == "budget" else "error"] += 1
                continue
            try:
                resp = await asyncio.to_thread(caller, inst, r1.EXTRACTION_SYSTEM, user, r1.EXTRACTION_SCHEMA)
            except Exception as e:                      # noqa: BLE001 — paid attempts may have run: stop clean
                guard.charge(worst, worst)
                cost += worst
                causes["error"] += 1
                stopped = "error"
                votes[f].append(None)
                logger.warning("claim_shadow_call_failed", surface=obj.surface, error=type(e).__name__)
                continue
            calls += 1
            amount, _notes = r1.billed_cost(inst, user, resp)
            guard.charge(amount, worst)
            cost += amount
            models[str(getattr(resp, "model_used", "") or "")[:200]] += 1
            rung = getattr(resp, "route_position", None)
            if isinstance(rung, int) and not isinstance(rung, bool) and rung >= 0:
                max_rung = max(max_rung or 0, rung)
            signal = {"shadow": True, "app": "cis", "issue": "AA-1050", "surface": obj.surface, "run_kind": run_kind,
                      "run_id": run_id[:64], "instrument": inst.id, "vote": vote, "field": f, "cache_key": key[:16]}
            stop = str(getattr(resp, "stop_reason", "") or "").lower()
            try:
                parsed = json.loads(getattr(resp, "content", "") or "")
                if not isinstance(parsed, dict):
                    raise ValueError("reply is not a JSON object")
            except Exception as e:                      # noqa: BLE001 — billed_unusable row, no vote
                await log(resp, signal, None, e)
                causes["refused" if stop in REFUSAL_STOPS else "miss"] += 1
                votes[f].append(None)
                continue
            await log(resp, signal, parsed, None)
            if stop in REFUSAL_STOPS:
                causes["refused"] += 1
                votes[f].append(None)
                continue
            if not r1._identity_ok(inst, resp):
                pinned = False
                causes["identity"] += 1
                votes[f].append(None)
                continue
            votes[f].append(parsed)
    result = tally(obj, votes, causes, inst)
    answered = models.most_common(1)[0][0] if models else None
    return {**result, "calls": calls, "cost_usd": round(cost, 6), "answered_model": answered or None,
            "max_rung": max_rung, "pinned": pinned and calls > 0, "stopped": stopped}


# ── store ──────────────────────────────────────────────────────────────────────────────────────────────────────────

_CACHE_SQL = """
SELECT verdict, reason, shadow_action, details, pinned, answered_model, max_rung
FROM shared.claim_shadow_verdict
WHERE cache_key = $1 AND NOT cached AND verdict <> 'unverified'
ORDER BY created_at DESC LIMIT 1
"""
_LEDGER_SQL = """
SELECT coalesce(sum(cost_usd), 0)::float8 AS spent FROM shared.llm_call_log
WHERE stage = $1 AND created_at >= now() - make_interval(days => $2)
"""
_INSERT_SQL = """
INSERT INTO shared.claim_shadow_verdict
    (run_kind, run_id, surface, tenant_id, piece_id, version_id, job_id, content_sha256, cache_key, instrument_id,
     policy_sha, cached, verdict, reason, shadow_action, baseline, baseline_status, agreement, pinned, calls,
     cost_usd, answered_model, max_rung, details)
VALUES ($1, $2, $3, $4::uuid, $5::uuid, $6::uuid, $7::uuid, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18,
        $19, $20, $21, $22, $23, $24::jsonb)
ON CONFLICT (run_kind, run_id, cache_key) DO NOTHING
RETURNING verdict_id
"""


async def cached_verdict(conn, key: str) -> Optional[dict]:
    row = await conn.fetchrow(_CACHE_SQL, key)
    if row is None:
        return None
    details = row["details"]
    if isinstance(details, str):
        details = json.loads(details)
    return {"verdict": row["verdict"], "reason": row["reason"], "shadow_action": row["shadow_action"],
            "details": details or {}, "pinned": row["pinned"], "answered_model": row["answered_model"],
            "max_rung": row["max_rung"]}


async def ledger_spend(conn, days: Optional[int] = None) -> float:
    return float(await conn.fetchval(_LEDGER_SQL, SHADOW_STAGE, int(days or window_days())) or 0.0)


async def insert_verdict(conn, obj: ShadowObject, inst: r1.Instrument, result: dict, *, run_kind: str, run_id: str,
                         key: str, cached: bool) -> Optional[str]:
    vid = await conn.fetchval(
        _INSERT_SQL, run_kind, run_id[:128], obj.surface, obj.tenant_id, obj.piece_id, obj.version_id, obj.job_id,
        obj.content_sha256, key, inst.id, policy.policy_sha(), cached, result["verdict"], result["reason"],
        result["shadow_action"], obj.baseline, obj.baseline_status, agreement(obj.baseline, result["verdict"]),
        bool(result.get("pinned")), 0 if cached else int(result.get("calls") or 0),
        0 if cached else float(result.get("cost_usd") or 0.0), result.get("answered_model"),
        result.get("max_rung"), json.dumps(result.get("details") or {}, default=str))
    return str(vid) if vid else None


async def shadow_object(pool, obj: ShadowObject, *, run_kind: str, run_id: str, guard: Optional[SpendGuard] = None,
                        caller: Optional[Caller] = None, inst: Optional[r1.Instrument] = None,
                        attempts: Optional[int] = None) -> Optional[dict]:
    """Cache first, then the guarded calls; one verdict row. Returns a small summary (no text), None for an object
    with no text."""
    if not obj.fields:
        return None
    inst = inst or instrument()
    prompts = user_prompts(obj, inst)
    key = cache_key(inst, prompts)
    async with pool.acquire() as conn:
        hit = await cached_verdict(conn, key)
    if hit is not None:
        result = {**hit, "calls": 0, "cost_usd": 0.0}
        cached = True
    else:
        guard = guard or SpendGuard(cap_usd())

        async def probe() -> Optional[float]:
            async with pool.acquire() as c:
                return await ledger_spend(c)

        async def log(resp, signal, parsed, error):
            await _log(pool, resp, signal, parsed, error, obj=obj)

        result = await run_object(obj, inst, guard, probe=probe, caller=caller or gateway_call, log=log,
                                  run_kind=run_kind, run_id=run_id, key=key, attempts=attempts)
        cached = False
    async with pool.acquire() as conn:
        vid = await insert_verdict(conn, obj, inst, result, run_kind=run_kind, run_id=run_id, key=key, cached=cached)
    summary = {"verdict_id": vid, "surface": obj.surface, "verdict": result["verdict"], "reason": result["reason"],
               "baseline": obj.baseline, "agreement": agreement(obj.baseline, result["verdict"]),
               "shadow_action": result["shadow_action"], "cached": cached, "calls": 0 if cached else result["calls"],
               "cost_usd": 0.0 if cached else result["cost_usd"], "stopped": result.get("stopped")}
    logger.info("claim_shadow_verdict", **{k: v for k, v in summary.items() if k != "verdict_id"},
                object_id=obj.object_id, instrument=inst.id)
    return summary


# ── admin read (counts and ids, no text) ───────────────────────────────────────────────────────────────────────────

_SUMMARY_FILTER = """
created_at >= now() - make_interval(days => $1) AND ($2::text = 'all' OR run_kind = $2)
  AND ($3::text IS NULL OR surface = $3)
"""
_CELLS_SQL = f"""
SELECT surface, baseline, verdict, agreement, cached, pinned, shadow_action, count(*)::int AS n,
       coalesce(sum(cost_usd), 0)::float8 AS cost
FROM shared.claim_shadow_verdict WHERE {_SUMMARY_FILTER}
GROUP BY surface, baseline, verdict, agreement, cached, pinned, shadow_action
"""
_REASONS_SQL = f"""
SELECT reason, count(*)::int AS n FROM shared.claim_shadow_verdict WHERE {_SUMMARY_FILTER}
GROUP BY reason ORDER BY n DESC, reason LIMIT 50
"""
# A seeded, stable sample of the disagreements (the tech lead labels about 30, W4): ids only.
_SAMPLE_SQL = f"""
SELECT verdict_id, created_at, run_kind, surface, piece_id, version_id, baseline, baseline_status, verdict, reason,
       instrument_id
FROM shared.claim_shadow_verdict WHERE {_SUMMARY_FILTER} AND agreement = 'disagree'
ORDER BY md5(verdict_id::text || $4), verdict_id LIMIT $5
"""
RUN_KINDS = ("live", "replay", "all")


async def summary(conn, *, days: int = 30, run_kind: str = "live", surface: Optional[str] = None, sample: int = 30,
                  seed: str = "aa1050") -> dict:
    """Agreement counts per surface, the cells (baseline x verdict), reasons, cache and pin shares, the shadow spend
    over the window vs its cap, and a seeded sample of disagreement ids (no text, no tenant id)."""
    args = (int(days), run_kind, surface)
    cells = [dict(r) for r in await conn.fetch(_CELLS_SQL, *args)]
    reasons = {r["reason"]: r["n"] for r in await conn.fetch(_REASONS_SQL, *args)}
    rows = await conn.fetch(_SAMPLE_SQL, *args, seed, int(sample))
    spend = await ledger_spend(conn, window_days())
    by_surface: dict[str, dict[str, int]] = {}
    total = cached = pinned = non_pinned_holds = would_act = 0
    for c in cells:
        agg = by_surface.setdefault(c["surface"], {a: 0 for a in ("agree", "disagree", "unverified", "no_baseline")})
        agg[c["agreement"]] += c["n"]
        total += c["n"]
        cached += c["n"] if c["cached"] else 0
        pinned += c["n"] if c["pinned"] else 0
        would_act += c["n"] if c["shadow_action"] in ("hold", "review") else 0
        non_pinned_holds += c["n"] if c["shadow_action"] == "hold" and not c["pinned"] else 0
    disagree = sum(a["disagree"] for a in by_surface.values())
    inst = instrument()
    return {
        "switch": ON if enabled() else "off", "instrument": inst.id, "calibrated_classes": sorted(
            CALIBRATED.get(inst.id, frozenset())),
        "enforce_classes": sorted(k for k, v in ENFORCE_OVERRIDES.items() if v == ENFORCE),
        "default_mode": WARN, "window_days": window_days(), "cap_usd": cap_usd(), "spend_usd": round(spend, 6),
        "filter": {"days": days, "run_kind": run_kind, "surface": surface},
        "rows": total, "cached_share": round(cached / total, 4) if total else None,
        "pinned_share": round(pinned / total, 4) if total else None,
        "would_hold_or_review": would_act, "non_pinned_holds": non_pinned_holds,
        "by_surface": by_surface,
        "cells": [{k: c[k] for k in ("surface", "baseline", "verdict", "agreement", "n")}
                  | {"cost_usd": round(c["cost"], 6)} for c in sorted(
                      cells, key=lambda c: (c["surface"], c["baseline"], c["verdict"], c["agreement"]))],
        "reasons": reasons,
        "disagreements": {"total": disagree, "seed": seed, "sample": [
            {"verdict_id": str(r["verdict_id"]), "created_at": r["created_at"].isoformat(),
             "run_kind": r["run_kind"], "surface": r["surface"],
             "piece_id": str(r["piece_id"]) if r["piece_id"] else None,
             "version_id": str(r["version_id"]) if r["version_id"] else None, "baseline": r["baseline"],
             "baseline_status": r["baseline_status"], "verdict": r["verdict"], "reason": r["reason"],
             "instrument": r["instrument_id"]} for r in rows]},
    }


# ── live hooks (never raise, never change a status) ────────────────────────────────────────────────────────────────


async def after_t9_piece(pool, **kw) -> Optional[dict]:
    """The T9 hook, called after ``_finalize_piece`` wrote the piece. Off (default): returns None before any read."""
    if not enabled():
        return None
    try:
        from services.acp_content_writing import piece_store

        if piece_store.active() is not None:          # an offline replay (DryStore): never a paid shadow call
            return None
        if kw.get("status") not in ("approved", "held"):
            return None
        obj = t9_object(**kw)
        return await shadow_object(pool, obj, run_kind="live", run_id=str(obj.job_id or obj.piece_id))
    except Exception as e:                              # noqa: BLE001 — the shadow never fails the write
        logger.warning("claim_shadow_t9_failed", error_type=type(e).__name__, error=str(e)[:200])
        return None


_T2_ROW_SQL = """
SELECT status, edit_source, rewritten_content, judge_breakdown
FROM gold_aa_internal.tenant_tour_versions WHERE id = $1::uuid
"""


async def after_t2_version(pool, *, version_id, tenant_id, job_id, tour_dict: dict,
                           brand_rules: Optional[dict]) -> Optional[dict]:
    """The T2 hook, called after the job saved the version (either T2 path). Reads the saved row, so the shadow sees
    exactly the stored text. A policy hold (D8: Master text, 0 LLM calls) and a tenant edit are skipped."""
    if not enabled():
        return None
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(_T2_ROW_SQL, str(version_id))
        if row is None or row["status"] not in ("ai_generated", "needs_review") or row["edit_source"] != "ai_generated":
            return None
        jb = _json(row["judge_breakdown"])
        if isinstance(jb, dict) and jb.get("policy_hold"):
            return None
        obj = t2_object(version_id=version_id, tenant_id=tenant_id, job_id=job_id,
                        rewritten=_json(row["rewritten_content"]) or {}, status=row["status"], judge_breakdown=jb,
                        tour_dict=tour_dict, brand_rules=brand_rules)
        return await shadow_object(pool, obj, run_kind="live", run_id=str(obj.job_id or obj.version_id))
    except Exception as e:                              # noqa: BLE001 — the shadow never fails the save
        logger.warning("claim_shadow_t2_failed", error_type=type(e).__name__, error=str(e)[:200])
        return None


def _json(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


__all__ = ["CALIBRATED", "ENFORCE_OVERRIDES", "SHADOW_STAGE", "ShadowObject", "SpendGuard", "after_t2_version",
           "after_t9_piece", "agreement", "baseline_t2", "baseline_t9", "cache_key", "enabled", "enforce_map",
           "instrument", "mode_for", "run_object", "shadow_object", "summary", "t2_object", "t9_object", "tally"]
