"""AA-660 — the gateway's decision path: `decide(stage, subject_key, state, question_keys)`.

Jev (TypeSafe "System One") answers typed questions about a piece of state — Noul (probability a
statement is true), Choice (pick one label) or Score (a level on an ordered rubric) — and never
writes text. Design: docs/architecture/at-series-v2-design.md §3.

- The questions live in `shared.decision_question` (wording, mode, floors), so a stage names only
  question keys and admin can move a question between off / shadow / enforce.
- Each answer lands in a **zone**: `accept` / `reject` (confident — only these can change what a
  stage does, and only in `enforce` mode), `grey` (the stage keeps its existing rule), `error`
  (Jev unreachable or misconfigured: fail-open, the stage keeps its existing rule) or `skipped`
  (mode off, or tenant content that may not leave AA yet).
- **Tenant guard (design C2):** tenant content goes to TypeSafe only for tenants in
  `shared.jev_tenant_allowlist` until the DPA/ZDR is confirmed. Platform content always may.
- Every verdict is written to `shared.decision_log` (all probabilities kept) and each call to
  `shared.llm_call_log` (provider `typesafe`, role `validate`), under the current job if any.

`decide()` never raises: a caller can always read `decision.accepted(k)` / `rejected(k)`, which are
True only for an enforced, confident verdict.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import asyncpg
import httpx
import structlog

from shared.secrets import get_database_url, get_typesafe_api_key

from .call_log import current_job_id, record_call_with_pool
from .catalog import get_model_sync

logger = structlog.get_logger()

JEV_MODEL = "jev-latest"
JEV_URL = os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai").rstrip("/") + "/v1/systemone"
_FALLBACK_PRICE_IN_PER_MTOK = 0.042          # used only if the catalog row cannot be read
_TIMEOUT_S = 3.0
_MAX_ATTEMPTS = 3
_RETRY_STATUS = {429, 500, 502, 503, 504, 529}
_CONFIG_TTL_S = 20.0
PLATFORM_TENANT_ID = "00000000-0000-0000-0000-000000000001"


@dataclass(frozen=True)
class Question:
    key: str
    stage: str
    kind: str                          # noul | choice | score
    instructions: str
    criteria: Any
    mode: str                          # off | shadow | enforce
    accept_floor: Optional[float]
    reject_ceiling: Optional[float]
    threshold_version: int
    question_hash: Optional[str] = None      # md5 of the wording (DB-generated, migration 184)


@dataclass
class Verdict:
    question_key: str
    mode: str
    zone: str                          # accept | grey | reject | error | skipped
    probability: Optional[float] = None
    choice: Optional[str] = None
    probabilities: Optional[dict] = None
    error: Optional[str] = None
    cached: bool = False                     # served from an earlier Verdict of the same wording

    @property
    def enforced(self) -> bool:
        """True only for a confident verdict of an enforce-mode question — the only case a stage acts on."""
        return self.mode == "enforce" and self.zone in ("accept", "reject")


@dataclass
class Decision:
    verdicts: dict[str, Verdict] = field(default_factory=dict)
    model: Optional[str] = None
    latency_ms: Optional[int] = None
    input_tokens: int = 0
    cost_usd: float = 0.0

    def accepted(self, key: str) -> bool:
        v = self.verdicts.get(key)
        return bool(v and v.enforced and v.zone == "accept")

    def rejected(self, key: str) -> bool:
        v = self.verdicts.get(key)
        return bool(v and v.enforced and v.zone == "reject")

    def choice(self, key: str) -> Optional[str]:
        """The picked label of a choice/score question, only when it is enforced and confident."""
        return self.verdicts[key].choice if self.accepted(key) else None


# ── pure helpers (unit-tested) ────────────────────────────────────────────────────────────────

def wire_question(q: Question) -> dict:
    """TypeSafe request shape for one question: {"type", "instructions", "criteria"?}."""
    out: dict[str, Any] = {"type": q.kind, "instructions": q.instructions}
    if q.criteria is not None:
        out["criteria"] = q.criteria
    return out


def zone_for(q: Question, answer: dict) -> Verdict:
    """Map one TypeSafe answer onto a zone using the question's floors.

    noul:          p >= accept_floor -> accept;  p <= reject_ceiling -> reject;  else grey.
    choice/score:  confidence >= accept_floor -> accept (the pick is trusted);   else grey.
    A missing floor never produces that zone, so an uncalibrated question is always grey."""
    kind = answer.get("type")
    if kind != q.kind:
        return Verdict(q.key, q.mode, "error", error=f"answer type {kind!r} != question kind {q.kind!r}")
    if kind == "noul":
        p = float(answer["noul"])
        zone = "grey"
        if q.accept_floor is not None and p >= q.accept_floor:
            zone = "accept"
        elif q.reject_ceiling is not None and p <= q.reject_ceiling:
            zone = "reject"
        return Verdict(q.key, q.mode, zone, probability=p)
    confidence = float(answer.get("confidence", 0.0))
    pick = answer.get("choice") if kind == "choice" else _top_level(answer.get("probabilities") or {})
    zone = "accept" if q.accept_floor is not None and confidence >= q.accept_floor else "grey"
    return Verdict(q.key, q.mode, zone, probability=confidence, choice=pick,
                   probabilities=answer.get("probabilities"))


def _top_level(probabilities: dict) -> Optional[str]:
    return max(probabilities, key=probabilities.get) if probabilities else None


# ── config caches ─────────────────────────────────────────────────────────────────────────────

_questions: dict[str, Question] = {}
_questions_loaded_at = 0.0
_allowlist: set[str] = set()
_allowlist_loaded_at = 0.0


async def _load_config(conn) -> None:
    global _questions, _questions_loaded_at, _allowlist, _allowlist_loaded_at
    now = time.monotonic()
    if now - _questions_loaded_at > _CONFIG_TTL_S:
        rows = await conn.fetch(
            "SELECT question_key, stage, kind, instructions, criteria, mode, accept_floor, "
            "reject_ceiling, threshold_version, question_hash FROM shared.decision_question"
        )
        _questions = {
            r["question_key"]: Question(
                key=r["question_key"], stage=r["stage"], kind=r["kind"], instructions=r["instructions"],
                criteria=json.loads(r["criteria"]) if isinstance(r["criteria"], str) else r["criteria"],
                mode=r["mode"],
                accept_floor=float(r["accept_floor"]) if r["accept_floor"] is not None else None,
                reject_ceiling=float(r["reject_ceiling"]) if r["reject_ceiling"] is not None else None,
                threshold_version=r["threshold_version"],
                question_hash=r["question_hash"],
            )
            for r in rows
        }
        _questions_loaded_at = now
    if now - _allowlist_loaded_at > _CONFIG_TTL_S:
        rows = await conn.fetch("SELECT tenant_id::text AS t FROM shared.jev_tenant_allowlist")
        _allowlist = {r["t"] for r in rows}
        _allowlist_loaded_at = now


def tenant_allowed(tenant_id: Optional[str]) -> bool:
    return tenant_id is None or str(tenant_id) == PLATFORM_TENANT_ID or str(tenant_id) in _allowlist


# ── the call ──────────────────────────────────────────────────────────────────────────────────

async def _call_jev(state: Any, questions: dict[str, dict]) -> dict:
    body = {"state": state, "model": JEV_MODEL, "questions": questions}
    headers = {"Authorization": f"Bearer {get_typesafe_api_key()}", "Content-Type": "application/json"}
    last: Exception | None = None
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = await client.post(JEV_URL, json=body, headers=headers)
                if resp.status_code in _RETRY_STATUS and attempt < _MAX_ATTEMPTS - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                resp.raise_for_status()
                return resp.json()
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = exc
                if attempt < _MAX_ATTEMPTS - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))
    raise last or RuntimeError("TypeSafe call failed")


def _price_in_per_mtok() -> float:
    m = get_model_sync(JEV_MODEL)
    return float(m.price_in_per_mtok) if m is not None and m.price_in_per_mtok is not None \
        else _FALLBACK_PRICE_IN_PER_MTOK


# A Verdict is a fact about (wording, subject): reuse it instead of asking Jev again (Ms. Thư's
# "judge once, store beside the subject"). The Zone is recomputed with today's Floors, so moving a
# Floor never needs a new call. Errors and skips are never reused.
CACHE_MAX_AGE_DAYS = 180
_CACHE_SQL = """
    SELECT DISTINCT ON (l.question_key) l.question_key, l.probability::float AS probability, l.choice,
           l.probabilities
    FROM shared.decision_log l
    JOIN unnest($2::text[], $3::text[]) AS w(question_key, question_hash)
      ON w.question_key = l.question_key AND w.question_hash = l.question_hash
    WHERE l.subject_key = $1
      AND l.tenant_id IS NOT DISTINCT FROM $4::uuid
      AND l.zone IN ('accept', 'grey', 'reject')
      AND l.created_at > now() - make_interval(days => $5)
    ORDER BY l.question_key, l.created_at DESC
"""


def cached_answer(q: Question, row: dict) -> Optional[dict]:
    """Rebuild a TypeSafe-shaped answer from a stored Verdict, so zone_for() applies today's Floors."""
    if row.get("probability") is None:
        return None
    if q.kind == "noul":
        return {"type": "noul", "noul": row["probability"]}
    probs = row.get("probabilities")
    if isinstance(probs, str):
        probs = json.loads(probs)
    return {"type": q.kind, "choice": row.get("choice"), "confidence": row["probability"],
            "probabilities": probs or {}}


_LOG_SQL = """
    INSERT INTO shared.decision_log
        (stage, question_key, subject_key, tenant_id, job_id, mode, zone, probability, choice,
         probabilities, threshold_version, model, latency_ms, cost_usd, error, question_hash, cached)
    VALUES ($1, $2, $3, $4::uuid, $5::uuid, $6, $7, $8, $9, $10::jsonb, $11, $12, $13, $14, $15, $16, $17)
"""


class _SingleConn:
    """A one-connection stand-in for an asyncpg.Pool (the no-pool path): `acquire()` yields the same
    connection every time. `_decide()` only ever holds one acquisition at a time, so this is safe."""

    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return self

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


async def _decide(db, stage: str, subject_key: str, state: Any, question_keys: list[str],
                  tenant_id: Optional[str], use_cache: bool = True) -> Decision:
    """`db` is a pool (or `_SingleConn`). A connection is held only while reading config and while
    writing logs — never across the Jev HTTP call and never two at once. Holding one across the
    whole call and then acquiring a second for llm_call_log deadlocked a pool under concurrent
    decides (S203: 8 parallel calls on a 4-connection pool hung until timeout)."""
    decision = Decision()
    try:
        async with db.acquire() as conn:
            await _load_config(conn)
    except Exception as exc:                      # tables missing / DB error → fail open
        logger.warning("decide_config_unavailable", stage=stage, error=str(exc))
        decision.verdicts = {k: Verdict(k, "off", "error", error=f"config: {exc}") for k in question_keys}
        return decision

    asked: dict[str, Question] = {}
    for key in question_keys:
        q = _questions.get(key)
        if q is None:
            decision.verdicts[key] = Verdict(key, "off", "error", error="unknown question")
        elif q.mode == "off":
            decision.verdicts[key] = Verdict(key, "off", "skipped")
        elif not tenant_allowed(tenant_id):
            decision.verdicts[key] = Verdict(key, q.mode, "skipped", error="tenant not on Jev allow-list")
        else:
            asked[key] = q

    to_ask = dict(asked)
    if asked and use_cache:
        try:
            async with db.acquire() as conn:
                rows = await conn.fetch(
                    _CACHE_SQL, subject_key, list(asked), [q.question_hash or "" for q in asked.values()],
                    str(tenant_id) if tenant_id else None, CACHE_MAX_AGE_DAYS,
                )
            for r in rows:
                q = asked[r["question_key"]]
                answer = cached_answer(q, dict(r))
                if answer is not None:
                    v = zone_for(q, answer)
                    v.cached = True
                    decision.verdicts[q.key] = v
                    to_ask.pop(q.key, None)
        except Exception as exc:                  # a cache miss is never an error
            logger.warning("decide_cache_read_failed", stage=stage, error=str(exc)[:200])

    if to_ask:
        started = time.perf_counter()
        try:
            data = await _call_jev(state, {k: wire_question(q) for k, q in to_ask.items()})
            decision.latency_ms = int((time.perf_counter() - started) * 1000)
            decision.model = data.get("model", JEV_MODEL)
            decision.input_tokens = int((data.get("usage") or {}).get("input_tokens") or 0)
            decision.cost_usd = decision.input_tokens * _price_in_per_mtok() / 1_000_000
            answers = data.get("answers") or {}
            for key, q in to_ask.items():
                answer = answers.get(key)
                decision.verdicts[key] = zone_for(q, answer) if answer else \
                    Verdict(key, q.mode, "error", error="no answer returned")
        except Exception as exc:
            decision.latency_ms = int((time.perf_counter() - started) * 1000)
            logger.warning("decide_call_failed", stage=stage, subject=subject_key, error=str(exc)[:200])
            for key, q in to_ask.items():
                decision.verdicts[key] = Verdict(key, q.mode, "error", error=str(exc)[:500])

    await _write_logs(db, stage, subject_key, tenant_id, decision, to_ask)
    return decision


async def _write_logs(db, stage, subject_key, tenant_id, decision: Decision, asked) -> None:
    job_id = current_job_id()
    per_q_cost = decision.cost_usd / len(asked) if asked else 0.0
    rows = []
    for key, v in decision.verdicts.items():
        q = _questions.get(key)
        rows.append((
            stage, key, subject_key, str(tenant_id) if tenant_id else None, job_id, v.mode,
            v.zone, v.probability, v.choice,
            json.dumps(v.probabilities) if v.probabilities is not None else None,
            q.threshold_version if q else None, decision.model, decision.latency_ms,
            per_q_cost if key in asked else 0.0, v.error, q.question_hash if q else None, v.cached,
        ))
    try:
        async with db.acquire() as conn:
            await conn.executemany(_LOG_SQL, rows)
    except Exception as exc:
        logger.warning("decision_log_write_failed", stage=stage, error=str(exc))
    if asked and decision.model:
        zones: dict[str, int] = {}
        for v in decision.verdicts.values():
            zones[v.zone] = zones.get(v.zone, 0) + 1
        kwargs = dict(stage=stage, role="validate", model=decision.model, tokens_in=decision.input_tokens,
                      tokens_out=0, cost_usd=decision.cost_usd, provider="typesafe",
                      quality_signal={"source": "decide", "questions": len(asked), "zones": zones},
                      tenant_id=str(tenant_id) if tenant_id else None)
        await record_call_with_pool(db, **kwargs)       # its own, separate acquisition


async def decide(stage: str, subject_key: str, state: Any, question_keys: list[str], *,
                 tenant_id: Optional[str] = None, pool=None, use_cache: bool = True) -> Decision:
    """Ask Jev `question_keys` about `state` (text or a JSON object). Never raises.

    `subject_key` identifies what was judged (e.g. "kw:US:mongar bhutan", "atom:atom_1a2b") so the
    Decisions view and calibration can join back to it. `tenant_id` = whose content `state` is
    (None or aa_internal = platform content)."""
    try:
        if pool is not None:
            return await _decide(pool, stage, subject_key, state, question_keys, tenant_id, use_cache)
        conn = await asyncpg.connect(get_database_url(), ssl="require")
        try:
            return await _decide(_SingleConn(conn), stage, subject_key, state, question_keys, tenant_id,
                                 use_cache)
        finally:
            await conn.close()
    except Exception as exc:                      # never break the calling stage
        logger.warning("decide_failed", stage=stage, subject=subject_key, error=str(exc)[:200])
        return Decision(verdicts={k: Verdict(k, "off", "error", error=str(exc)[:500]) for k in question_keys})


def decide_sync(stage: str, subject_key: str, state: Any, question_keys: list[str], *,
                tenant_id: Optional[str] = None) -> Decision:
    """For plain-`def` call sites that run off the event loop (S1 graph nodes run in a worker
    thread). Opens its own connection. Called from a thread that already runs a loop, it cannot
    wait, so it fails open (zone='error') instead of blocking that loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(decide(stage, subject_key, state, question_keys, tenant_id=tenant_id))
    logger.warning("decide_sync_called_on_event_loop", stage=stage)
    return Decision(verdicts={k: Verdict(k, "off", "error", error="decide_sync on event loop")
                              for k in question_keys})
