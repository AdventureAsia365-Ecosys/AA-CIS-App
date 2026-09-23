"""tests/unit/test_aa511_slate.py — AA-511: the Slate's Bar logic.

Pure-function tests only (no DB/HTTP) — `_clears_bar()`/`CHANNEL_BARS` against the literal
thresholds STEP0 confirmed from Ms. Thư's `reference/channels.toml`
(docs/claude_audit/AA-511-step0-slate-investigation.md).

Gap B (2026-09-02, post-Done follow-up) adds `TestChoosePlaceDedupAndHubCap` — pure-function
tests for `_choose()`, the ported `choose()` (place de-dup + `most_per_hub`). `_fetch_segment_hub_map()`
and `_resolve_representative_atom()` (Gap A) are DB-touching and covered by the live-verify record
in docs/implementation-notes/AA-511.md instead, same precedent every other DB-facing function in
this module already follows (`propose_slate()`/`fetch_slate()`/`pick_subject()` have no unit
tests here either).
"""
from services.acp_shared.slate import CHANNEL_BARS, ON_DEMAND_CHANNELS, WEEKLY_RHYTHM_CHANNELS
from services.acp_shared.slate import Candidate, _choose, _clears_bar


def _segment_candidate(demand=None, questions=0, said=0):
    return Candidate(
        segment_id="seg1", route_id=None, score=5, demand=demand, questions=questions,
        said=said, place="Matsumoto Castle", action="visit",
    )


def _route_candidate(demand=None, questions=0):
    return Candidate(
        route_id="r1", segment_id=None, score=3, demand=demand, questions=questions,
        said=0, hub_name="Kyoto -> Magome",
    )


class TestChannelBarsShape:
    def test_eight_channels_declared(self):
        assert set(CHANNEL_BARS) == {
            "blog", "linkedin", "facebook", "instagram", "tiktok",
            "email", "landing_page", "ads",
        }

    def test_blog_thresholds_match_reference_channels_toml(self):
        assert CHANNEL_BARS["blog"]["needs_demand"] == 1000
        assert CHANNEL_BARS["blog"]["needs_questions"] == 3
        assert CHANNEL_BARS["blog"]["grain"] == "route"
        assert CHANNEL_BARS["blog"]["on_demand"] is False

    def test_attention_led_channels_share_needs_said_150(self):
        for channel in ("linkedin", "facebook", "instagram", "tiktok"):
            spec = CHANNEL_BARS[channel]
            assert spec["needs_said"] == 150
            assert spec["needs_demand"] == 0
            assert spec["needs_questions"] == 0
            assert spec["grain"] == "segment"

    def test_on_demand_channels_have_open_bar(self):
        for channel in ("email", "landing_page", "ads"):
            spec = CHANNEL_BARS[channel]
            assert spec["on_demand"] is True
            assert spec["needs_demand"] == 0
            assert spec["needs_questions"] == 0
            assert spec["needs_said"] == 0

    def test_weekly_vs_on_demand_partition(self):
        assert set(WEEKLY_RHYTHM_CHANNELS) == {
            "blog", "linkedin", "facebook", "instagram", "tiktok",
        }
        assert set(ON_DEMAND_CHANNELS) == {"email", "landing_page", "ads"}
        assert set(WEEKLY_RHYTHM_CHANNELS) | set(ON_DEMAND_CHANNELS) == set(CHANNEL_BARS)


class TestClearsBarBlog:
    def test_clears_with_demand_and_questions_over_threshold(self):
        cleared, reason = _clears_bar("blog", _route_candidate(demand=1450, questions=5))
        assert cleared is True
        assert reason["demand_ok"] is True
        assert reason["questions_ok"] is True

    def test_fails_below_demand_threshold_even_with_questions(self):
        cleared, reason = _clears_bar("blog", _route_candidate(demand=999, questions=10))
        assert cleared is False
        assert reason["demand_ok"] is False
        assert reason["questions_ok"] is True

    def test_fails_below_questions_threshold_even_with_demand(self):
        cleared, reason = _clears_bar("blog", _route_candidate(demand=50_000, questions=2))
        assert cleared is False
        assert reason["demand_ok"] is True
        assert reason["questions_ok"] is False

    def test_exact_threshold_clears(self):
        cleared, _ = _clears_bar("blog", _route_candidate(demand=1000, questions=3))
        assert cleared is True

    def test_none_demand_treated_as_zero(self):
        cleared, reason = _clears_bar("blog", _route_candidate(demand=None, questions=10))
        assert cleared is False
        assert reason["demand_ok"] is False


class TestClearsBarAttentionLed:
    def test_facebook_clears_on_said_alone_no_demand_no_questions(self):
        cleared, reason = _clears_bar("facebook", _segment_candidate(demand=None, questions=0, said=200))
        assert cleared is True
        assert reason["demand_ok"] is True  # 0 >= 0
        assert reason["questions_ok"] is True  # 0 >= 0

    def test_instagram_fails_below_said_threshold(self):
        cleared, reason = _clears_bar("instagram", _segment_candidate(said=149))
        assert cleared is False
        assert reason["said_ok"] is False

    def test_tiktok_exact_threshold_clears(self):
        cleared, _ = _clears_bar("tiktok", _segment_candidate(said=150))
        assert cleared is True

    def test_linkedin_high_demand_does_not_substitute_for_said(self):
        # Attention-led channels don't accept demand in place of said -- each axis is its own gate.
        cleared, reason = _clears_bar("linkedin", _segment_candidate(demand=1_000_000, said=0))
        assert cleared is False
        assert reason["said_ok"] is False


class TestClearsBarOnDemand:
    def test_email_clears_anything_zero_bar(self):
        cleared, _ = _clears_bar("email", _segment_candidate(demand=None, questions=0, said=0))
        assert cleared is True

    def test_ads_clears_anything_zero_bar(self):
        cleared, _ = _clears_bar("ads", _segment_candidate())
        assert cleared is True

    def test_landing_page_reason_flags_on_demand(self):
        _, reason = _clears_bar("landing_page", _segment_candidate())
        assert reason["on_demand"] is True


class TestChoosePlaceDedupAndHubCap:
    """`_choose()` — the ported `choose()` (Gap B). All candidates below clear facebook's
    needs_said=150 bar so the Bar itself never explains an exclusion here — only de-dup/hub-cap
    do, isolating exactly what this port is meant to test."""

    def _seg(self, segment_id, place, score, said=200):
        return Candidate(segment_id=segment_id, route_id=None, score=score, demand=None,
                          questions=0, said=said, place=place, action="visit")

    def _route(self, route_id, score, hub_id=None, demand=1200, questions=5):
        return Candidate(route_id=route_id, segment_id=None, score=score, demand=demand,
                          questions=questions, said=0, hub_name=hub_id or route_id, hub_id=hub_id)

    def test_dedups_same_place_keeps_strongest_by_score(self):
        # Two Segments at the same place -- the stronger (lower/better score) wins, matching
        # choose()'s "strongest first" iteration + first-come seen-place skip.
        weaker = self._seg("seg_weak", "Itsukushima Shrine", score=9)
        stronger = self._seg("seg_strong", "Itsukushima Shrine", score=2)
        chosen = _choose("facebook", [weaker, stronger], hub_of={})
        ids = [c.segment_id for c, _ in chosen]
        assert ids == ["seg_strong"]

    def test_keeps_distinct_places(self):
        a = self._seg("seg_a", "Matsumoto Castle", score=3)
        b = self._seg("seg_b", "Kiso-Hirasawa", score=4)
        chosen = _choose("facebook", [a, b], hub_of={})
        assert {c.segment_id for c, _ in chosen} == {"seg_a", "seg_b"}

    def test_seen_places_not_shared_by_default(self):
        # AA-632 default (seen_places=None) — each _choose() call still gets its own fresh set,
        # matching the pre-AA-632 behavior every existing test above already relies on. Two
        # separate calls for the SAME place both succeed because neither call sees the other's
        # `seen_places`.
        a = self._seg("seg_a", "Itsukushima Shrine", score=1)
        b = self._seg("seg_b", "Itsukushima Shrine", score=1)
        chosen_facebook = _choose("facebook", [a], hub_of={})
        chosen_instagram = _choose("instagram", [b], hub_of={})
        assert [c.segment_id for c, _ in chosen_facebook] == ["seg_a"]
        assert [c.segment_id for c, _ in chosen_instagram] == ["seg_b"]

    def test_seen_places_shared_across_channels_when_passed_in(self):
        # AA-632 — propose_slate() passes ONE seen_places set across its whole loop over
        # CHANNEL_BARS, so "not already on slate" dedupes a place across Channels, not just
        # within one Channel's own call. Simulates that loop directly: same place proposed to
        # facebook first (wins), then instagram (loses, even though instagram's own candidate
        # pool never saw facebook's winner).
        seen_places: set[str] = set()
        strong_on_facebook = self._seg("seg_fb", "Itsukushima Shrine", score=1)
        weaker_on_instagram = self._seg("seg_ig", "Itsukushima Shrine", score=1)
        chosen_facebook = _choose("facebook", [strong_on_facebook], hub_of={}, seen_places=seen_places)
        chosen_instagram = _choose("instagram", [weaker_on_instagram], hub_of={}, seen_places=seen_places)
        assert [c.segment_id for c, _ in chosen_facebook] == ["seg_fb"]
        assert chosen_instagram == []

    def test_seen_places_shared_does_not_affect_route_grain(self):
        # Route candidates never set `place` (test_route_grain_has_no_place_dedup above) — a
        # shared seen_places set must not accidentally cap route-grain Channels like blog just
        # because a segment-grain Channel ran first in the same loop.
        seen_places: set[str] = set()
        seg = self._seg("seg_a", "Itsukushima Shrine", score=1)
        _choose("facebook", [seg], hub_of={}, seen_places=seen_places)
        route = self._route("r1", score=1, hub_id="hub_a")
        chosen_blog = _choose("blog", [route], hub_of={}, seen_places=seen_places)
        assert [c.route_id for c, _ in chosen_blog] == ["r1"]

    def test_bar_still_applies_before_dedup(self):
        fails_bar = self._seg("seg_fail", "Nijo Castle", score=1, said=10)  # under needs_said=150
        chosen = _choose("facebook", [fails_bar], hub_of={})
        assert chosen == []

    def test_hub_cap_limits_route_candidates_per_hub(self):
        # 3 Routes sharing one Hub (6-itinerary Nakasendo case), most_per_hub monkeypatched to 1.
        original = CHANNEL_BARS["blog"]["most_per_hub"]
        CHANNEL_BARS["blog"]["most_per_hub"] = 1
        try:
            routes = [
                self._route("r1", score=5, hub_id="hub_nakasendo"),
                self._route("r2", score=1, hub_id="hub_nakasendo"),  # strongest of the 3
                self._route("r3", score=8, hub_id="hub_nakasendo"),
            ]
            chosen = _choose("blog", routes, hub_of={})
            assert [c.route_id for c, _ in chosen] == ["r2"]
        finally:
            CHANNEL_BARS["blog"]["most_per_hub"] = original

    def test_hub_cap_is_per_hub_not_global(self):
        original = CHANNEL_BARS["blog"]["most_per_hub"]
        CHANNEL_BARS["blog"]["most_per_hub"] = 1
        try:
            routes = [
                self._route("r1", score=1, hub_id="hub_a"),
                self._route("r2", score=2, hub_id="hub_b"),
            ]
            chosen = _choose("blog", routes, hub_of={})
            assert {c.route_id for c, _ in chosen} == {"r1", "r2"}
        finally:
            CHANNEL_BARS["blog"]["most_per_hub"] = original

    def test_standalone_route_falls_back_to_its_own_route_id_as_hub(self):
        # No hub_id (standalone Route) -- "a family of one is not a family": each is its own
        # singleton hub, so most_per_hub=1 never collides between two unrelated standalone Routes.
        original = CHANNEL_BARS["blog"]["most_per_hub"]
        CHANNEL_BARS["blog"]["most_per_hub"] = 1
        try:
            routes = [self._route("r1", score=1, hub_id=None), self._route("r2", score=2, hub_id=None)]
            chosen = _choose("blog", routes, hub_of={})
            assert {c.route_id for c, _ in chosen} == {"r1", "r2"}
        finally:
            CHANNEL_BARS["blog"]["most_per_hub"] = original

    def test_segment_grain_uses_hub_of_map_for_hub_cap(self):
        original = CHANNEL_BARS["facebook"]["most_per_hub"]
        CHANNEL_BARS["facebook"]["most_per_hub"] = 1
        try:
            a = self._seg("seg_a", "Place A", score=1)
            b = self._seg("seg_b", "Place B", score=2)
            chosen = _choose("facebook", [a, b], hub_of={"seg_a": "hub_x", "seg_b": "hub_x"})
            assert [c.segment_id for c, _ in chosen] == ["seg_a"]
        finally:
            CHANNEL_BARS["facebook"]["most_per_hub"] = original

    def test_on_demand_channel_has_no_hub_cap(self):
        # email's most_per_hub is None (no origin number exists for on-demand channels) -- never
        # capped, however many candidates share a hub.
        assert CHANNEL_BARS["email"]["most_per_hub"] is None
        a = self._seg("seg_a", "Place A", score=1, said=0)
        b = self._seg("seg_b", "Place B", score=2, said=0)
        chosen = _choose("email", [a, b], hub_of={"seg_a": "hub_x", "seg_b": "hub_x"})
        assert {c.segment_id for c, _ in chosen} == {"seg_a", "seg_b"}

    def test_route_grain_has_no_place_dedup(self):
        # A Route has no `place` (Candidate.place is None for route grain) -- de-dup never
        # applies at Route grain, only hub-cap does.
        r1 = self._route("r1", score=1, hub_id="hub_a")
        r2 = self._route("r2", score=2, hub_id="hub_b")
        chosen = _choose("blog", [r1, r2], hub_of={})
        assert {c.route_id for c, _ in chosen} == {"r1", "r2"}

    def test_iterates_strongest_first_no_recency_preference(self):
        # Order in, order out is irrelevant -- only score decides who wins a place/hub collision,
        # never insertion order or a "prefer newest" rule (the origin's choose() has neither).
        newer_but_weaker = self._seg("seg_new", "Place A", score=9)
        older_but_stronger = self._seg("seg_old", "Place A", score=1)
        chosen = _choose("facebook", [newer_but_weaker, older_but_stronger], hub_of={})
        assert [c.segment_id for c, _ in chosen] == ["seg_old"]
