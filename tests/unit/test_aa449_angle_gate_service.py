"""AA-449 — services/acp_angle_gate/service.py (request lifecycle). Mocked asyncpg pool, same
convention test_aa448_v1_planning.py already uses. generate_angles() is patched — this file
tests the DB lifecycle/state-machine, not LLM behavior (see test_aa449_angle_gate_generate.py
for that).

AA-522 — TestCreateRequest and TestSetChannel (+ their _patch_compute_chain fixture) removed:
create_request()/set_channel() (Luồng B's atom-only creation + post-angle-choice Channel step)
were deleted along with the FE that was their only real caller. See services/acp_angle_gate/
service.py's own module docstring for the full rationale."""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_angle_gate import service

TENANT_ID = uuid.uuid4()
OTHER_TENANT_ID = uuid.uuid4()
REQUEST_ID = uuid.uuid4()
TRIP_ID = uuid.uuid4()


class _TxnCM:
    """conn.transaction() is a sync method returning an async context manager — AsyncMock's
    default mocks the METHOD as async, not its return value, so `async with conn.transaction():`
    breaks unless overridden this way. Same fix as test_aa309_tenant_onboarding.py/
    test_aa367_packets.py/test_aa448_v1_planning.py."""
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _make_pool(conn):
    conn.transaction = MagicMock(return_value=_TxnCM())
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


def _atom_row():
    return {"atom_id": "atom_abc123", "tour_id": TRIP_ID, "text": "Cross the bamboo bridge at dawn"}


def _request_row(**over):
    base = {
        "request_id": REQUEST_ID, "tenant_id": TENANT_ID, "atom_id": "atom_abc123",
        "trip_id": TRIP_ID, "channel": "facebook", "goal": None, "cta": None,
        "status": "pending_goal", "dfs_paa_snapshot": None,  # AA-501, migration 127
        "route_segment_ids": None,  # AA-511 Gap A, migration 134
        "subject_id": None,  # AA-512, migration 133 (subject_id) — fetch_request()'s header join
        "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
    }
    base.update(over)
    return base


def _option_row(idx, recommended=False, chosen=False, answers=None, violations=None):
    return {
        "idx": idx, "name": f"Angle {idx}", "why_it_works": "why", "formula_fit": "AIDA",
        "best_final_style": "style", "recommended": recommended, "chosen": chosen,
        "answers": answers, "violations": violations,  # AA-512, migration 135
    }


class _StatefulAngleOptionConn:
    """AA-494 prerequisite fix test double — tracks angle_gate_option.chosen state directly
    across multiple choose_angle() calls, unlike the AsyncMock-with-side_effect style the rest
    of this file uses (which only checks call counts/args, not resulting DB state). Needed here
    because the fix's correctness is specifically about final state after 2 calls, not just
    which queries fire once."""

    async def fetchval(self, *args, **kwargs):
        # AA-764 — fetch_request() reads the latest t8_angles job status; none in these tests.
        return None

    def __init__(self):
        self.options = {0: False, 1: False, 2: False}
        self.request_status = "pending_choice"

    async def fetchrow(self, query, *args):
        if "angle_gate_request" in query:
            return _request_row(status=self.request_status, goal="promotion")
        return None

    async def fetch(self, query, *args):
        return [_option_row(i, chosen=self.options[i]) for i in sorted(self.options)]

    async def execute(self, query, *args):
        if "SET chosen = true" in query:
            _, idx = args
            if idx not in self.options:
                return "UPDATE 0"
            self.options[idx] = True
            return "UPDATE 1"
        if "SET chosen = false" in query:
            _, idx = args
            n = 0
            for i in self.options:
                if i != idx:
                    self.options[i] = False
                    n += 1
            return f"UPDATE {n}"
        if "SET status = 'approved'" in query:
            self.request_status = "approved"
            return "UPDATE 1"
        return "UPDATE 0"


@pytest.mark.asyncio
class TestSetGoalAndGenerate:
    async def test_happy_path_writes_goal_and_three_options(self):
        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            _request_row(status="pending_goal"),  # _fetch_request_row (status check)
            _atom_row(),                          # _fetch_atom_for_tenant
            {"customer_segment": "Senior execs", "customer_mindset": "seek depth"},  # brand fetchrow
            None,  # AA-469: fetch_search_demand_signal -> no seo_context row for this trip
        ]
        conn.fetch.return_value = []  # fetch_tenant_trips -> no trips (trip_name stays None, fine)
        pool = _make_pool(conn)

        angles = [
            {"name": "A", "why_it_works": "wa", "formula_fit": "fa", "best_final_style": "sa"},
            {"name": "B", "why_it_works": "wb", "formula_fit": "fb", "best_final_style": "sb"},
            {"name": "C", "why_it_works": "wc", "formula_fit": "fc", "best_final_style": "sc"},
        ]
        with patch.object(service, "generate_angles", new=AsyncMock(return_value=(angles, 1, "B is best", 0.02))), \
             patch.object(service, "fetch_request", new=AsyncMock(return_value={"status": "pending_choice"})):
            result = await service.set_goal_and_generate(TENANT_ID, REQUEST_ID, "promotion", pool)

        assert result["status"] == "pending_choice"
        insert_calls = [c for c in conn.execute.call_args_list if "angle_gate_option" in c[0][0]]
        assert len(insert_calls) == 3
        # AA-512 — channel is "facebook" (set by _request_row()'s default) here, so measurable
        # ranking now runs and OVERRIDES the LLM's own recommended_index=1: all 3 angles tie at
        # (0 violations, 0 answers) since none claims an "answers" field and none of their
        # "w{a,b,c}" text matches facebook's avoid-list — ranking.py's documented tie-break picks
        # the earliest idx, so idx 0 wins, not the LLM's original idx 1. See
        # TestSetGoalAndGenerate::test_measurable_ranking_overrides_llm_recommendation below for
        # a case where the 3 angles actually differ.
        recommended_flags = [c[0][7] for c in insert_calls]
        assert recommended_flags == [True, False, False]

    async def test_search_demand_signal_fetched_and_passed_when_trip_id_present(self):
        """AA-469 Việc 4 — set_goal_and_generate() must actually resolve the DFS/PAA signal
        (via fetch_search_demand_signal) and hand it to generate_angles(), not just accept the
        new kwarg without ever populating it."""
        from services.acp_shared.dfs_relevance import SearchDemandSignal
        signal = SearchDemandSignal(
            relevance="HIGH", people_also_ask=["q1"], related_keywords=["k1"],
        )
        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            _request_row(status="pending_goal"),
            _atom_row(),
            {"customer_segment": "Senior execs", "customer_mindset": "seek depth"},
        ]
        conn.fetch.return_value = []
        pool = _make_pool(conn)

        angles = [
            {"name": "A", "why_it_works": "wa", "formula_fit": "fa", "best_final_style": "sa"},
            {"name": "B", "why_it_works": "wb", "formula_fit": "fb", "best_final_style": "sb"},
            {"name": "C", "why_it_works": "wc", "formula_fit": "fc", "best_final_style": "sc"},
        ]
        mock_generate = AsyncMock(return_value=(angles, 0, "reason", 0.02))
        with patch.object(service, "generate_angles", new=mock_generate), \
             patch.object(service, "fetch_search_demand_signal", new=AsyncMock(return_value=signal)), \
             patch.object(service, "fetch_request", new=AsyncMock(return_value={"status": "pending_choice"})):
            await service.set_goal_and_generate(TENANT_ID, REQUEST_ID, "promotion", pool)

        assert mock_generate.call_args.kwargs["search_demand"] is signal

    async def test_search_demand_signal_skipped_when_no_trip_id(self):
        """A request with no trip_id (atom not linked to a trip) must not attempt a seo_context
        lookup at all — mirrors the existing trip_name/destination guard just above it."""
        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            _request_row(status="pending_goal", trip_id=None),
            _atom_row(),
            {"customer_segment": "Senior execs", "customer_mindset": "seek depth"},
        ]
        pool = _make_pool(conn)

        angles = [
            {"name": "A", "why_it_works": "wa", "formula_fit": "fa", "best_final_style": "sa"},
            {"name": "B", "why_it_works": "wb", "formula_fit": "fb", "best_final_style": "sb"},
            {"name": "C", "why_it_works": "wc", "formula_fit": "fc", "best_final_style": "sc"},
        ]
        mock_generate = AsyncMock(return_value=(angles, 0, "reason", 0.02))
        mock_signal = AsyncMock()
        with patch.object(service, "generate_angles", new=mock_generate), \
             patch.object(service, "fetch_search_demand_signal", new=mock_signal), \
             patch.object(service, "fetch_request", new=AsyncMock(return_value={"status": "pending_choice"})):
            await service.set_goal_and_generate(TENANT_ID, REQUEST_ID, "promotion", pool)

        mock_signal.assert_not_called()
        assert mock_generate.call_args.kwargs["search_demand"] is None

    async def test_dfs_paa_snapshot_persisted_when_signal_present(self):
        """AA-501 (migration 127) — the SearchDemandSignal used to build the LLM prompt must be
        persisted onto angle_gate_request.dfs_paa_snapshot in the SAME UPDATE that sets goal/
        status, as a JSON-serialized snapshot (not re-fetched live later)."""
        import json

        from services.acp_shared.dfs_relevance import SearchDemandSignal
        signal = SearchDemandSignal(relevance="HIGH", people_also_ask=["q1"], related_keywords=["k1"])
        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            _request_row(status="pending_goal"),
            _atom_row(),
            {"customer_segment": "Senior execs", "customer_mindset": "seek depth"},
        ]
        conn.fetch.return_value = []
        pool = _make_pool(conn)

        angles = [
            {"name": "A", "why_it_works": "wa", "formula_fit": "fa", "best_final_style": "sa"},
            {"name": "B", "why_it_works": "wb", "formula_fit": "fb", "best_final_style": "sb"},
            {"name": "C", "why_it_works": "wc", "formula_fit": "fc", "best_final_style": "sc"},
        ]
        with patch.object(service, "generate_angles", new=AsyncMock(return_value=(angles, 0, "r", 0.02))), \
             patch.object(service, "fetch_search_demand_signal", new=AsyncMock(return_value=signal)), \
             patch.object(service, "fetch_request", new=AsyncMock(return_value={"status": "pending_choice"})):
            await service.set_goal_and_generate(TENANT_ID, REQUEST_ID, "promotion", pool)

        update_call = next(
            c for c in conn.execute.call_args_list if "UPDATE acp_shared.angle_gate_request" in c[0][0]
        )
        _, _request_id, _goal_key, snapshot_json = update_call[0]
        assert json.loads(snapshot_json) == {
            "relevance": "HIGH", "people_also_ask": ["q1"], "related_keywords": ["k1"],
        }

    async def test_dfs_paa_snapshot_null_when_no_signal(self):
        """No trip_id / no seo_context row -> fetch_search_demand_signal() returns None ->
        dfs_paa_snapshot must be persisted as NULL, not an empty object."""
        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            _request_row(status="pending_goal", trip_id=None),
            _atom_row(),
            {"customer_segment": "Senior execs", "customer_mindset": "seek depth"},
        ]
        pool = _make_pool(conn)

        angles = [
            {"name": "A", "why_it_works": "wa", "formula_fit": "fa", "best_final_style": "sa"},
            {"name": "B", "why_it_works": "wb", "formula_fit": "fb", "best_final_style": "sb"},
            {"name": "C", "why_it_works": "wc", "formula_fit": "fc", "best_final_style": "sc"},
        ]
        with patch.object(service, "generate_angles", new=AsyncMock(return_value=(angles, 0, "r", 0.02))), \
             patch.object(service, "fetch_request", new=AsyncMock(return_value={"status": "pending_choice"})):
            await service.set_goal_and_generate(TENANT_ID, REQUEST_ID, "promotion", pool)

        update_call = next(
            c for c in conn.execute.call_args_list if "UPDATE acp_shared.angle_gate_request" in c[0][0]
        )
        assert update_call[0][3] is None

    async def test_measurable_ranking_overrides_llm_recommendation(self):
        """AA-512 — ADR 0004: when channel is known (Subject-driven request), the LLM's own
        recommended_index (here 2, "C per LLM") must be OVERRIDDEN by the measurable formula:
        fewest avoid-list violations first, then most real PAA-answered questions. Angle 0
        contains linkedin's own avoid-list phrase ("hard sell") -> disqualified regardless of its
        PAA claim. Angle 1 is clean and correctly claims the one real PAA question (case/
        punctuation differs, still matches via _plain() normalization). Angle 2 is clean but
        claims nothing -> Angle 1 must win: 0 violations (tied with 2) but more real answers."""
        import json

        from services.acp_shared.dfs_relevance import SearchDemandSignal
        signal = SearchDemandSignal(
            relevance="HIGH", people_also_ask=["Is Sapa safe to trek?"], related_keywords=[],
        )
        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            _request_row(status="pending_goal", channel="linkedin"),
            _atom_row(),
            {"customer_segment": "Senior execs", "customer_mindset": "seek depth"},
        ]
        conn.fetch.return_value = []
        pool = _make_pool(conn)

        angles = [
            {"name": "A", "why_it_works": "a hard sell pitch", "formula_fit": "fa",
             "best_final_style": "sa", "answers": ["is sapa safe to trek?"]},
            {"name": "B", "why_it_works": "wb", "formula_fit": "fb",
             "best_final_style": "sb", "answers": ["is sapa safe to trek?!"]},  # matches via _plain()
            {"name": "C", "why_it_works": "wc", "formula_fit": "fc",
             "best_final_style": "sc", "answers": []},
        ]
        with patch.object(service, "generate_angles", new=AsyncMock(return_value=(angles, 2, "C per LLM", 0.02))), \
             patch.object(service, "fetch_search_demand_signal", new=AsyncMock(return_value=signal)), \
             patch.object(service, "fetch_request", new=AsyncMock(return_value={"status": "pending_choice"})):
            await service.set_goal_and_generate(TENANT_ID, REQUEST_ID, "promotion", pool)

        insert_calls = [c for c in conn.execute.call_args_list if "angle_gate_option" in c[0][0]]
        recommended_flags = [c[0][7] for c in insert_calls]
        assert recommended_flags == [False, True, False]
        violations_by_idx = {c[0][2]: json.loads(c[0][9]) for c in insert_calls}
        answers_by_idx = {c[0][2]: json.loads(c[0][8]) for c in insert_calls}
        assert any("hard sell" in v for v in violations_by_idx[0])
        assert violations_by_idx[1] == []
        assert answers_by_idx[1] == ["Is Sapa safe to trek?"]  # real question text, not the claim

    async def test_legacy_atom_picker_path_keeps_llm_recommendation_no_ranking(self):
        """AA-512 — channel is NULL (the legacy, dead-but-present atom-picker path, channel only
        set later at step 8) -> ranking must NOT run (avoid-list is channel-scoped, can't be
        computed yet): recommended_index stays the LLM's own choice, answers/violations columns
        stay NULL, no regression on this path."""
        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            _request_row(status="pending_goal", channel=None),
            _atom_row(),
            {"customer_segment": "Senior execs", "customer_mindset": "seek depth"},
            None,  # fetch_search_demand_signal -> no seo_context row
        ]
        conn.fetch.return_value = []
        pool = _make_pool(conn)

        angles = [
            {"name": "A", "why_it_works": "wa", "formula_fit": "fa", "best_final_style": "sa"},
            {"name": "B", "why_it_works": "wb", "formula_fit": "fb", "best_final_style": "sb"},
            {"name": "C", "why_it_works": "wc", "formula_fit": "fc", "best_final_style": "sc"},
        ]
        with patch.object(service, "generate_angles", new=AsyncMock(return_value=(angles, 2, "C per LLM", 0.02))), \
             patch.object(service, "fetch_request", new=AsyncMock(return_value={"status": "pending_choice"})):
            await service.set_goal_and_generate(TENANT_ID, REQUEST_ID, "promotion", pool)

        insert_calls = [c for c in conn.execute.call_args_list if "angle_gate_option" in c[0][0]]
        recommended_flags = [c[0][7] for c in insert_calls]
        assert recommended_flags == [False, False, True]  # unchanged LLM pick (idx 2)
        for c in insert_calls:
            assert c[0][8] is None and c[0][9] is None  # answers/violations stay NULL

    async def test_wrong_status_raises(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = _request_row(status="pending_choice")  # already past pending_goal
        pool = _make_pool(conn)
        with pytest.raises(service.WrongStatusError):
            await service.set_goal_and_generate(TENANT_ID, REQUEST_ID, "promotion", pool)

    async def test_unknown_goal_raises(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = _request_row(status="pending_goal")
        pool = _make_pool(conn)
        with pytest.raises(service.InvalidGoalError):
            await service.set_goal_and_generate(TENANT_ID, REQUEST_ID, "not_a_real_goal", pool)

    async def test_cross_tenant_request_not_found(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = None  # WHERE tenant_id=$2 excludes another tenant's request
        pool = _make_pool(conn)
        with pytest.raises(service.RequestNotFoundError):
            await service.set_goal_and_generate(OTHER_TENANT_ID, REQUEST_ID, "promotion", pool)


@pytest.mark.asyncio
class TestChooseAngle:
    async def test_happy_path_sets_chosen_and_approved(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = _request_row(status="pending_choice", goal="promotion")
        conn.execute.side_effect = ["UPDATE 1", "UPDATE 2", "UPDATE 1"]
        pool = _make_pool(conn)

        with patch.object(service, "fetch_request", new=AsyncMock(return_value={"status": "approved"})):
            result = await service.choose_angle(TENANT_ID, REQUEST_ID, 1, pool)

        assert result["status"] == "approved"
        status_update = [c for c in conn.execute.call_args_list if "SET status = 'approved'" in c[0][0]]
        assert len(status_update) == 1
        # AA-494 prerequisite fix — the other 2 options must be explicitly unset in the same call.
        unset_call = [c for c in conn.execute.call_args_list if "SET chosen = false" in c[0][0]]
        assert len(unset_call) == 1
        _, unset_request_id, unset_idx = unset_call[0][0]
        assert (unset_request_id, unset_idx) == (REQUEST_ID, 1)

    async def test_choosing_twice_unsets_previous_choice(self):
        """The bug this fixes: choose_angle() used to leave a previously-chosen option's
        chosen=true forever, so a future design that allows re-choosing a different angle
        (Decision 3) would have T9 read the FIRST chosen=true row by idx, not the latest. The
        live guard (status must be 'pending_choice') blocks a second real call today — this
        test bypasses it via a stateful fake connection (resetting status between calls) so the
        underlying data-mutation logic is verified now and stays meaningful once the guard is
        loosened later, per the build task's own requirement."""
        conn = _StatefulAngleOptionConn()
        pool = _make_pool(conn)

        await service.choose_angle(TENANT_ID, REQUEST_ID, 0, pool)
        assert conn.options == {0: True, 1: False, 2: False}

        conn.request_status = "pending_choice"  # simulate a future status allowing re-choice
        await service.choose_angle(TENANT_ID, REQUEST_ID, 2, pool)

        assert conn.options == {0: False, 1: False, 2: True}, (
            "only the most recently chosen option should have chosen=true"
        )

    async def test_wrong_status_raises(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = _request_row(status="pending_goal")  # no angles generated yet
        pool = _make_pool(conn)
        with pytest.raises(service.WrongStatusError):
            await service.choose_angle(TENANT_ID, REQUEST_ID, 0, pool)

    async def test_invalid_idx_raises(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = _request_row(status="pending_choice")
        pool = _make_pool(conn)
        with pytest.raises(service.AngleGateError):
            await service.choose_angle(TENANT_ID, REQUEST_ID, 7, pool)


@pytest.mark.asyncio
class TestFetchRequest:
    async def test_returns_request_with_ordered_angles(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = _request_row(status="pending_choice", goal="promotion")
        conn.fetch.return_value = [_option_row(0), _option_row(1, recommended=True), _option_row(2)]
        pool = _make_pool(conn)

        result = await service.fetch_request(TENANT_ID, REQUEST_ID, pool)

        assert result["status"] == "pending_choice"
        assert len(result["angles"]) == 3
        assert result["angles"][1]["recommended"] is True

    async def test_not_found_raises(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = None
        pool = _make_pool(conn)
        with pytest.raises(service.RequestNotFoundError):
            await service.fetch_request(TENANT_ID, REQUEST_ID, pool)

    async def test_subject_header_shows_the_live_segment_label(self):
        """AA-857 — a Subject stored on an id that later merged into another shows the live
        Segment's label, the one the tenant saw in the Slate when picking it."""
        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            _request_row(status="pending_choice", goal="promotion", subject_id=uuid.uuid4()),
            {"score": 2.0, "segment_id": "old1", "canonical_place": "stale", "canonical_action": "visit",
             "hub_name": None},
        ]
        conn.fetch.return_value = []
        conn.fetchval.return_value = None
        labels = AsyncMock(return_value={"old1": ("live1", "Delhi", "explore")})
        with patch.object(service, "live_segment_labels", labels):
            result = await service.fetch_request(TENANT_ID, REQUEST_ID, _make_pool(conn))
        assert (result["subject_place"], result["subject_action"]) == ("Delhi", "explore")
        assert labels.await_args.args[1] == ["old1"]

        conn.fetchrow.side_effect = [
            _request_row(status="pending_choice", goal="promotion", subject_id=uuid.uuid4()),
            {"score": 2.0, "segment_id": "seg1", "canonical_place": "Agra", "canonical_action": "visit",
             "hub_name": None},
        ]
        with patch.object(service, "live_segment_labels", AsyncMock(return_value={})):
            result = await service.fetch_request(TENANT_ID, REQUEST_ID, _make_pool(conn))
        assert result["subject_place"] == "Agra"

    async def test_dfs_paa_snapshot_parsed_from_json_string(self):
        """AA-501 — asyncpg has no jsonb codec registered on this app's connections (same gap
        admin_a4.py's _parse_jsonb already works around), so dfs_paa_snapshot arrives as a raw
        JSON string and must be parsed, not returned as-is."""
        import json
        conn = AsyncMock()
        conn.fetchrow.return_value = _request_row(
            status="pending_choice", goal="promotion",
            dfs_paa_snapshot=json.dumps({"relevance": "MED", "people_also_ask": ["q"], "related_keywords": []}),
        )
        conn.fetch.return_value = []
        pool = _make_pool(conn)

        result = await service.fetch_request(TENANT_ID, REQUEST_ID, pool)

        assert result["dfs_paa_snapshot"] == {
            "relevance": "MED", "people_also_ask": ["q"], "related_keywords": [],
        }

    async def test_dfs_paa_snapshot_none_stays_none(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = _request_row(status="pending_choice", goal="promotion")
        conn.fetch.return_value = []
        pool = _make_pool(conn)

        result = await service.fetch_request(TENANT_ID, REQUEST_ID, pool)

        assert result["dfs_paa_snapshot"] is None
