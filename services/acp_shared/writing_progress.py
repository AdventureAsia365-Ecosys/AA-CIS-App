"""AA-637 — live writing progress for tenant jobs (docs/adr/0004).

A `WritingProgress` is bound as the LLM stream sink (shared/llm_client/stream_sink.py) for one
background job. It keeps the job's steps and the CURRENT writer call's raw text in memory
(deltas arrive on worker threads → guarded by a lock) and an asyncio flusher writes a
tenant-facing snapshot to Redis every ~0.4s:

    wp:{tenant_id}:{kind}:{job_id} -> {"status", "steps", "sections", "revision", "message", "updated_at"}

`sections` is what the tenant sees — never the raw model output: JSON envelopes are parsed from
their partial text into labelled fields, internal citation tags are stripped (a half-written
trailing tag is held back until it completes), and a `===SUMMARY===` tail is hidden.

Best-effort by design: every Redis error is logged and swallowed. Nothing here touches the rows
the job persists.
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from typing import Any, Iterable, Optional

import structlog

logger = structlog.get_logger()

TTL_SECONDS = 3600
FLUSH_INTERVAL = 0.4
KINDS = ("tour", "piece")


def progress_key(tenant_id: str, kind: str, job_id: str) -> str:
    return f"wp:{tenant_id}:{kind}:{job_id}"


# ── Display shaping ────────────────────────────────────────────────────────────

_COMPLETE_TAG = re.compile(r"\s?\[(?:R|F):[^\]\n]{0,120}\]")
# A trailing tag that has started but not closed yet: "[", "[R", "[R:", "[F:abc12"
_PARTIAL_TAG_TAIL = re.compile(r"\s?\[(?:[RF](?::[^\]\n]{0,120})?)?$")
_SUMMARY_MARKER = "===SUMMARY==="


def strip_tags_streaming(text: str) -> str:
    """Remove complete internal citation tags and hold back a trailing half-written one."""
    text = _COMPLETE_TAG.sub("", text)
    return _PARTIAL_TAG_TAIL.sub("", text)


def _strip_leading_fence(text: str) -> str:
    t = text.lstrip()
    if t.startswith("```"):
        nl = t.find("\n")
        return "" if nl == -1 else t[nl + 1:]
    return text


def plain_text_view(raw: str) -> str:
    """Non-blog T9 channels: plain text + a trailing `===SUMMARY===` block that is internal."""
    text = _strip_leading_fence(raw)
    cut = text.find(_SUMMARY_MARKER)
    if cut != -1:
        text = text[:cut]
    else:
        # hold back a partially streamed marker ("=", "===SUM", ...) at the very end
        for n in range(len(_SUMMARY_MARKER) - 1, 0, -1):
            if text.endswith(_SUMMARY_MARKER[:n]):
                text = text[:-n]
                break
    text = text.replace("```", "")
    return strip_tags_streaming(text).rstrip()


_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


def scan_partial_json(raw: str) -> list[tuple[tuple, str]]:
    """Every scalar VALUE seen so far in a (possibly truncated) JSON document, in order, as
    (path, text). `path` is the tuple of object keys / array indices leading to the value. A
    string still being written at the end is included with what has arrived so far. Anything
    before the first `{` (a ``` fence, prose) is ignored. Never raises."""
    out: list[tuple[tuple, str]] = []
    start = raw.find("{")
    if start == -1:
        return out
    stack: list[dict[str, Any]] = []   # {"t": "{"|"[", "key": str|None, "idx": int, "await": ...}
    i, n = start, len(raw)

    def path() -> tuple:
        return tuple(f["key"] if f["t"] == "{" else f["idx"] for f in stack)

    def read_string(j: int) -> tuple[str, int, bool]:
        buf = []
        while j < n:
            c = raw[j]
            if c == '"':
                return "".join(buf), j + 1, True
            if c == "\\":
                if j + 1 >= n:
                    break
                e = raw[j + 1]
                if e == "u":
                    hexs = raw[j + 2:j + 6]
                    if len(hexs) < 4:
                        break
                    try:
                        buf.append(chr(int(hexs, 16)))
                    except ValueError:
                        pass
                    j += 6
                    continue
                buf.append(_ESCAPES.get(e, e))
                j += 2
                continue
            buf.append(c)
            j += 1
        return "".join(buf), n, False

    try:
        while i < n:
            c = raw[i]
            top = stack[-1] if stack else None
            if c in " \t\r\n":
                i += 1
            elif c == "{":
                stack.append({"t": "{", "key": None, "idx": 0, "await": "key"})
                i += 1
            elif c == "[":
                stack.append({"t": "[", "key": None, "idx": 0, "await": "value"})
                i += 1
            elif c in "}]":
                if stack:
                    stack.pop()
                if stack:
                    stack[-1]["await"] = "comma"
                else:
                    break
                i += 1
            elif c == ":":
                if top is not None:
                    top["await"] = "value"
                i += 1
            elif c == ",":
                if top is not None:
                    if top["t"] == "{":
                        top["await"] = "key"
                    else:
                        top["idx"] += 1
                        top["await"] = "value"
                i += 1
            elif c == '"':
                s, i, _closed = read_string(i + 1)
                if top is None:
                    continue
                if top["t"] == "{" and top["await"] == "key":
                    top["key"] = s
                    top["await"] = "colon"
                else:
                    out.append((path(), s))
                    top["await"] = "comma"
            else:
                # number / true / false / null
                j = i
                while j < n and raw[j] not in ",}] \t\r\n":
                    j += 1
                if top is not None and top["await"] == "value":
                    out.append((path(), raw[i:j]))
                    top["await"] = "comma"
                i = max(j, i + 1)
    except Exception as e:  # defensive: a malformed stream only degrades the preview
        logger.warning("scan_partial_json_error", error=str(e))
    return out


_TOUR_LABELS = {
    "name": "Title", "subtitle": "Subtitle", "summary": "Summary", "description": "Description",
    "highlights": "Highlights", "itineraries": "Itinerary",
    "seo_title": "SEO title", "seo_meta": "Meta description",
}
_BLOG_LABELS = {"body": "", "seo_title": "SEO title", "meta_description": "Meta description"}


def _sections_from_values(values: Iterable[tuple[tuple, str]], labels: dict[str, str]) -> list[dict]:
    sections: list[dict] = []
    by_key: dict[str, dict] = {}
    day_of: dict[int, str] = {}
    for p, v in values:
        if not p or p[0] not in labels:
            continue
        top = p[0]
        sec = by_key.get(top)
        if sec is None:
            sec = {"key": top, "label": labels[top], "parts": []}
            by_key[top] = sec
            sections.append(sec)
        v = strip_tags_streaming(v)
        if top == "highlights" and len(p) >= 2:
            sec["parts"].append(f"• {v}")
        elif top == "itineraries" and len(p) >= 3:
            idx, field = p[1], p[2]
            if field == "day":
                day_of[idx] = v
            elif field == "title":
                sec["parts"].append(f"Day {day_of.get(idx, idx + 1 if isinstance(idx, int) else '')} — {v}")
            elif field in ("body", "description"):
                sec["parts"].append(v)
        elif len(p) == 1:
            sec["parts"].append(v)
    # highlights read as a tight bullet list; everything else as paragraphs
    return [{"key": s["key"], "label": s["label"],
             "text": ("\n" if s["key"] == "highlights" else "\n\n").join(x for x in s["parts"] if x)}
            for s in sections if any(s["parts"])]


def build_sections(raw: str, mode: str) -> list[dict]:
    if not raw:
        return []
    if mode == "text":
        text = plain_text_view(raw)
        return [{"key": "body", "label": "", "text": text}] if text else []
    labels = _TOUR_LABELS if mode == "tour_json" else _BLOG_LABELS
    return _sections_from_values(scan_partial_json(raw), labels)


# ── Tracker ────────────────────────────────────────────────────────────────────

class WritingProgress:
    """Stream sink + step tracker for one tenant job. Create in the request handler (it needs the
    app's Redis), then inside the background coroutine:

        async with progress.running():
            with stream_sink.bind(progress):
                ...  # progress.step("write") etc.
    """

    def __init__(self, redis, *, tenant_id: str, kind: str, job_id: str,
                 steps: list[tuple[str, str]], stream_stages: Iterable[str], display: str,
                 stage_steps: Optional[dict[str, str]] = None, revise_step: Optional[str] = "revise",
                 write_step: str = "write"):
        if kind not in KINDS:
            raise ValueError(f"unknown progress kind {kind!r}")
        self._redis = redis
        self.key = progress_key(str(tenant_id), kind, str(job_id))
        self._lock = threading.Lock()
        self._steps = [{"key": k, "label": label, "state": "pending"} for k, label in steps]
        self._stream_stages = set(stream_stages)
        self._stage_steps = dict(stage_steps or {})
        self._revise_step = revise_step
        self._write_step = write_step
        self._display = display
        self._raw = ""
        self._writer_calls = 0
        self._status = "running"
        self._message: Optional[str] = None
        self._dirty = True
        self._failed = False
        self._task: Optional[asyncio.Task] = None

    # ── StreamSink ──
    def wants(self, stage: Optional[str]) -> bool:
        return stage in self._stream_stages

    def on_call_start(self, stage: Optional[str]) -> None:
        with self._lock:
            if stage in self._stream_stages:
                self._writer_calls += 1
                self._raw = ""
                if self._writer_calls > 1 and self._revise_step and self._has(self._revise_step):
                    self._activate(self._revise_step)
                else:
                    self._activate(self._write_step)
            elif stage in self._stage_steps:
                self._activate(self._stage_steps[stage])
            self._dirty = True

    def on_restart(self, stage: Optional[str]) -> None:
        if stage in self._stream_stages:
            with self._lock:
                self._raw = ""
                self._dirty = True

    def on_delta(self, stage: Optional[str], text: str) -> None:
        if stage in self._stream_stages:
            with self._lock:
                self._raw += text
                self._dirty = True

    def on_call_end(self, stage: Optional[str], ok: bool) -> None:
        return None

    # ── explicit steps ──
    @property
    def failed(self) -> bool:
        return self._failed

    def fail(self) -> None:
        """The job hit an error it handled itself (it still returns normally)."""
        self._failed = True

    def step(self, key: str) -> None:
        with self._lock:
            self._activate(key)
            self._dirty = True

    def _has(self, key: str) -> bool:
        return any(s["key"] == key for s in self._steps)

    def _activate(self, key: str) -> None:
        keys = [s["key"] for s in self._steps]
        if key not in keys:
            return
        target = keys.index(key)
        active = next((i for i, s in enumerate(self._steps) if s["state"] == "active"), None)
        if active is not None and active != target:
            self._steps[active]["state"] = "done"
        if active is None or target > active:
            # moving forward: anything skipped over that never ran is marked skipped
            for s in self._steps[:target]:
                if s["state"] == "pending":
                    s["state"] = "skipped"
        self._steps[target]["state"] = "active"

    def snapshot(self) -> dict:
        with self._lock:
            raw, steps = self._raw, [dict(s) for s in self._steps]
            status, message, revision = self._status, self._message, self._writer_calls
            self._dirty = False
        return {
            "status": status, "message": message, "revision": revision,
            "steps": steps, "sections": build_sections(raw, self._display),
            "updated_at": time.time(),
        }

    # ── lifecycle ──
    async def _write(self) -> None:
        if self._redis is None:  # no Redis on this app (tests / degraded) -> no live view
            return
        try:
            await self._redis.set(self.key, json.dumps(self.snapshot()), ex=TTL_SECONDS)
        except Exception as e:
            logger.warning("writing_progress_flush_failed", key=self.key, error=str(e))

    async def _flusher(self) -> None:
        while True:
            if self._dirty:
                await self._write()
            await asyncio.sleep(FLUSH_INTERVAL)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._flusher())

    async def finish(self, ok: bool, message: Optional[str] = None) -> None:
        with self._lock:
            for s in self._steps:
                if s["state"] == "active":
                    s["state"] = "done" if ok else "failed"
                elif s["state"] == "pending" and ok:
                    s["state"] = "skipped"
            self._status = "done" if ok else "failed"
            self._message = message
            self._dirty = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        await self._write()


# ── job presets + helpers for code running under a bound tracker ───────────────

# T2 tour rewrite. Order matters only for "skipped" marking (see _activate): judge/brand-audit
# calls map to "check", flag-fix/itinerary-nudge calls to "polish".
TOUR_STEPS = [
    ("research", "Reading the original tour and search keywords"),
    ("write", "Writing your version"),
    ("check", "Checking facts, tone and SEO"),
    ("revise", "Revising what the checks flagged"),
    ("polish", "Polishing the details"),
    ("save", "Saving to My Catalog Tours"),
]
TOUR_STAGE_STEPS = {
    "s1_judge": "check", "s1_brand_audit": "check",
    "s1_flag_fix": "polish", "s1_itinerary_nudge": "polish",
}


def _bound() -> Optional["WritingProgress"]:
    from shared.llm_client import stream_sink
    s = stream_sink.current()
    return s if isinstance(s, WritingProgress) else None


def progress_step(key: str) -> None:
    """Mark `key` active on the job's bound tracker, if any. Never raises."""
    t = _bound()
    if t is not None:
        try:
            t.step(key)
        except Exception as e:
            logger.warning("writing_progress_step_failed", step=key, error=str(e))


def progress_fail() -> None:
    t = _bound()
    if t is not None:
        t.fail()


async def read_progress(redis, tenant_id: str, kind: str, job_id: str) -> Optional[dict]:
    raw = await redis.get(progress_key(str(tenant_id), kind, str(job_id)))
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)
