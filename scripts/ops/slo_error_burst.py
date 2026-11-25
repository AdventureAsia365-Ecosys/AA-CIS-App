#!/usr/bin/env python3
"""AA-1142 — the Dev drill that proves the error budget burn alert of the Ecosystem status page (/admin/overview).

It calls ``GET /admin/overview/status/slo/drill`` (Dev only, ops.read; the route answers 503 on purpose and reads or
writes nothing) ``--count`` times. Each answer is one server error in the CIS API's CloudWatch metrics (API Gateway
5XXError), so within the next 5-minute period the fast burn rate of CIS rises above
``cis.ops.status_burn_fast_factor`` (14.4 by default: more than 7.2 % server errors over the last hour at a 99.5 %
objective). ``--check`` then reads ``GET /admin/overview/status?range=1h`` and prints the CIS SLO row and whether the
Needs-attention item ``slo-fast-cis`` is listed (exit 0 when it is, 1 otherwise).

Sign-in: a staff bearer token (a Cognito ID token of a staff member with ops.read, e.g. the E2E admin's session) in a
file, never on the command line. Nothing is paid: no LLM, no AWS call from this script. Expect a CIS 5xx CloudWatch
alarm e-mail during the drill (that is the alarm path working); the burn falls back under the factor about an hour
after the burst, and the burst spends part of the 30-day Dev error budget (shown on the page).

    python3 scripts/ops/slo_error_burst.py --base https://api-cis.example --token-file /path/token --count 300
    python3 scripts/ops/slo_error_burst.py --base https://api-cis.example --token-file /path/token --check
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

DRILL = "/admin/overview/status/slo/drill"
STATUS = "/admin/overview/status?range=1h"


def _get(base: str, path: str, token: str, timeout: float = 20.0) -> tuple[int, bytes]:
    req = urllib.request.Request(base.rstrip("/") + path, headers={"Authorization": f"Bearer {token}",
                                                                   "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 — https base given by the operator
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def burst(base: str, token: str, count: int, pace_s: float) -> dict:
    seen: dict[int, int] = {}
    for _ in range(count):
        code, _body = _get(base, DRILL, token)
        seen[code] = seen.get(code, 0) + 1
        if code in (401, 403, 404):
            break   # not signed in, no ops.read, or not Dev: stop at once
        time.sleep(pace_s)
    return seen


def check(base: str, token: str) -> int:
    code, body = _get(base, STATUS, token, timeout=60.0)
    if code != 200:
        print(f"status page answered {code}")
        return 1
    data = json.loads(body)
    sec = data["sections"].get("slo") or {}
    rows = {r["app"]: r for r in (sec.get("data") or {}).get("rows") or []}
    print(json.dumps({"slo_status": sec.get("status"), "cis": rows.get("cis")}, indent=1))
    ids = [i["id"] for i in data["verdict"]["attention"]]
    fired = "slo-fast-cis" in ids
    print(f"slo-fast-cis listed: {fired}; attention ids: {ids}")
    return 0 if fired else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", required=True, help="the CIS API origin (https)")
    ap.add_argument("--token-file", required=True, help="a file holding a staff bearer token (ops.read)")
    ap.add_argument("--count", type=int, default=300, help="drill calls (default 300)")
    ap.add_argument("--pace", type=float, default=0.1, help="seconds between calls (default 0.1)")
    ap.add_argument("--check", action="store_true", help="only read the status page and look for slo-fast-cis")
    a = ap.parse_args()
    if not a.base.startswith("https://"):
        print("--base must be an https origin")
        return 2
    token = open(a.token_file, encoding="utf-8").read().strip()
    if a.check:
        return check(a.base, token)
    seen = burst(a.base, token, a.count, a.pace)
    print(f"drill answers by status: {seen}")
    if seen.get(503, 0) == 0:
        print("no 503 came back: the drill did not run (sign-in, role or environment)")
        return 1
    print("wait about 6-10 minutes (CloudWatch period + the page's 5-minute cache), then run with --check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
