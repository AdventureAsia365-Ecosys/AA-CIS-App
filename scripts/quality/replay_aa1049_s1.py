"""AA-1049 W3 (root ADR 0020, eval item "then the S1 and T2 dry stores", S1 train) — free offline S1 replay with a
declared-diff report.

No LLM, no DB, no network. It runs over STORED S1 Master versions (silver_aa_internal.generated_content rows written by
the S1 writer v2, with the raw_tours source the job built its tour from; exported read-only by
``scripts/quality/export_aa1049_s1.py``) and drives the PRODUCTION A1 rewrite of the checkout it runs in
(``v1_pipeline._rewrite_tour``: the LangGraph, the writer v2, validate, the judge step, the brand pre-audit, grounding,
flag_fix or, with ``S1_CONTROL=contract``, the S1 train, revalidate, the final checks and the REPORT-mode final
contract) with the stored version as the cassette. The run is a dry store: nothing is written anywhere; the outcome is
what the job's store and publish steps would read from the result (``admin_pipeline._is_publishable``).

- ``s1_generate`` writer call (the W system prompt) answers the stored page as the writer's JSON (the stored
  itinerary split back into one object per ``Day N — Title`` block);
- ``s1_generate`` meta call (and its retry) answers the stored ``seo_title`` / ``seo_meta``;
- any other model call (``s1_flag_fix``: a flag_fix field rewrite, its ``_rerepair_meta`` loop or a grounding
  sentence repair; ``s1_judge``; ``s1_brand_audit``; ``s1_itinerary_nudge``) is a cassette MISS: it raises, the
  code's fail-open path runs, and the version is counted under ``misses`` (never as a pass);
- Jev (``grounding.judge_units``: the grounding claims and the brand audit's meal claims) answers ``skipped``: no
  verdict clears anything, nothing is degraded; the SEO data is empty and the writing memory is off.

Per version it records the outcome (publishable, brand_audit_status, failure codes, the hard codes left, the
final-contract verdict, the sha of the text the store would write, retry count and exit), the model calls by stage and
kind, the misses and (switch on) the S1 train's report.

Two runs make the diff: one on a main checkout (``--out base.json``), one on the branch (``--base base.json``):

    PYTHONPATH=<main checkout> python <branch>/scripts/quality/replay_aa1049_s1.py --masters m.json --out base.json
    S1_CONTROL=contract PYTHONPATH=<branch> python <branch>/scripts/quality/replay_aa1049_s1.py --masters m.json \\
        --base base.json --out on.json --report on.md --opt-in

Every difference is DECLARED (one id per change the S1 train makes on purpose, checked against its shape), UNDECLARED,
or MISS (the version met a cassette miss on either side: the difference may be the cassette's). Exit 1 when any
difference is undeclared or a version is missing on one side, 3 when the only non-equal rows are MISS rows, 0
otherwise. With ``S1_CONTROL`` unset the branch must equal main: 0 declared and 0 undeclared. The declared ids belong to
``S1_CONTROL=contract`` only, and a diff is labelled with one only when the run was started with ``--opt-in`` AND the
switch is on in the run (recorded under "flags"). A branch outcome MORE permissive than the base (base held, branch
publishable; a final-contract verdict that becomes ``publish``) is never declared: the "no gate more permissive"
invariant (a base that met a miss makes it MISS instead: its outcome is unknown).

Known limits: the cassette replays the KEPT text of each version (after main's own repairs), not the writer's raw
reply, so a version main repaired replays its repaired text; the stored versions carry no raw replies.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
import re
import sys
from collections import Counter
from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, patch

SCHEMA = "aa1049-s1-replay/1"
MASTERS_SCHEMA = "aa1049-s1-masters/1"
FLAG = "S1_CONTROL"
S1_SWITCHES = ("S1_WRITER_V2", "S1_STRUCTURED_FACTS", "S1_PER_DAY_TARGETS", "S1_WRITER_TEMPERATURE",
               "A1_BRAND_FIT_RUBRIC", "A1_V2_BRAND_FIT_GATE", "S1_JUDGE_LIVE", FLAG)
COMPARED = ("publishable", "brand_audit_status", "failure_codes", "hard_left", "contract_verdict", "stored_sha",
            "llm_calls", "retry_count", "exit_path")
DECLARED = {
    "D-s1-compose": ("opt-in S1_CONTROL=contract (a): the stored text differs: code-numbered day headings, the source "
                     "day's verbatim fact lines in place of the writer's, the SEO title fitted at a word boundary, the "
                     "code meta template for an out-of-band meta, a local cut"),
    "D-s1-calls": ("opt-in (c)(d)(e): no flag_fix / meta re-repair / second grounding repair / judge / LLM "
                   "brand-audit call; at most 2 writer drafts"),
    "D-s1-route": ("opt-in (e): a rewrite-class code redrafts once (retry_count, exit_path) where main's saturated "
                   "score did not"),
    "D-s1-codes": ("opt-in: the failure / brand codes left on the stored text differ (a code-composed field clears "
                   "one, or a code no deterministic step clears is left for review)"),
    "D-s1-status": ("opt-in (c): stricter only: publishable -> held, brand status -> manual_check (a code main's "
                    "whole-field LLM rewrite cleared is left for the reviewer); the final-contract verdict of the "
                    "different text"),
}
_NON_WRITER_STAGES = ("s1_flag_fix", "s1_judge", "s1_brand_audit", "s1_itinerary_nudge", "s1_source_facts")
_HEAD_RE = re.compile(r"^Day\s+(\d{1,3}(?:\s*-\s*\d{1,3})?)\s*[—–-]+\s*(.*)$")


def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:16]


def _json(v: Any, default):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return default
    return v if v is not None else default


# ── the cassette ────────────────────────────────────────────────────────────────────────────────


def writer_items(itineraries: Any) -> list[dict]:
    """The stored canonical itinerary as the writer's array: one {day, title, body} per ``Day N — Title`` block."""
    if isinstance(itineraries, list):
        return [x for x in itineraries if isinstance(x, dict)]
    out: list[dict] = []
    for line in str(itineraries or "").splitlines():
        m = _HEAD_RE.match(line.strip())
        if m:
            out.append({"day": m.group(1).replace(" ", ""), "title": m.group(2).strip(), "body": ""})
        elif out:
            out[-1]["body"] = (out[-1]["body"] + "\n" + line).strip()
    return out


class Cassette:
    """``LLMClient.generate`` answered from one stored version (``answer``)."""

    def __init__(self, stored: dict):
        stored = stored or {}
        self.writer = {"name": stored.get("name"), "subtitle": stored.get("subtitle"),
                       "summary": stored.get("summary"), "highlights": _json(stored.get("highlights"), []),
                       "itineraries": writer_items(stored.get("itineraries")), "trip_type": stored.get("trip_type")}
        self.meta = {"seo_title": stored.get("seo_title") or "", "seo_meta": stored.get("seo_meta") or ""}
        self.calls: list[str] = []
        self.misses: list[str] = []

    def answer(self, request) -> SimpleNamespace:
        """The reply ``LLMClient.generate`` returns in the replay (no model call, nothing billed)."""
        stage = getattr(request, "stage", None) or "?"
        system = getattr(request, "system_prompt", "") or ""
        if stage == "s1_generate" and system.startswith("You write the master page"):
            kind, content = "writer", self.writer
        elif stage == "s1_generate" and system.startswith("You write the SEO title"):
            kind, content = "meta", self.meta
        else:
            self.calls.append(f"{stage}:miss")
            self.misses.append(stage)
            raise RuntimeError(f"replay cassette miss (stage={stage})")
        self.calls.append(f"{stage}:{kind}")
        return SimpleNamespace(content=json.dumps(content, ensure_ascii=False), cost_usd=0.0, model_used="replay",
                               input_tokens=0, output_tokens=0, stop_reason="end_turn", satellite_account=None,
                               fallback_used=False, cache_read_tokens=0, cache_write_tokens=0, provider="replay")


def _skipped_judge_units(units, sources, *, tenant_id=None):
    from services.content_generation import grounding as g
    from shared.llm_client.decide import Verdict
    return {i: Verdict(g.QUESTION, "off", "skipped") for i in (sources or {})}


# ── one version ────────────────────────────────────────────────────────────────────────────────


def replay_one(master: dict) -> dict:
    """Drive the production A1 rewrite over one stored version; returns its outcome record."""
    from api.routers import admin_pipeline
    from api.routers.v1_pipeline import _rewrite_tour
    from services.content_generation import flag_fix_node, grounding
    from services.quality import final_contract as fc
    from shared.llm_client import call_log
    from shared.llm_client.client import LLMClient

    cas = Cassette(master.get("stored") or {})
    with ExitStack() as st:
        st.enter_context(patch.object(LLMClient, "__init__", lambda self, *a, **k: None))
        st.enter_context(patch.object(LLMClient, "generate", lambda self, req: cas.answer(req)))
        st.enter_context(patch.object(call_log, "record_call", AsyncMock(return_value=None)))
        st.enter_context(patch.object(call_log, "record_calls", AsyncMock(return_value=None)))
        st.enter_context(patch.object(flag_fix_node, "_write_lessons_safe", lambda *a, **k: None))
        st.enter_context(patch.object(grounding, "judge_units", _skipped_judge_units))
        result = asyncio.run(_rewrite_tour(copy.deepcopy(master.get("tour") or {}), idx=0, total=1,
                                           brand_rules=copy.deepcopy(master.get("brand_rules") or {}), seo={},
                                           tour_agency=copy.deepcopy(master.get("tour_agency") or {}),
                                           writing_memory=None))
    generated = result.get("generated") or {}
    contract = result.get("final_contract") or {}
    hard = sorted(admin_pipeline._surviving_hard_codes(result)) if result.get("status") == "success" else []
    return {
        "tour_id": master.get("tour_id"), "status": result.get("status"),
        "publishable": bool(result.get("status") == "success" and admin_pipeline._is_publishable(result)),
        "brand_audit_status": result.get("brand_audit_status"),
        "failure_codes": sorted(set(result.get("failure_codes") or [])),
        "brand_audit_codes": sorted(set(result.get("brand_audit_codes") or [])),
        "hard_left": hard, "contract_verdict": contract.get("verdict"),
        "stored_sha": fc.tour_text_sha(generated) if generated else None,
        "llm_calls": dict(Counter(cas.calls)), "misses": sorted(set(cas.misses)),
        "retry_count": result.get("retry_count"), "exit_path": result.get("exit_path"),
        "s1_train": _json(json.dumps(result.get("s1_train"), default=str), None),
        "error": (result.get("error") or "")[:200] or None,
    }


def run(masters: list[dict]) -> list[dict]:
    clean = {k: v for k, v in os.environ.items() if k not in S1_SWITCHES or k == FLAG}
    out = []
    with patch.dict(os.environ, {**clean, "S1_WRITER_V2": "on"}, clear=True):
        for m in masters:
            try:
                out.append(replay_one(m))
            except Exception as e:  # noqa: BLE001 — a crash is a row, never a silent drop
                out.append({"tour_id": m.get("tour_id"), "status": "crash",
                            "error": f"{type(e).__name__}: {str(e)[:200]}", "misses": []})
    return out


# ── the diff ───────────────────────────────────────────────────────────────────────────────────


def _writer_drafts(calls: dict) -> int:
    return sum(v for k, v in (calls or {}).items() if k == "s1_generate:writer")


def _non_writer(calls: dict) -> int:
    return sum(v for k, v in (calls or {}).items() if k.split(":")[0] in _NON_WRITER_STAGES)


def classify(base: dict, branch: dict, *, declare: bool) -> tuple[str, list[str], list[str]]:
    """(equal | declared | undeclared | miss, declared ids, the compared keys that differ)."""
    diffs = [k for k in COMPARED if base.get(k) != branch.get(k)]
    if base.get("status") == "crash" or branch.get("status") == "crash":
        diffs = diffs or ["status"]
        return "undeclared", [], diffs
    if not diffs:
        return "equal", [], []
    looser = ((branch.get("publishable") and not base.get("publishable"))
              or (branch.get("contract_verdict") == "publish" and base.get("contract_verdict") != "publish"))
    if base.get("misses") or branch.get("misses"):
        return "miss", [], diffs
    if not declare or looser:
        return "undeclared", [], diffs
    ids: list[str] = []
    for k in diffs:
        if k == "stored_sha":
            ids.append("D-s1-compose")
        elif k == "llm_calls":
            if (_non_writer(branch["llm_calls"]) > _non_writer(base["llm_calls"])
                    or _writer_drafts(branch["llm_calls"]) > 2):
                return "undeclared", [], diffs
            ids.append("D-s1-calls")
        elif k in ("retry_count", "exit_path"):
            ids.append("D-s1-route")
        elif k in ("failure_codes", "hard_left"):
            ids.append("D-s1-codes")
        else:   # publishable / brand_audit_status / contract_verdict: stricter only (looser returned above)
            if k == "brand_audit_status" and branch.get(k) not in ("manual_check", "fixed", "pass", "flagged"):
                return "undeclared", [], diffs
            ids.append("D-s1-status")
    return "declared", sorted(set(ids)), diffs


def diff(base_rows: list[dict], rows: list[dict], *, declare: bool) -> dict:
    b = {r["tour_id"]: r for r in base_rows}
    n = {r["tour_id"]: r for r in rows}
    missing = sorted(set(b) ^ set(n))
    out = {"rows": [], "declared": Counter(), "undeclared": 0, "miss": 0, "equal": 0, "missing": missing}
    for tid in sorted(set(b) & set(n)):
        kind, ids, keys = classify(b[tid], n[tid], declare=declare)
        out["rows"].append({"tour_id": tid, "kind": kind, "ids": ids, "keys": keys})
        if kind == "declared":
            out["declared"].update(ids)
        elif kind == "equal":
            out["equal"] += 1
        else:
            out[kind] += 1
    out["declared"] = dict(out["declared"])
    return out


def summary(rows: list[dict]) -> dict:
    return {"versions": len(rows), "publishable": sum(1 for r in rows if r.get("publishable")),
            "brand_audit_status": dict(Counter(r.get("brand_audit_status") for r in rows)),
            "contract_verdict": dict(Counter(r.get("contract_verdict") for r in rows)),
            "misses": dict(Counter(m for r in rows for m in r.get("misses") or [])),
            "llm_calls": dict(sum((Counter(r.get("llm_calls") or {}) for r in rows), Counter())),
            "crash": sum(1 for r in rows if r.get("status") == "crash")}


def report_md(doc: dict) -> str:
    s, d = doc["summary"], doc.get("diff") or {}
    lines = [f"# AA-1049 S1 replay ({doc['schema']})", "",
             f"flags: `{json.dumps(doc['flags'])}`, opt-in: {doc['opt_in']}", "",
             f"versions {s['versions']}, publishable {s['publishable']}, crash {s['crash']}",
             f"brand_audit_status {s['brand_audit_status']}", f"contract_verdict {s['contract_verdict']}",
             f"model calls {s['llm_calls']}", f"cassette misses {s['misses']}", ""]
    if d:
        lines += [f"declared {d['declared']} · undeclared {d['undeclared']} · MISS {d['miss']} · "
                  f"equal {d['equal']} · missing {len(d['missing'])}", "",
                  "| tour | kind | ids | keys |", "|---|---|---|---|"]
        lines += [f"| {r['tour_id']} | {r['kind']} | {', '.join(r['ids'])} | {', '.join(r['keys'])} |"
                  for r in d["rows"] if r["kind"] != "equal"]
    lines += ["", "Declared ids:"] + [f"- {k}: {v}" for k, v in DECLARED.items()]
    return "\n".join(lines) + "\n"


def exit_code(d: Optional[dict]) -> int:
    if not d:
        return 0
    if d["undeclared"] or d["missing"]:
        return 1
    return 3 if d["miss"] else 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--masters", required=True, help=f"export JSON ({MASTERS_SCHEMA})")
    ap.add_argument("--out", required=True)
    ap.add_argument("--base", help="a previous --out to diff against (the main run)")
    ap.add_argument("--report", help="Markdown report path")
    ap.add_argument("--opt-in", action="store_true", help="label diffs with the declared ids (switch on only)")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args(argv)
    doc = json.load(open(a.masters, encoding="utf-8"))
    if doc.get("schema") != MASTERS_SCHEMA:
        print(f"masters schema must be {MASTERS_SCHEMA}", file=sys.stderr)
        return 2
    masters = doc.get("masters") or []
    if a.limit:
        masters = masters[:a.limit]
    flags = {k: os.environ[k] for k in S1_SWITCHES if k in os.environ and k != "S1_WRITER_V2"}
    rows = run(masters)
    out = {"schema": SCHEMA, "flags": flags, "opt_in": a.opt_in, "rows": rows, "summary": summary(rows)}
    if a.base:
        base = json.load(open(a.base, encoding="utf-8"))
        declare = a.opt_in and (os.environ.get(FLAG) or "").strip().lower() == "contract"
        out["diff"] = diff(base["rows"], rows, declare=declare)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1, sort_keys=True, ensure_ascii=False, default=str)
        fh.write("\n")
    if a.report:
        with open(a.report, "w", encoding="utf-8") as fh:
            fh.write(report_md(out))
    d = out.get("diff")
    print("S1_REPLAY " + json.dumps({"versions": len(rows), **({k: d[k] for k in ("declared", "undeclared", "miss",
                                                                                     "equal")} if d else {}),
                                     "missing": len(d["missing"]) if d else 0, "flags": flags}))
    return exit_code(d)


if __name__ == "__main__":
    sys.exit(main())
