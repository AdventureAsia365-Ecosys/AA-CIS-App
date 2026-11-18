"""AA-1135 — the child process that computes one custom date range of Platform › Delivery.

api/services/delivery_range.py starts this module in a child (`_worker_argv`: a small boot that never imports the
`api` package's __init__, which builds the whole FastAPI app) for each range it has not cached, writes one JSON
payload on stdin and reads one JSON answer on stdout:

  in:  {"bucket", "prefix" (<docs reader prefix>/delivery), "summary": {"generated_at", "config", "definitions",
        "apps": [{"repo", "complete_since", "git": {"complete_since"}}]}, "start", "end" (ISO times, [start, end);
        both null: prepare only), "snapshot" (optional: the prepared store file of this summary)}
  out: {"generated_at", "engine_sha256", "start", "end", "apps": [{"id", "range"}]}   (delivery_engine.custom_range)
       or {"generated_at", "engine_sha256", "prepared": true} for a prepare-only payload

Two phases (S284 cold-path fix): `prepare` reads the collector's daily event files of the stored history
(docs-reader/delivery/events/yyyy/mm/dd.ndjson, one GET each, read only, the same grant as the summary), checks they
rebuild the summary's store (engine.store_fingerprint) and builds every app's window-independent context
(engine.app_context); `ranges` cuts the window out of those contexts (engine.range_block). That is the body of
engine.custom_range split in two, with the byte copy of the collector's engine (api/services/delivery_engine.py) doing
every computation, so the numbers equal a preset of the same window (the parity tests run both paths). With a
`snapshot` path the child saves the prepared contexts there (pickle, in the parent's private temporary folder), and a
later child of the same summary loads them and only runs `ranges`: no day file read, no re-check.

A separate process because the store is about 50 MB of events (some 250 MB in memory while it computes): the memory
goes back to the system when the child exits, never stays in the API process. Exit 0 with the answer, 1 with a one-line
error on stderr, OUT_OF_STEP (3) when the day files do not rebuild the store the summary was computed from
(engine.StoreMismatch: the summary's definitions.store_fingerprint differs; a failed upload or a lost collector cache),
so the parent refuses the range instead of serving numbers that disagree with a preset.
"""
from __future__ import annotations

import gc
import json
import os
import pickle
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from api.services import delivery_engine as engine

READERS = 8  # parallel GETs of the day files (about 100 files)
OUT_OF_STEP = 3  # exit code: the day files are out of step with the summary (engine.StoreMismatch)
SNAPSHOT_FORMAT = 1


class IndexedStore(engine.Store):
    """The engine's Store with `of(kind, repo)` answered from an index built once: the same events in the same
    (time, id) order as Store.of, without a scan of the whole store per call (app_context asks about 36 times).
    Every call returns a new list, as Store.of does. A change of the events (put, prune) drops the index."""

    _MISSING = object()

    def __init__(self, events: Optional[dict] = None, state: Optional[dict] = None):
        super().__init__(events, state)
        self._index: Optional[dict] = None

    def _build(self) -> dict:
        index: dict[tuple, list[dict]] = {}
        for e in sorted(self.events.values(), key=lambda e: (e.get("time") or "", e["id"])):
            repo = e["attributes"].get("vcs.repository.name", self._MISSING)
            index.setdefault((e["event"], None), []).append(e)
            index.setdefault((e["event"], repo), []).append(e)
        return index

    def of(self, kind: str, repo: Optional[str] = None) -> list[dict]:
        if self._index is None:
            self._index = self._build()
        if repo is not None and (kind, self._MISSING) in self._index:
            return super().of(kind, repo)  # an event without a repo name fails there exactly as in Store.of
        return list(self._index.get((kind, repo), ()))

    def put(self, e: dict) -> bool:
        self._index = None
        return super().put(e)

    def prune(self, keep_from: datetime) -> int:
        self._index = None
        return super().prune(keep_from)


def read_days(payload: dict, read: Callable[[str], Optional[bytes]]) -> list[Optional[str]]:
    """The text of every day file of the summary's store (None for a day without a file): one read per day."""
    summary = payload["summary"]
    now = engine.ts(summary["generated_at"])
    if now is None:
        raise ValueError("payload without generated_at")
    keys = [f"{payload['prefix']}/{k}" for k in engine.day_keys(summary["config"], now)]
    with ThreadPoolExecutor(max_workers=READERS) as pool:
        return [None if raw is None else raw.decode("utf-8") for raw in pool.map(read, keys)]


def prepare(summary: dict, texts: list[Optional[str]]) -> dict:
    """The window-independent part of engine.custom_range: the store rebuilt from the day files, checked against the
    summary's fingerprint (StoreMismatch), and every app's context."""
    cfg, now = summary["config"], engine.ts(summary["generated_at"])
    if now is None:
        raise ValueError("summary without generated_at")
    store = IndexedStore(engine.load_events(texts), engine.state_from_summary(summary))
    cut = engine.store_cut(cfg, now)
    store.prune(cut)
    want = (summary.get("definitions") or {}).get("store_fingerprint")
    got = engine.store_fingerprint(store.events.values(), cut)
    if want != got:
        off = sorted(r for r in set(want or {}) | set(got) if (want or {}).get(r) != got.get(r))
        raise engine.StoreMismatch(
            f"the day files do not rebuild the summary's event store (repos: {', '.join(off) or '-'})")
    contexts = [(app, engine.app_context(cfg, app, store, now)) for app in cfg["apps"]]
    return {"format": SNAPSHOT_FORMAT, "generated_at": summary["generated_at"],
            "engine_sha256": engine.engine_sha256(), "cfg": cfg, "now": now, "contexts": contexts}


def ranges(prepared: dict, a: datetime, b: datetime) -> dict:
    """engine.custom_range's answer over [a, b) from prepared contexts."""
    cfg = prepared["cfg"]
    apps = [{"id": app["id"], "range": engine.range_block(cfg, app, ctx, a, b)} for app, ctx in prepared["contexts"]]
    return {"generated_at": prepared["generated_at"], "engine_sha256": engine.engine_sha256(), "start": engine.iso(a),
            "end": engine.iso(b), "apps": apps}


def run(payload: dict, read: Callable[[str], Optional[bytes]]) -> dict:
    """The custom range of `payload` from the day files; `read(key)` returns a day file's bytes, None when that day
    has no file."""
    a, b = engine.ts(payload["start"]), engine.ts(payload["end"])
    if a is None or b is None:
        raise ValueError("payload without start or end")
    return ranges(prepare(payload["summary"], read_days(payload, read)), a, b)


def save(prepared: dict, path: Path) -> None:
    """Write the prepared contexts to `path` (a new file renamed into place, so a reader never sees half of one)."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "wb") as fh:
        pickle.dump(prepared, fh, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, path)


def load(path: Path, summary: dict) -> Optional[dict]:
    """The prepared contexts saved at `path` for this summary and this engine; None when there is no such file.
    The file is written only by this module, in the parent's private temporary folder (mode 0700)."""
    try:
        with open(path, "rb") as fh:
            prepared = pickle.load(fh)  # noqa: S301 - our own file, see the docstring
    except FileNotFoundError:
        return None
    ok = (isinstance(prepared, dict) and prepared.get("format") == SNAPSHOT_FORMAT
          and prepared.get("generated_at") == summary["generated_at"]
          and prepared.get("engine_sha256") == engine.engine_sha256())
    return prepared if ok else None


def answer(payload: dict, read_for: Callable[[str], Callable[[str], Optional[bytes]]]) -> dict:
    """What the child prints for `payload`: the range from the snapshot when there is one, else from the day files
    (read through `read_for(bucket)`), saving the snapshot when the payload names one."""
    summary = payload["summary"]
    snap = Path(payload["snapshot"]) if payload.get("snapshot") else None
    prepared = load(snap, summary) if snap is not None else None
    if prepared is None:
        prepared = prepare(summary, read_days(payload, read_for(payload["bucket"])))
        if snap is not None:
            save(prepared, snap)
    if payload.get("start") is None and payload.get("end") is None:
        return {"generated_at": summary["generated_at"], "engine_sha256": engine.engine_sha256(), "prepared": True}
    a, b = engine.ts(payload["start"]), engine.ts(payload["end"])
    if a is None or b is None:
        raise ValueError("payload without start or end")
    return ranges(prepared, a, b)


def _s3_reader(bucket: str) -> Callable[[str], Optional[bytes]]:
    from api.services import docs_reader

    client = docs_reader._s3_client()

    def read(key: str) -> Optional[bytes]:
        return docs_reader._read_bytes(client, bucket, key, docs_reader.MAX_MANIFEST_BYTES)

    return read


def main(stdin: Any = None, stdout: Any = None) -> int:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    # a short-lived child that builds a large graph of small objects and frees it at exit: the cyclic collector would
    # only rescan it (about 10 % of the time); reference counting still frees everything that is dropped
    gc.disable()
    try:
        payload = json.load(stdin)
        out = answer(payload, _s3_reader)
    except engine.StoreMismatch as err:  # the parent answers 409
        print(f"StoreMismatch: {str(err)[:300]}", file=sys.stderr)
        return OUT_OF_STEP
    except Exception as err:  # the parent logs the type and answers 503
        print(f"{type(err).__name__}: {str(err)[:300]}", file=sys.stderr)
        return 1
    json.dump(out, stdout, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
