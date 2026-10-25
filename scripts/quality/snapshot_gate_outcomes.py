"""AA-926 — gate-outcome snapshot: proves the Quality System v1 registry refactor changes no gate outcome.

`build()` evaluates every gate decision the refactor touches, through the real functions, with no LLM,
no DB and no network:

- T10: the 40 AA-791 fault-set cases (`tests/unit/aa791_t9_fault_set.py`) under `judges_pass()` — the full
  ledger, `passed` / `first_failure` / `flags`, `hold_failure`, `never_holds`, the `sentence_repair.plan`
  route, `service._style_only_repair`, `service._repair_feedback` and the AA-791 `ledger_snapshot`; the gate
  order per channel; and the membership of every routing set built from gate names.
- A1: `should_retry`, the judge tie-break (`in_tie_band`, `tie_break_score`), the brand-fit repeat band,
  the mission cap (`t2_judge_score`), the MISSING_FIELD cap (`validate_node`), `human_edit_gate_node`,
  `revalidate`-style pass, `_HARD_BLOCK_CODES` and the `HARD_CODE_REPAIR` keys.
- T2 status, the T3 pass line, the T7 brand-fit cut and its Jev reject score, T10 reuse similarity,
  the validation-handler route, the LLM-ops judge pass line.
- admin_pipeline: `_is_publishable`, `_tour_passed`, failure-summary / review-block / failure-class
  strings, the review-queue score-band SQL and the score-histogram CASE text, byte for byte.
- The golden-set verdict (`scripts/aa729/score.py::t2_verdict`).

Usage (from the repo root):
    python -m scripts.quality.snapshot_gate_outcomes            # print the snapshot JSON
    python -m scripts.quality.snapshot_gate_outcomes --write    # rewrite the committed fixture

The committed fixture is `tests/unit/fixtures/aa926_gate_snapshot.json`; `tests/unit/test_aa926_equivalence.py`
recomputes `build()` and compares it. Regenerate the fixture ONLY on a commit that changes no gate code
(the harness-only commit "A"), never to make the refactor pass.
An intentional behaviour change regenerates it on the behaviour-change commit itself, with a PR note naming
the changed sections. Run locally with a dummy AWS_PROFILE and AWS_EC2_METADATA_DISABLED=true (the api
router import otherwise triggers a Secrets Manager lookup that prompts for MFA).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "unit" / "fixtures" / "aa926_gate_snapshot.json"

# Score boundary grid around the 7.0 pass line and the other cut-offs.
_SCORES = [0.0, 3.99, 4.0, 4.01, 4.99, 5.0, 5.5, 5.99, 6.0, 6.4, 6.5, 6.9, 6.95, 6.99, 7.0, 7.01, 7.05, 7.1,
           7.5, 7.51, 7.99, 8.0, 8.01, 8.99, 9.0, 10.0]
_CHANNELS = ("blog", "linkedin", "facebook", "instagram", "tiktok", "email", "landing_page", "ads")


def _jsonable(x):
    if isinstance(x, (frozenset, set)):
        return sorted(_jsonable(i) for i in x)
    if isinstance(x, tuple):
        return [_jsonable(i) for i in x]
    if isinstance(x, list):
        return [_jsonable(i) for i in x]
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in sorted(x.items(), key=lambda kv: str(kv[0]))}
    return x


# ------------------------------------------------------------------------------------------ T10

def _t10() -> dict:
    from services.acp_content_writing import quality_gates as qg
    from services.acp_content_writing import sentence_repair
    from services.acp_content_writing import service
    from services.acp_content_writing.gate_eval import ledger_snapshot
    from tests.unit import aa791_t9_fault_set as fs

    cases = {}
    with fs.judges_pass():
        for case in fs.FAULT_CASES + fs.CLEAN_CASES:
            out = fs.run_case(case)
            ledger = out["gate_ledger"]
            hold = qg.hold_failure(ledger)
            cases[case.case_id] = {
                "target_gate": case.gate,
                "ledger": ledger,
                "passed": out["passed"],
                "first_failure": (out["first_failure"] or {}).get("gate"),
                "flags": [g["gate"] for g in out["flags"]],
                "hold_failure": hold["gate"] if hold else None,
                "never_holds": {g["gate"]: qg.never_holds(g) for g in ledger if not g["passed"]},
                "repair_plan": sentence_repair.plan(case.content_text, ledger),
                "style_only_repair": service._style_only_repair(ledger),
                "ledger_snapshot": ledger_snapshot(ledger),
            }
        # Gate order per channel (AA-924: 23 blog, 22 LinkedIn, 21 the others; AA-787 had 25 / 24 / 23).
        clean = next(c for c in fs.CLEAN_CASES if c.channel == "facebook")
        order = {}
        for ch in _CHANNELS:
            r = qg.run_quality_gates(content_text=clean.content_text, atom_text=clean.atom_text, cta=clean.cta,
                                     brand_rubric_text="rubric", channel=ch, brand_pack=clean.brand_pack)
            order[ch] = [g["gate"] for g in r["gate_ledger"]]
    routing = {
        "quality_gates.STYLE_ONLY_GATES": qg.STYLE_ONLY_GATES,
        "quality_gates.CHEAP_REPAIR_GATES": qg.CHEAP_REPAIR_GATES,
        "quality_gates.OWNED_BY_FIX_STEP": qg.OWNED_BY_FIX_STEP,
        "sentence_repair._ELIGIBLE_GATES": sentence_repair._ELIGIBLE_GATES,
        "sentence_repair._QUOTED_GATES": sentence_repair._QUOTED_GATES,
        "sentence_repair.TRUTH_GATES": list(sentence_repair.TRUTH_GATES),
        "sentence_repair.OWNED_ELSEWHERE": sentence_repair.OWNED_ELSEWHERE,
        "service.TRUTH_CUT_GATES": service.TRUTH_CUT_GATES,
        "service.F17_CUT_GATE": service.F17_CUT_GATE,
        "quality_gates.named": {n: getattr(qg, n) for n in sorted(dir(qg))
                                if n.startswith("F") and n.endswith("_GATE") and isinstance(getattr(qg, n), str)},
        "authenticity.GATE": __import__("services.acp_content_writing.authenticity", fromlist=["GATE"]).GATE,
    }
    # AA-777 truth-cut routing over a synthetic repair_log (sentence repair of each gate once).
    gates = sorted({g for c in cases.values() for g in (e["gate"] for e in c["ledger"])})
    log = [{"repair_mode": "sentences", "gate_ledger": [{"gate": g, "passed": False, "blocking": True}]}
           for g in gates]
    routing["service._cut_gates_repaired(all)"] = service._cut_gates_repaired(log)
    routing["service._truth_repaired_before(all)"] = service._truth_repaired_before(log)
    # The rewrite feedback per failed blocking gate (AA-768; AA-984 adds the F4 over-max target on a blog only).
    feedback = {f"{cid}|{ch}": service._repair_feedback(c["ledger"], ch)
                for cid, c in cases.items() for ch in ("blog", "facebook")}
    return {"cases": cases, "channel_gate_order": order, "routing_sets": routing, "repair_feedback": feedback}


# ------------------------------------------------------------------------------------------- A1

def _a1() -> dict:
    from services.content_generation import brand_fit, graph, judge_node

    retry = {}
    for s in _SCORES:
        for rc in range(graph.MAX_RETRIES + 1):
            retry[f"{s}|{rc}"] = graph.should_retry({"quality_score": s, "retry_count": rc})

    class _D:
        def __init__(self, acc, rej):
            self._a, self._r = acc, rej

        def accepted(self, _q):
            return self._a

        def rejected(self, _q):
            return self._r

    tie = {}
    for js in _SCORES:
        for vs in (6.99, 7.0, 9.0):
            tie[f"in_band|{js}|{vs}"] = judge_node.in_tie_band(js, vs)
        for name, d in (("accept", _D(True, False)), ("reject", _D(False, True)), ("none", _D(False, False))):
            tie[f"break|{js}|{name}"] = judge_node.tie_break_score(js, d)
    lo, hi = brand_fit._REPEAT_BAND
    repeat = {str(s): lo <= s <= hi for s in _SCORES}

    mission = {}
    for s in _SCORES:
        for present in (True, False):
            r = SimpleNamespace(brand_fit_score=s, mission_present=present, judge_score=s, cross_brand_distinct=s)
            for gate in ("brand_fit_mission", "brand_fit", "combined"):
                for has in (True, False):
                    mission[f"{s}|{present}|{gate}|{has}"] = judge_node.t2_judge_score(r, gate, has_mission=has)

    full = {
        "name": "Volcanic Island Trails", "subtitle": "Seven days across a southern island",
        "summary": "A grounded week of coastal hikes and island villages that rewards slow travel.",
        "highlights": ["Hike the crater rim", "Walk the coastal paths", "Stay in a fishing village"],
        "itineraries": "Day 1 -- Arrive and walk the eastern shore at dusk.\n\nDay 2 -- Climb toward the crater.",
        "seo_title": "Volcanic Island Trails: A Seven-Day Island Walk",
        "seo_meta": "Walk a volcanic coast and island villages on a grounded seven-day hiking journey for slow "
                    "travellers.",
    }

    def _state(gen):
        return {"generated": dict(gen), "tour": {"country": "South Korea"}, "seo": {}, "brand_forbidden_words": [],
                "retry_count": 0, "seo_mode": "dataforseo"}

    partial = dict(full)
    partial["seo_meta"] = ""
    validate = {}
    for name, gen in (("empty", {}), ("partial", partial), ("full", full)):
        out = graph.validate_node(_state(gen))
        validate[name] = {"quality_score": out["quality_score"], "failure_codes": out["failure_codes"],
                          "route_r0": graph.should_retry({**out, "retry_count": 0}),
                          "route_last": graph.should_retry({**out, "retry_count": graph.MAX_RETRIES - 1})}

    gate = {}
    for s in (6.9, 6.99, 7.0, 7.01):
        for audit in ("", "pass", "flagged", "manual_check"):
            for codes in ([], ["SUBTITLE_GENERIC"], ["FORBIDDEN_WORD"]):
                out = graph.human_edit_gate_node({"quality_score": s, "brand_audit_status": audit,
                                                  "failure_codes": codes})
                gate[f"{s}|{audit}|{','.join(codes)}"] = out.get("revalidate_passed")
    return {
        "MIN_QUALITY": graph.MIN_QUALITY, "MISSING_FIELD_CAP": graph.MISSING_FIELD_CAP,
        "judge_node._MIN_QUALITY": judge_node._MIN_QUALITY, "judge_node._TIE_BAND": judge_node._TIE_BAND,
        "judge_node._BELOW_LINE": judge_node._BELOW_LINE,
        "judge_node._MISSION_ABSENT_CAP": judge_node._MISSION_ABSENT_CAP,
        "brand_fit._REPEAT_BAND": list(brand_fit._REPEAT_BAND),
        "should_retry": retry, "tie_break": tie, "repeat_band": repeat, "mission_cap": mission,
        "validate": validate, "human_edit_gate": gate,
        "_HARD_BLOCK_CODES": graph._HARD_BLOCK_CODES, "HARD_CODE_REPAIR": sorted(graph.HARD_CODE_REPAIR),
        "_FAILURE_MAP": graph._FAILURE_MAP,
    }


# ----------------------------------------------------------------------------------- T2/T3/T7

def _t_series() -> dict:
    from services.acp_produce import tenant_pipeline
    from services.acp_shared import debate
    from services.acp_content_writing import service
    from services.jobs import t2_rewrite_job
    from services.validation import handler

    return {
        "t2_status": {str(s): t2_rewrite_job._t2_status(s) for s in _SCORES + [-1.0]},
        "t3_pass_line": tenant_pipeline._T3_PASS_LINE,
        "t3_below_line": {str(s): s < tenant_pipeline._T3_PASS_LINE for s in _SCORES},
        "t7_cut": debate.BRAND_FIT_CUT_THRESHOLD,
        "t7_jev_reject_score": debate._T7_JEV_REJECT_SCORE,
        "t7_passes": {str(s): s >= debate.BRAND_FIT_CUT_THRESHOLD for s in _SCORES},
        "t7_jev_reject_passes": debate._T7_JEV_REJECT_SCORE >= debate.BRAND_FIT_CUT_THRESHOLD,
        "t10_reuse_similarity": service._REUSE_SIMILARITY_THRESHOLD,
        "t10_reuse": {str(s): s >= service._REUSE_SIMILARITY_THRESHOLD for s in (0.9199, 0.92, 0.9201)},
        "validation_route": {f"{s}|{rc}": list(handler._validation_route(s, rc)) for s in _SCORES for rc in (0, 1, 2)},
    }


def _llm_ops() -> dict:
    from api.routers import admin_llm_ops as ops
    return {"_JUDGE_PASS_LINE": ops._JUDGE_PASS_LINE}


# ------------------------------------------------------------------------------- admin_pipeline

def _admin_pipeline() -> dict:
    from api.routers import admin_pipeline as ap

    pub, passed, summary, block, klass = {}, {}, {}, {}, {}
    for s in (0.0, 6.9, 6.95, 6.99, 7.0, 7.01, 9.0):
        passed[str(s)] = ap._tour_passed({"quality_score": s})
        for audit in (None, "pass", "flagged", "manual_check"):
            for fixed in (False, True):
                for codes in ([], ["SUBTITLE_GENERIC"], ["FORBIDDEN_WORD"]):
                    r = {"quality_score": s, "brand_audit_status": audit, "fix_pass_applied": fixed,
                         "failure_codes": codes}
                    key = f"{s}|{audit}|{fixed}|{','.join(codes)}"
                    pub[key] = ap._is_publishable(r)
                    if not fixed:
                        summary[key] = ap._build_failure_summary(r)
        for codes in ([], ["SUBTITLE_GENERIC"], ["META_TOO_SHORT"], ["UNSUPPORTED_CLAIM"]):
            for audit in (None, "manual_check"):
                key = f"{s}|{','.join(codes)}|{audit}"
                block[key] = ap._review_block(s, codes, [], audit)
                klass[key] = ap._review_failure_class(s, codes, [], audit, "", 2000)
    block["None|-|None"] = ap._review_block(None, [], [], None)
    klass["None|-|None"] = ap._review_failure_class(None, [], [], None, "", 2000)
    return {
        "_is_publishable": pub, "_tour_passed": passed, "_build_failure_summary": summary,
        "_review_block": block, "_review_failure_class": klass,
        "_score_band_where": {b: ap._score_band_where(b) for b in ("all", "critical", "low", "ok", "x")},
        "_SCORE_HISTOGRAM_CASE": ap._SCORE_HISTOGRAM_CASE,
    }


# ----------------------------------------------------------------------------------------- eval

def _eval() -> dict:
    from scripts.aa729 import score
    out = {}
    for s in (6.99, 7.0, 7.01):
        for qa in (None, {"passed": True}, {"passed": False}):
            out[f"{s}|{None if qa is None else qa['passed']}"] = score.t2_verdict({"quality_score": s}, qa)["passed"]
    return out


def _prompts() -> dict:
    """Prompt / reference texts that quote the pass line: pinned by hash so moving the literal keeps the bytes."""
    import hashlib
    from services.content_generation import brand_standards
    return {"brand_standards.AA_COWORK_STRUCTURE_PROMPT":
            hashlib.sha256(brand_standards.AA_COWORK_STRUCTURE_PROMPT.encode("utf-8")).hexdigest()}


def build() -> dict:
    import tests.unit.conftest  # noqa: F401 — the CI unit-test env (dummy values), set at import
    return _jsonable({
        "t10": _t10(), "a1": _a1(), "t_series": _t_series(), "llm_ops": _llm_ops(),
        "admin_pipeline": _admin_pipeline(), "eval": _eval(), "prompts": _prompts(),
    })


def dumps(snapshot: dict) -> str:
    return json.dumps(snapshot, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> None:  # pragma: no cover — manual
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true", help="rewrite the committed fixture")
    args = ap.parse_args()
    text = dumps(build())
    if args.write:
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(text, encoding="utf-8")
        print(f"wrote {FIXTURE} ({len(text)} bytes)")
    else:
        print(text, end="")


if __name__ == "__main__":  # pragma: no cover
    main()
