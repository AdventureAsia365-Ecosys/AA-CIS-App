"""AA-741 S271 review — a Segment-grain Blog walk is written about its Subject's own moment.

Review finding: with Segment grain every Bar-clearing place on one Route became its own Blog Subject, and each pick
stored the same walk. T8 wrote its angles from the digest of the whole walk (every rich moment an ANCHOR CANDIDATE) and
T9 from the same moments, so sibling Subjects of one Route were likely to give near-identical articles, and a Subject
whose own moment was thin got an article about other places. Now:

- T8 (route_digest): the Subject's moment is the FOCUS, the other rich moments CONTEXT; an angle that leads with any
  other moment is flagged like a thin anchor (re-ranked; three of them get the one retry).
- T9 (prompts): the moment is marked FOCUS in the CONTENT SEED and the WALK FOCUS rule follows the route format.
- T8 and T9 apply one rule to the same blocks (walk_focus.usable_focus): focus on the walk and not thin, else a
  single-place piece. A Route-grain Subject or no Subject keeps the whole walk, as before.
- The Slate does not propose a Blog Subject whose own seed is thin (slate._fetch_thin_walk_segments).

No DB, no LLM. Invented places and ids only.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.acp_angle_gate import generate as t8_generate
from services.acp_angle_gate.channel_style import get_channel_style
from services.acp_angle_gate.goals import get_goal
from services.acp_angle_gate.route_digest import render_digest, route_moments, thin_anchor, with_focus
from services.acp_content_writing import prompts as t9_prompts
from services.acp_shared import slate, walk_focus
from services.acp_shared.slate import Candidate, _choose_slate

TOUR = "0a0a0a0a-0000-4000-8000-0000000000a1"
PASS = ("atom-p", "Mountain crossing on foot\nLocation: Corvin Pass, Valdoria\nDay 2 of the trip\n"
                  "Source itinerary: Cross the pass on foot with a local guide. Lunch at the mountain hut. "
                  "Descend by the old mule road.")
LAKE = ("atom-l", "Shore walk and ferry\nLocation: Lake Aurel, Valdoria\nDay 3 of the trip\n"
                  "Source itinerary: Walk the shore path to the ferry pier. Boat to Tamsel Isle. Visit the chapel.")
FORD = ("atom-f", "River wading\nLocation: Mirsa Ford, Valdoria\nDay 4 of the trip\n"
                  "Source itinerary: Cross the ford.")                                     # 1 fact: thin
WALK = [PASS, LAKE, FORD]


# ── the shared rule ──────────────────────────────────────────────────────────────────────────────────

def test_usable_focus_needs_a_walk_a_moment_on_it_and_facts():
    assert walk_focus.usable_focus(WALK, 1) == 1
    assert walk_focus.usable_focus(WALK, 2) is None            # the Subject's own moment is thin
    assert walk_focus.usable_focus(WALK, None) is None          # not on the walk (dropped, excluded, rebuilt away)
    assert walk_focus.usable_focus(WALK, 7) is None
    assert walk_focus.usable_focus([LAKE], 0) is None           # one moment is not a walk
    assert walk_focus.usable_focus([], 0) is None and walk_focus.usable_focus(None, 0) is None


def test_focus_block_index_finds_a_merged_member():
    groups = [["atom-p"], ["atom-l", "atom-l2"], ["atom-f"]]
    assert walk_focus.focus_block_index(groups, "atom-l2") == 1
    assert walk_focus.focus_block_index(groups, "atom-x") is None
    assert walk_focus.focus_block_index(groups, None) is None


@pytest.mark.asyncio
async def test_subject_focus_segment_resolves_the_live_id_and_is_none_for_a_route_subject():
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value="seg-old")
    with patch.object(walk_focus, "resolve_segment_aliases", new=AsyncMock(return_value={"seg-old": "seg-live"})):
        assert await walk_focus.subject_focus_segment(conn, uuid4(), uuid4()) == "seg-live"
    conn.fetchval = AsyncMock(return_value=None)                 # Route Subject: segment_id NULL
    assert await walk_focus.subject_focus_segment(conn, uuid4(), uuid4()) is None
    assert await walk_focus.subject_focus_segment(conn, uuid4(), None) is None


@pytest.mark.asyncio
async def test_seed_is_thin_counts_card_background_and_source_sentences():
    parts = {"text": "River wading", "evidence": "Cross the ford.", "hook": None, "season": None,
             "place": "Mirsa Ford", "country": "Valdoria", "day": 4, "card": None}
    with patch.object(walk_focus, "atom_seed_parts", new=AsyncMock(return_value=parts)):
        assert await walk_focus.seed_is_thin(MagicMock(), "atom-f", uuid4()) is True
    rich = {**parts, "evidence": "Cross the ford at dawn. Walk to the mill. Lunch by the weir."}
    with patch.object(walk_focus, "atom_seed_parts", new=AsyncMock(return_value=rich)):
        assert await walk_focus.seed_is_thin(MagicMock(), "atom-f", uuid4()) is False
    with patch.object(walk_focus, "atom_seed_parts", new=AsyncMock(return_value=None)):
        assert await walk_focus.seed_is_thin(MagicMock(), "atom-f", uuid4()) is True


# ── T8: the digest and the anchor check ─────────────────────────────────────────────────────────────

def test_sibling_subjects_on_one_route_get_different_digests_and_one_anchor_each():
    moments = route_moments(WALK)
    on_pass, on_lake = render_digest(with_focus(moments, 0)), render_digest(with_focus(moments, 1))
    assert on_pass != on_lake
    assert "gives it the most room): Corvin Pass" in on_pass and "gives it the most room): Lake Aurel" in on_lake
    assert "never the hook, the title or the climax): Lake Aurel" in on_pass
    assert "never the hook, the title or the climax): Corvin Pass" in on_lake
    for digest in (on_pass, on_lake):
        assert "ANCHOR CANDIDATES" not in digest
        assert digest.count("FOCUS (") == 1
        assert "THIN MOMENTS" in digest and digest.rstrip().endswith("Mirsa Ford")


def test_a_walk_without_focus_keeps_the_whole_walk_digest():
    digest = render_digest(route_moments(WALK))
    assert "ANCHOR CANDIDATES (3 or more source facts — every angle leads with one of these): Corvin Pass; Lake Aurel" \
        in digest
    assert "FOCUS" not in digest and "CONTEXT" not in digest


def test_an_angle_leading_with_another_moment_is_flagged_on_a_focus_walk():
    moments = with_focus(route_moments(WALK), 1)
    on_pass = {"name": "Over Corvin Pass", "best_final_style": "Open on the climb."}
    on_lake = {"name": "The Lake Aurel Shore", "best_final_style": "Open on the shore path."}
    assert thin_anchor(on_pass, moments) == "Corvin Pass (not the focus)"
    assert thin_anchor(on_lake, moments) is None
    # without a focus the same angle is a fine anchor (the old Route behaviour)
    assert thin_anchor(on_pass, route_moments(WALK)) is None


def test_three_off_focus_angles_get_the_focus_retry_feedback():
    moments = with_focus(route_moments(WALK), 1)
    angles = [{"name": "Over Corvin Pass", "best_final_style": "Open on the climb."}] * 3
    out = t8_generate.angle_contract_violations(angles, {}, moments)
    assert out and out[0].startswith("every angle leads with a moment other than the FOCUS (Corvin Pass (not the "
                                     "focus))")
    assert "lead with the FOCUS, Lake Aurel" in out[0]
    assert "—" not in out[0]


def test_t8_system_prompt_names_the_focus_rule():
    from services.acp_angle_gate.prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_R8
    for prompt in (SYSTEM_PROMPT, SYSTEM_PROMPT_R8):
        assert "If the ROUTE names a FOCUS, the piece is about that moment: every angle leads with the FOCUS" in prompt


def _t8_req(subject_id="subj-1"):
    return {"request_id": "req-1", "tenant_id": "t1", "atom_id": "atom-l", "trip_id": TOUR, "channel": "blog",
            "goal": None, "status": "pending_goal", "dfs_paa_snapshot": None, "subject_id": subject_id,
            "route_segment_ids": json.dumps(["seg-pass", "seg-lake", "seg-ford"])}


def _fetch_with_focus(segments, index_of):
    async def fetch(tenant_id, trip_id, segment_ids, pool, atoms_out=None, *, focus_segment_id=None,
                    focus_out=None):
        if focus_out is not None:
            focus_out.append(index_of.get(focus_segment_id))
        return segments
    return fetch


def _angle_inputs(req, focus_segment, segments=WALK):
    from services.acp_angle_gate import service as ag
    search = AsyncMock(return_value=(None, None))
    with patch.object(ag, "request_route_segment_ids",
                      new=AsyncMock(return_value=["seg-pass", "seg-lake", "seg-ford"])), \
         patch.object(ag, "_fetch_atom_for_tenant", new=AsyncMock(return_value={"text": "single seed"})), \
         patch.object(ag, "fetch_brand_audience", new=AsyncMock(return_value={})), \
         patch("services.acp_content_writing.service._fetch_route_segments",
               new=_fetch_with_focus(segments, {"seg-pass": 0, "seg-lake": 1, "seg-ford": 2})), \
         patch.object(ag.walk_focus, "subject_focus_segment", new=AsyncMock(return_value=focus_segment)), \
         patch.object(ag, "fetch_tenant_trips", new=AsyncMock(return_value=[])), \
         patch.object(ag, "fetch_search_demand_signal", new=AsyncMock(return_value=None)), \
         patch.object(ag, "_blog_search_demand", new=search), \
         patch.object(ag, "fetch_piece_history", new=AsyncMock(return_value=[])):
        inputs = asyncio.run(ag.angle_inputs("t1", req, {"key": "promotion"}, MagicMock()))
    return inputs, search


def test_t8_sibling_requests_get_their_own_focus():
    on_pass, _ = _angle_inputs(_t8_req("subj-pass"), "seg-pass")
    on_lake, _ = _angle_inputs(_t8_req("subj-lake"), "seg-lake")
    assert on_pass.content_seed != on_lake.content_seed
    assert "most room): Corvin Pass" in on_pass.content_seed and "most room): Lake Aurel" in on_lake.content_seed
    assert [m["focus"] for m in on_pass.moments] == [True, False, False]
    assert on_pass.generate_kwargs()["is_route"] is True


def test_t8_thin_or_missing_focus_is_a_single_place_piece():
    for focus in ("seg-ford", "seg-gone"):
        inputs, search = _angle_inputs(_t8_req(), focus)
        assert inputs.content_seed == "single seed" and inputs.moments is None
        assert "is_route" not in inputs.generate_kwargs()
        assert search.await_args.args[0]["route_segment_ids"] == []    # only the Subject's own questions


def test_t8_route_subject_keeps_the_whole_walk():
    inputs, _ = _angle_inputs(_t8_req("subj-route"), None)
    assert "ANCHOR CANDIDATES" in inputs.content_seed and "FOCUS" not in inputs.content_seed
    assert all("focus" not in m for m in inputs.moments)


# ── T9: the prompt ──────────────────────────────────────────────────────────────────────────────────

GOAL = get_goal("promotion")
ANGLE = {"name": "The Lake Aurel Shore", "why_it_works": "w", "formula_fit": "f", "best_final_style": "Plain."}


def _t9_prompt(**kw):
    return t9_prompts.build_user_prompt(content_seed="x", goal=GOAL, channel_style=get_channel_style("blog"),
                                        brand_audience={"brand_name": "Pine Lantern Trips"}, angle=ANGLE,
                                        cta="Message us.", route_segments=WALK, **kw)


def test_t9_marks_the_focus_moment_and_adds_the_rule():
    p = _t9_prompt(walk_focus="atom-l")
    assert f"[Moment id=atom-l]\n{t9_prompts._WALK_FOCUS_MARK}\nShore walk and ferry" in p
    assert p.count(t9_prompts._WALK_FOCUS_MARK) == 1 and p.count(t9_prompts._WALK_FOCUS_RULE) == 1
    # the rule closes the route format, before the length block and the envelope
    assert p.index("WALK FOCUS:") < p.index("LENGTH AND EFFORT FOR THIS BLOG") < p.index("OUTPUT FORMAT")
    assert "—" not in t9_prompts._WALK_FOCUS_RULE and "—" not in t9_prompts._WALK_FOCUS_MARK


def test_t9_without_a_usable_focus_is_byte_identical():
    base = _t9_prompt()
    assert _t9_prompt(walk_focus=None) == base
    assert _t9_prompt(walk_focus="atom-x") == base               # not on the walk
    single = t9_prompts.build_user_prompt(content_seed="Lake Aurel seed", goal=GOAL,
                                          channel_style=get_channel_style("blog"), brand_audience={}, angle=ANGLE,
                                          cta="Message us.", atom_id="atom-l")
    assert t9_prompts.build_user_prompt(content_seed="Lake Aurel seed", goal=GOAL,
                                        channel_style=get_channel_style("blog"), brand_audience={}, angle=ANGLE,
                                        cta="Message us.", atom_id="atom-l", walk_focus="atom-l") == single


def test_t9_sibling_prompts_differ_by_their_focus():
    on_pass, on_lake = _t9_prompt(walk_focus="atom-p"), _t9_prompt(walk_focus="atom-l")
    assert on_pass != on_lake
    assert f"[Moment id=atom-p]\n{t9_prompts._WALK_FOCUS_MARK}" in on_pass
    assert f"[Moment id=atom-l]\n{t9_prompts._WALK_FOCUS_MARK}" in on_lake


def test_manifest_v3_hashes_the_focus_symbols():
    from shared.llm_client.prompt_registry import load_manifest, newest, prompt_hash_of
    entry = newest(load_manifest(), "t9_write")
    # S279 L2: v8 length values at call time (CIS 410); AA-1077: v7 plan + no-source rule (CIS 405), v6 target (CIS
    # 399); v3 walk focus
    assert entry["version"] == "v8"
    assert {"_WALK_FOCUS_MARK", "_WALK_FOCUS_RULE", "_blog_length_instructions"} <= set(entry["symbols"])
    assert prompt_hash_of(entry["source_path"], entry["symbols"]) == entry["prompt_sha256"]
    t8 = newest(load_manifest(), "t8_angle_gen")
    assert t8["version"] == "v2" and prompt_hash_of(t8["source_path"], t8["symbols"]) == t8["prompt_sha256"]


# ── T9: the service rule ────────────────────────────────────────────────────────────────────────────

def _walk(segments, focus_segment, index_of=None):
    from services.acp_content_writing import service as cw
    req = {"request_id": "req-1", "trip_id": TOUR, "subject_id": "subj-1", "atom_id": "atom-l"}
    atoms: list = []
    fetch = _fetch_with_focus(segments, index_of or {"seg-pass": 0, "seg-lake": 1, "seg-ford": 2})
    with patch.object(cw.walk_focus, "subject_focus_segment", new=AsyncMock(return_value=focus_segment)), \
         patch.object(cw, "_fetch_route_segments", new=fetch):
        out = asyncio.run(cw._walk_with_focus("t1", req, ["seg-pass", "seg-lake", "seg-ford"], _Pool(None), atoms))
    return out, atoms


def test_t9_walk_with_a_rich_focus_returns_the_walk_and_the_moment_id():
    (segments, focus), _ = _walk(WALK, "seg-lake")
    assert segments == WALK and focus == "atom-l"


def test_t9_walk_with_a_thin_or_missing_focus_is_a_single_place_piece():
    for focus in ("seg-ford", "seg-gone"):
        (segments, moment), atoms = _walk(WALK, focus)
        assert segments is None and moment is None and atoms == []


def test_t9_route_subject_keeps_the_whole_walk_with_no_focus():
    (segments, moment), _ = _walk(WALK, None)
    assert segments == WALK and moment is None


@pytest.mark.asyncio
async def test_fetch_route_segments_reports_the_focus_block_after_a_merge():
    """The focus is the block that holds the Subject's Segment's atom, also when same-place moments merged."""
    from services.acp_content_writing import service as cw
    rows = {"seg-pass": {"atom_id": "atom-p", "text": "Corvin Pass", "itinerary_day": 2, "created_at": None},
            "seg-lake": {"atom_id": "atom-l", "text": "Lake Aurel", "itinerary_day": 3, "created_at": None},
            "seg-lake2": {"atom_id": "atom-l2", "text": "Lake Aurel again", "itinerary_day": 5, "created_at": None}}

    class Conn:
        async def fetchrow(self, sql, segment_id, trip_id, tenant):
            return rows.get(segment_id)

    async def merged(conn, atoms, tenant_id, groups_out=None):
        groups = [["atom-p"], ["atom-l", "atom-l2"]]
        if groups_out is not None:
            groups_out.extend(groups)
        return [("atom-p", "Corvin Pass block"), ("atom-l", "Lake Aurel merged block")]

    focus_out: list = []
    with patch.object(cw, "route_moment_seeds", new=merged):
        out = await cw._fetch_route_segments("t1", TOUR, ["seg-pass", "seg-lake", "seg-lake2"], _Pool(Conn()),
                                             focus_segment_id="seg-lake2", focus_out=focus_out)
    assert len(out) == 2 and focus_out == [1]


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


# ── the Slate: a thin Blog Subject is not proposed ──────────────────────────────────────────────────

def _seg(sid, place, score):
    return Candidate(segment_id=sid, route_id=None, score=score, demand=2400, questions=6, said=300, place=place,
                     action="visit")


def test_a_thin_blog_subject_gives_its_seat_to_the_next_place():
    segments = [_seg("s-ford", "Mirsa Ford", 1), _seg("s-lake", "Lake Aurel", 2), _seg("s-pass", "Corvin Pass", 3)]
    chosen = _choose_slate(["blog", "linkedin"], segments, [], hub_of={}, thin={"blog": frozenset({"s-ford"})})
    assert [c.segment_id for c, _ in chosen["blog"]] == ["s-lake", "s-pass"]
    assert "s-ford" in [c.segment_id for c, _ in chosen["linkedin"]]      # other Channels are not affected


@pytest.mark.asyncio
async def test_fetch_thin_walk_segments_reads_only_bar_clearing_blog_candidates():
    from services.acp_shared.subject_seed import Member
    segments = [_seg("s-ford", "Mirsa Ford", 1), _seg("s-lake", "Lake Aurel", 2),
                Candidate(segment_id="s-low", route_id=None, score=3, demand=10, questions=0, said=300,
                          place="Velk Bridge", action="visit")]                  # under the Blog Bar
    members = {"s-ford": [Member("atom-f", TOUR, "Mirsa Ford", "Valdoria")],
               "s-lake": [Member("atom-l", TOUR, "Lake Aurel", "Valdoria")]}
    load = AsyncMock(return_value=members)
    thin_atom = AsyncMock(side_effect=lambda conn, atom_id, tenant_id: atom_id == "atom-f")
    with patch.object(slate, "_tenant_tour_ids", new=AsyncMock(return_value=[TOUR])), \
         patch.object(slate, "load_segment_members", new=load), \
         patch.object(slate, "load_alias_cards", new=AsyncMock(return_value={})), \
         patch.object(slate.walk_focus, "seed_is_thin", new=thin_atom):
        out = await slate._fetch_thin_walk_segments(uuid4(), MagicMock(), segments, ["blog", "linkedin"])
    assert out == {"blog": frozenset({"s-ford"})}
    assert load.await_args.args[1] == ["s-ford", "s-lake"]
    assert thin_atom.await_count == 2
    # no takes_route Channel enabled: nothing is read
    with patch.object(slate, "_tenant_tour_ids", new=AsyncMock()) as tours:
        assert await slate._fetch_thin_walk_segments(uuid4(), MagicMock(), segments, ["linkedin"]) == {}
    tours.assert_not_awaited()
