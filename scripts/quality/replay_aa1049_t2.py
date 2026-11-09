"""AA-1049 W3 (root ADR 0020, eval item "then the S1 and T2 dry stores", T2 train) — free offline T2 replay with a
declared-diff report.

No LLM, no DB, no network. It runs over STORED T2 versions (gold_aa_internal.tenant_tour_versions rows exported
read-only with the ``t2_rewrite_job._PT_SQL`` row of their published tour, see EXPORT below), and drives the PRODUCTION
T2 path of the checkout it runs in (``t2_rewrite_job._rewrite_and_save``: the policy route, then main's LangGraph + T3
path or, with ``T2_ORCHESTRATOR=linear``, the linear orchestrator) with the stored version as the cassette:

- ``t2_generate`` answers the stored ``rewritten_content`` (the writer's text as it was kept) on every writer call;
- ``t2_judge`` answers the stored verdict (``judge_breakdown``: brand_fit, cross_brand_distinct, mission_present,
  feedback); a version without one is a judge MISS (the call fails, non-blocking: report-only under the T2 interim);
- any other model call (``t2_flag_fix``: a sentence repair or a meta regenerate, ``t2_itinerary_nudge``) is a cassette
  MISS: it raises, the code's fail-open path runs, and the version is counted under ``misses`` (never as a pass);
- Jev answers ``skipped`` (it can only clear a T3 hit; skipped keeps every hit: fail closed), the SEO and
  writing-memory lookups answer "none", every SQL statement goes to an in-memory connection.

Per version it records: the production input (``t2_store.production_input`` on a checkout with the seam, else the
same builders called directly) and its shas; the outcome the store step would write (status, qa_status,
qa_repair_count, quality_score, sha of the stored text, final-contract verdict); the review rows; the model calls by
stage; the misses.

Two runs make the diff: one on a main checkout (``--out base.json``), one on the branch (``--base base.json``):

    PYTHONPATH=<main checkout> python <branch>/scripts/quality/replay_aa1049_t2.py --versions v.json --out base.json
    PYTHONPATH=<branch>        python <branch>/scripts/quality/replay_aa1049_t2.py --versions v.json \\
        --base base.json --out branch.json --report report.md [--opt-in]

Every difference is a DECLARED diff (one id per change the T2 train makes on purpose, checked against its shape),
UNDECLARED, or MISS (the version met an outcome-moving cassette miss on either side: the difference may be the
cassette's; re-record it). Exit 1 when any difference is undeclared or a version is missing on one side, 3 when the
only non-equal rows are MISS rows, 0 otherwise. Defaults vs opt-in (S272):
with every AA-1049 switch at its default (``T2_ORCHESTRATOR`` unset = main's graph path) the branch must equal main:
0 declared and 0 undeclared diffs. The declared ids belong to ``T2_ORCHESTRATOR=linear`` only, and a diff is labelled
with one only when the run was started with ``--opt-in`` AND the switch is on in the run (recorded under "flags").
A branch status MORE permissive than the base (base needs_review, branch ai_generated), a stored score above the
base's (or where the base stored none), a QA pass the base did not give and a review row with fewer findings are never
declared: the "no gate more permissive" invariant.

EXPORT (one JSON object, read-only SQL by the orchestrator; never committed: tour text): ``{"schema":
"aa1049-t2-versions/1", "versions": [{"id", "tenant_id", "status", "quality_score", "qa_status", "qa_repair_count",
"rewritten_content", "judge_breakdown", "pt": {<_PT_SQL columns>}, "brand_rules": {<brand_rules_from_pack>}}]}``.
The unit test drives it with a synthetic fixture (invented tours only).

Known limits: the cassette replays the KEPT text of each version, not every draft its run saw (the stored versions
carry no raw replies of rejected drafts: the design's replay record is what the linear path stores from now on), so a
version whose kept text needed a repair call shows that call as a miss on both sides; the corpus is mostly
sonnet-4-6 + v1 writer output (design review, T2 pick).
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
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

SCHEMA = "aa1049-t2-replay/1"
VERSIONS_SCHEMA = "aa1049-t2-versions/1"
FLAG = "T2_ORCHESTRATOR"
COMPARED = ("status", "qa_status", "qa_repair_count", "quality_score", "stored_sha", "contract_verdict", "reviews",
            "llm_stages", "policy_hold")
DECLARED = {
    "D-t2-status": ("opt-in T2_ORCHESTRATOR=linear (L1): ai_generated -> needs_review when an enforce finding is left "
                    "on the selected draft (T3 hard code, T3 grounding, a Master day-slot break) or a held fallback "
                    "writer; never the other way"),
    "D-t2-calls": ("opt-in linear (L2/L3/L4): at most 2 writer calls, one report-only judge call, no brand-audit "
                   "flag_fix / itinerary nudge call"),
    "D-t2-select": ("opt-in linear (L5): the selected draft / stored text, its score (never above main's for the same "
                    "lineage) and the qa repair count differ"),
    "D-t2-review": ("opt-in linear: the review row lists the enforce findings left (a day-slot code may be added)"),
    "D-t2-contract": ("opt-in linear: the REPORT-mode final-contract verdict of the different stored text / status"),
}
T2_SWITCHES = ("T2_JUDGE_GATE", "T2_JUDGE_REPEAT", "T2_JUDGE_ANCHORS", "T2_BRAND_CLAIM_CHECK", "T2_FACTS",
               "T2_BEST_DRAFT", "T2_TARGETED_REPAIR", "T3_BACKGROUND_FACTS", "T2_INTERIM", FLAG)


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


class Cassette:
    """``LLMClient.generate`` answered from one stored version (``answer``)."""

    def __init__(self, version: dict):
        self.writer = _writer_reply(_json(version.get("rewritten_content"), {}))
        jb = _json(version.get("judge_breakdown"), {}) or {}
        self.judge = ({"brand_fit_score": jb.get("brand_fit"), "cross_brand_distinct": jb.get("cross_brand_distinct"),
                       "mission_present": jb.get("mission_present"), "feedback": jb.get("feedback") or ""}
                      if jb.get("brand_fit") is not None else None)
        self.calls: list[str] = []
        self.misses: list[str] = []

    def answer(self, request) -> SimpleNamespace:
        """The reply ``LLMClient.generate`` returns in the replay (no model call, nothing billed)."""
        stage = getattr(request, "stage", None) or "?"
        self.calls.append(stage)
        if stage == "t2_generate":
            content = self.writer
        elif stage == "t2_judge" and self.judge is not None:
            content = self.judge
        else:
            self.misses.append(stage)
            raise RuntimeError(f"replay: cassette miss (stage={stage})")
        return SimpleNamespace(content=json.dumps(content), cost_usd=0.0, model_used="replay-cassette",
                               input_tokens=0, output_tokens=0, stop_reason="end_turn", satellite_account=None,
                               fallback_used=False, cache_read_tokens=0, cache_write_tokens=0)


def _writer_reply(stored: dict) -> dict:
    """The stored version as the writer's JSON reply (the kept fields; ``status`` is the save's own key)."""
    return {k: v for k, v in (stored or {}).items() if k != "status"}


class _Conn:
    def __init__(self):
        self.executed: list[tuple] = []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "OK"

    async def fetchrow(self, sql, *args):
        self.executed.append((sql, args))
        return None

    async def fetchval(self, sql, *args):
        self.executed.append((sql, args))
        return None

    async def fetch(self, sql, *args):
        self.executed.append((sql, args))
        return []


def _pool(conn: _Conn):
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    p = MagicMock()
    p.acquire = MagicMock(return_value=ctx)
    return p


def _skipped_decide():
    from shared.llm_client.decide import Verdict

    async def decide(stage, subject, state, questions, **_kw):
        return SimpleNamespace(verdicts={q: Verdict(q, "off", "skipped") for q in questions},
                               accepted=lambda q: False)
    return decide


def production_input(pt: dict) -> dict:
    """The checkout's production input builder (the T2 store seam when the checkout has it)."""
    try:
        from services.content_generation import t2_store
    except ImportError:   # a main checkout before the seam: the same builders, called directly
        from services.jobs import t2_rewrite_job as job
        td = job.source_tour_dict(pt)
        return {"source_sha": _sha(td), "grounding_sha": _sha([str(v) for v in td.values() if v]), "fs_sha": None,
                "policy_hold": bool(job.policy_hold_hits(pt, td))}
    inp = t2_store.production_input(pt)
    return {"source_sha": inp["source_sha"], "grounding_sha": inp["grounding_sha"], "fs_sha": inp["fs_sha"],
            "policy_hold": bool(inp["policy_hold_hits"])}


def _fold(sql: str) -> str:
    return re.sub(r"\s+", " ", sql or "").strip()


def replay_version(version: dict, env: dict) -> dict:
    """Drive the checkout's production T2 path over one stored version; the outcome its store step would write."""
    from services.content_generation import flag_fix_node
    from services.jobs import t2_rewrite_job as job
    from services.quality import final_contract as fc
    from shared.llm_client import call_log
    from shared.llm_client.client import LLMClient

    pt = dict(version.get("pt") or {})
    tape = Cassette(version)
    conn = _Conn()
    tour_dict = job.source_tour_dict(pt)
    clean_env = {k: v for k, v in os.environ.items() if k not in T2_SWITCHES}
    out: dict = {"id": str(version.get("id")), "input": production_input(pt)}
    with ExitStack() as st:
        st.enter_context(patch.dict(os.environ, {**clean_env, **env}, clear=True))
        st.enter_context(patch.object(LLMClient, "__init__", lambda self, *a, **k: None))
        st.enter_context(patch.object(LLMClient, "generate", lambda self, req: tape.answer(req)))
        st.enter_context(patch.object(call_log, "record_call", AsyncMock(return_value=None)))
        st.enter_context(patch.object(call_log, "record_calls", AsyncMock(return_value=None)))
        st.enter_context(patch.object(flag_fix_node, "_write_lessons_safe", lambda *a, **k: None))
        st.enter_context(patch("services.acp_produce.tenant_pipeline.decide", _skipped_decide()))
        st.enter_context(patch("services.seo_intelligence.s1_prefetch.tenant_market_seo",
                               AsyncMock(return_value=("US", None))))
        st.enter_context(patch("services.writing_memory.insertion.resolve", AsyncMock(return_value=None)))
        try:
            asyncio.run(job._rewrite_and_save(_pool(conn), pt, tour_dict, copy.deepcopy(version.get("brand_rules")
                                                                                       or {}),
                                              str(version.get("tenant_id") or "replay-tenant"),
                                              str(version.get("id")), str(pt.get("id") or "")))
        except Exception as e:  # noqa: BLE001 — recorded: a run that raises is an outcome too (the job would retry)
            out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    saves = [args for sql, args in conn.executed if "SET rewritten_content" in sql]
    reviews = [args for sql, args in conn.executed if "INSERT INTO silver_aa_internal.review_queue" in _fold(sql)]
    if saves:
        a = saves[-1]
        policy = "'needs_review'" in [s for s, _ in conn.executed if "SET rewritten_content" in s][-1]
        stored = _json(a[0], {})
        if policy:   # _save_policy_hold: literal status, NULL score, its own breakdown position
            jb = _json(a[1], {}) or {}
            out.update(status="needs_review", qa_status="escalated", qa_repair_count=0, quality_score=None,
                       policy_hold=True)
        else:
            jb = _json(a[8], {}) or {}
            out.update(status=a[1], quality_score=float(a[2]) if a[2] is not None else None, qa_status=a[3],
                       qa_repair_count=a[4], policy_hold=False)
        out["stored_sha"] = fc.tour_text_sha(stored)
        out["contract_verdict"] = (jb.get("final_contract") or {}).get("verdict")
    out["reviews"] = sorted({str(d.get("check_id")) for r in reviews for d in (_json(r[4], []) or [])})
    out["llm_stages"] = dict(Counter(tape.calls))
    out["misses"] = dict(Counter(tape.misses))
    out["cassette_hits"] = len(tape.calls) - len(tape.misses)
    out["judge_miss"] = tape.judge is None
    return out


def run(versions: list[dict], env: Optional[dict] = None) -> dict:
    env = dict(env or {})
    rows = [replay_version(v, env) for v in versions]
    return {"schema": SCHEMA, "flags": {k: v for k, v in env.items() if k in T2_SWITCHES},
            "versions": {r["id"]: r for r in rows}}


# ── the diff ────────────────────────────────────────────────────────────────────────────────────


def more_permissive(field: str, b: dict, r: dict) -> bool:
    """The branch is looser than the base on this field: a status the base did not give (ai_generated), a stored score
    above the base's or where the base stored none, a QA pass the base did not give, a review row with fewer
    findings. Never declared, and never hidden in the MISS bucket."""
    bv, rv = b.get(field), r.get(field)
    if field == "status":
        return rv == "ai_generated" and bv != "ai_generated"
    if field == "quality_score":
        return rv is not None and (bv is None or rv > bv)
    if field == "qa_status":
        return rv == "passed" and bv != "passed"
    if field == "reviews":
        return not set(rv or []) >= set(bv or [])
    return False


def _declared(field: str, b: dict, r: dict) -> Optional[str]:
    """The declared id of one field difference, None when its shape is not a declared one."""
    if more_permissive(field, b, r):
        return None
    if field == "status":
        return "D-t2-status" if (b.get("status"), r.get("status")) == ("ai_generated", "needs_review") else None
    if field == "llm_stages":
        bs, rs = b.get("llm_stages") or {}, r.get("llm_stages") or {}
        ok = (rs.get("t2_generate", 0) <= 2 and rs.get("t2_judge", 0) <= 1 and rs.get("t2_itinerary_nudge", 0) == 0
              and rs.get("t2_brand_audit", 0) == 0 and rs.get("t2_flag_fix", 0) <= max(3, bs.get("t2_flag_fix", 0)))
        return "D-t2-calls" if ok else None
    if field in ("stored_sha", "qa_repair_count", "quality_score", "qa_status"):
        return "D-t2-select"
    if field == "reviews":
        return "D-t2-review"   # more enforce findings than main's (fewer is more_permissive)
    if field == "contract_verdict":
        return "D-t2-contract"
    return None


# Stages whose cassette miss cannot move a stored outcome: the T2 interim judge is report-only (its verdict never feeds
# the status, the score, qa or the stored text), so its miss is counted but does not make a diff a MISS. Only while the
# interim profile is on in the run (T2_INTERIM unset or on).
NEUTRAL_MISS_STAGES = ("t2_judge",)


def _interim_on(flags: dict) -> bool:
    return str((flags or {}).get("T2_INTERIM", "")).strip().lower() not in ("0", "false", "off", "no")


def _outcome_misses(v: dict, flags: dict) -> dict:
    neutral = NEUTRAL_MISS_STAGES if _interim_on(flags) else ()
    return {k: n for k, n in (v.get("misses") or {}).items() if k not in neutral and n}


def _sum(side: dict, key: str) -> dict:
    return dict(sum((Counter(v.get(key) or {}) for v in side["versions"].values()), Counter()))


def diff(base: dict, branch: dict, *, opt_in: bool) -> dict:
    """Every field difference is DECLARED, UNDECLARED or MISS. MISS: the version met a cassette miss that can move its
    outcome (any stage but a report-only one, ``NEUTRAL_MISS_STAGES``) on EITHER side, so the difference may be the
    cassette's, not the code's: it needs a re-recorded cassette and is counted in neither the declared nor the equal
    numbers (a non-pass bucket: exit 3). An input difference is always undeclared (it reads no cassette)."""
    linear_on = (branch.get("flags") or {}).get(FLAG, "").strip().lower() == "linear"
    declared: Counter = Counter()
    undeclared: list[dict] = []
    miss_rows: list[dict] = []
    missing = sorted(set(base["versions"]) ^ set(branch["versions"]))
    for vid in sorted(set(base["versions"]) & set(branch["versions"])):
        b, r = base["versions"][vid], branch["versions"][vid]
        if b.get("input") != r.get("input"):
            # the input builder must not move (main's builders, the same row)
            keys = [k for k in ("source_sha", "grounding_sha", "policy_hold") if b["input"].get(k) != r["input"].get(k)]
            if keys:
                undeclared.append({"id": vid, "field": "input", "keys": keys})
        fields = [f for f in ("error",) + COMPARED if b.get(f) != r.get(f)]
        bm, rm = _outcome_misses(b, base.get("flags") or {}), _outcome_misses(r, branch.get("flags") or {})
        if fields and (bm or rm):
            looser = [f for f in fields if more_permissive(f, b, r)]
            for f in looser:   # a looser outcome is never hidden by a miss
                undeclared.append({"id": vid, "field": f, "base": b.get(f), "branch": r.get(f),
                                   "why": "more permissive than the base (a cassette miss does not excuse it)"})
            rest = [f for f in fields if f not in looser]
            if rest:
                miss_rows.append({"id": vid, "fields": rest, "base_misses": bm, "branch_misses": rm})
            continue
        for f in fields:
            did = _declared(f, b, r) if (opt_in and linear_on and f != "error") else None
            if did:
                declared[did] += 1
            else:
                undeclared.append({"id": vid, "field": f, "base": b.get(f), "branch": r.get(f),
                                   **({} if opt_in and linear_on else {"why": "opt-in diff without --opt-in"
                                       if linear_on else "the default must equal main"})})
    return {"declared": dict(declared), "undeclared": undeclared, "missing": missing, "miss": miss_rows,
            "versions": len(branch["versions"]), "opt_in": opt_in, "flags": branch.get("flags") or {},
            "misses": _sum(branch, "misses"), "base_misses": _sum(base, "misses"),
            "cassette_hits": sum(v.get("cassette_hits") or 0 for v in branch["versions"].values()),
            "base_cassette_hits": sum(v.get("cassette_hits") or 0 for v in base["versions"].values()),
            "judge_misses": sum(1 for v in branch["versions"].values() if v.get("judge_miss")),
            "status": dict(Counter(v.get("status") for v in branch["versions"].values())),
            "base_status": dict(Counter(v.get("status") for v in base["versions"].values()))}


def exit_code(d: dict) -> int:
    """0 pass; 1 an undeclared difference or a missing version; 3 only MISS rows (re-record the cassette)."""
    if d["undeclared"] or d["missing"]:
        return 1
    return 3 if d["miss"] else 0


def report_md(d: dict) -> str:
    lines = [f"# AA-1049 T2 replay ({d['versions']} versions)", "",
             f"- flags: `{json.dumps(d['flags'], sort_keys=True)}`, --opt-in: {d['opt_in']}",
             f"- status base -> branch: {d['base_status']} -> {d['status']}",
             f"- declared: {d['declared'] or 0}", f"- undeclared: {len(d['undeclared'])}",
             f"- missing: {len(d['missing'])}",
             f"- MISS (a difference on a version with an outcome-moving cassette miss; re-record, never a pass): "
             f"{len(d['miss'])}",
             f"- cassette hits base / branch: {d['base_cassette_hits']} / {d['cassette_hits']}",
             f"- cassette misses base: {d['base_misses'] or 0}; branch: {d['misses'] or 0}; "
             f"judge misses (no stored verdict): {d['judge_misses']}", ""]
    for m in d["miss"][:50]:
        lines.append(f"- MISS {m['id']} {','.join(m['fields'])}: base {m['base_misses']}, branch {m['branch_misses']}")
    for u in d["undeclared"][:50]:
        lines.append(f"- UNDECLARED {u['id']} {u['field']}: {json.dumps({k: u.get(k) for k in ('base', 'branch', 'why', 'keys') if k in u}, default=str)[:300]}")  # noqa: E501
    lines += ["", "Declared ids:"] + [f"- {k}: {v}" for k, v in DECLARED.items()]
    return "\n".join(lines) + "\n"


def load_versions(path: str) -> list[dict]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if doc.get("schema") != VERSIONS_SCHEMA:
        raise SystemExit(f"{path}: schema must be {VERSIONS_SCHEMA!r}")
    return list(doc.get("versions") or [])


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--versions", required=True, help="export of stored T2 versions (schema aa1049-t2-versions/1)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--base", help="the base run (main checkout) to diff against")
    ap.add_argument("--report", help="write the Markdown report here")
    ap.add_argument("--opt-in", action="store_true", help="label diffs of an opt-in switch that is on in this run")
    ap.add_argument("--linear", action="store_true", help="run this side with T2_ORCHESTRATOR=linear")
    a = ap.parse_args(argv)
    result = run(load_versions(a.versions), {FLAG: "linear"} if a.linear else {})
    Path(a.out).write_text(json.dumps(result, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8")
    if not a.base:
        return 0
    d = diff(json.loads(Path(a.base).read_text(encoding="utf-8")), result, opt_in=a.opt_in)
    text = report_md(d)
    if a.report:
        Path(a.report).write_text(text, encoding="utf-8")
    print(text)
    return exit_code(d)


if __name__ == "__main__":
    sys.exit(main())
