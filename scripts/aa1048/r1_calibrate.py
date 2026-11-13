#!/usr/bin/env python3
"""AA-1048 (Quality W2) — R1 calibration of the claim instrument candidates (services/ground_truth/r1.py).

DRY RUN by default: no client, no call. It prints the plan (calls per instrument, expected and worst-case cost) and
refuses a plan whose expected cost x 2 passes the cap. A live run is PAID and needs all three of --live,
--confirm-spend and a --cap <= 2 USD (D12 spirit: each paid step after its own yes); it runs judge-only, 3 votes,
pinned model + connection, stage adhoc_aa1048, one shared.llm_call_log row per reply. The cap is hard (r1.py "Cost"):
each call is gated on the spend so far plus its worst case, each reply is charged with its cut attempt and earlier
attempts, and the spend is also re-read from shared.llm_call_log (stage adhoc_aa1048 since the run started) before
every call. A caller error or the cap stops the run cleanly; the partial report is still printed and written.

  python -m scripts.aa1048.r1_calibrate --export <scratch>/claims.jsonl --finals <scratch>/finals.jsonl  # DRY RUN
  python -m scripts.aa1048.r1_calibrate --export <scratch>/claims.jsonl --finals <scratch>/finals.jsonl --live \\
      --confirm-spend --cap 2 --out <scratch>/r1-report.json [--candidates <model>@<connection>,...] \\
      [--modes entailment,extraction] [--split held_out|train|all]

Only the held-out split (the default) may list passing classes or a recommendation; train / all are exploration.
The export and finals files come from scripts/aa1048/cli.py (export-fixture, finals); both hold real text: keep them
and the report in the workspace scratch folder (.tmp-session/): --out is refused inside a git work tree unless git
ignores it there (scripts/aa1048/outpath.py). Run the live mode where the gateway reaches the models and
DATABASE_URL is set (the worker container), with the repo on PYTHONPATH.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from typing import Optional

from scripts.aa1048.outpath import OutRefused, check_out
from services.ground_truth import panel as pn
from services.ground_truth import r1


def _candidates(text: Optional[str]) -> list[tuple[str, str]]:
    if not text:
        return list(r1.DEFAULT_CANDIDATES)
    out = []
    for part in text.split(","):
        model, _, conn = part.strip().partition("@")
        if not model or not conn:
            raise r1.R1Refused(f"a candidate is <model>@<connection>, got {part!r}")
        out.append((model, conn))
    return out


# The ledger probe lives in services/ground_truth/r1.py (the image has no scripts/); this name stays for the tests.
LEDGER_SQL = r1.LEDGER_SQL


def ledger_probe(since: datetime) -> r1.SpendProbe:
    """The ledger spend of stage r1.STAGE since `since` (r1.ledger_probe)."""
    return r1.ledger_probe(since)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.aa1048.r1_calibrate", description=__doc__.split("\n")[0])
    ap.add_argument("--export", required=True)
    ap.add_argument("--finals", required=True)
    ap.add_argument("--candidates")
    ap.add_argument("--modes", default=",".join(r1.MODES))
    ap.add_argument("--votes", type=int, default=r1.VOTES)
    ap.add_argument("--cap", type=float, default=r1.HARD_CAP_USD)
    ap.add_argument("--live", action="store_true", help="PAID: make the calls")
    ap.add_argument("--confirm-spend", action="store_true", help="required with --live")
    ap.add_argument("--split", choices=r1.SPLIT_CHOICES, default="held_out",
                    help="held_out (default: the only split that may pass or recommend), train or all (exploration)")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    if a.live and not a.confirm_spend:
        ap.error("--live is paid: add --confirm-spend")
    try:
        out_path = check_out(a.out) if a.out else None
    except OutRefused as e:
        print(json.dumps({"ok": False, "refused": str(e)}))
        return 2
    with open(a.export, encoding="utf-8") as f:
        export = pn.read_jsonl(f.read())
    with open(a.finals, encoding="utf-8") as f:
        finals = {r["claim_hash"]: r for r in pn.read_jsonl(f.read())}
    try:
        insts = r1.instruments(_candidates(a.candidates), [m.strip() for m in a.modes.split(",") if m.strip()])
        items = r1.items_from(export, finals, split=a.split)
        the_plan = r1.plan(items, insts, votes=a.votes, cap=a.cap)
    except r1.R1Refused as e:
        print(json.dumps({"ok": False, "refused": str(e)}))
        return 2
    if not a.live:
        from services.golden_set import llm_guard

        llm_guard.install("aa1048_r1_dry_run")
        print(json.dumps({"ok": the_plan["fits_cap"], "mode": "dry_run", "plan": the_plan}, indent=1))
        return 0 if the_plan["fits_cap"] else 3
    if not the_plan["fits_cap"]:
        print(json.dumps({"ok": False, "refused": "the plan does not fit the cap", "plan": the_plan}, indent=1))
        return 3
    run_id = str(uuid.uuid4())
    started = datetime.now(timezone.utc)
    result = r1.run(items, insts, cap=a.cap, votes=a.votes, run_id=run_id, caller=_caller,
                    spend_probe=ledger_probe(started))
    rep = {"ok": not result["stopped"], "mode": "live", "run_id": run_id, "plan": the_plan,
           **r1.report(items, result, insts)}
    if out_path:                                          # written partial too: the calls so far were paid
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({**rep, "votes": result["votes"]}, indent=1, default=str))
    print(json.dumps(rep, indent=1, default=str))
    return 0 if not result["stopped"] else 3


_caller: Optional[r1.Caller] = None                     # tests put a fake here; None = the Model Gateway


if __name__ == "__main__":
    sys.exit(main())
