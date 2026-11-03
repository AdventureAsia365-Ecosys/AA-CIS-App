"""AA-1025 H2 live drill ($0, no provider call): a low TPM row makes request N+1 wait instead of failing.

Runs inside the api container (S3-mediated ECS exec, ``python3 /tmp/s.py``) with the container's DATABASE_URL
(cis_app). It:

1. writes one ``shared.llm_capacity_limit`` row on a drill model key that no route uses
   (``<connection>/zz-h2-drill``, tpm_limit 1000, bulk_pct 50), so real traffic on the connection is not limited;
2. runs four stub "calls" through the real limiter path (``breaker.limit``, the same reservation ``breaker.guarded``
   makes before every provider call) with 400 tokens each: two prod calls fit (800 / 1000), the third prod call
   WAITS until the first leaves the 60 s window, and a bulk call meanwhile waits behind it (bulk holds at most 50 %);
3. prints one JSON line per call (line, waited_s, error) and the windows, then deletes the row (always).

Expected: calls 1-2 waited_s 0 and no pressure after them (pressure_after_fits []); call 3 waited_s ~60 and no error; the bulk call waited, no error. Nothing is sent to
any provider and nothing is written to shared.llm_call_log.

  python3 scripts/aa1025_h2_tpm_drill.py [--connection acc2-usw2]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
import time

sys.path.insert(0, "/app")

from shared.llm_client import breaker, capacity, connections  # noqa: E402

DRILL_MODEL = "zz-h2-drill"
TOKENS = 400
TPM = 1000

_PUT = ("INSERT INTO shared.llm_capacity_limit (connection_key, model_key, tpm_limit, bulk_pct, notes, updated_by) "
        "VALUES ($1, $2, $3, 50, 'AA-1025 H2 live drill (deleted at the end)', 'drill:aa1025-h2') "
        "ON CONFLICT (connection_key, model_key) DO UPDATE SET tpm_limit = EXCLUDED.tpm_limit")
_DELETE = "DELETE FROM shared.llm_capacity_limit WHERE connection_key = $1 AND model_key = $2"


async def _sql(sql: str, *args) -> None:
    conn = await connections._connect()
    try:
        await conn.execute(sql, *args)
    finally:
        await conn.close()


def _call(conn, line: str, out: list) -> None:
    started = time.monotonic()
    err = None
    try:
        with capacity.bind_request(TOKENS, line), breaker.limit(conn, DRILL_MODEL):
            pass                                     # the stub call: no provider is called
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
    out.append({"line": line, "waited_s": round(time.monotonic() - started, 1), "error": err})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--connection", default=None, help="an LLM connection key (default: the first enabled one)")
    args = ap.parse_args()
    key = args.connection or next((k for k, c in sorted(connections.registry_rows_sync().items()) if c.enabled),
                                  None)
    conn = connections.get_connection_sync(key) if key else None
    if conn is None:
        print(json.dumps({"error": f"no LLM connection {key!r}"}))
        return 2
    asyncio.run(_sql(_PUT, key, DRILL_MODEL, TPM))
    capacity.invalidate()
    out: list = []
    pressure_after_fits: list = []
    try:
        _call(conn, capacity.PROD, out)
        _call(conn, capacity.PROD, out)
        pressure_after_fits = sorted(capacity.pressure())   # calls that fit at once hold no claims: expect []
        third = threading.Thread(target=_call, args=(conn, capacity.PROD, out))
        third.start()
        time.sleep(1.0)                               # the third prod call is waiting now
        bulk = threading.Thread(target=_call, args=(conn, capacity.BULK, out))
        bulk.start()
        third.join(capacity.WAIT_S[capacity.PROD] + 10)
        bulk.join(capacity.WAIT_S[capacity.BULK] + 10)
        print(json.dumps({"connection": key, "model": DRILL_MODEL, "tpm_limit": TPM, "tokens_per_call": TOKENS,
                          "calls": out, "pressure_after_fits": pressure_after_fits,
                          "windows": capacity.windows_snapshot()}, default=str))
    finally:
        asyncio.run(_sql(_DELETE, key, DRILL_MODEL))
        capacity.invalidate()
    prod_waits = sorted(c["waited_s"] for c in out if c["line"] == capacity.PROD)
    ok = (len(out) == 4 and all(c["error"] is None for c in out) and prod_waits[-1] >= 0.8 * capacity.WINDOW_S
          and prod_waits[0] < 1 and pressure_after_fits == [])
    print(json.dumps({"drill_ok": ok}))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
