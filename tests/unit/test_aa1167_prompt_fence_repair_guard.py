"""AA-1167 (OWASP LLM01) — the fence guard of the REPAIR writer calls of the S1 / T2 / T9 flows (review round 1).

The first-draft writers are guarded in test_aa1167_prompt_fence_guard.py. The repair calls of the same flows quote the
same untrusted text (supplier source, Facts Entries, web search questions) and must fence it too:

- T9 (services/acp_content_writing/generate.py): the sentence repair, the extend repair, the SEO fit and the FAQ writer;
- S1 / T2 / T3 (services/content_generation): the grounding sentence repair, the flag-fix field fix and meta re-repair,
  and the per-day itinerary nudge.

1. Canary renders: every untrusted input carries an invented marker; with the fence on no marker is left outside an
   <untrusted_data> block and the system prompt carries prompt_fence.SYSTEM_RULE.
2. Classification: every parameter of a repair builder is fed a marker here or listed as trusted with the reason.
3. Off = main: with the fence off each call sends exactly what main sent (sha256 of main's renders, see MAIN below).
4. Wiring: a None mode reads the setting cis.llm.prompt_data_fence; the graph passes its run's mode (state key
   "prompt_fence") to the grounding repair, the flag fix and both itinerary nudges; every LLMRequest of these modules
   carries prompt_fence.system_rule.
Invented text only."""
import ast
import hashlib
import inspect
import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from services.acp_content_writing import generate
from services.ai_safety import prompt_fence as pf
from services.content_generation import flag_fix_node as ff
from services.content_generation import grounding, itinerary_utils
from shared import settings
from shared.llm_client import call_log as _call_log
from shared.llm_client import client as _client_mod

from tests.unit import test_aa1167_prompt_fence_guard as g

ROOT = Path(__file__).resolve().parents[2]


class _Stop(Exception):
    """Raised by the fake model after the request is captured (the reply is not under test)."""


def _setting(on: bool):
    real_get = settings.get
    return patch.object(settings, "get", side_effect=lambda k, *a: on if k == pf.KEY else real_get(k, *a))


def _mode(fence):
    return {} if fence is None else {"fence": fence}


# ── capture: the request each repair call sends ─────────────────────────────────────────────────────────────────

@contextmanager
def _t9_capture(sent: list):
    def fake(fn, request, *a, **k):
        sent.append(request)
        raise _Stop()
    with patch.object(generate, "generate_with_budget_retry", side_effect=fake):
        yield


def _t9_requests(fence) -> list:
    sent: list = []
    with _t9_capture(sent):
        calls = [
            lambda: generate.repair_sentences_detailed(
                sentences=[{"sentence": "The market opens daily.", "problem": "unsupported time",
                            "context": ["Traders sell mats.", "Boats leave at noon."]}],
                content_seed="Corran reed market sells woven mats QZMRP01.",
                facts_text="[Fact id=f1] Ferry tickets cost 4 coins QZMRP02", channel="blog", **_mode(fence)),
            lambda: generate.extend_sections(
                sections=[{"heading": "Corran", "text": "Traders sell mats.", "words": 40}],
                facts=["Boats cross to Isle Marrow QZMEX03."], voice="calm and plain", **_mode(fence)),
            lambda: generate.extend_sections(
                sections=[{"heading": "Corran", "text": "Traders sell mats.", "words": 40, "hole_chars": 12}],
                facts=["Reed is cut in spring QZMEX04."], background=["Isle Marrow QZMEX05"], **_mode(fence)),
            lambda: generate.extend_sections(
                sections=[{"heading": "Corran", "text": "Traders sell mats.", "words": 40}],
                facts=[], background=[], **_mode(fence)),
            lambda: generate.fit_seo_fields(
                body="Corran market post body QZMSF06.", seo_title="Corran market", meta_description="Mats.",
                keyword="corran market", brand_name="Northstep Journeys", taken_titles=["Velor walks"],
                forbidden_words=["Gobi"], meta_band=(120, 155), **_mode(fence)),
            lambda: generate.write_faq(
                source="Corran reed market sells woven mats QZMFQ07.", body="A post about the market QZMFQ08.",
                brand_name="Northstep Journeys", unused_facts=["Boats leave at noon QZMFQ09."],
                paa_questions=["What is sold at Corran QZMFQ10?"],
                required_questions=["Is Corran open daily QZMFQ11?"], **_mode(fence)),
        ]
        for call in calls:
            with pytest.raises(_Stop):
                call()
    return sent


_VIOLATION = {"field": "summary", "sentence": "A short trip around the lake.", "code": "UNSUPPORTED_NUMBER",
              "novel_numbers": ["4"]}


def _grounding_request(fence):
    sent = []

    class _Client:
        def generate(self, request):
            sent.append(request)
            raise _Stop("captured")

    with patch.object(_client_mod, "LLMClient", _Client), patch.object(_call_log, "record_unusable_sync"):
        out = grounding.repair({"summary": _VIOLATION["sentence"]}, [_VIOLATION], g._tour(), **_mode(fence))
    assert out["error"]                                    # the fake model raised; repair never raises
    return sent[0]


def _fix_state(**kw):
    tour = g._tour()
    return dict(kw, brand_audit_status="flagged", brand_audit_codes=["SUMMARY_OFF_BRAND"],
                brand_audit_fields=["summary"], brand_audit_issues=["Summary reads generic."], failure_codes=[],
                generated={"name": "Velor Reed Loop", "summary": "Old summary."}, tour=tour)


def _flag_fix_request(state):
    resp = MagicMock(content=json.dumps({"summary": "New summary."}), cost_usd=0.0)
    with patch.object(ff, "LLMClient") as client, patch.object(ff, "record_call_sync"), \
            patch.object(ff, "_write_lessons_safe"):
        client.return_value.generate.return_value = resp
        ff.flag_fix_node(state)
    return client.return_value.generate.call_args.args[0]


def _rerepair_request(fence):
    resp = MagicMock(content=json.dumps({"seo_meta": "x"}), cost_usd=0.0)
    with patch.object(ff, "LLMClient") as client, patch.object(ff, "record_call_sync"):
        client.return_value.generate.return_value = resp
        ff._rerepair_meta("Short meta.", g._tour(), {"name": "Velor Reed Loop"}, None,
                          intent_clue="Is Velor open in winter QZMRR12?", **_mode(fence))
    return client.return_value.generate.call_args_list[0].args[0]


def _nudge_request(fence):
    client = MagicMock()
    client.generate.return_value = MagicMock(content="{}")
    itinerary_utils.nudge_itinerary_day(client, "Day 2: Boat to Isle Marrow QZMND13.", "Isle Marrow",
                                        "Short.", 50, **_mode(fence))
    return client.generate.call_args.args[0]


def _all_requests(fence) -> list:
    return (_t9_requests(fence) + [_grounding_request(fence), _rerepair_request(fence), _nudge_request(fence)]
            + [_flag_fix_request(_fix_state(prompt_fence=fence, is_tenant_rewrite=False))])


# ── 1. canary renders ─────────────────────────────────────────────────────────────────────────────────────────

def test_the_markers_reach_the_repair_prompts():
    """With the fence off every marker shows up (else the guard is blind)."""
    text = "\n".join(r.user_prompt for r in _all_requests(False))
    expected = {f"QZM{p}{n:02d}" for p, n in (("RP", 1), ("RP", 2), ("EX", 3), ("EX", 4), ("EX", 5), ("SF", 6),
                                                ("FQ", 7), ("FQ", 8), ("FQ", 9), ("FQ", 10), ("FQ", 11),
                                                ("RR", 12), ("ND", 13))}
    expected |= {"QZMNA01", "QZMSU04", "QZMIT07", "QZMCO02", "QZMDU03"}      # tour fields: grounding / flag fix
    assert expected <= set(g.MARK.findall(text)), sorted(expected - set(g.MARK.findall(text)))


def test_every_repair_prompt_fences_its_untrusted_values():
    for request in _all_requests(True):
        assert g._leaks(request.user_prompt) == [], request.user_prompt[:300]
        assert pf.SYSTEM_RULE in request.system_prompt and g._leaks(request.system_prompt) == []


def test_the_task_stays_outside_the_fence():
    """The sentence under repair and the checker's PROBLEM, the sections being lengthened and our headings are the
    task: they stay outside, so the model still acts on them."""
    repair, extend = _t9_requests(True)[:2]
    outside = pf.outside_fences(repair.user_prompt)
    assert "SENTENCE: The market opens daily." in outside and "PROBLEM: unsupported time" in outside
    assert "SENTENCES TO FIX:" in outside and "FACTS:" in outside
    assert "TEXT:\nTraders sell mats." in pf.outside_fences(extend.user_prompt)
    assert "SENTENCES TO CORRECT:" in pf.outside_fences(_grounding_request(True).user_prompt)


def test_empty_lists_keep_their_placeholders_when_fenced():
    empty = _t9_requests(True)[3]
    assert "SOURCE FACTS:\n(none left)" in empty.user_prompt
    assert "NAMED:\n(only what the sections name)" in empty.user_prompt


# ── 2. classification ─────────────────────────────────────────────────────────────────────────────────────────

COMMON = {"tenant_id": "a ledger id", "angle_gate_request_id": "a ledger id", "content_piece_id": "a ledger id",
          "channel": "a channel key", "stage": "a route key", "attempt": "a counter", "mode": "a ledger mode key",
          "max_tokens": "a ceiling from token_budget", "step": "a ledger step key", "fence": "the fence mode itself",
          "model_tier": "a route tier", "system_prompt": "our system prompt", "client": "the gateway client"}
CLASSIFIED = {
    generate.repair_sentences_detailed: (
        {"content_seed", "facts_text"},
        {"sentences": "the draft sentences under repair with the checker's PROBLEM and neighbours: the task"}),
    generate.extend_sections: (
        {"facts", "background"},
        {"sections": "our draft's sections being lengthened: the task", "voice": "the tenant brand pack's voice"}),
    generate.fit_seo_fields: (
        {"body"},
        {"seo_title": "the draft title under repair", "meta_description": "the draft meta under repair",
         "keyword": "a keyword from our research cache, matched by keyword_fit (as in the writer prompt)",
         "brand_name": "the tenant brand pack", "forbidden_words": "code-built scope words",
         "taken_titles": "the brand's own earlier titles", "meta_band": "the policy band"}),
    generate.write_faq: (
        {"source", "body", "unused_facts", "paa_questions", "required_questions"},
        {"brand_name": "the tenant brand pack"}),
    grounding.repair: (
        {"tour"},
        {"generated": "the draft under repair", "violations": "our checker's sentences and codes: the task",
         "forbidden": "our forbidden-word list", "still_unsupported": "a checker callback"}),
    ff._rerepair_meta: (
        {"tour", "intent_clue"},
        {"post": "the draft meta under repair", "content": "the draft (its name is fenced with the tour line)",
         "forbidden": "our forbidden-word list"}),
    itinerary_utils.nudge_itinerary_day: (
        {"source_day_text"},
        {"current_title": "the draft day under repair", "current_body": "the draft day under repair",
         "target_word_count": "a code-computed length", "voice_hint": "the tenant brand pack's voice"}),
}


@pytest.mark.parametrize("fn", list(CLASSIFIED), ids=lambda fn: fn.__name__)
def test_every_repair_parameter_is_classified(fn):
    untrusted, trusted = CLASSIFIED[fn]
    params = set(inspect.signature(fn).parameters)
    unclassified = params - untrusted - set(trusted) - set(COMMON)
    assert unclassified == set(), f"{fn.__name__}: classify {sorted(unclassified)} (fence it or say why not)"


# ── 3. off = main ─────────────────────────────────────────────────────────────────────────────────────────────
# sha256 of (system, user) of each call above, rendered by main's builders before this change (60867997c, no fence
# parameter; the fence kwarg dropped). A later deliberate prompt edit updates them in its own PR.
MAIN = [
    ("f6fedf175d70a28162c06a513df4df7a9bf6066b8a2180341cf5ba6936c97a31",  # T9 sentence repair
     "319268d3ce822e09fbb33f9fad909762cf966da03408bfbc87d9fdc1c67e5d8d"),
    ("12b9538ab278d99020ddccf9dc77b911f3a709f931fceef3d5c52db6b0016240",  # T9 extend
     "381cbb51126ddf69c7e31497d272fb47fd1afd448c3637ec5a556fb7fb9bd044"),
    ("026dc6d29bdaeddc9ebb9242213e71f2bf55f720040214c4e54a784bb45ad453",  # T9 extend + background
     "ba309e0d830788be4e7a850347374ac5954d1da6ff22a7bbac1a2dd961be8c18"),
    ("026dc6d29bdaeddc9ebb9242213e71f2bf55f720040214c4e54a784bb45ad453",  # T9 extend, nothing left
     "e981d6e5b9581ab96b1ae6a9584246d518737e6eeac8e8afb357090984e28f7b"),
    ("8b76382c02b0f30a6fb3414089fb56a336465a70de7f6554a6771f55a5c18bba",  # T9 SEO fit
     "1e4e1526a22f16d818220da3fca38612b98c06b69de39f5e3930b9397af829db"),
    ("f77bda99a37f373a676f8fef6c1771d48dd1ddbb31392c04e7c365d4cd34f229",  # T9 FAQ
     "9a8b0c7f0944054793ad574f81576ab287a649bd8ab96ac3255916923b406f98"),
    ("249a364f5352bd5be82570874f702fde9d19a67f7ce816d8d4df3da7166d9110",  # grounding repair
     "3b828dd9e0bc775c46de011474b21099f0e9df493adeac05bb2fc2d2377aeb55"),
    ("46f2360b102227b357cc082f2e67ee9d3b28f661cb4cbea548b1a13fc8f4d91c",  # meta re-repair
     "a64525850a42b082b590094514a920c63b7dc7ae1088890fc22ff3b39de54b58"),
    ("10fade9d0aff801bf5b42e528f75c1edbe93bf4d876a23c93889569c9b173ca2",  # itinerary nudge
     "892ad07289d33ad568097922c6213225ef1a4314a3f6a7392cf93007a4bd08b2"),
    ("46f2360b102227b357cc082f2e67ee9d3b28f661cb4cbea548b1a13fc8f4d91c",  # A1 flag fix
     "c3af69773157abe5a02da66fae89da35282071d6b4c0fa6ddca3a2552d6f0e0a"),
]


def _h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_fence_off_is_main_byte_for_byte():
    assert [(_h(r.system_prompt), _h(r.user_prompt)) for r in _all_requests(False)] == MAIN


def test_the_setting_default_is_off():
    for request in _all_requests(None):
        assert "untrusted_data" not in request.user_prompt and pf.SYSTEM_RULE not in request.system_prompt


# ── 4. wiring ─────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("on", [False, True])
def test_a_none_mode_reads_the_setting(on):
    with _setting(on):
        requests = _t9_requests(None) + [_grounding_request(None), _rerepair_request(None), _nudge_request(None)]
    for request in requests:
        assert (pf.SYSTEM_RULE in request.system_prompt) is on
        if g.MARK.search(request.user_prompt):               # a call with no untrusted value has nothing to fence
            assert ("untrusted_data" in request.user_prompt) is on


@pytest.mark.parametrize("on", [False, True])
def test_the_flag_fix_follows_the_runs_mode(on):
    with _setting(not on):                                   # the run's pinned mode wins over the setting
        request = _flag_fix_request(_fix_state(prompt_fence=on, is_tenant_rewrite=False))
    assert (pf.SYSTEM_RULE in request.system_prompt) is on and ("untrusted_data" in request.user_prompt) is on


def test_the_grounding_repair_gets_the_runs_mode():
    assert "prompt_fence" in grounding.REPAIR_RUN_KEYS
    assert set(grounding.REPAIR_RUN_KEYS) <= set(inspect.signature(grounding.repair_and_recheck).parameters)
    with patch.object(grounding, "repair", return_value={"fields": [], "cost_usd": 0.0}) as rep:
        grounding.repair_and_recheck({}, [_VIOLATION], g._tour(), prompt_fence=True)
    assert rep.call_args.kwargs["fence"] is True


def _calls(path: Path, name: str) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and getattr(n.func, "id", getattr(n.func, "attr", None)) == name]


def test_the_graph_passes_its_mode_to_the_nudges_and_the_repair():
    gen = ROOT / "services/content_generation"
    for path in (gen / "graph.py", gen / "flag_fix_node.py"):
        for call in _calls(path, "nudge_itinerary_day"):
            assert "fence" in {k.arg for k in call.keywords}, f"{path.name}:{call.lineno} nudge without fence="
    for call in _calls(gen / "graph.py", "_process_itineraries") + _calls(gen / "flag_fix_node.py", "_rerepair_meta"):
        assert "fence" in {k.arg for k in call.keywords}, f"{call.lineno}: no fence="
    for path in (gen / "graph.py", gen / "grounding.py"):
        for call in _calls(path, "repair_and_recheck"):
            assert any(k.arg is None for k in call.keywords), f"{path.name}:{call.lineno}: pass REPAIR_RUN_KEYS"


def _has_rule(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "system_rule" for n in ast.walk(node))


@pytest.mark.parametrize("rel", ["services/acp_content_writing/generate.py", "services/content_generation/grounding.py",
                                 "services/content_generation/flag_fix_node.py",
                                 "services/content_generation/itinerary_utils.py"])
def test_every_writer_request_in_these_modules_carries_the_fence_rule(rel):
    """A new writer call added here must fence its untrusted inputs: its system prompt carries system_rule (directly,
    or through a local name assigned from an expression that does)."""
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    found = 0
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        ruled = {t.id for n in ast.walk(fn) if isinstance(n, ast.Assign) and _has_rule(n.value)
                 for t in n.targets if isinstance(t, ast.Name)}
        for call in ast.walk(fn):
            if isinstance(call, ast.Call) and getattr(call.func, "id", None) == "LLMRequest":
                found += 1
                system = next(k.value for k in call.keywords if k.arg == "system_prompt")
                assert _has_rule(system) or (isinstance(system, ast.Name) and system.id in ruled), \
                    f"{rel}:{call.lineno}: LLMRequest without prompt_fence.system_rule"
    assert found, f"{rel}: no LLMRequest found, the guard is blind"
