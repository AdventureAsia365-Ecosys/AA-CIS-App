"""AA-840 — the Slate proposes for every enabled Channel, not only the first one.

Before AA-840, `propose_slate()` looped over every Channel in `CHANNEL_BARS` with one
`seen_places` set shared across Channels, so LinkedIn (first, same Bar as Facebook) took every
place and Facebook/Instagram/TikTok got nothing; the tenant's channel config was never read.

Pure tests — no DB, no LLM: `_choose_slate()` directly, and `propose_slate()` / `fetch_slate()`
against a fake pool that records the SQL they run.
"""
from __future__ import annotations

import json
import random
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from services.acp_shared import slate
from services.acp_shared.slate import (CHANNEL_BARS, MAX_CHANNELS_PER_PLACE, Candidate, _choose_slate,
                                       _tenant_enabled_channels)

SEGMENT_CHANNELS = [c for c, spec in CHANNEL_BARS.items() if spec["grain"] == "segment"]
SOCIAL_CHANNELS = ["linkedin", "facebook", "instagram", "tiktok"]


def _seg(i: int, said: int = 200, place: str | None = None) -> Candidate:
    return Candidate(segment_id=f"seg_{i}", route_id=None, score=float(i), demand=None, questions=0,
                     said=said, place=place or f"Place {i}", action="visit")


def _route(i: int) -> Candidate:
    return Candidate(segment_id=None, route_id=f"route_{i}", score=float(i), demand=1500, questions=5,
                     said=0, hub_name=f"Hub {i}", hub_id=f"hub_{i}")


def _ids(pairs) -> list[str]:
    return [c.segment_id or c.route_id for c, _ in pairs]


# ── _choose_slate(): the place rule ─────────────────────────────────────────────

class TestChooseSlate:
    def test_five_bar_clearing_segments_give_every_segment_channel_proposals(self):
        # The S238 lane B repro: 5 segments that all clear the Bar used to give LinkedIn 5 and
        # every other Channel 0.
        segments = [_seg(i) for i in range(1, 6)]
        chosen = _choose_slate(SEGMENT_CHANNELS, segments, [], hub_of={})
        for channel in SEGMENT_CHANNELS:
            assert chosen[channel], f"{channel} got no proposals"
        assert chosen["facebook"] and chosen["linkedin"]

    def test_a_place_appears_once_per_channel(self):
        segments = [_seg(1, place="Ha Long Bay"), _seg(2, place="Ha Long Bay"), _seg(3, place="Sapa")]
        chosen = _choose_slate(["facebook"], segments, [], hub_of={})
        assert _ids(chosen["facebook"]) == ["seg_1", "seg_3"]  # the stronger Segment keeps the place

    def test_a_place_is_capped_across_channels(self):
        segments = [_seg(i) for i in range(1, 6)]
        chosen = _choose_slate(SEGMENT_CHANNELS, segments, [], hub_of={})
        per_place: dict[str, int] = {}
        for channel in SEGMENT_CHANNELS:
            places = [c.place for c, _ in chosen[channel]]
            assert len(places) == len(set(places)), f"{channel} repeats a place"
            for place in places:
                per_place[place] = per_place.get(place, 0) + 1
        assert max(per_place.values()) <= MAX_CHANNELS_PER_PLACE
        # 5 places x 3 seats = 15 seats, all used across the 7 Channels
        assert sum(per_place.values()) == 5 * MAX_CHANNELS_PER_PLACE

    def test_cap_none_lets_every_channel_take_every_place(self):
        segments = [_seg(i) for i in range(1, 6)]
        chosen = _choose_slate(SEGMENT_CHANNELS, segments, [], hub_of={}, max_channels_per_place=None)
        for channel in SEGMENT_CHANNELS:
            assert len(chosen[channel]) == 5

    def test_channels_take_turns_strongest_first(self):
        # Round-robin in channel order, one pick per Channel per round, strongest first.
        # Round 1: linkedin/facebook/instagram take place 1 (its 3 seats are then used), tiktok
        # falls through to place 2. Round 2: linkedin and facebook take place 2 (3 seats used),
        # instagram and tiktok have nothing left. No Channel ends up empty.
        segments = [_seg(i) for i in range(1, 3)]
        chosen = _choose_slate(["linkedin", "facebook", "instagram", "tiktok"], segments, [], hub_of={})
        assert _ids(chosen["linkedin"]) == ["seg_1", "seg_2"]
        assert _ids(chosen["facebook"]) == ["seg_1", "seg_2"]
        assert _ids(chosen["instagram"]) == ["seg_1"]
        assert _ids(chosen["tiktok"]) == ["seg_2"]

    def test_bar_still_applies_per_channel(self):
        # said=100 fails the social Bar (150) but clears the on-demand Channels' open Bar.
        segments = [_seg(1, said=100)]
        chosen = _choose_slate(["facebook", "email"], segments, [], hub_of={})
        assert chosen["facebook"] == []
        assert _ids(chosen["email"]) == ["seg_1"]

    def test_hub_cap_still_per_channel(self):
        segments = [_seg(i) for i in range(1, 4)]
        hub_of = {s.segment_id: "hub_x" for s in segments}
        with patch.dict(CHANNEL_BARS["facebook"], {"most_per_hub": 1}):
            chosen = _choose_slate(["facebook", "linkedin"], segments, [], hub_of=hub_of)
        assert _ids(chosen["facebook"]) == ["seg_1"]
        assert _ids(chosen["linkedin"]) == ["seg_1", "seg_2", "seg_3"]

    def test_blog_uses_routes_and_is_not_touched_by_the_place_cap(self):
        chosen = _choose_slate(["blog", "linkedin"], [_seg(1)], [_route(1), _route(2)], hub_of={},
                               max_channels_per_place=1)
        assert _ids(chosen["blog"]) == ["route_1", "route_2"]
        assert _ids(chosen["linkedin"]) == ["seg_1"]

    def test_deterministic_whatever_the_input_order(self):
        segments = [_seg(i, place=f"Place {i % 4}") for i in range(1, 13)]
        first = _choose_slate(SEGMENT_CHANNELS, segments, [], hub_of={})
        shuffled = segments[:]
        random.Random(840).shuffle(shuffled)
        second = _choose_slate(SEGMENT_CHANNELS, shuffled, [], hub_of={})
        assert {c: _ids(v) for c, v in first.items()} == {c: _ids(v) for c, v in second.items()}

    def test_only_the_channels_asked_for_are_returned(self):
        chosen = _choose_slate(["blog", "linkedin"], [_seg(1)], [_route(1)], hub_of={})
        assert set(chosen) == {"blog", "linkedin"}


# ── _tenant_enabled_channels(): where the config comes from ─────────────────────

class TestTenantEnabledChannels:
    @pytest.mark.asyncio
    async def test_no_config_row_means_every_channel(self):
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=None)
        assert await _tenant_enabled_channels(uuid4(), conn) == list(CHANNEL_BARS)

    @pytest.mark.asyncio
    async def test_configured_channels_in_channel_bars_order(self):
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=["linkedin", "blog"])
        assert await _tenant_enabled_channels(uuid4(), conn) == ["blog", "linkedin"]

    @pytest.mark.asyncio
    async def test_unknown_values_are_ignored(self):
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=["facebook", "pinterest"])
        assert await _tenant_enabled_channels(uuid4(), conn) == ["facebook"]

    @pytest.mark.asyncio
    async def test_reads_tenant_config(self):
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=None)
        tenant_id = uuid4()
        await _tenant_enabled_channels(tenant_id, conn)
        sql, arg = conn.fetchval.await_args.args
        assert "acp_shared.tenant_config" in sql and arg == tenant_id


# ── propose_slate() / fetch_slate() against a fake pool ─────────────────────────

class _FakeConn:
    def __init__(self, channels_config, subject_rows=()):
        self.channels_config = channels_config
        self.subject_rows = list(subject_rows)
        self.executed: list[tuple] = []

    async def fetchval(self, sql, *args):
        assert "acp_shared.tenant_config" in sql
        return self.channels_config

    async def fetch(self, sql, *args):
        if "atom_segment_alias" in sql:     # AA-857 alias resolution: no id has given way here
            return []
        assert "FROM acp_shared.subject" in sql
        return self.subject_rows

    async def execute(self, sql, *args):
        self.executed.append((sql, args))

    @asynccontextmanager
    async def transaction(self):
        yield


class _FakePool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def _inserted(conn: _FakeConn) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for sql, args in conn.executed:
        if sql.lstrip().startswith("INSERT INTO acp_shared.subject"):
            out.setdefault(args[2], []).append(args[1])
    return out


def _deleted_channels(conn: _FakeConn) -> set[str]:
    return {args[1] for sql, args in conn.executed if sql.lstrip().startswith("DELETE FROM acp_shared.subject")}


async def _propose(channels_config, segments, routes):
    conn = _FakeConn(channels_config)
    seg_fetch = AsyncMock(return_value=segments)
    route_fetch = AsyncMock(return_value=routes)
    debate = AsyncMock(side_effect=lambda _tid, candidates, _conn: candidates)
    with patch.object(slate, "_tenant_market_codes", AsyncMock(return_value=(["US"], []))), \
         patch.object(slate, "_fetch_segment_candidates", seg_fetch), \
         patch.object(slate, "_fetch_route_candidates", route_fetch), \
         patch.object(slate, "_fetch_segment_hub_map", AsyncMock(return_value={})), \
         patch("services.acp_shared.debate.apply_debate", debate):
        result = await slate.propose_slate(uuid4(), _FakePool(conn))
    return conn, result, seg_fetch, route_fetch, debate


class TestProposeSlate:
    @pytest.mark.asyncio
    async def test_no_config_proposes_for_every_channel(self):
        segments = [_seg(i) for i in range(1, 6)]
        conn, result, *_ = await _propose(None, segments, [_route(1)])
        inserted = _inserted(conn)
        assert set(inserted) == set(CHANNEL_BARS)
        assert result["channels"] == list(CHANNEL_BARS)
        assert all(result["proposed_per_channel"][c] > 0 for c in CHANNEL_BARS)

    @pytest.mark.asyncio
    async def test_blog_and_linkedin_tenant_gets_no_facebook(self):
        segments = [_seg(i) for i in range(1, 6)]
        conn, result, *_ = await _propose(["blog", "linkedin"], segments, [_route(1)])
        inserted = _inserted(conn)
        assert set(inserted) == {"blog", "linkedin"}
        assert "facebook" not in inserted
        assert result["channels"] == ["blog", "linkedin"]
        # Every Channel still gets its stale-`proposed` cleanup, so switching facebook off
        # removes the facebook proposals it had before.
        assert _deleted_channels(conn) == set(CHANNEL_BARS)

    @pytest.mark.asyncio
    async def test_facebook_tenant_gets_facebook_proposals(self):
        segments = [_seg(i) for i in range(1, 6)]
        conn, *_ = await _propose(["linkedin", "facebook"], segments, [])
        inserted = _inserted(conn)
        assert len(inserted["facebook"]) == 5 and len(inserted["linkedin"]) == 5

    @pytest.mark.asyncio
    async def test_inserted_rows_carry_the_bar_reason(self):
        conn, *_ = await _propose(["facebook"], [_seg(1)], [])
        sql, args = next((s, a) for s, a in conn.executed if s.lstrip().startswith("INSERT"))
        reason = json.loads(args[3])
        assert reason["channel"] == "facebook" and reason["said_ok"] is True

    @pytest.mark.asyncio
    async def test_blog_only_tenant_skips_segment_fetch_and_debate(self):
        # Debate's brand-fit cut is a paid call on a cache miss: a grain no enabled Channel uses
        # is not fetched nor debated.
        conn, result, seg_fetch, route_fetch, debate = await _propose(["blog"], [_seg(1)], [_route(1)])
        seg_fetch.assert_not_awaited()
        route_fetch.assert_awaited_once()
        assert debate.await_count == 2  # segments ([]) and routes, unchanged call shape
        assert _inserted(conn) == {"blog": ["route_1"]}
        assert result["segments_seen"] == 0

    @pytest.mark.asyncio
    async def test_segment_only_tenant_skips_route_fetch(self):
        _, _, seg_fetch, route_fetch, _ = await _propose(["facebook"], [_seg(1)], [_route(1)])
        seg_fetch.assert_awaited_once()
        route_fetch.assert_not_awaited()


def _row(channel, state, segment_id="seg_1"):
    return {
        "subject_id": uuid4(), "channel": channel, "state": state, "score": 1.0,
        "cleared_bar_reason": json.dumps({"channel": channel}), "segment_id": segment_id,
        "route_id": None, "created_at": None, "canonical_place": "Place 1",
        "canonical_action": "visit", "hub_name": None, "ordered_segment_ids": None, "tour_id": None,
    }


class TestFetchSlate:
    @pytest.mark.asyncio
    async def test_disabled_channel_hides_proposed_keeps_used(self):
        rows = [_row("facebook", "proposed"), _row("facebook", "used", "seg_2"),
                _row("linkedin", "proposed")]
        out = await slate.fetch_slate(uuid4(), _FakePool(_FakeConn(["blog", "linkedin"], rows)))
        assert out["facebook"]["enabled"] is False
        assert [s["state"] for s in out["facebook"]["subjects"]] == ["used"]
        assert out["facebook"]["eligible_count"] == 0
        assert out["linkedin"]["enabled"] is True and out["linkedin"]["eligible_count"] == 1

    @pytest.mark.asyncio
    async def test_no_config_every_channel_enabled(self):
        out = await slate.fetch_slate(uuid4(), _FakePool(_FakeConn(None, [_row("facebook", "proposed")])))
        assert all(out[c]["enabled"] for c in CHANNEL_BARS)
        assert out["facebook"]["eligible_count"] == 1

    @pytest.mark.asyncio
    async def test_fetch_slate_only_reads(self):
        conn = _FakeConn(None, [])
        await slate.fetch_slate(uuid4(), _FakePool(conn))
        assert conn.executed == []


def test_planning_config_fallback_lists_every_slate_channel():
    # The admin Planning config form shows this fallback for a tenant with no row; it must match
    # what the Slate does for that tenant (every Channel), not the old ["blog"].
    from services.acp_planning import tenant_config
    assert tenant_config._DEFAULT_CHANNELS == list(CHANNEL_BARS)
