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
import threading
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
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

# AA-720 — Jev credit monitoring. TypeSafe has no balance API; a 402 with error_type
# "billing_error" is the only signal that credit ran out. decide() fails open (stages keep their
# existing rule), so a silent credit outage would otherwise cost ~_TIMEOUT_S per failed call across
# tens of thousands of verdicts (the China rerun, S209). The breaker stops calling TypeSafe for a
# cooldown window after a 402; the alert surfaces it to an admin.
JEV_CREDIT_EXHAUSTED_EVENT = "platform.jev_credit.exhausted"
_JEV_COOLDOWN_MIN = float(os.environ.get("JEV_CREDIT_COOLDOWN_MIN", "15"))
# Breaker state is process-local (same as the _questions / _allowlist caches below): each worker
# trips and recovers on its own clock. A truly global breaker would need DB/Redis; per-process is
# the minimal design that matches the existing caches and is enough to stop the latency bleed.
_jev_cooldown_until = 0.0        # time.monotonic() deadline; 0.0 = breaker closed (calls allowed)


class JevBillingError(Exception):
    """Raised inside _call_jev when TypeSafe returns 402 with error_type 'billing_error' (credit
    exhausted). Distinct from generic failures so _decide can trip the breaker and alert."""


def jev_breaker_open() -> bool:
    """True while the credit breaker is cooling down (TypeSafe calls are being skipped)."""
    return time.monotonic() < _jev_cooldown_until


def _trip_jev_breaker() -> None:
    global _jev_cooldown_until
    _jev_cooldown_until = time.monotonic() + _JEV_COOLDOWN_MIN * 60.0


def reset_jev_breaker() -> None:
    """Close the breaker now (used by the canary once a probe succeeds, and by tests)."""
    global _jev_cooldown_until
    _jev_cooldown_until = 0.0


def is_billing_error(status_code: int, body: Any) -> bool:
    """A TypeSafe 402 whose body says error_type == 'billing_error' means credit is exhausted.
    Keyed on error_type (not the message text) per the AA-720 live probe (S209)."""
    if status_code != 402:
        return False
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        return detail.get("error_type") == "billing_error"
    return True        # any 402 from this endpoint is treated as a credit problem


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
                if resp.status_code == 402:          # AA-720 — credit exhausted, do not retry
                    try:
                        parsed = resp.json()
                    except Exception:
                        parsed = None
                    if is_billing_error(402, parsed):
                        raise JevBillingError("TypeSafe 402 billing_error (credit exhausted)")
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
      AND NOT l.cached
      AND l.created_at > now() - make_interval(days => $5)
    ORDER BY l.question_key, l.created_at DESC
"""
# AA-742: `NOT l.cached` — the age check runs on the verdict Jev actually gave. Before, every cache
# hit was re-logged with a fresh created_at, so a verdict that kept being re-read never expired.

# AA-742: in-process memo of reusable answers, in front of the DB cache. A3 recomputes ask the same
# (subject, question) for every segment after each atomized tour; the memo answers those without a
# lookup query. Keyed by the wording hash, so a reworded question misses. The Zone is still computed
# with today's Floors (zone_for), exactly like a DB hit. TTL is far below CACHE_MAX_AGE_DAYS.
_MEMO_MAX = 50_000
_MEMO_TTL_S = 6 * 3600
_memo: OrderedDict = OrderedDict()            # key -> (expires_at monotonic, answer row)
_memo_lock = threading.Lock()                 # decide_sync runs loops in worker threads

# AA-742: cache hits are counted, not logged row by row — flushed into
# shared.decision_cache_hits_daily on the next ledger write once enough are pending or time passed.
# A crash loses at most the unflushed counts (observability only, never a verdict).
_HITS_FLUSH_N = 500
_HITS_FLUSH_S = 60.0
_hits: Counter = Counter()                    # (day, stage, question_key, mode, zone) -> n
_hits_flushed_at = time.monotonic()
_HITS_SQL = """
    INSERT INTO shared.decision_cache_hits_daily (day, stage, question_key, mode, zone, hits)
    VALUES ($1, $2, $3, $4, $5, $6)
    ON CONFLICT (day, stage, question_key, mode, zone)
    DO UPDATE SET hits = shared.decision_cache_hits_daily.hits + excluded.hits, updated_at = now()
"""


def _memo_key(subject_key: str, tenant_id, q: Question) -> tuple:
    return (subject_key, str(tenant_id) if tenant_id else None, q.key, q.question_hash or "")


def _memo_get(key: tuple) -> Optional[dict]:
    with _memo_lock:
        hit = _memo.get(key)
        if hit is None:
            return None
        if hit[0] < time.monotonic():
            _memo.pop(key, None)
            return None
        _memo.move_to_end(key)
        return hit[1]


def _memo_put(key: tuple, row: dict) -> None:
    with _memo_lock:
        _memo[key] = (time.monotonic() + _MEMO_TTL_S, row)
        _memo.move_to_end(key)
        while len(_memo) > _MEMO_MAX:
            _memo.popitem(last=False)


def _take_due_hits(force: bool = False) -> list[tuple]:
    """Pop the pending hit counts if a flush is due (or forced); [] otherwise."""
    global _hits_flushed_at
    with _memo_lock:
        if not _hits:
            return []
        if not force and sum(_hits.values()) < _HITS_FLUSH_N \
                and time.monotonic() - _hits_flushed_at < _HITS_FLUSH_S:
            return []
        rows = [(*k, n) for k, n in _hits.items()]
        _hits.clear()
        _hits_flushed_at = time.monotonic()
        return rows


def reset_decision_memo() -> None:
    """Tests: drop the memo and any pending hit counts."""
    with _memo_lock:
        _memo.clear()
        _hits.clear()


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


_JEV_ALERT_RECENT_SQL = """
    SELECT 1 FROM shared.notifications
     WHERE event_type = $1 AND is_read = FALSE AND created_at >= now() - interval '24 hours'
     LIMIT 1
"""
_JEV_ALERT_INSERT_SQL = """
    INSERT INTO shared.notifications
        (tenant_id, actor_type, event_type, entity_type, entity_id, payload, target_roles)
    VALUES ($1::uuid, 'system', $2, 'jev_account', 'typesafe', $3::jsonb, ARRAY['admin','content'])
"""
_JEV_ALERT_MESSAGE = (
    "Jev (TypeSafe) returned 402 billing_error — API credits are exhausted. The decision layer is "
    "failing open (stages run on their existing rules without Jev). Top up the TypeSafe account "
    "(thu@adventure.asia); there is no balance API, so this bell is the only signal."
)


async def maybe_alert_jev_credit(conn, *, source: str) -> bool:
    """Insert a platform.jev_credit.exhausted notification unless one is already unread within 24h
    (mirrors AA-627's _maybe_alert_low_balance throttle). Returns True if a new alert was inserted.
    Best-effort: a notification failure must never fail the calling stage."""
    try:
        if await conn.fetchval(_JEV_ALERT_RECENT_SQL, JEV_CREDIT_EXHAUSTED_EVENT):
            return False
        payload = json.dumps({"message": _JEV_ALERT_MESSAGE, "error_type": "billing_error", "source": source})
        await conn.execute(_JEV_ALERT_INSERT_SQL, PLATFORM_TENANT_ID, JEV_CREDIT_EXHAUSTED_EVENT, payload)
        logger.warning("jev_credit_exhausted_alert", source=source)
        return True
    except Exception as exc:
        logger.warning("jev_credit_alert_insert_failed", error=str(exc)[:200])
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
        for key, q in list(to_ask.items()):
            row = _memo_get(_memo_key(subject_key, tenant_id, q))
            answer = cached_answer(q, row) if row else None
            if answer is not None:
                v = zone_for(q, answer)
                v.cached = True
                decision.verdicts[key] = v
                to_ask.pop(key)
    if to_ask and use_cache:
        try:
            async with db.acquire() as conn:
                rows = await conn.fetch(
                    _CACHE_SQL, subject_key, list(to_ask), [q.question_hash or "" for q in to_ask.values()],
                    str(tenant_id) if tenant_id else None, CACHE_MAX_AGE_DAYS,
                )
            for r in rows:
                q = to_ask[r["question_key"]]
                answer = cached_answer(q, dict(r))
                if answer is not None:
                    v = zone_for(q, answer)
                    v.cached = True
                    decision.verdicts[q.key] = v
                    to_ask.pop(q.key, None)
                    _memo_put(_memo_key(subject_key, tenant_id, q), dict(r))
        except Exception as exc:                  # a cache miss is never an error
            logger.warning("decide_cache_read_failed", stage=stage, error=str(exc)[:200])

    if to_ask and jev_breaker_open():
        # AA-720 — credit breaker open: skip TypeSafe entirely (no 3s-per-call bleed), the stage
        # keeps its existing rule just as it would on a fail-open error.
        for key, q in to_ask.items():
            decision.verdicts[key] = Verdict(key, q.mode, "error", error="credit_exhausted (breaker open)")
        to_ask = {}

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
                v = decision.verdicts[key]
                if v.zone in ("accept", "grey", "reject") and v.probability is not None:
                    _memo_put(_memo_key(subject_key, tenant_id, q),
                              {"probability": v.probability, "choice": v.choice, "probabilities": v.probabilities})
        except JevBillingError as exc:
            # AA-720 — credit exhausted: trip the breaker and raise one throttled alert, then fail
            # open like any other error so the stage keeps running on its existing rule.
            decision.latency_ms = int((time.perf_counter() - started) * 1000)
            _trip_jev_breaker()
            logger.warning("decide_call_billing_error", stage=stage, subject=subject_key)
            for key, q in to_ask.items():
                decision.verdicts[key] = Verdict(key, q.mode, "error", error="credit_exhausted")
            try:
                async with db.acquire() as conn:
                    await maybe_alert_jev_credit(conn, source="decide")
            except Exception as alert_exc:
                logger.warning("jev_credit_alert_failed", error=str(alert_exc)[:200])
        except Exception as exc:
            decision.latency_ms = int((time.perf_counter() - started) * 1000)
            logger.warning("decide_call_failed", stage=stage, subject=subject_key, error=str(exc)[:200])
            for key, q in to_ask.items():
                decision.verdicts[key] = Verdict(key, q.mode, "error", error=str(exc)[:500])

    await _write_logs(db, stage, subject_key, tenant_id, decision, to_ask)
    return decision


async def _insert_logs(conn, rows, hit_rows) -> None:
    """Ledger rows + cache-hit counts in one transaction, so a retry never writes either twice."""
    async with conn.transaction():
        if rows:
            await conn.executemany(_LOG_SQL, rows)
        if hit_rows:
            await conn.executemany(_HITS_SQL, hit_rows)


async def _write_logs(db, stage, subject_key, tenant_id, decision: Decision, asked) -> None:
    """One ledger row per verdict that was asked, errored or skipped. A cache hit gets no row
    (AA-742): it is counted into shared.decision_cache_hits_daily, flushed in batches."""
    job_id = current_job_id()
    per_q_cost = decision.cost_usd / len(asked) if asked else 0.0
    rows = []
    day = datetime.now(timezone.utc).date()
    for key, v in decision.verdicts.items():
        if v.cached:
            with _memo_lock:
                _hits[(day, stage, key, v.mode, v.zone)] += 1
            continue
        q = _questions.get(key)
        rows.append((
            stage, key, subject_key, str(tenant_id) if tenant_id else None, job_id, v.mode,
            v.zone, v.probability, v.choice,
            json.dumps(v.probabilities) if v.probabilities is not None else None,
            q.threshold_version if q else None, decision.model, decision.latency_ms,
            per_q_cost if key in asked else 0.0, v.error, q.question_hash if q else None, v.cached,
        ))
    hit_rows = _take_due_hits()
    if rows or hit_rows:
        try:
            async with db.acquire() as conn:
                await _insert_logs(conn, rows, hit_rows)
        except Exception as exc:
            # AA-756: retry once on a fresh connection — a lost row is a billed verdict that the
            # cache never sees, so every later run asks Jev (and pays) again.
            logger.warning("decision_log_pool_write_failed", stage=stage, error=str(exc))
            try:
                conn = await asyncpg.connect(get_database_url(), ssl="require")
                try:
                    await _insert_logs(conn, rows, hit_rows)
                finally:
                    await conn.close()
            except Exception as exc2:
                logger.warning("decision_log_write_failed", stage=stage, error=str(exc2))
                if hit_rows:                        # keep the counts for the next flush
                    with _memo_lock:
                        for *k, n in hit_rows:
                            _hits[tuple(k)] += n
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
