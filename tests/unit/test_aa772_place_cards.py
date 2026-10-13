"""AA-772 (S233) — place cards, seed with card/day/labelled siblings, scope line, trimmed evidence."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from services.acp_content_writing import service
from services.acp_shared import place_cards as pc
from services.acp_shared.atom_seed import MAX_EVIDENCE_CHARS, compose_seed, trim_evidence
from shared.llm_client.models import LLMResponse

ATOM = {"atom_id": "a1", "place": "Pawangka Monastery", "country": "China", "itinerary_day": 4,
        "tour_name": "Mount Kailash", "evidence": "Visit Pawangka Monastery, built in the seventh century."}
CARD = {"display_name": "Pabongka Monastery", "locality": "Lhasa", "region": "Tibet", "country": "China",
        "naming_note": "Also written Pawangka.",
        "facts": [{"text": "It sits on a hillside north of Lhasa.", "kind": "setting"},
                  {"text": "It is said to date from the 7th century.", "kind": "history"},
                  {"text": "It has 5000 monks today.", "kind": "what_it_is"}]}


def _resp(content) -> LLMResponse:
    return LLMResponse(content=json.dumps(content), model_used="m", provider="p", cost_usd=0.01)


def _pool(conn):
    pool = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = ctx
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    return pool


def test_place_key_same_place_same_key():
    # S234: locality is no longer part of the key (LLM free text split one lake into 5 cards).
    assert pc.place_key("Pabongka Monastery", "China") == pc.place_key("pabongka  monastery", "china")
    assert pc.place_key("Lake Khövsgöl", "Mongolia") == pc.place_key("Khövsgöl Lake", "Mongolia")


class TestBuildCard:
    def _run(self, card, verdicts, identity_ok=True):
        conn = AsyncMock()
        conn.fetchval.return_value = "card-uuid"
        client = MagicMock()
        client.generate.side_effect = [_resp(card), _resp({"identity_ok": identity_ok, "verdicts": verdicts})]
        with patch.object(pc, "LLMClient", return_value=client), \
                patch.object(pc, "record_call_with_pool", new=AsyncMock()) as log:
            out = asyncio.run(pc.build_card_for_atom(dict(ATOM), _pool(conn)))
        return out, conn, client, log

    def test_only_supported_facts_are_kept(self):
        out, conn, client, log = self._run(CARD, ["supported", "supported", "wrong"])
        assert out == "card-uuid"
        insert_args = conn.fetchval.await_args.args
        kept = json.loads(insert_args[7])
        assert [f["text"] for f in kept] == [CARD["facts"][0]["text"], CARD["facts"][1]["text"]]
        assert insert_args[2] == "Pabongka Monastery" and insert_args[3] == "Lhasa"
        stages = [c.args[0].stage for c in client.generate.call_args_list]
        assert stages == ["a3_place_card", "a3_place_card_check"]
        assert [c.kwargs["stage"] for c in log.await_args_list] == ["a3_place_card", "a3_place_card_check"]
        link = conn.executemany.await_args_list[0]
        assert "atom_place_card" in link.args[0] and link.args[1] == [("a1", "card-uuid")]

    def test_misaligned_verdicts_keep_nothing(self):
        _, conn, _, _ = self._run(CARD, ["supported"])
        assert json.loads(conn.fetchval.await_args.args[7]) == []

    def test_rejected_identity_links_nothing(self):
        out, conn, _, _ = self._run(CARD, ["supported"] * 3, identity_ok=False)
        assert out is None
        conn.fetchval.assert_not_awaited()

    def test_generic_place_makes_no_card(self):
        conn = AsyncMock()
        client = MagicMock()
        client.generate.return_value = _resp({"display_name": "", "facts": []})
        with patch.object(pc, "LLMClient", return_value=client), \
                patch.object(pc, "record_call_with_pool", new=AsyncMock()):
            assert asyncio.run(pc.build_card_for_atom(dict(ATOM, place="the market"), _pool(conn))) is None
        assert client.generate.call_count == 1


def test_ensure_skips_linked_atoms_and_soft_fails():
    conn = AsyncMock()
    conn.fetch.side_effect = [[{"atom_id": "a1"}], [dict(ATOM, atom_id="a2")], [], [], []]
    with patch.object(pc, "build_card_for_group", new=AsyncMock(side_effect=RuntimeError("boom"))) as build, \
            patch.object(pc, "_record_miss", new=AsyncMock()) as miss:
        out = asyncio.run(pc.ensure_place_cards(["a1", "a2", "a2"], _pool(conn)))
    assert out["built"] == 0 and out["errors"] == 1 and out["already_linked"] == 1
    assert build.await_count == 1
    assert miss.await_args.args[3] == "error"
    assert conn.fetch.await_args_list[1].args[1] == ["a2"]


class TestSeed:
    def test_card_replaces_location_and_adds_background(self):
        seed = compose_seed("Pawangka Monastery — visit", "Visit it.", None, None, [], place="Pawangka Monastery",
                            country="China", day=4, card=dict(CARD, facts=json.dumps(CARD["facts"][:1])))
        assert "Location: Pabongka Monastery, Lhasa, Tibet, China (the itinerary calls it Pawangka Monastery)" in seed
        assert "Naming: Also written Pawangka." in seed
        assert "- It sits on a hillside north of Lhasa." in seed
        assert "Day 4 of the trip" in seed

    def test_siblings_are_labelled_other_trips(self):
        seed = compose_seed("x", None, None, None, ["Sib."], country="Laos")
        assert "never promise these details for this trip" in seed

    def test_whole_day_evidence_is_trimmed_to_the_place(self):
        body = "Morning drive along the river. " * 10 + "Visit Pawangka Monastery on the hill. " \
               "Then lunch in town. " + "Afternoon rest at the hotel. " * 20
        out = trim_evidence(body, "Pawangka Monastery")
        assert out.startswith("Visit Pawangka Monastery") and len(out) <= MAX_EVIDENCE_CHARS
        assert trim_evidence("Short.", "x") == "Short."


class TestScope:
    def _run(self, rows, ids):
        conn = AsyncMock()
        conn.fetch.return_value = rows
        return asyncio.run(service._scope_line(ids, _pool(conn)))

    def test_route_scope_lists_days_and_places_in_order(self):
        line = self._run([{"atom_id": "b", "itinerary_day": 3, "place": "Nong Khiaw"},
                          {"atom_id": "a", "itinerary_day": 1, "place": "Luang Prabang"}], ["a", "b"])
        assert "days 1–3" in line and "Luang Prabang → Nong Khiaw" in line and "even if the TRIP name" in line

    def test_single_moment_scope(self):
        line = self._run([{"atom_id": "a", "itinerary_day": 4, "place": "Pawangka Monastery"}], ["a"])
        assert line.startswith("SCOPE: this piece is about one moment of the trip (day 4)")

    def test_scope_failure_is_soft(self):
        conn = AsyncMock()
        conn.fetch.side_effect = RuntimeError("db")
        assert asyncio.run(service._scope_line(["a"], _pool(conn))) is None


def test_scope_reaches_the_writer_prompt():
    from services.acp_angle_gate.channel_style import get_channel_style
    from services.acp_angle_gate.goals import get_goal
    from services.acp_content_writing.prompts import build_user_prompt
    prompt = build_user_prompt(content_seed="seed", goal=get_goal("promotion"),
                               channel_style=get_channel_style("blog"), brand_audience={},
                               angle={"name": "a", "why_it_works": "w", "best_final_style": "s"}, cta="c",
                               trip_name="Laos to Vietnam", scope="SCOPE: days 1–3")
    assert "SCOPE: days 1–3" in prompt and prompt.index("TRIP:") < prompt.index("SCOPE:")


def test_resolve_facts_rereads_seed_and_never_builds_cards():
    """S234 (plan C8): T9 only reads place cards — no draft call path is left in the T9 job."""
    conn = AsyncMock()
    ctx = {"facts_pending": True, "tenant_id": "00000000-0000-0000-0000-0000000000aa", "atom_id": "a1",
           "atom_text": "old seed", "destination": "China"}
    with patch.object(service, "atom_seed_text", new=AsyncMock(return_value="new seed with card")), \
            patch.object(service, "fetch_facts_for_writing", new=AsyncMock(return_value=[])) as facts:
        asyncio.run(service.resolve_facts("r", ctx, _pool(conn)))
    assert ctx["atom_text"] == "new seed with card"
    assert facts.await_args.kwargs["country"] == "China"
    assert not hasattr(service, "ensure_place_cards")


def test_resolve_facts_rereads_every_route_moment():
    conn = AsyncMock()
    ctx = {"facts_pending": True, "tenant_id": "00000000-0000-0000-0000-0000000000aa", "atom_id": "a1",
           "route_segments": [("a1", "m1"), ("a2", "m2")], "atom_text": "m1\n\nm2", "destination": "Laos"}
    with patch.object(service, "atom_seed_text", new=AsyncMock(side_effect=["card m1", None])), \
            patch.object(service, "fetch_facts_for_writing", new=AsyncMock(return_value=[])):
        asyncio.run(service.resolve_facts("r", ctx, _pool(conn)))
    assert ctx["route_segments"] == [("a1", "card m1"), ("a2", "m2")]
    assert ctx["atom_text"] == "card m1\n\nm2"
