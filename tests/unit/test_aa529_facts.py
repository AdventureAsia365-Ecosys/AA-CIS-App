"""AA-529 — services/acp_content_writing/facts.py (new module) + its wiring into
prompts.py::build_user_prompt() and service.py's start_write()/run_write_background(). Same
mocking conventions as test_aa450_content_writing_{generate,service}.py.

Confirmed real gap this closes (AA-529 issue, piece c771a4d5/7ca09d4b, tenant wanderlux-travel):
F1_grounding permanently blocks a claim not present in a tour's own itinerary (price, season,
visa, transfer time) because there was never anything to cite it against. A Facts Entry is a
hand-written, sourced claim for exactly this case — scope='platform' (every tenant) or
scope='tenant' (only that tenant), cited via [F:<fact_id>] (same TAG_RE that already accepted
this prefix, previously unused)."""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_content_writing import service
from services.acp_content_writing.facts import fetch_facts_for_writing, format_facts_block
from services.acp_content_writing.prompts import build_user_prompt
from services.acp_angle_gate.goals import get_goal
from services.acp_angle_gate.channel_style import get_channel_style

TENANT_ID = uuid.uuid4()
REQUEST_ID = uuid.uuid4()
PIECE_ID = uuid.uuid4()
GOAL = get_goal("promotion")
ANGLE = {"name": "A", "why_it_works": "wa", "formula_fit": "AIDA", "best_final_style": "warm",
         "idx": 0, "recommended": True, "chosen": True}
CHANNEL_STYLE = get_channel_style("facebook")
BRAND_AUDIENCE = {"customer_segment": "Senior execs", "customer_mindset": "seek depth"}


def _request(**over):
    base = {
        "request_id": str(REQUEST_ID), "tenant_id": str(TENANT_ID), "atom_id": "atom_abc123",
        "trip_id": None, "channel": "facebook", "goal": "promotion", "cta": "Book a consultation",
        "status": "approved", "created_at": "2026-08-24T00:00:00", "updated_at": "2026-08-24T00:00:00",
        "angles": [ANGLE], "route_segment_ids": None, "dfs_paa_snapshot": None,
    }
    base.update(over)
    return base


def _placeholder_row(**over):
    import datetime as _dt
    base = {
        "piece_id": PIECE_ID, "tenant_id": TENANT_ID, "angle_gate_request_id": REQUEST_ID,
        "attempt_number": 1, "content_text": "", "status": "processing",
        "held_reason": None, "gate_ledger": [], "repair_log": [],
        "created_at": _dt.datetime.now(_dt.timezone.utc),
    }
    base.update(over)
    return base


def _passing_outcome():
    ledger = [{"gate": "F6_cta_present", "passed": True, "violations": [], "repairable": True}]
    return {"passed": True, "gate_ledger": ledger, "first_failure": None, "flags": []}


def _write_context(**over):
    base = {
        "atom_text": "Don Det and Don Khone islands — cross the French colonial-era bridge",
        "goal": GOAL, "channel_style": CHANNEL_STYLE,
        "brand_audience": BRAND_AUDIENCE, "chosen": ANGLE, "cta": "Book a consultation",
        "destination": None, "trip_name": None, "brand_rubric_text": "rubric",
        "channel": "facebook", "atom_id": "atom_abc123",
        "facts_text": "[Fact id=fact_price100]\nReference price: About $100 for 5 days.",
    }
    base.update(over)
    return base


def _make_pool(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


class TestFetchFactsForWriting:
    @pytest.mark.asyncio
    async def test_queries_platform_or_own_tenant_scope(self):
        conn = AsyncMock()
        conn.fetch.return_value = [
            {"fact_id": "fact_1", "scope": "platform", "tenant_id": None,
             "title": "Visa", "body": "E-visa costs $30.", "stated_on": None, "provenance": "Admin"},
        ]
        pool = _make_pool(conn)
        facts = await fetch_facts_for_writing(TENANT_ID, pool)
        assert facts == [
            {"fact_id": "fact_1", "scope": "platform", "tenant_id": None,
             "title": "Visa", "body": "E-visa costs $30.", "stated_on": None, "provenance": "Admin"},
        ]
        query, tenant_arg, country_arg = conn.fetch.call_args.args
        assert "scope = 'platform'" in query
        assert "scope = 'tenant' AND tenant_id = $1" in query
        assert "lower(country) = lower($2::text)" in query and country_arg is None
        # AA-501-class "uuid = text" bug this codebase already hit once — the real UUID object is
        # passed, not str(tenant_id), and it's the ONLY thing bound to $1 in this query (no reuse
        # against a text column), so no ambiguity to repeat that bug here.
        assert tenant_arg == TENANT_ID

    @pytest.mark.asyncio
    async def test_empty_is_not_an_error(self):
        conn = AsyncMock()
        conn.fetch.return_value = []
        pool = _make_pool(conn)
        facts = await fetch_facts_for_writing(TENANT_ID, pool)
        assert facts == []


class TestFormatFactsBlock:
    def test_empty_list_returns_empty_string(self):
        assert format_facts_block([]) == ""

    def test_labels_each_fact_with_its_own_id(self):
        facts = [
            {"fact_id": "fact_abc123", "title": "Reference price",
             "body": "A comfortable 5-day Southern Laos trip runs about $100-150 per person."},
            {"fact_id": "fact_def456", "title": "Visa",
             "body": "Most nationalities get a 30-day e-visa for about $30."},
        ]
        block = format_facts_block(facts)
        assert "[Fact id=fact_abc123]" in block
        assert "[Fact id=fact_def456]" in block
        assert "Reference price: A comfortable 5-day" in block
        assert "Visa: Most nationalities" in block


class TestBuildUserPromptFactsIntegration:
    def _channel(self, key="facebook"):
        return get_channel_style(key)

    def test_facts_text_appended_to_flat_content_seed(self):
        prompt = build_user_prompt(
            content_seed="Don Det and Don Khone islands — cross the French colonial-era bridge",
            goal=GOAL, channel_style=self._channel(), brand_audience=BRAND_AUDIENCE,
            angle=ANGLE, cta="Book a consultation",
            facts_text="[Fact id=fact_price100]\nReference price: About $100 for 5 days.",
        )
        assert "Don Det and Don Khone" in prompt
        assert "[Fact id=fact_price100]" in prompt
        assert "$100 for 5 days" in prompt
        assert "FACTS" in prompt

    def test_no_facts_text_is_a_no_op(self):
        prompt = build_user_prompt(
            content_seed="Don Det and Don Khone islands", goal=GOAL,
            channel_style=self._channel(), brand_audience=BRAND_AUDIENCE, angle=ANGLE,
            cta="Book a consultation", facts_text=None,
        )
        assert "[Fact id=" not in prompt

    def test_facts_text_appended_after_route_aware_moments(self):
        route_segments = [("atom_1", "Moment one text"), ("atom_2", "Moment two text")]
        prompt = build_user_prompt(
            content_seed="ignored in route-aware branch", goal=GOAL,
            channel_style=self._channel("blog"), brand_audience=BRAND_AUDIENCE, angle=ANGLE,
            cta="Book a consultation", route_segments=route_segments,
            facts_text="[Fact id=fact_season]\nSeason: Dry season Nov-Apr.",
        )
        assert "[Moment id=atom_1]" in prompt
        assert "[Moment id=atom_2]" in prompt
        assert "[Fact id=fact_season]" in prompt
        assert "Dry season Nov-Apr" in prompt

    def test_blog_instructions_mention_fact_tag(self):
        prompt = build_user_prompt(
            content_seed="seed text", goal=GOAL, channel_style=self._channel("blog"),
            brand_audience=BRAND_AUDIENCE, angle=ANGLE, cta="Book now", atom_id="atom_xyz",
            facts_text="[Fact id=fact_1]\nTitle: body",
        )
        assert "[F:<fact id>]" in prompt or "[F:" in prompt


@pytest.mark.asyncio
class TestStartWriteFactsIntegration:
    """start_write() marks Facts pending; resolve_facts() (run in the t9_write job, AA-764)
    fetches them once and carries them in `context`."""

    async def test_facts_text_landed_in_context(self):
        conn = AsyncMock()
        conn.fetchrow.side_effect = [{"text": "atom text"}, _placeholder_row()]
        conn.fetch.return_value = [
            {"fact_id": "fact_price100", "scope": "platform", "tenant_id": None,
             "title": "Reference price", "body": "About $100 for 5 days.",
             "stated_on": None, "provenance": "Admin, based on the published rate card"},
        ]
        pool = _make_pool(conn)

        with patch.object(service.angle_gate_service, "fetch_request", new=AsyncMock(return_value=_request())), \
             patch.object(service, "fetch_brand_audience", new=AsyncMock(return_value=BRAND_AUDIENCE)), \
             patch.object(service, "fetch_brand_rubric_text", new=AsyncMock(return_value="rubric")), \
             patch.object(service, "get_goal", return_value=GOAL):
            result = await service.start_write(TENANT_ID, REQUEST_ID, pool)
        assert result["context"]["facts_pending"] is True  # AA-764 — not selected in the request
        assert result["context"]["facts_text"] is None
        await service.resolve_facts(REQUEST_ID, result["context"], pool)

        assert result["context"]["facts_pending"] is False
        assert "[Fact id=fact_price100]" in result["context"]["facts_text"]
        assert "About $100 for 5 days" in result["context"]["facts_text"]
        # ALL platform + own-tenant facts, never another tenant's — enforced by the query itself
        # (fetch_facts_for_writing()'s own test covers the query text); here just confirm the
        # real UUID tenant_id was what start_write() actually passed through.
        fetch_call = conn.fetch.call_args
        assert fetch_call.args[1] == TENANT_ID

    async def test_no_facts_written_yet_is_empty_string_not_none(self):
        conn = AsyncMock()
        conn.fetchrow.side_effect = [{"text": "atom text"}, _placeholder_row()]
        conn.fetch.return_value = []  # no Facts Entry exists for anyone yet
        pool = _make_pool(conn)

        with patch.object(service.angle_gate_service, "fetch_request", new=AsyncMock(return_value=_request())), \
             patch.object(service, "fetch_brand_audience", new=AsyncMock(return_value=BRAND_AUDIENCE)), \
             patch.object(service, "fetch_brand_rubric_text", new=AsyncMock(return_value="rubric")), \
             patch.object(service, "get_goal", return_value=GOAL):
            result = await service.start_write(TENANT_ID, REQUEST_ID, pool)
        await service.resolve_facts(REQUEST_ID, result["context"], pool)  # AA-764

        assert result["context"]["facts_text"] == ""


@pytest.mark.asyncio
class TestRunWriteBackgroundFactsIntegration:
    """The real reported bug (AA-529): a piece opening on a price claim ("$100 in Laos") held
    forever because F1_grounding's atom_text had no digits in it at all. These tests confirm
    facts_text (a) reaches write_content()'s prompt-building call and (b) is folded into
    run_quality_gates()'s own atom_text argument, so a Facts-sourced number is treated as
    grounded."""

    async def test_facts_text_passed_to_write_content(self):
        with patch.object(service, "write_content", return_value=("piece", 0.02, {}, None)) as mock_write, \
             patch.object(service, "run_quality_gates", return_value=_passing_outcome()), \
             patch.object(service, "_finalize_piece", new=AsyncMock(return_value=_placeholder_row(status="approved"))):
            await service.run_write_background(REQUEST_ID, PIECE_ID, _write_context(), pool=MagicMock())

        assert mock_write.call_args.kwargs["facts_text"] == _write_context()["facts_text"]

    async def test_facts_text_folded_into_grounding_gate_atom_text(self):
        with patch.object(service, "write_content", return_value=("piece", 0.02, {}, None)), \
             patch.object(service, "run_quality_gates", return_value=_passing_outcome()) as mock_gates, \
             patch.object(service, "_finalize_piece", new=AsyncMock(return_value=_placeholder_row(status="approved"))):
            await service.run_write_background(REQUEST_ID, PIECE_ID, _write_context(), pool=MagicMock())

        gate_atom_text = mock_gates.call_args.kwargs["atom_text"]
        # The gate's own view of the source text must contain BOTH the original atom text AND
        # the Facts block — this is what lets a claim like "$100" pass gate_grounding() when the
        # atom's own text (real example: "Don Det and Don Khone islands...") has no digits at all.
        assert "Don Det and Don Khone" in gate_atom_text
        assert "About $100 for 5 days" in gate_atom_text

    async def test_no_facts_text_leaves_grounding_gate_atom_text_unchanged(self):
        """Backward compatible: a request with zero Facts (the common case today, nothing
        written yet) must not change gate_grounding()'s existing behavior at all."""
        with patch.object(service, "write_content", return_value=("piece", 0.02, {}, None)), \
             patch.object(service, "run_quality_gates", return_value=_passing_outcome()) as mock_gates, \
             patch.object(service, "_finalize_piece", new=AsyncMock(return_value=_placeholder_row(status="approved"))):
            await service.run_write_background(
                REQUEST_ID, PIECE_ID, _write_context(facts_text=""), pool=MagicMock(),
            )

        # AA-768 — the gate text now also carries the audience/trip lines (service._grounding_text);
        # with no Facts it is still the atom text first, and no Facts block is added.
        gate_atom_text = mock_gates.call_args.kwargs["atom_text"]
        assert gate_atom_text.startswith(_write_context()["atom_text"])
        assert "[Fact id=" not in gate_atom_text
