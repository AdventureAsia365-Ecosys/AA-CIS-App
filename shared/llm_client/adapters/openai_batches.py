"""AA-1076 — the OpenAI Batch API adapter of the gateway batch lane (``shared/llm_client/batch_lane.py``), the second
provider behind the same interface as ``adapters/anthropic_batches.py``.

One batch on one ``openai`` connection (kind ``openai`` only: Azure OpenAI's batch deployments are a different
product and are not wired), through the SDK client of the connection (``adapters/openai_chat.client_for``: its
secret or allowlisted credential env, its checked endpoint, its timeout, no SDK retries):

- ``submit``: one JSONL input file (``files.create(purpose="batch")``), one line per request
  ``{"custom_id", "method": "POST", "url": "/v1/chat/completions", "body"}``, then ``batches.create`` with a 24-hour
  completion window. The ``body`` of a line is ``client.openai_chat_body`` (the same Chat Completions arguments an
  on-demand call of the stage sends).
- ``status``: ``batches.retrieve``; terminal = completed | failed | expired | cancelled.
- ``results``: the output file (one line per finished request: ``response.status_code`` + ``response.body``, a Chat
  Completion) and the error file; a request in neither is ``expired`` (window passed) or ``canceled``.

OpenAI Batch facts (to re-check on the OpenAI pricing page before a paid run, as the issue says): 50% of the
synchronous price, results within 24 hours, up to 50,000 requests / 200 MB per input file. The SDK the image
installs (``openai>=1.16.2``, latest at build) has ``client.batches``; an older one raises a clear error here.
"""
from __future__ import annotations

import json
from typing import Iterable, Optional

import structlog

from .. import connections
from . import openai_chat
from .anthropic_batches import BatchState, ItemOutcome

logger = structlog.get_logger()

ENDPOINT = "/v1/chat/completions"
COMPLETION_WINDOW = "24h"
MAX_REQUESTS = 50_000
MAX_BYTES = 200 * 1024 * 1024
TERMINAL = ("completed", "failed", "expired", "cancelled")


class OpenAIBatchError(RuntimeError):
    """A refused or failed Batch API call (classified ``other``; a 401 drops the cached key)."""
    llm_error_class = "other"


def _client(connection: connections.ProviderConnection):
    if connection.kind != "openai":
        raise connections.ConnectionCredentialError(
            f"connection {connection.connection_key!r} is a {connection.kind!r} connection: the batch lane runs "
            "OpenAI batches on an 'openai' connection only")
    client = openai_chat.client_for(connection)
    if not hasattr(client, "batches"):
        raise OpenAIBatchError("the installed openai SDK has no Batch API (client.batches): upgrade the image")
    return client


def _call(connection: connections.ProviderConnection, fn):
    client = _client(connection)
    try:
        return fn(client)
    except Exception as e:
        if openai_chat.is_auth_error(e):
            openai_chat.forget_connection(connection.connection_key)
        raise OpenAIBatchError(f"OpenAI Batch API on connection {connection.connection_key!r}: "
                               f"{type(e).__name__}: {str(e)[:300]}") from e


def input_jsonl(lines: list[tuple[str, dict]]) -> bytes:
    """The batch input file: one ``(custom_id, chat completions body)`` per line."""
    seen: set[str] = set()
    out = []
    for custom_id, body in lines:
        if custom_id in seen:
            raise ValueError(f"duplicate custom_id {custom_id!r}")
        seen.add(custom_id)
        out.append(json.dumps({"custom_id": custom_id, "method": "POST", "url": ENDPOINT, "body": body},
                              ensure_ascii=False, sort_keys=True))
    return ("\n".join(out) + "\n").encode("utf-8")


def submit(connection: connections.ProviderConnection, lines: list[tuple[str, dict]], *,
           metadata: Optional[dict] = None) -> str:
    if not lines:
        raise ValueError("a batch needs at least one request")
    if len(lines) > MAX_REQUESTS:
        raise ValueError(f"{len(lines)} requests: an OpenAI batch takes at most {MAX_REQUESTS}")
    data = input_jsonl(lines)
    if len(data) > MAX_BYTES:
        raise ValueError(f"batch input is {len(data)} bytes: an OpenAI batch file takes at most {MAX_BYTES}")

    def go(c):
        f = c.files.create(file=("aa-batch.jsonl", data, "application/jsonl"), purpose="batch")
        return c.batches.create(input_file_id=f.id, endpoint=ENDPOINT, completion_window=COMPLETION_WINDOW,
                                metadata={k: str(v)[:512] for k, v in (metadata or {}).items()})
    batch = _call(connection, go)
    logger.info("llm_batch_openai_created", connection=connection.connection_key, batch=batch.id, requests=len(lines))
    return str(batch.id)


def _get(obj, name, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def status(connection: connections.ProviderConnection, batch_id: str) -> BatchState:
    batch = _call(connection, lambda c: c.batches.retrieve(batch_id))
    rc = _get(batch, "request_counts")
    counts = {k: int(_get(rc, k, 0) or 0) for k in ("total", "completed", "failed")}
    st = str(_get(batch, "status") or "")
    return BatchState(processing_status=st, counts=counts, ended=st in TERMINAL)


def cancel(connection: connections.ProviderConnection, batch_id: str) -> None:
    _call(connection, lambda c: c.batches.cancel(batch_id))


def _int(obj, name) -> int:
    v = _get(obj, name)
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def outcome_of_line(line: dict) -> ItemOutcome:
    """One output / error file line -> ItemOutcome."""
    custom_id = str(line.get("custom_id") or "")
    resp = line.get("response") or {}
    err = line.get("error")
    code = resp.get("status_code")
    body = resp.get("body") or {}
    if err or not isinstance(code, int) or code >= 400:
        e = err or body.get("error") or {}
        return ItemOutcome(custom_id=custom_id, outcome="errored", error_type=str(_get(e, "code") or code or "error"),
                           error=str(_get(e, "message") or e or "errored")[:500])
    usage = body.get("usage") or {}
    prompt = _int(usage, "prompt_tokens")
    cached = min(_int(usage.get("prompt_tokens_details") or {}, "cached_tokens"), prompt)
    choice = (body.get("choices") or [{}])[0]
    return ItemOutcome(
        custom_id=custom_id, outcome="succeeded", text=str((choice.get("message") or {}).get("content") or ""),
        stop_reason=choice.get("finish_reason"), provider_model=body.get("model"), request_id=resp.get("request_id"),
        input_tokens=prompt - cached, output_tokens=_int(usage, "completion_tokens"), cache_read_tokens=cached,
        extra={"reasoning_tokens": _int(usage.get("completion_tokens_details") or {}, "reasoning_tokens") or None})


def _lines(text: str) -> list[dict]:
    out = []
    for raw in (text or "").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            logger.warning("llm_batch_openai_line_unreadable", length=len(raw))
    return out


def results(connection: connections.ProviderConnection, batch_id: str) -> Iterable[ItemOutcome]:
    def go(c):
        batch = c.batches.retrieve(batch_id)
        texts = []
        for fid in (_get(batch, "output_file_id"), _get(batch, "error_file_id")):
            if fid:
                texts.append(c.files.content(fid).text)
        return batch, texts
    _batch, texts = _call(connection, go)
    # a request no file names gets missing_outcome(<the batch's status>) from the lane
    return [outcome_of_line(line) for text in texts for line in _lines(text)]


def missing_outcome(batch_status: Optional[str]) -> str:
    """The outcome of a request no result file names: expired (window passed), canceled, else errored."""
    return {"expired": "expired", "cancelled": "canceled"}.get(batch_status or "", "errored")
