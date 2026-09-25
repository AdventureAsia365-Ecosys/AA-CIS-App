"""AA-637 — optional, per-job side channel for live LLM output (docs/adr/0004).

A background writing job binds a sink for its own async context; `LLMClient.generate()` and the
provider calls report into it. `asyncio.to_thread()` and LangGraph's `run_in_executor` both copy
the context, so the sink reaches the worker threads that make the synchronous Bedrock calls.
With no sink bound (every other caller) nothing here does anything.

Sink methods are called from worker threads and must be thread-safe and never raise into the
LLM call — `emit_*` below swallows sink errors so a broken progress view can never fail a write.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator, Optional, Protocol

import structlog

logger = structlog.get_logger()


class StreamSink(Protocol):
    def wants(self, stage: Optional[str]) -> bool: ...          # stream text deltas for this stage?
    def on_call_start(self, stage: Optional[str]) -> None: ...  # generate() entered
    def on_restart(self, stage: Optional[str]) -> None: ...     # a provider attempt starts (drop partial text)
    def on_delta(self, stage: Optional[str], text: str) -> None: ...
    def on_call_end(self, stage: Optional[str], ok: bool) -> None: ...


_current: ContextVar[Optional[StreamSink]] = ContextVar("llm_stream_sink", default=None)


def current() -> Optional[StreamSink]:
    return _current.get()


@contextmanager
def bind(sink: StreamSink) -> Iterator[StreamSink]:
    token = _current.set(sink)
    try:
        yield sink
    finally:
        _current.reset(token)


def _safe(fn: Callable[[], None], what: str) -> None:
    try:
        fn()
    except Exception as e:  # never let a progress view break an LLM call
        logger.warning("stream_sink_error", hook=what, error=str(e))


def emit_call_start(stage: Optional[str]) -> None:
    s = current()
    if s is not None:
        _safe(lambda: s.on_call_start(stage), "call_start")


def emit_restart(stage: Optional[str]) -> None:
    s = current()
    if s is not None:
        _safe(lambda: s.on_restart(stage), "restart")


def emit_call_end(stage: Optional[str], ok: bool) -> None:
    s = current()
    if s is not None:
        _safe(lambda: s.on_call_end(stage, ok), "call_end")


def delta_callback(stage: Optional[str]) -> Optional[Callable[[str], None]]:
    """A per-chunk callback for `stage`, or None when no sink is bound / the sink doesn't stream
    this stage. Provider code uses None to mean "keep the old non-streaming request"."""
    s = current()
    if s is None:
        return None
    try:
        if not s.wants(stage):
            return None
    except Exception:
        return None

    def _cb(text: str) -> None:
        if text:
            _safe(lambda: s.on_delta(stage, text), "delta")
    return _cb
