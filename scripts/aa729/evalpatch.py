"""AA-729 — in-process patches that make a harness run safe to point at the real writer path.

Only this Python process is affected (an ECS exec / one-off task runs its own interpreter); nothing in the
app code changes. `install(run_id)`:

1. **Logs every model call under `adhoc_aa729`.** `shared.llm_client.call_log` writes every
   `llm_call_log` row (LLMClient callers, Jev `decide()`, embeddings, judges) through one statement,
   `_INSERT_SQL`, looked up at call time. It is replaced by a copy that stores `stage = 'adhoc_aa729'` and
   moves the real stage into `quality_signal.aa729_stage`, with the run id in `quality_signal.aa729_run`.
   Routing is untouched: the real stage still picks the model.
2. **Captures T9 output instead of storing it.** `service._finalize_piece` (the one UPDATE of a finished
   piece) and `service._insert_placeholder_piece` (the INSERT) are replaced by in-memory captures.
3. **No eval lesson reaches `shared.pipeline_lessons`** (`flag_fix_node._write_lessons_safe` → no-op).
4. **No shadow A/B call** (`LLMClient._start_shadow` → no-op): no extra spend, no `llm_shadow_log` row.
5. **Counts in-process spend** (`LLMClient.generate` wrapper sums `LLMResponse.cost_usd` and calls), the
   cost guard's fallback when a log write fails.

`install` refuses to run (RuntimeError) when a patched name is gone or the INSERT's column list changed,
so a refactor of the gateway cannot silently send eval rows to real stages.
"""
from __future__ import annotations

import re
import threading
import uuid
from typing import Any

from scripts.aa729.common import STAGE

INSERT_COLUMNS = ("tenant_id, stage, role, model, tokens_in, tokens_out, cost_usd, quality_signal, "
                  "content_piece_id, angle_gate_request_id, stop_reason, account, fallback_used, provider, job_id")
N_PARAMS = 15


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def relabel_sql(original: str, run_id: str) -> str:
    """The patched INSERT: same 15 parameters, stage forced to adhoc_aa729, real stage + run id in
    quality_signal. Raises if `original` is not the statement this was written against."""
    run = str(uuid.UUID(run_id))  # validated: it is inlined as a literal
    norm = _norm(original)
    if f"({INSERT_COLUMNS})" not in norm.replace("( ", "(").replace(" )", ")"):
        raise RuntimeError("shared.llm_client.call_log._INSERT_SQL changed its column list — update "
                           "scripts/aa729/evalpatch.py before running the harness")
    if any(f"${i}" not in norm for i in range(1, N_PARAMS + 1)) or f"${N_PARAMS + 1}" in norm:
        raise RuntimeError("shared.llm_client.call_log._INSERT_SQL changed its parameters — update "
                           "scripts/aa729/evalpatch.py before running the harness")
    signal = ("(CASE WHEN jsonb_typeof($8::jsonb) = 'object' THEN $8::jsonb "
              "ELSE jsonb_build_object('signal', $8::jsonb) END) || "
              f"jsonb_build_object('aa729_stage', $2::text, 'aa729_run', '{run}'::text)")
    return (f"INSERT INTO shared.llm_call_log ({INSERT_COLUMNS}) VALUES ($1::uuid, '{STAGE}', $3, $4, $5, $6, $7, "
            f"{signal}, $9::uuid, $10::uuid, $11, $12, $13, $14, $15::uuid)")


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


SPEND = Spend()
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
    from shared.llm_client import call_log
    from shared.llm_client.client import LLMClient

    for owner, name in ((call_log, "_INSERT_SQL"), (t9_service, "_finalize_piece"),
                        (t9_service, "_insert_placeholder_piece"), (flag_fix_node, "_write_lessons_safe"),
                        (LLMClient, "_start_shadow"), (LLMClient, "generate")):
        if not hasattr(owner, name):
            raise RuntimeError(f"{getattr(owner, '__name__', owner)}.{name} is gone — "
                               "update scripts/aa729/evalpatch.py")

    call_log._INSERT_SQL = relabel_sql(call_log._INSERT_SQL, run_id)
    t9_service._finalize_piece = _capture_finalize
    t9_service._insert_placeholder_piece = _capture_placeholder
    flag_fix_node._write_lessons_safe = lambda *a, **k: None
    LLMClient._start_shadow = lambda self, *a, **k: None

    original_generate = LLMClient.generate

    def generate(self, request):
        resp = original_generate(self, request)
        SPEND.add(getattr(resp, "cost_usd", 0.0))
        return resp

    LLMClient.generate = generate
    _INSTALLED["run_id"] = run_id
