"""AA-508 — atom_id content-hash + per-day fingerprint-skip + real UPSERT.

STEP0 (docs/claude_audit/AA-508-step0-atom-identity-investigation.md) found run_t5_atomize()
atomized a whole tenant tour version in ONE LLM call, keyed to skip-or-not by ONE source_hash over
the whole itinerary — a single day's edit re-atomized (and re-randomized every atom_id of) the
entire tour. STEP0b cross-checked the reference repo (aa-social-media) directly: content-hash
atom_id + a real ON CONFLICT UPSERT, gated per DAY by a fingerprint that BLOCKS the LLM call
(not just logged after one).

AA-509 updated this file's JSON mocking shape from a combined `text` field to separate
`place`/`action` (T5 decompose now returns both — atom_extraction.py SYSTEM_PROMPT) and
content_hash_atom_id()'s signature/argument order to the build prompt's literal formula
(owner_scope, tour_id, day, place, action) — see that task's implementation notes Decision 2/4.

Drives the real coroutine (services.acp_produce.tenant_pipeline.run_t5_atomize), same mocking
shape as test_aa445_t5_distinctiveness.py (pool.acquire() fake, the gateway LLMClient patched at
its import site) — `itineraries` here is in the canonical "Day N — Title\\nBody" format (T4 reuses
T2's engine, graph.py's generate_node/_process_itineraries, which always emits this) so these
tests exercise _atomize_per_day(), not the legacy whole-tour fallback
(test_aa445_t5_distinctiveness.py's empty-string `itineraries` already covers that path and is
untouched by this change).

AA-757 moved atomize onto the gateway (LLMClient.generate, stage "t5_atomize"), so these tests
now patch tenant_pipeline.LLMClient and build the fake as an LLMResponse-shaped object (`.content`
instead of `.text`, flat token/cost fields instead of a `.usage` dict). The assertions are
unchanged.
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_produce import tenant_pipeline
from services.acp_shared.atom_extraction import (
    content_hash_atom_id, day_fingerprint, derive_atom_text, normalise,
)
from shared.llm_client.role_config import SAFE_DEFAULTS

# AA-619 — the day-fingerprint is now keyed on the live t5_atomize config model (was the removed
# `tenant_pipeline._T5_MODEL_TIER` constant). With no DB in the unit env, get_stage_config()
# falls back to SAFE_DEFAULTS, so the fingerprint the code computes uses this model.
_T5_FP_MODEL = SAFE_DEFAULTS["t5_atomize"].model_id

TENANT_ID = "33333333-3333-3333-3333-333333333333"
TOUR_ID = "44444444-4444-4444-4444-444444444444"
VERSION_ID = "77777777-7777-7777-7777-777777777777"

TWO_DAY_ITINERARY = (
    "Day 1 — Arrival in Hanoi\n"
    "Walk through the Old Quarter and try street food.\n\n"
    "Day 2 — Halong Bay Cruise\n"
    "Board a traditional junk boat and kayak through limestone caves."
)


def _pool_ctx(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


def _fake_conn(existing_fingerprints=None):
    """`existing_fingerprints`: list of {"day_number":..., "fingerprint_hash":...} rows
    returned by the ONE `SELECT ... FROM acp_contract.atomize_day_fingerprint` fetch()."""
    conn = AsyncMock()
    conn.fetch.return_value = existing_fingerprints or []
    return conn


class _FakeLLMResult:
    """AA-757 — LLMClient.generate() returns an LLMResponse. The atomize code reads
    `.content` (JSON text), the flat token/cost fields, `.model_used`, `.stop_reason`, and the
    routing fields (`.satellite_account`, `.fallback_used`, `.provider`) for the cost log."""
    def __init__(self, content, model_used="satellite-haiku-4-5", input_tokens=100,
                 output_tokens=50, cost_usd=0.001, stop_reason="end_turn",
                 satellite_account="acc3", fallback_used=False, provider="bedrock-satellite"):
        self.content = content
        self.model_used = model_used
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cost_usd = cost_usd
        self.stop_reason = stop_reason
        self.satellite_account = satellite_account
        self.fallback_used = fallback_used
        self.provider = provider


def _patch_gateway(*, return_value=None, side_effect=None):
    """Patch tenant_pipeline.LLMClient so LLMClient().generate(...) yields the fake response(s).
    Returns the `generate` MagicMock so a test can assert call count exactly as it did on
    invoke_claude before AA-757."""
    gen = MagicMock(return_value=return_value, side_effect=side_effect)
    client = MagicMock()
    client.generate = gen
    cm = patch("services.acp_produce.tenant_pipeline.LLMClient", return_value=client)
    return cm, gen


def _day1_atoms_json(place="Old Quarter", action="walk"):
    return json.dumps({"atoms": [{"place": place, "action": action, "activity_type": "culture"}]})


def _day2_atoms_json(place="Halong Bay", action="kayak through limestone caves"):
    return json.dumps({"atoms": [{"place": place, "action": action, "activity_type": "trek"}]})


@pytest.mark.asyncio
async def test_first_atomize_reads_every_day_content_hash_ids():
    """N days, first run: no fingerprint rows exist yet, so both days are read; every atom_id
    matches content_hash_atom_id(tenant_id, tour_id, day_number, place, action) exactly — the
    real formula, not a random UUID."""
    conn = _fake_conn(existing_fingerprints=[])
    pool = _pool_ctx(conn)

    day1_place, day1_action = "Old Quarter", "walk"
    day2_place, day2_action = "Halong Bay", "kayak through limestone caves"

    cm, _ = _patch_gateway(side_effect=[
        _FakeLLMResult(_day1_atoms_json(day1_place, day1_action)),
        _FakeLLMResult(_day2_atoms_json(day2_place, day2_action))])
    with cm:
        result = await tenant_pipeline.run_t5_atomize(
            TENANT_ID, TOUR_ID,
            {"name": "Sapa Trek", "summary": "s", "highlights": [], "itineraries": TWO_DAY_ITINERARY},
            pool, country="Vietnam", version_id=VERSION_ID,
        )

    assert result["status"] == "success"
    assert result["atom_count"] == 2
    assert result["days_total"] == 2
    assert result["days_read"] == 2
    assert result["days_skipped"] == 0

    insert_calls = [c for c in conn.execute.call_args_list
                     if "INSERT INTO acp_contract.tour_atoms" in c.args[0]]
    assert len(insert_calls) == 2

    expected_id_day1 = content_hash_atom_id(TENANT_ID, TOUR_ID, 1, day1_place, day1_action)
    expected_id_day2 = content_hash_atom_id(TENANT_ID, TOUR_ID, 2, day2_place, day2_action)
    actual_ids = {c.args[1] for c in insert_calls}  # atom_id is the 1st bind param ($1)
    assert actual_ids == {expected_id_day1, expected_id_day2}

    # text stays populated (derived), place/action are the new real columns
    texts = {c.args[4] for c in insert_calls}
    assert texts == {derive_atom_text(day1_place, day1_action), derive_atom_text(day2_place, day2_action)}
    places = {c.args[5] for c in insert_calls}
    assert places == {day1_place, day2_place}

    # ON CONFLICT UPSERT, not a plain INSERT
    assert all("ON CONFLICT (atom_id) DO UPDATE" in c.args[0] for c in insert_calls)

    # itinerary_day bound correctly per day (18th positional param, index 17 — shifted by 3 vs.
    # pre-AA-509 since place/action/evidence are now 3 extra bind params ahead of it)
    itinerary_days = {c.args[17] for c in insert_calls}
    assert itinerary_days == {1, 2}

    # AA-610 — these mocks don't return an "evidence" field (real-world models sometimes won't
    # either), so checkable_evidence() falls back to the whole day's body — never empty/None,
    # never crashes. evidence is bind param $7 (args[7] — args[0] is the SQL string itself).
    evidences = {c.args[7] for c in insert_calls}
    assert evidences == {
        "Walk through the Old Quarter and try street food.",
        "Board a traditional junk boat and kayak through limestone caves.",
    }

    # fingerprint rows written for both days
    fp_calls = [c for c in conn.execute.call_args_list
                 if "INSERT INTO acp_contract.atomize_day_fingerprint" in c.args[0]]
    assert len(fp_calls) == 2
    fp_versions = {c.args[1] for c in fp_calls}
    assert fp_versions == {VERSION_ID}


@pytest.mark.asyncio
async def test_rerun_unchanged_skips_every_day_zero_llm_calls():
    """Re-atomizing with identical days (fingerprints already on file) reads nothing — zero
    invoke_claude() calls, atom_count=0, status='skipped'."""
    fp1 = day_fingerprint("Arrival in Hanoi",
                           "Walk through the Old Quarter and try street food.",
                           _T5_FP_MODEL)
    fp2 = day_fingerprint("Halong Bay Cruise",
                           "Board a traditional junk boat and kayak through limestone caves.",
                           _T5_FP_MODEL)
    conn = _fake_conn(existing_fingerprints=[
        {"day_number": 1, "fingerprint_hash": fp1},
        {"day_number": 2, "fingerprint_hash": fp2},
    ])
    pool = _pool_ctx(conn)

    cm, m_llm = _patch_gateway(return_value=_FakeLLMResult("{}"))
    with cm:
        result = await tenant_pipeline.run_t5_atomize(
            TENANT_ID, TOUR_ID,
            {"name": "Sapa Trek", "summary": "s", "highlights": [], "itineraries": TWO_DAY_ITINERARY},
            pool, country="Vietnam", version_id=VERSION_ID,
        )

    m_llm.assert_not_called()
    assert result == {
        "status": "skipped", "atom_count": 0,
        "days_total": 2, "days_read": 0, "days_skipped": 2,
    }
    # No atom/fingerprint writes at all when every day is skipped
    assert not any("INSERT INTO acp_contract.tour_atoms" in c.args[0]
                   for c in conn.execute.call_args_list)
    assert not any("acp_contract.atomize_day_fingerprint" in c.args[0]
                   for c in conn.execute.call_args_list)


@pytest.mark.asyncio
async def test_one_day_changed_only_that_day_reatomizes_other_kept():
    """Day 1's fingerprint is stale (content changed); Day 2's still matches. Only Day 1 calls
    the LLM and gets a new content-hash atom_id; Day 2 is never touched — no INSERT, no
    fingerprint re-write, no invoke_claude() call for it."""
    fp2_current = day_fingerprint("Halong Bay Cruise",
                                   "Board a traditional junk boat and kayak through limestone caves.",
                                   _T5_FP_MODEL)
    conn = _fake_conn(existing_fingerprints=[
        {"day_number": 1, "fingerprint_hash": "stale-hash-from-a-prior-different-day-1-wording"},
        {"day_number": 2, "fingerprint_hash": fp2_current},
    ])
    pool = _pool_ctx(conn)

    new_day1_place, new_day1_action = "Old Quarter market", "visit at dawn"

    cm, m_llm = _patch_gateway(
        return_value=_FakeLLMResult(_day1_atoms_json(new_day1_place, new_day1_action)))
    with cm:
        result = await tenant_pipeline.run_t5_atomize(
            TENANT_ID, TOUR_ID,
            {"name": "Sapa Trek", "summary": "s", "highlights": [], "itineraries": TWO_DAY_ITINERARY},
            pool, country="Vietnam", version_id=VERSION_ID,
        )

    m_llm.assert_called_once()  # exactly 1 day re-read, not 2
    assert result["status"] == "success"
    assert result["days_read"] == 1
    assert result["days_skipped"] == 1

    insert_calls = [c for c in conn.execute.call_args_list
                     if "INSERT INTO acp_contract.tour_atoms" in c.args[0]]
    assert len(insert_calls) == 1
    assert insert_calls[0].args[17] == 1  # itinerary_day
    assert insert_calls[0].args[1] == content_hash_atom_id(
        TENANT_ID, TOUR_ID, 1, new_day1_place, new_day1_action)

    fp_calls = [c for c in conn.execute.call_args_list
                 if "INSERT INTO acp_contract.atomize_day_fingerprint" in c.args[0]]
    assert len(fp_calls) == 1
    assert fp_calls[0].args[2] == 1  # day_number


@pytest.mark.asyncio
async def test_llm_failure_on_one_day_keeps_other_days_committed():
    """Day 1 fails (LLM raises); Day 2 succeeds. Prior days' work is not rolled back —
    result reports 'failed' overall (so the tenant/A4 sees it) but atom_count/fingerprint for
    Day 2 are still real, committed writes, and Day 1's failure is named specifically."""
    conn = _fake_conn(existing_fingerprints=[])
    pool = _pool_ctx(conn)

    cm, _ = _patch_gateway(side_effect=[
        RuntimeError("BedrockError: throttled"),
        _FakeLLMResult(_day2_atoms_json())])
    with cm:
        result = await tenant_pipeline.run_t5_atomize(
            TENANT_ID, TOUR_ID,
            {"name": "Sapa Trek", "summary": "s", "highlights": [], "itineraries": TWO_DAY_ITINERARY},
            pool, country="Vietnam", version_id=VERSION_ID,
        )

    assert result["status"] == "failed"
    assert result["days_failed"] == [1]
    assert result["days_read"] == 1
    assert result["atom_count"] == 1

    insert_calls = [c for c in conn.execute.call_args_list
                     if "INSERT INTO acp_contract.tour_atoms" in c.args[0]]
    assert len(insert_calls) == 1
    assert insert_calls[0].args[17] == 2  # only Day 2 got written

    fp_calls = [c for c in conn.execute.call_args_list
                 if "INSERT INTO acp_contract.atomize_day_fingerprint" in c.args[0]]
    assert len(fp_calls) == 1
    assert fp_calls[0].args[2] == 2  # Day 1 has no fingerprint row -> retried next call


@pytest.mark.asyncio
async def test_zero_atom_day_writes_deterministic_marker_not_random():
    """A day the model reads as truly empty still gets a marker row, but the marker's atom_id
    is content-hash-deterministic (owner_scope, tour_id, day, '__empty__', '__empty__'), not
    uuid4()-random — so a repeat empty-day read UPSERTs the same marker instead of piling up a
    new random one every time it's forced to re-run."""
    conn = _fake_conn(existing_fingerprints=[])
    pool = _pool_ctx(conn)

    cm, _ = _patch_gateway(side_effect=[
        _FakeLLMResult(json.dumps({"atoms": []})),
        _FakeLLMResult(_day2_atoms_json())])
    with cm:
        result = await tenant_pipeline.run_t5_atomize(
            TENANT_ID, TOUR_ID,
            {"name": "Sapa Trek", "summary": "s", "highlights": [], "itineraries": TWO_DAY_ITINERARY},
            pool, country="Vietnam", version_id=VERSION_ID,
        )

    assert result["status"] == "success"
    marker_calls = [c for c in conn.execute.call_args_list
                     if "INSERT INTO acp_contract.tour_atoms" in c.args[0]
                     and "is_empty_marker" in c.args[0]]
    assert len(marker_calls) == 1
    expected_marker_id = content_hash_atom_id(TENANT_ID, TOUR_ID, 1, "__empty__", "__empty__")
    assert marker_calls[0].args[1] == expected_marker_id


def test_content_hash_atom_id_stable_and_tenant_scoped():
    """Same (owner_scope, tour_id, day, place, action) -> same id across calls (re-run
    stability); a different tenant on the same tour/day/place/action -> a different id (no
    cross-tenant PK collision on tour_atoms' single global atom_id primary key)."""
    id_a = content_hash_atom_id(TENANT_ID, TOUR_ID, 3, "Bamboo Bridge", "cross at dawn")
    id_b = content_hash_atom_id(TENANT_ID, TOUR_ID, 3, "Bamboo Bridge", "cross at dawn")
    assert id_a == id_b

    other_tenant = "99999999-9999-9999-9999-999999999999"
    id_c = content_hash_atom_id(other_tenant, TOUR_ID, 3, "Bamboo Bridge", "cross at dawn")
    assert id_c != id_a

    # normalise() is what absorbs cosmetic wording differences (case/punctuation), not the
    # hash itself skipping normalisation
    id_d = content_hash_atom_id(TENANT_ID, TOUR_ID, 3, "Bamboo-Bridge!!", "CROSS at Dawn")
    assert id_d == id_a


def test_normalise_matches_reference_repo_formula():
    assert normalise("CROSS the Historic Bamboo-Bridge at dawn!!") == "cross the historic bamboo bridge at dawn"
    assert normalise("") == ""
    assert normalise(None) == ""


def test_derive_atom_text_combines_place_and_action():
    """AA-509 — tour_atoms.text is derived, not LLM-written, once T5 returns place/action
    separately; still populated (not dropped) for T9/research/etc. (AA-754 removed the former
    score_distinctiveness() reader)."""
    assert derive_atom_text("Magome", "walk to Tsumago") == "Magome — walk to Tsumago"
    assert derive_atom_text("Magome", "") == "Magome"
    assert derive_atom_text("", "walk") == "walk"
    assert derive_atom_text("", "") == ""
    assert derive_atom_text(None, None) == ""
