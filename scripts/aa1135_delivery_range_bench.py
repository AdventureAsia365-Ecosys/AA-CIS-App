#!/usr/bin/env python3
"""AA-1135 (S284) — time the custom-range child of Platform › Delivery against a local copy of the delivery store.

Runs the child exactly as the API does (delivery_range._worker_argv: the boot without the app import, the payload on
stdin, the answer on stdout); only the S3 reader is swapped for one that reads the local copy. Three timings:

  boot     the child's start-up alone (imports), from the same boot
  cold     a summary not prepared yet: day files, fingerprint check, every app's context, the range (+ the snapshot)
  warm     the same summary again: the range from the snapshot, no day file
  legacy   (--legacy) the start-up of the old entry `python -m api.services.delivery_range_worker`, which runs
           api/__init__.py (the whole FastAPI app); needs the env the app refuses to start without (JWT_SECRET, ...)

Make the copy once (read only; the Dev store is GitHub data of the public repos):
  aws s3 sync s3://<bronze bucket>/docs-reader/delivery/ <dir>/ --profile aa365-admin --region us-west-1
Then, from the repo root:
  python3 scripts/aa1135_delivery_range_bench.py --store <dir> --from 2026-11-09 --to 2026-11-15
Closer to the Dev task (0.5 vCPU, slower cores; Dev matched about 25 % of a fast laptop core):
  systemd-run --user --scope -q -p CPUQuota=25% python3 scripts/aa1135_delivery_range_bench.py ...

No network, no AWS call, no database: the child never reaches S3 (the reader is local) and the parent is this script.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_pkg = types.ModuleType("api")  # the same stand-in as the child's boot: this script never builds the app either
_pkg.__path__ = [str(ROOT / "api")]
sys.modules["api"] = _pkg

from api.services import delivery_engine as engine  # noqa: E402
from api.services import delivery_range as dr  # noqa: E402

_LOCAL = ("import os\n"
          "def _local(bucket):\n"
          "    base = os.environ['AA_BENCH_STORE']\n"
          "    def read(key):\n"
          "        p = os.path.join(base, key.split('delivery/', 1)[1])\n"
          "        return open(p, 'rb').read() if os.path.exists(p) else None\n"
          "    return read\n"
          "worker._s3_reader = _local\n")


def _child(argv: list[str], payload: dict, store: Path, answer: bool = True) -> tuple[float, dict]:
    env = {**os.environ, "AA_BENCH_STORE": str(store)}
    t = time.perf_counter()
    out = subprocess.run(argv, cwd=str(ROOT), input=json.dumps(payload).encode(), capture_output=True, env=env,
                         timeout=dr.WORKER_TIMEOUT_S)
    took = time.perf_counter() - t
    if out.returncode != 0:
        raise SystemExit(f"child exit {out.returncode}: {out.stderr.decode(errors='replace')[-400:]}")
    return took, json.loads(out.stdout or b"{}") if answer else {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--store", required=True, type=Path, help="local copy of <docs reader prefix>/delivery/")
    ap.add_argument("--from", dest="start", required=True, type=date.fromisoformat)
    ap.add_argument("--to", dest="end", required=True, type=date.fromisoformat)
    ap.add_argument("--legacy", action="store_true", help="also time the old `python -m` start-up")
    args = ap.parse_args()
    summary = json.loads((args.store / "summary.json").read_text(encoding="utf-8"))
    gen = engine.ts(summary["generated_at"])
    a = datetime.combine(args.start, datetime.min.time(), timezone.utc)
    b = min(datetime.combine(args.end + timedelta(days=1), datetime.min.time(), timezone.utc), gen)
    boot = [sys.executable, "-c", dr._STUB + _LOCAL + "sys.exit(worker.main())\n", str(ROOT / "api")]
    with tempfile.TemporaryDirectory(prefix="aa-delivery-bench-") as tmp:
        payload = dr._payload(summary, a, b, "local", "docs-reader/delivery", Path(tmp) / "snapshot.pickle")
        rows = {"boot": _child([sys.executable, "-c", dr._STUB, str(ROOT / "api")], {}, args.store)[0]}
        rows["cold"], cold = _child(boot, payload, args.store)
        rows["warm"], warm = _child(boot, payload, args.store)
        if cold != warm:
            raise SystemExit("the warm answer differs from the cold one")
    if args.legacy:
        # the app's import logs to stdout (the old child's answer could start with a log line): not parsed
        rows["legacy"] = _child([sys.executable, "-c", "import api.services.delivery_range_worker"], {},
                                args.store, answer=False)[0]
    days = len(engine.day_keys(summary["config"], gen))
    print(json.dumps({"store_days": days, "apps": len(cold.get("apps") or []),
                      "seconds": {k: round(v, 2) for k, v in rows.items()}}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
