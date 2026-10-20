"""AA-800 S245 follow-up (owner decision 20/10/2026): the T2 writer prompt carries the tenant's
signature-words line, and rule 1 nudges the writer to use a signature word where the source states that
level, never otherwise.

The live T2 rewrite for Atlas & Hearth (S245, on a source that mentions luxury) wrote "luxury" 0
times: rule 1 only said the word was "yours to use where the facts fit". The first test builds the
prompt the way a live T2 job does — the tenant's `tenant_brand_rules` row as asyncpg returns it →
`t2_rewrite_job._prepare` → `_rewrite_tour`'s initial state → `graph.generate_node` — and reads the
system prompt handed to the LLM client. Mock-only: no AWS, no DB, no LLM.
"""
import asyncio
import json

import pytest

from api.routers import v1_pipeline
from services.acp_shared import signature_vocabulary as sv
from services.content_generation import batch_prompt, graph as graph_mod, prompts
from services.content_generation.forbidden_words import VALIDATE_FORBIDDEN
from services.jobs import t2_rewrite_job

# Atlas & Hearth's active shared.tenant_brand_rules row as `SELECT *` hands it back through asyncpg (jsonb
# as JSON text, no codec: AA-300). T0 values from the S234 pilot fixtures; brand_type is the live text the
# AA-800 notes rely on ("Luxury cultural travel brand" declares "luxury"); custom_validators is empty.
AH_ROW = {
    "tenant_id": "00000000-0000-0000-0000-0000000000ah", "brand_name": "Atlas & Hearth",
    "brand_type": "Luxury cultural travel brand",
    "core_idea": "Private cultural journeys with depth, beauty, and meaning for discerning travellers",
    "customer_segment": "High-net-worth couples, senior executives, luxury travel advisors",
    "customer_mindset": None,
    "voice_examples": json.dumps(["Elegant", "Discreet", "Cultured", "Calm", "Precise", "Atmospheric",
                                  "Intelligent"]),
    "style_guide": None, "forbidden_words": json.dumps(["bucket list"]), "good_examples": None,
    "default_cta": None, "system_prompt": None, "version": 1, "is_active": True, "custom_validators": None,
}
# A published_tours row (`_PT_SQL` shape) whose source states the level: "luxurious" supports "luxury".
PT_ROW = {
    "aa_name": "Bhutan: Paro and Punakha in 4 Days", "aa_subtitle": "Paro valley walks and Punakha Dzong",
    "aa_summary": "A walk through the Paro valley with dzong visits and nights in a luxurious heritage lodge.",
    "aa_description": "", "aa_highlights": "Tiger's Nest hike\nPunakha Dzong",
    "aa_itineraries": "Day 1: Arrive in Paro.\nDay 2: Hike to Tiger's Nest.\nDay 3: Punakha Dzong.\n"
                      "Day 4: Depart.",
    "seo_title": "Bhutan Paro and Punakha tour", "seo_meta": "", "country": "Bhutan", "duration": "4 Days",
}


class _Conn:
    def __init__(self, row):
        self.row, self.queries = row, []

    async def fetchrow(self, sql, *args):
        self.queries.append(sql)
        return self.row


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def __init__(self, row):
        self.conn = _Conn(row)

    def acquire(self):
        return _Acquire(self.conn)


class _CaptureClient:
    """Stands in for LLMClient inside generate_node: keeps the request, sends nothing."""
    requests: list = []

    def generate(self, request):
        _CaptureClient.requests.append(request)
        raise RuntimeError("no LLM in unit tests")


class _GenerateOnlyGraph:
    """The compiled graph _rewrite_tour streams, cut to its first node (generate_node)."""

    async def astream(self, state, stream_mode="updates"):
        yield {"generate": graph_mod.generate_node(state)}


def _live_t2_system_prompt(monkeypatch, row: dict) -> str:
    """The system prompt a live T2 job sends the writer for a tenant whose brand row is `row`."""
    _CaptureClient.requests = []
    monkeypatch.setattr(graph_mod, "LLMClient", _CaptureClient)
    monkeypatch.setattr(v1_pipeline, "build_graph", lambda *a, **kw: _GenerateOnlyGraph())
    monkeypatch.delenv("S1_STRUCTURED_FACTS", raising=False)
    pool = _Pool(row)

    async def run():
        tour, rules = await t2_rewrite_job._prepare(pool, PT_ROW, row["tenant_id"], "EN-GB")
        assert rules["_brand_loaded"] is True
        return await v1_pipeline._rewrite_tour(tour, 1, 1, brand_rules=rules, is_tenant_rewrite=True,
                                               tenant_id=row["tenant_id"], generate_stage="t2_generate")

    out = asyncio.run(run())
    assert out["error"] == "no LLM in unit tests"
    assert "tenant_brand_rules" in pool.conn.queries[0]
    assert len(_CaptureClient.requests) == 1 and _CaptureClient.requests[0].stage == "t2_generate"
    return _CaptureClient.requests[0].system_prompt


def _rule_1(system: str) -> str:
    return system.split("STRICT RULES:\n", 1)[1].split("2. Tour name", 1)[0]


# ── (1) the live T2 prompt carries the signature-words line ──────────────────────────────────────

def test_live_t2_prompt_for_atlas_and_hearth_carries_the_signature_words_line(monkeypatch):
    system = _live_t2_system_prompt(monkeypatch, AH_ROW)
    rule_1 = _rule_1(system)
    assert rule_1.startswith("1. NEVER use these words: curated, refined, tailored, bespoke, cheap, deal, book now, "
                             "instant booking, discount\n")
    assert "luxury" not in rule_1.split("\n", 1)[0]                     # declared: not under NEVER
    assert "This brand's own signature words: luxury." in rule_1
    assert "(luxury: the source says luxury, luxurious or five-star)" in rule_1
    assert "use that signature word at least once, on the fact the source states" in rule_1
    assert "When the source does not state it, leave the word out" in rule_1
    # and nothing later in the prompt tells the writer to avoid it
    hard = system.split("HARD CONSTRAINT: FORBIDDEN WORDS", 1)[1]
    assert "luxury" not in hard and "bucket list" in hard
    assert "Brand type: Luxury cultural travel brand" in system


def test_live_t2_prompt_for_an_explicit_custom_validators_declaration(monkeypatch):
    """A tenant whose T0 text does not name the word declares it in custom_validators instead."""
    row = {**AH_ROW, "brand_type": "Cultural travel brand",
           "custom_validators": json.dumps({"signature_words": ["curated"]})}
    rule_1 = _rule_1(_live_t2_system_prompt(monkeypatch, row))
    assert rule_1.startswith("1. NEVER use these words: refined, tailored, bespoke, luxury, cheap")
    assert "This brand's own signature words: curated." in rule_1
    assert "(curated: the source says curated)" in rule_1


def test_live_t2_prompt_without_signature_words_is_the_base_rule(monkeypatch):
    row = {**AH_ROW, "brand_type": "Cultural travel brand"}
    system = _live_t2_system_prompt(monkeypatch, row)
    assert "signature word" not in system
    assert _rule_1(system) == _rule_1(prompts.T2_SYSTEM_PROMPT)


# ── (2) rule 1: use a signature word where the source states it, never otherwise ─────────────────

def test_rule_1_names_the_source_wording_for_every_declared_word():
    pack = {"brand_type": "Luxury travel brand", "signature_words": ["curated", "refined", "tailored", "bespoke"]}
    rule_1 = sv.t2_rule_1(pack)
    assert rule_1.startswith("1. NEVER use these words: cheap, deal, book now, instant booking, discount\n")
    assert ("(curated: the source says curated; refined: the source says refined; tailored: the source says "
            "tailored, tailor-made, bespoke, customised or personalised; bespoke: the source says bespoke, "
            "tailor-made, customised or personalised; luxury: the source says luxury, luxurious or "
            "five-star)") in rule_1
    assert "Source fidelity (rule 6) comes first" in rule_1


@pytest.mark.parametrize("word", sv.POSITIONING)
def test_every_source_cue_is_support_the_evidence_rule_accepts(word):
    """What rule 1 calls "the source states it" is what service_level_claims accepts as support, so a
    claim the writer makes on a named cue never turns into a T3 grounding finding."""
    for cue in sv._SUPPORT_CUES[word]:
        assert sv._supported(word, [f"Nights in a {cue} lodge."]), (word, cue)
        assert sv.service_level_claims(f"A {word} lodge by the river.", f"Nights in a {cue} lodge.") == []
    assert sv.service_level_claims(f"A {word} lodge by the river.", "Nights in a farmhouse lodge.")


@pytest.mark.parametrize("weak", ["premium", "deluxe"])
def test_weak_luxury_roots_are_evidence_only_not_prompt_cues(weak):
    """Review finding (S245): "premium supplement" or "deluxe room" at a mid-range hotel does not state a
    luxury level, so rule 1 must not name them as a reason to write "luxury". The gate is unchanged: they
    stay accepted roots, so an existing tour on such a source does not start failing T3."""
    assert weak not in sv._SUPPORT_CUES["luxury"]
    assert weak not in sv.t2_rule_1({"brand_type": "Luxury cultural travel brand"})
    assert sv.service_level_claims("A luxury lodge by the river.", f"Nights in a {weak} room.") == []


def test_the_nudge_leaves_the_gates_unchanged():
    """The nudge is prompt-only: the word lists, the evidence rule and the T9 WORDS line are as before."""
    ah = {"brand_type": "Luxury cultural travel brand", "forbidden_words": ["bucket list"]}
    assert VALIDATE_FORBIDDEN[:5] == ["curated", "pristine", "refined", "tailored", "bespoke"]
    assert sv.tenant_blocked_words(ah) == ["cheap", "deal", "book now", "instant booking", "discount", "curated",
                                           "refined", "tailored", "bespoke", "bucket list"]
    assert sv.service_level_claims("Nights in a luxury lodge.", "a farmhouse lodge") == [
        {"word": "luxury", "phrase": "luxury lodge"}]
    assert "signature words, yours to use where the content seed fits: luxury." in sv.t9_words_line(ah)


def test_a1_prompt_has_no_nudge():
    a1 = batch_prompt.build_s1_system_prompt({"brand_type": "Luxury cultural travel brand", "seo": {},
                                              "few_shots": [], "tour": {}})
    assert prompts._RULE_1_CORE_FORBIDDEN in a1 and "signature word" not in a1
