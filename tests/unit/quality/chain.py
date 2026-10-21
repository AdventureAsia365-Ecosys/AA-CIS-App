"""AA-925 (Q-2) — the Q0 corpus runners: every case drives PRODUCTION code, never a hand-made copy
of its order (design §4.4, "cả chuỗi, đúng thứ tự sản xuất").

* ``a1``    — ``graph.build_graph_from_generated().invoke(state)``. The case's writer JSON is the batch
              text, so ``seed_generated_node`` runs exactly the generate post-processing (seo keywords,
              seo_meta fit, itinerary serialise, forbidden strip), then the real validate → judge →
              brand_audit → grounding → flag_fix → revalidate. Stubbed: the judge (pass), the brand audit
              (from the case; ``brand_audit.pre_audit: true`` adds the real deterministic pre-audit codes),
              Jev (``grounding.judge_units``, from the case), every model call
              (``LLMClient.generate`` answers ``case["llm_stub"]`` in order and records any call the case
              did not script) and the DB writers (``call_log.record_call``, lessons). No network.
* ``t9``    — the real ``service.run_write_background(..., pool=MagicMock())`` the way
              test_aa450_content_writing_service drives it: the writer / rewrite / sentence repair answer
              from the case, the F9 judge passes (or is down, ``f9: "unavailable"``), and the piece write
              (``_finalize_piece``) is captured in memory.
* ``gates`` — the AA-791 fault set through its own gate-only runner (``aa791_t9_fault_set.run_case``),
              unchanged: full-chain fixers could legitimately change those outcomes.

Import rule (red-on-base, .github/scripts/q0_red_on_base.py): this module and the corpus tests import
only modules that exist on ``main``. Outputs added by AA-925 are read with ``state.get("degraded", [])`` /
``state.get("quality_shadow", [])`` — never by importing ``services.quality`` at load time — so a new
case run on the base checkout FAILS (an assertion) instead of erroring (an import), and only a real
failure counts as red there.
"""
from __future__ import annotations

import asyncio
import copy
import json
import uuid
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

CASES_DIR = Path(__file__).parent / "cases"
FIXTURES_FILE = CASES_DIR / "_fixtures.json"
CHAINS = ("a1", "t9", "gates")
EXPECTED = ("fire", "no_fire", "keep", "drop")
SEVERITIES = ("hard", "block")
REQUIRED = ("id", "class_id", "severity", "writer", "artefact", "chain", "detector", "expected", "input")


# ── loading ──────────────────────────────────────────────────────────────────────────────────────


def load_fixtures() -> dict:
    with open(FIXTURES_FILE, encoding="utf-8") as f:
        return json.load(f)


def load_cases() -> list[dict]:
    """Every case of every ``cases/*.json`` file (a file holds a JSON list), in file then list order."""
    out: list[dict] = []
    for path in sorted(CASES_DIR.glob("*.json")):
        if path.name.startswith("_"):
            continue
        with open(path, encoding="utf-8") as f:
            for case in json.load(f):
                out.append({**case, "_file": path.name})
    return out


def _merge(base: Any, patch_: Any) -> Any:
    """Deep merge for dicts; a ``null`` in the patch deletes the key; lists and scalars replace."""
    if not isinstance(base, dict) or not isinstance(patch_, dict):
        return copy.deepcopy(patch_)
    out = copy.deepcopy(base)
    for k, v in patch_.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = _merge(out.get(k), v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def resolve_input(case: dict, fixtures: Optional[dict] = None) -> dict:
    """The case input with ``base`` (a key of _fixtures.json) expanded and ``*_patch`` merged in."""
    fixtures = fixtures if fixtures is not None else load_fixtures()
    inp = copy.deepcopy(case["input"])
    base = fixtures.get(inp.pop("base", None) or "", {}) if isinstance(inp, dict) else {}
    out = copy.deepcopy(base)
    for k, v in inp.items():
        if k.endswith("_patch"):
            key = k[: -len("_patch")]
            out[key] = _merge(out.get(key, {}), v)
        else:
            out[k] = copy.deepcopy(v)
    return out


# ── stubs ────────────────────────────────────────────────────────────────────────────────────────


class ScriptedLLM:
    """``LLMClient.generate`` answering the case's ``llm_stub`` in order. A call the case did not
    script is recorded (``unexpected``) and raises — many callers catch it (flag_fix fails open), so
    the corpus test asserts ``unexpected == []`` itself."""

    def __init__(self, script: list[dict]):
        self.script = list(script or [])
        self.calls: list[str] = []
        self.unexpected: list[str] = []

    def generate(self, _client, request):
        stage = getattr(request, "stage", None)
        self.calls.append(stage)
        if not self.script:
            self.unexpected.append(stage)
            raise RuntimeError(f"Q0: unscripted model call (stage={stage})")
        step = self.script.pop(0)
        if step.get("stage") and step["stage"] != stage:
            self.unexpected.append(f"{stage} (scripted for {step['stage']})")
            raise RuntimeError(f"Q0: model call for stage {stage}, script expected {step['stage']}")
        content = step.get("content", "")
        return SimpleNamespace(
            content=content if isinstance(content, str) else json.dumps(content), cost_usd=0.0,
            model_used="q0-stub", input_tokens=0, output_tokens=0, stop_reason="end_turn",
            satellite_account=None, fallback_used=False, cache_read_tokens=0, cache_write_tokens=0)


async def _no_record(*_a, **_k):
    return None


def _jev_verdicts(spec: Any):
    """The Jev answer for every grounding unit: ``none`` (Jev gives no signal: the question is off, so every
    unit is ``skipped`` — the same as ``skipped``), ``accept`` / ``reject`` (an enforce-mode confident verdict),
    ``error`` (the breaker / an outage: zone error), ``down`` (judge_units failed open and returned ``{}``:
    the loop, the pool or the call crashed), ``raise`` (a crash grounding_node itself must catch),
    ``error-shadow`` (an outage on a shadow-mode question: counted, never a hold)."""
    from shared.llm_client.decide import Verdict

    kind = spec or "none"

    def judge_units(units, sources, *, tenant_id=None):
        if kind in ("none", "skipped"):
            return {i: Verdict("a1_claim_supported", "off", "skipped") for i in sources}
        if kind == "down":
            return {}
        if kind == "raise":   # a crash below judge_units' own guard (grounding_node must not pass it)
            raise RuntimeError("Q0: grounding crashed")
        if kind == "error-shadow":
            return {i: Verdict("a1_claim_supported", "shadow", "error", error="timeout") for i in sources}
        if kind == "accept":
            return {i: Verdict("a1_claim_supported", "enforce", "accept", probability=0.99) for i in sources}
        if kind == "reject":
            return {i: Verdict("a1_claim_supported", "enforce", "reject", probability=0.01) for i in sources}
        if kind == "error":
            return {i: Verdict("a1_claim_supported", "enforce", "error", error="credit_exhausted (breaker open)")
                    for i in sources}
        raise ValueError(f"unknown jev stub {kind!r}")

    return judge_units


def _judge_pass(state: dict) -> dict:
    return {**state, "judge_score": 9.0, "judge_feedback": ""}


def _brand_audit(spec: Optional[dict]):
    """The brand audit answers from the case (the LLM audit is never called). ``pre_audit: true`` also runs
    the real deterministic pre-audit (``brand_audit_node.pre_audit_checks`` on the tour and the generated
    content, with Jev as the case stubs it) and adds its codes, the way the production node does (AA-803)."""
    spec = spec or {}

    def node(state: dict) -> dict:
        codes = list(spec.get("codes") or [])
        status = spec.get("status", "pass")
        if spec.get("pre_audit"):
            from services.content_generation import brand_audit_node as ba

            pre = ba.pre_audit_checks(state.get("generated") or {}, state.get("tour"))
            codes = list(dict.fromkeys(codes + pre))
            status = ba.derive_status(codes, spec.get("status"))
        return {**state, "brand_audit_status": status,
                "brand_audit_codes": codes,
                "brand_audit_issues": list(spec.get("issues") or []),
                "brand_audit_fields": list(spec.get("fields") or [])}

    return node


def signal_names(entries: Any) -> set[str]:
    """Codes / names in a list of str or dicts (``code`` or ``name``) — the AA-925 shapes, read loosely."""
    out = set()
    for e in entries or []:
        if isinstance(e, str):
            out.add(e)
        elif isinstance(e, dict):
            for k in ("code", "name", "gate"):
                if e.get(k):
                    out.add(str(e[k]))
    return out


# ── A1 ───────────────────────────────────────────────────────────────────────────────────────────


def a1_state(inp: dict) -> dict:
    """The initial state v1_pipeline._rewrite_tour builds for a Bedrock Batch attempt-1 (A1)."""
    br = inp.get("brand_rules") or {}
    return {
        "tour": copy.deepcopy(inp["tour"]), "seo": copy.deepcopy(inp.get("seo") or {}), "model_tier": None,
        "is_tenant_rewrite": False, "tenant_id": None, "generate_stage": "s1_generate", "few_shots": [],
        "generated": {}, "quality_score": 0.0, "retry_count": 0, "feedback": "", "error": "", "cost_usd": 0.0,
        "model_used": "q0-batch", "brand_system_prompt": br.get("system_prompt", ""),
        "brand_style_guide": "", "brand_forbidden_words": list(br.get("forbidden_words") or []),
        "rewrite_language": "en-US", "brand_core_idea": "", "brand_customer_segment": "",
        "brand_customer_mindset": "", "brand_voice_examples": [], "brand_good_examples": "",
        "brand_name": "", "brand_type": "", "brand_default_cta": "", "brand_signature_words": [],
        "subtitle_focus": "standard", "seo_mode": "dataforseo", "brand_audit_status": "",
        "brand_audit_codes": [], "brand_audit_issues": [], "brand_audit_fields": [], "lessons_extracted": [],
        "fix_pass_applied": False, "fix_pass_fields": [], "batch_text": json.dumps(inp["writer"]),
        "satellite_account": None,
    }


def run_a1(case: dict, fixtures: Optional[dict] = None) -> dict:
    from api.routers.admin_pipeline import _is_publishable
    from services.content_generation import flag_fix_node, graph, grounding
    from shared.llm_client import call_log
    from shared.llm_client.client import LLMClient

    inp = resolve_input(case, fixtures)
    llm = ScriptedLLM(case.get("llm_stub") or [])
    with ExitStack() as st:
        st.enter_context(patch.object(LLMClient, "__init__", lambda self: None))  # no boto3 / OpenAI client
        st.enter_context(patch.object(LLMClient, "generate", lambda self, req: llm.generate(self, req)))
        st.enter_context(patch.object(call_log, "record_call", _no_record))
        st.enter_context(patch.object(flag_fix_node, "_write_lessons_safe", lambda *a, **k: None))
        st.enter_context(patch.object(graph, "judge_node", _judge_pass))
        st.enter_context(patch.object(graph, "brand_audit_node", _brand_audit(inp.get("brand_audit"))))
        st.enter_context(patch.object(grounding, "judge_units", _jev_verdicts(inp.get("jev"))))
        final = graph.build_graph_from_generated().invoke(a1_state(inp))
    observed = (set(final.get("failure_codes") or []) | signal_names(final.get("quality_shadow"))
                | signal_names(final.get("degraded")))
    if (inp.get("brand_audit") or {}).get("pre_audit"):
        observed |= set(final.get("brand_audit_codes") or [])   # the real pre-audit codes (AA-803)
    return {"chain": "a1", "state": final, "generated": final.get("generated") or {},
            "observed": observed, "publishable": bool(_is_publishable(final)),
            "brand_audit_status": final.get("brand_audit_status"), "llm_unexpected": llm.unexpected,
            "llm_calls": llm.calls, "llm_left": len(llm.script)}


# ── T9 ───────────────────────────────────────────────────────────────────────────────────────────

T9_REQUEST_ID = uuid.UUID("00000000-0000-4000-8000-000000000925")
T9_PIECE_ID = uuid.UUID("00000000-0000-4000-8000-000000009250")


def t9_context(inp: dict) -> dict:
    ctx = {"atom_text": inp["seed"], "goal": {"key": "promotion", "name": "Promotion", "description": "d",
                                                "logic": "AIDA", "marketing_term": "AIDA"},
           "channel_style": {"key": inp.get("channel", "facebook"), "tone": "warm"},
           "brand_audience": copy.deepcopy(inp.get("brand_audience") or {}),
           "chosen": {"idx": 0, "name": "A", "why_it_works": "w", "formula_fit": "AIDA",
                      "best_final_style": "warm", "recommended": True, "chosen": True},
           "cta": inp.get("cta"), "destination": inp.get("destination"), "trip_name": inp.get("trip_name"),
           "brand_rubric_text": "rubric", "channel": inp.get("channel", "facebook"), "atom_id": "atom_q0"}
    ctx.update(copy.deepcopy(inp.get("context") or {}))
    return ctx


def run_t9(case: dict, fixtures: Optional[dict] = None) -> dict:
    from services.acp_content_writing import quality_gates as qg
    from services.acp_content_writing import service

    inp = resolve_input(case, fixtures)
    llm = ScriptedLLM(case.get("llm_stub") or [])
    captured: dict = {}
    rewrites = list(inp.get("rewrites") or [])
    repairs = list(inp.get("repairs") or [])
    unexpected: list[str] = []

    def write_content(**_kw):
        return inp["draft"], 0.0, dict(inp.get("seo_meta") or {"seo_title": None, "meta_description": None,
                                                                 "slug": None}), None

    def rewrite_with_feedback(**_kw):
        if not rewrites:
            unexpected.append("rewrite_with_feedback")
            raise RuntimeError("Q0: unscripted rewrite")
        return rewrites.pop(0), 0.0, dict(inp.get("seo_meta") or {"seo_title": None, "meta_description": None,
                                                                    "slug": None}), None

    def repair_sentences(*, sentences, **_kw):
        if not repairs:
            unexpected.append("repair_sentences")
            raise service.SentenceRepairError("Q0: unscripted sentence repair")
        step = repairs.pop(0)
        return {int(k): v for k, v in step.items()}, 0.0

    async def finalize(pool, *, piece_id, **kw):
        captured.update(kw)
        return {"piece_id": str(piece_id), **kw}

    f9 = inp.get("f9") or "pass"

    def _voice_pass(*_a, **_k):
        return [qg._result("F9_cta_fact", []), qg._result("F9_brand_style", [], blocking=False)]

    def _judge_down(*_a, **_k):
        raise RuntimeError("Q0: judge unavailable")

    from shared.llm_client import call_log
    from shared.llm_client.client import LLMClient

    with ExitStack() as st:
        st.enter_context(patch.object(LLMClient, "__init__", lambda self: None))  # no boto3 / OpenAI client
        st.enter_context(patch.object(LLMClient, "generate", lambda self, req: llm.generate(self, req)))
        st.enter_context(patch.object(call_log, "record_call", _no_record))
        st.enter_context(patch.object(service, "write_content", side_effect=write_content))
        st.enter_context(patch.object(service, "rewrite_with_feedback", side_effect=rewrite_with_feedback))
        st.enter_context(patch.object(service, "repair_sentences", side_effect=repair_sentences))
        st.enter_context(patch.object(service, "_finalize_piece", new=AsyncMock(side_effect=finalize)))
        if f9 == "pass":
            st.enter_context(patch.object(qg, "gate_brand_voice", side_effect=_voice_pass))
        elif f9 == "unavailable":
            st.enter_context(patch.object(qg, "invoke_judge", side_effect=_judge_down))
        else:
            raise ValueError(f"unknown f9 stub {f9!r}")
        asyncio.run(service.run_write_background(T9_REQUEST_ID, T9_PIECE_ID, t9_context(inp), pool=MagicMock()))
    ledger = captured.get("gate_ledger") or []
    flags = captured.get("flags") or []
    observed = ({g["gate"] for g in ledger if isinstance(g, dict) and not g.get("passed", True)}
                | signal_names(f for f in flags if isinstance(f, dict))
                | signal_names(captured.get("degraded"))
                | {f.get("degraded") for f in flags if isinstance(f, dict) and f.get("degraded")})
    observed.discard(None)
    return {"chain": "t9", "captured": captured, "content_text": captured.get("content_text") or "",
            "status": captured.get("status"), "held_reason": captured.get("held_reason"),
            "observed": observed, "llm_unexpected": llm.unexpected + unexpected, "llm_calls": llm.calls,
            "llm_left": len(llm.script) + len(rewrites) + len(repairs)}


# ── gates (AA-791) ───────────────────────────────────────────────────────────────────────────────


def run_gates(case: dict, fixtures: Optional[dict] = None) -> dict:
    from tests.unit import aa791_t9_fault_set as fs

    ref = case["input"]["aa791_case"]
    fault = next((c for c in fs.FAULT_CASES + fs.CLEAN_CASES if c.case_id == ref), None)
    if fault is None:
        raise LookupError(f"AA-791 case {ref!r} is gone")
    with fs.judges_pass():
        res = fs.run_case(fault)
    target = res["target"]
    fired = target is not None and not target["passed"] and not res["passed"]
    return {"chain": "gates", "observed": {fault.gate} if fired else set(),
            "target_ran": target is not None, "target_gate": fault.gate, "llm_unexpected": [], "llm_left": 0}


RUNNERS = {"a1": run_a1, "t9": run_t9, "gates": run_gates}


def run_case(case: dict, fixtures: Optional[dict] = None) -> dict:
    return RUNNERS[case["chain"]](case, fixtures)


# ── truth + extra checks ─────────────────────────────────────────────────────────────────────────


def _field(result: dict, name: str) -> str:
    if result["chain"] == "t9":
        return result["content_text"] if name == "content_text" else str((result["captured"] or {}).get(name) or "")
    value = (result.get("generated") or {}).get(name)
    if isinstance(value, list):
        return "\n".join(str(v) for v in value)
    return "" if value is None else str(value)


def problems(case: dict, result: dict) -> list[str]:
    """Every way ``result`` disagrees with the case's truth label and its ``expect`` checks ([] = pass)."""
    out: list[str] = []
    detector = case["detector"]
    fired = detector in result["observed"]
    want_fire = case["expected"] in ("fire", "drop")
    if fired != want_fire:
        out.append(f"truth: {detector} {'fired' if fired else 'did not fire'}, expected {case['expected']} "
                   f"(observed {sorted(result['observed'])})")
    if result["chain"] == "gates" and not result.get("target_ran"):
        out.append(f"gate {result.get('target_gate')} did not run")
    exp = case.get("expect") or {}
    for code in exp.get("observed_include", []):
        if code not in result["observed"]:
            out.append(f"expect: {code} not observed (observed {sorted(result['observed'])})")
    for code in exp.get("observed_exclude", []):
        if code in result["observed"]:
            out.append(f"expect: {code} observed")
    if "publishable" in exp and result.get("publishable") != exp["publishable"]:
        out.append(f"expect: publishable={result.get('publishable')}, wanted {exp['publishable']}")
    if "brand_audit_status" in exp and result.get("brand_audit_status") != exp["brand_audit_status"]:
        out.append(f"expect: brand_audit_status={result.get('brand_audit_status')!r}, wanted "
                   f"{exp['brand_audit_status']!r}")
    if "status" in exp and result.get("status") != exp["status"]:
        out.append(f"expect: status={result.get('status')!r}, wanted {exp['status']!r} "
                   f"(held_reason {result.get('held_reason')!r})")
    if "held_reason_contains" in exp and exp["held_reason_contains"] not in (result.get("held_reason") or ""):
        out.append(f"expect: held_reason {result.get('held_reason')!r} lacks {exp['held_reason_contains']!r}")
    for name, words in (exp.get("field_not_contains") or {}).items():
        text = _field(result, name).lower()
        for w in words:
            if w.lower() in text:
                out.append(f"expect: {name} contains {w!r}")
    for name, words in (exp.get("field_contains") or {}).items():
        text = _field(result, name)
        for w in words:
            if w not in text:
                out.append(f"expect: {name} lacks {w!r}")
    for name, value in (exp.get("field_equals") or {}).items():
        if _field(result, name) != value:
            out.append(f"expect: {name} = {_field(result, name)!r}, wanted {value!r}")
    for _label, n in (exp.get("count_equals") or {}).items():
        needle, field_ = n["text"], n.get("field", "content_text")
        got = _field(result, field_).count(needle)
        if got != n["n"]:
            out.append(f"expect: {needle!r} appears {got}x in {field_}, wanted {n['n']}")
    if result.get("llm_unexpected"):
        out.append(f"unscripted model calls: {result['llm_unexpected']}")
    if exp.get("all_stubs_used", True) and result.get("llm_left"):
        out.append(f"{result['llm_left']} scripted model answer(s) never used")
    return out
