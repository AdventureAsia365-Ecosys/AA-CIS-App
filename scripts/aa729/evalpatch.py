"""AA-729 — in-process patches that make a harness run safe to point at the real writer path.

Only this Python process is affected (an ECS exec / one-off task runs its own interpreter); nothing in the
app code changes. `install(run_id)`:

1. **Logs every model call under `adhoc_aa729`.** `shared.llm_client.call_log` writes every
   `llm_call_log` row (LLMClient callers, Jev `decide()`, embeddings, judges) through one statement,
   `_INSERT_SQL`, looked up at call time. It is replaced by a copy that stores `stage = 'adhoc_aa729'` and
   moves the real stage into `quality_signal.aa729_stage`, with the run id in `quality_signal.aa729_run`.
   `tenant_id`, `content_piece_id` and `angle_gate_request_id` are written NULL (the real values go to
   `quality_signal.aa729_tenant` / `aa729_piece` / `aa729_request`), so eval spend never reaches a tenant's
   usage / billing (`v_tenant_monthly_usage`, `llm_call_daily`) or a real piece's cost breakdown.
   Routing is untouched: the real stage still picks the model.
1b. **Logs every Jev verdict under `adhoc_aa729:<real stage>`.** `decide._LOG_SQL` (shared.decision_log)
   and `decide._HITS_SQL` (cache-hit counts) are replaced the same way, so zone-rate dashboards, reviewer
   outcome stamping by stage and AA-845 / AA-756 sampling can exclude eval rows with `stage LIKE 'adhoc_%'`.
   Tenant and subject stay real: the verdict cache (`_CACHE_SQL`, no stage filter) behaves as in production.
2. **Captures T9 output instead of storing it.** `service._finalize_piece` (the one UPDATE of a finished
   piece) and `service._insert_placeholder_piece` (the INSERT) are replaced by in-memory captures.
3. **No eval lesson reaches `shared.pipeline_lessons`** (`flag_fix_node._write_lessons_safe` → no-op).
4. **No shadow A/B call** (`LLMClient._start_shadow` → no-op): no extra spend, no `llm_shadow_log` row.
5. **Counts in-process spend and stops at the cap mid-sample** (`LLMClient.generate` wrapper sums
   `LLMResponse.cost_usd` and calls). Before every call it checks `BUDGET`: logged spend (refreshed by the
   scorer every few seconds) + in-process spend since + one call margin per concurrent sample; at the cap it
   raises CostCapReached and records the sample's job id in `BUDGET.tripped_jobs`, so the scorer reports that
   sample as an error, not as a verdict.

`install` refuses to run (RuntimeError) when a patched name is gone or an INSERT's column list changed,
so a refactor of the gateway cannot silently send eval rows to real stages or tenants.
"""
from __future__ import annotations

import re
import threading
import uuid
from typing import Any

from scripts.aa729.common import STAGE, CostCapReached

INSERT_COLUMNS = ("tenant_id, stage, role, model, tokens_in, tokens_out, cost_usd, quality_signal, "
                  "content_piece_id, angle_gate_request_id, stop_reason, account, fallback_used, provider, job_id")
N_PARAMS = 15
DECISION_COLUMNS = ("stage, question_key, subject_key, tenant_id, job_id, mode, zone, probability, choice, "
                    "probabilities, threshold_version, model, latency_ms, cost_usd, error, question_hash, cached")
N_DECISION_PARAMS = 17
HITS_COLUMNS = "day, stage, question_key, mode, zone, hits"
N_HITS_PARAMS = 6


def _check_insert(norm: str, table: str, columns: str, n_params: int, where: str) -> None:
    flat = norm.replace("( ", "(").replace(" )", ")")
    if f"INSERT INTO {table} ({columns})" not in flat:
        raise RuntimeError(f"{where} changed its column list — update scripts/aa729/evalpatch.py before running "
                           "the harness")
    if any(f"${i}" not in norm for i in range(1, n_params + 1)) or f"${n_params + 1}" in norm:
        raise RuntimeError(f"{where} changed its parameters — update scripts/aa729/evalpatch.py before running "
                           "the harness")


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def relabel_sql(original: str, run_id: str) -> str:
    """The patched INSERT: same 15 parameters, stage forced to adhoc_aa729, real stage + run id in
    quality_signal. Raises if `original` is not the statement this was written against."""
    run = str(uuid.UUID(run_id))  # validated: it is inlined as a literal
    _check_insert(_norm(original), "shared.llm_call_log", INSERT_COLUMNS, N_PARAMS,
                  "shared.llm_client.call_log._INSERT_SQL")
    # tenant / piece / request are written NULL and kept in quality_signal: an eval row must never count as
    # a tenant's usage or a real piece's cost. $1/$9/$10 stay referenced (cast ::uuid) so the 15 parameters
    # and their types are unchanged for the callers.
    signal = ("(CASE WHEN jsonb_typeof($8::jsonb) = 'object' THEN $8::jsonb "
              "ELSE jsonb_build_object('signal', $8::jsonb) END) || "
              f"jsonb_build_object('aa729_stage', $2::text, 'aa729_run', '{run}'::text, "
              "'aa729_tenant', $1::uuid::text, 'aa729_piece', $9::uuid::text, 'aa729_request', $10::uuid::text)")
    return (f"INSERT INTO shared.llm_call_log ({INSERT_COLUMNS}) VALUES (NULL, '{STAGE}', $3, $4, $5, $6, $7, "
            f"{signal}, NULL, NULL, $11, $12, $13, $14, $15::uuid)")


def _prefix_stage(norm: str, param: str) -> str:
    """`VALUES (..., $n, ...)` → `VALUES (..., 'adhoc_aa729:' || $n::text, ...)` for the stage parameter."""
    head, sep, tail = norm.partition("VALUES (")
    vals = [v.strip() for v in tail.rstrip().rstrip(")").split(",")]
    if not sep or param not in vals:
        raise RuntimeError("unexpected VALUES list — update scripts/aa729/evalpatch.py")
    vals[vals.index(param)] = f"'{STAGE}:' || {param}::text"
    return f"{head}VALUES ({', '.join(vals)})"


def relabel_decision_sql(original: str) -> str:
    """decide._LOG_SQL with the stage written as 'adhoc_aa729:<real stage>'; everything else unchanged."""
    norm = _norm(original)
    _check_insert(norm, "shared.decision_log", DECISION_COLUMNS, N_DECISION_PARAMS,
                  "shared.llm_client.decide._LOG_SQL")
    return _prefix_stage(norm, "$1")


def relabel_hits_sql(original: str) -> str:
    """decide._HITS_SQL (daily cache-hit counts) with the same stage prefix."""
    norm = _norm(original)
    _check_insert(norm, "shared.decision_cache_hits_daily", HITS_COLUMNS, N_HITS_PARAMS,
                  "shared.llm_client.decide._HITS_SQL")
    head, sep, tail = norm.partition(" ON CONFLICT")
    if not sep:
        raise RuntimeError("shared.llm_client.decide._HITS_SQL lost its ON CONFLICT — update "
                           "scripts/aa729/evalpatch.py")
    return _prefix_stage(head, "$2") + sep + tail


class Spend:
    """In-process LLM spend (LLMClient.generate only — Jev and embeddings are counted from the log)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.usd = 0.0
        self.calls = 0

    def add(self, usd: float) -> None:
        with self._lock:
            self.usd += float(usd or 0.0)
            self.calls += 1


CALL_MARGIN_USD = 0.10  # one model call's worst case (a Sonnet rewrite); kept free per concurrent sample


class Budget:
    """The in-call cost stop. The launch guard (common.CostGuard) only decides whether a sample may START;
    this one stops a sample already running (S1 Sonnet retry, T9 repairs, a timed-out thread still
    spending) at its next model call.

    projected = max(in-process spend, last logged spend + in-process spend since that read). A call may run
    only while projected + slots * CALL_MARGIN_USD < cap (slots = concurrency: every running sample may have
    one call in flight). Jev / embedding spend is seen through the logged number, refreshed by the scorer."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.cap_usd: float | None = None
        self.slots = 1
        self.logged_usd = 0.0
        self._local_at_read = 0.0
        self.tripped: str | None = None
        self.tripped_jobs: set[str] = set()

    def configure(self, cap_usd: float | None, slots: int = 1) -> None:
        """Start a run: set the cap, forget any earlier run's reading and trip."""
        with self._lock:
            self.cap_usd, self.slots = cap_usd, max(1, int(slots))
            self.logged_usd, self._local_at_read = 0.0, SPEND.usd
            self.tripped, self.tripped_jobs = None, set()

    def note_logged(self, usd: float) -> None:
        with self._lock:
            if usd >= self.logged_usd:
                self.logged_usd, self._local_at_read = float(usd), SPEND.usd

    def projected(self) -> float:
        with self._lock:
            return max(SPEND.usd, self.logged_usd + SPEND.usd - self._local_at_read)

    def check(self, job_id: str | None = None) -> None:
        if self.cap_usd is None:
            return
        p = self.projected()
        if p + self.slots * CALL_MARGIN_USD >= self.cap_usd:
            msg = (f"cap ${self.cap_usd:.2f}: projected ${p:.4f} + {self.slots} x ${CALL_MARGIN_USD:.2f} call margin "
                   "would pass it")
            with self._lock:
                self.tripped = self.tripped or msg
                if job_id:
                    self.tripped_jobs.add(str(job_id))
            raise CostCapReached(msg)


SPEND = Spend()
BUDGET = Budget()
T9_CAPTURE: dict[str, dict] = {}
_INSTALLED: dict[str, Any] = {}


async def _capture_finalize(pool, *, piece_id, **kw) -> dict:
    row = {"piece_id": str(piece_id), **kw}
    T9_CAPTURE[str(piece_id)] = row
    return row


async def _capture_placeholder(pool, *, tenant_id, request_id, **kw) -> dict:
    pid = str(uuid.uuid4())
    return {"piece_id": pid, "tenant_id": str(tenant_id), "angle_gate_request_id": str(request_id),
            "attempt_number": 1, "content_text": "", "status": "processing", **kw}


def install(run_id: str) -> None:
    """Apply all patches for this process (idempotent for the same run id)."""
    if _INSTALLED.get("run_id") == run_id:
        return
    if _INSTALLED:
        raise RuntimeError("evalpatch already installed for another run in this process")
    from services.acp_content_writing import service as t9_service
    from services.content_generation import flag_fix_node
    from shared.llm_client import call_log, decide
    from shared.llm_client.client import LLMClient

    for owner, name in ((call_log, "_INSERT_SQL"), (decide, "_LOG_SQL"), (decide, "_HITS_SQL"),
                        (t9_service, "_finalize_piece"),
                        (t9_service, "_insert_placeholder_piece"), (flag_fix_node, "_write_lessons_safe"),
                        (LLMClient, "_start_shadow"), (LLMClient, "generate")):
        if not hasattr(owner, name):
            raise RuntimeError(f"{getattr(owner, '__name__', owner)}.{name} is gone — "
                               "update scripts/aa729/evalpatch.py")

    insert_sql = relabel_sql(call_log._INSERT_SQL, run_id)  # all three validated before any is replaced
    log_sql, hits_sql = relabel_decision_sql(decide._LOG_SQL), relabel_hits_sql(decide._HITS_SQL)
    call_log._INSERT_SQL, decide._LOG_SQL, decide._HITS_SQL = insert_sql, log_sql, hits_sql
    t9_service._finalize_piece = _capture_finalize
    t9_service._insert_placeholder_piece = _capture_placeholder
    flag_fix_node._write_lessons_safe = lambda *a, **k: None
    LLMClient._start_shadow = lambda self, *a, **k: None

    original_generate = LLMClient.generate

    def generate(self, request):
        BUDGET.check(call_log.current_job_id())
        resp = original_generate(self, request)
        SPEND.add(getattr(resp, "cost_usd", 0.0))
        return resp

    LLMClient.generate = generate
    _INSTALLED["run_id"] = run_id
