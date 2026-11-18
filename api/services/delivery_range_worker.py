"""AA-1135 — the child process that computes one custom date range of Platform › Delivery.

api/services/delivery_range.py starts ``python -m api.services.delivery_range_worker`` for each range it has not
cached, writes one JSON payload on stdin and reads one JSON answer on stdout:

  in:  {"bucket", "prefix" (<docs reader prefix>/delivery), "summary": {"generated_at", "config", "definitions",
        "apps": [{"repo", "complete_since", "git": {"complete_since"}}]}, "start", "end"}   (ISO times, [start, end))
  out: delivery_engine.custom_range(...)  {"generated_at", "engine_sha256", "start", "end", "apps": [{"id", "range"}]}

It reads the collector's daily event files of the stored history (docs-reader/delivery/events/yyyy/mm/dd.ndjson, one
GET each, read only, the same grant as the summary) and recomputes with the byte copy of the collector's engine
(api/services/delivery_engine.py), so the numbers equal a preset of the same window. A separate process because the
store is about 50 MB of events (some 250 MB in memory while it computes): the memory goes back to the system when the
child exits, never stays in the API process. Exit 0 with the answer, 1 with a one-line error on stderr, OUT_OF_STEP (3)
when the day files do not rebuild the store the summary was computed from (engine.StoreMismatch: the summary's
definitions.store_fingerprint differs; a failed upload or a lost collector cache), so the parent refuses the range
instead of serving numbers that disagree with a preset.
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from api.services import delivery_engine as engine

READERS = 8  # parallel GETs of the day files (about 100 files)
OUT_OF_STEP = 3  # exit code: the day files are out of step with the summary (engine.StoreMismatch)


def run(payload: dict, read: Callable[[str], Optional[bytes]]) -> dict:
    """The custom range of `payload`; `read(key)` returns a day file's bytes, None when that day has no file."""
    summary = payload["summary"]
    now = engine.ts(summary["generated_at"])
    a, b = engine.ts(payload["start"]), engine.ts(payload["end"])
    if now is None or a is None or b is None:
        raise ValueError("payload without generated_at, start or end")
    keys = [f"{payload['prefix']}/{k}" for k in engine.day_keys(summary["config"], now)]
    with ThreadPoolExecutor(max_workers=READERS) as pool:
        texts = [None if raw is None else raw.decode("utf-8") for raw in pool.map(read, keys)]
    return engine.custom_range(summary, engine.load_events(texts), a, b)


def _s3_reader(bucket: str) -> Callable[[str], Optional[bytes]]:
    from api.services import docs_reader

    client = docs_reader._s3_client()

    def read(key: str) -> Optional[bytes]:
        return docs_reader._read_bytes(client, bucket, key, docs_reader.MAX_MANIFEST_BYTES)

    return read


def main(stdin: Any = None, stdout: Any = None) -> int:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    try:
        payload = json.load(stdin)
        out = run(payload, _s3_reader(payload["bucket"]))
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
