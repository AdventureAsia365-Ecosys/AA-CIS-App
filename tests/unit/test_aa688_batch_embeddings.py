"""AA-688 — PAA question landing embeds in batches (up to 96 texts per Cohere call) instead of one
paced 3.5 s call per text. Covers the batch embedding function, the two batch cache helpers, the
pre-pass in precompute_question_landings(), and the A3 job's progress forwarding.

Mocked Bedrock/DB only — no real call."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_contract import atom_matching as am_mod
from services.acp_contract.atom_matching import (
    _question_hash,
    embed_questions_cached,
    ensure_atom_embeddings_batch,
)
from services.acp_contract.atom_ranking import land_questions_for_segment, precompute_question_landings
from services.acp_shared import content_embedding as ce_mod
from services.acp_shared.content_embedding import MAX_TEXTS_PER_CALL, compute_embeddings
from shared.llm_client import embed as embed_mod
from shared.llm_client.role_config import SAFE_DEFAULTS


def _client_echoing_batch(fail_calls: set[int] = frozenset()):
    """A Bedrock client whose Nth invoke_model (0-based) fails if N is in `fail_calls`; otherwise
    returns one 4-float vector per text, the first float = the text's length (to check order)."""
    client = MagicMock()
    calls = {"n": 0}

    def invoke_model(**kw):
        n = calls["n"]
        calls["n"] += 1
        if n in fail_calls:
            raise RuntimeError("Throttling")
        texts = json.loads(kw["body"])["texts"]
        body = MagicMock()
        body.read.return_value = json.dumps(
            {"embeddings": {"float": [[float(len(t)), 0.0, 0.0, 0.0] for t in texts]}}).encode()
        return {"body": body}

    client.invoke_model.side_effect = invoke_model
    return client


@pytest.fixture
def gateway_offline():
    with patch.object(embed_mod, "get_stage_config_sync", return_value=SAFE_DEFAULTS["f10_embed"]), \
         patch.object(embed_mod, "get_model_sync", return_value=None), \
         patch.object(ce_mod, "EMBEDDING_DIMENSIONS", None), \
         patch.object(ce_mod, "record_call_sync") as m_log, \
         patch.object(ce_mod, "time") as m_time:
        m_time.monotonic.return_value = 0.0
        yield m_log


# ── compute_embeddings ──────────────────────────────────────────────────────────────────────


def test_compute_embeddings_splits_into_calls_of_96(gateway_offline):
    texts = [f"text {i}" for i in range(200)]
    client = _client_echoing_batch()
    with patch.object(embed_mod, "_runtime_for", return_value=client):
        out = compute_embeddings(texts)
    assert client.invoke_model.call_count == 3  # 96 + 96 + 8, not 200
    sizes = [len(json.loads(c.kwargs["body"])["texts"]) for c in client.invoke_model.call_args_list]
    assert sizes == [MAX_TEXTS_PER_CALL, MAX_TEXTS_PER_CALL, 8]
    assert [v[0] for v in out] == [float(len(t)) for t in texts]  # order kept
    # one llm_call_log row per call, carrying the batch size
    assert [c.kwargs["quality_signal"]["texts"] for c in gateway_offline.call_args_list] == [96, 96, 8]


def test_compute_embeddings_paces_per_call_not_per_text(gateway_offline):
    with patch.object(embed_mod, "_runtime_for", return_value=_client_echoing_batch()), \
         patch.object(ce_mod, "_pace_calls") as m_pace:
        compute_embeddings([f"q{i}" for i in range(100)])
    assert m_pace.call_count == 2


def test_compute_embeddings_blank_texts_are_none_and_not_sent(gateway_offline):
    client = _client_echoing_batch()
    with patch.object(embed_mod, "_runtime_for", return_value=client):
        out = compute_embeddings(["abc", "   ", "", "de"])
    assert out[1] is None and out[2] is None
    assert out[0][0] == 3.0 and out[3][0] == 2.0
    assert json.loads(client.invoke_model.call_args.kwargs["body"])["texts"] == ["abc", "de"]


def test_compute_embeddings_failed_batch_is_none_only_for_that_batch(gateway_offline):
    texts = [f"t{i}" for i in range(100)]
    with patch.object(embed_mod, "_runtime_for", return_value=_client_echoing_batch(fail_calls={0})):
        out = compute_embeddings(texts)
    assert all(v is None for v in out[:96])
    assert all(v is not None for v in out[96:])
    assert gateway_offline.call_count == 1  # no log row for the failed call


# ── embed_questions_cached ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_embed_questions_cached_embeds_only_misses_in_one_call():
    cached_q = "What is Gandan Monastery"
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[
        {"question_hash": _question_hash(cached_q), "embedding": "[0.5,0.5]"}])
    conn.executemany = AsyncMock()
    with patch.object(am_mod, "compute_embeddings",
                      side_effect=lambda texts: [[1.0, 2.0] for _ in texts]) as m_embed:
        out = await embed_questions_cached(conn, [cached_q, "Is it free", "How old is it"])
    m_embed.assert_called_once()
    assert sorted(m_embed.call_args.args[0]) == ["How old is it", "Is it free"]
    assert out[cached_q] == [0.5, 0.5]
    assert out["Is it free"] == [1.0, 2.0]
    rows = conn.executemany.call_args.args[1]
    assert len(rows) == 2  # both misses written to the cache


@pytest.mark.asyncio
async def test_embed_questions_cached_same_hash_embedded_once_and_mapped_to_both():
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.executemany = AsyncMock()
    with patch.object(am_mod, "compute_embeddings",
                      side_effect=lambda texts: [[1.0] for _ in texts]) as m_embed:
        out = await embed_questions_cached(conn, ["Is it Free", "is it   free"])
    assert len(m_embed.call_args.args[0]) == 1
    assert out == {"Is it Free": [1.0], "is it   free": [1.0]}


@pytest.mark.asyncio
async def test_embed_questions_cached_failed_embedding_is_absent_and_not_cached():
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.executemany = AsyncMock()
    with patch.object(am_mod, "compute_embeddings", return_value=[None]):
        out = await embed_questions_cached(conn, ["Is it free"])
    assert out == {}
    conn.executemany.assert_not_awaited()


@pytest.mark.asyncio
async def test_embed_questions_cached_reports_progress():
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.executemany = AsyncMock()
    seen = []
    with patch.object(am_mod, "compute_embeddings", side_effect=lambda texts: [[1.0] for _ in texts]):
        await embed_questions_cached(conn, [f"question {i}" for i in range(100)], seen.append)
    assert seen == [{"step": "embedding_questions", "done": 96, "total": 100},
                    {"step": "embedding_questions", "done": 100, "total": 100}]


# ── ensure_atom_embeddings_batch ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_atom_embeddings_batch_embeds_only_missing_views():
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[
        {"atom_id": "a1", "view": "place"}, {"atom_id": "a1", "view": "place_action"},
        {"atom_id": "a2", "view": "place"},
    ])
    conn.executemany = AsyncMock()
    with patch.object(am_mod, "compute_embeddings",
                      side_effect=lambda texts: [[1.0] for _ in texts]) as m_embed:
        written = await ensure_atom_embeddings_batch(conn, [
            ("a1", "Gandan Monastery", "visit"),
            ("a2", "Sukhbaatar Square", "walk"),
            ("a3", "Terelj", "ride"),
        ])
    assert m_embed.call_count == 1  # one call for all 3 missing views
    assert m_embed.call_args.args[0] == ["Sukhbaatar Square walk", "Terelj", "Terelj ride"]
    assert written == 3
    assert [r[:2] for r in conn.executemany.call_args.args[1]] == [
        ("a2", "place_action"), ("a3", "place"), ("a3", "place_action")]


@pytest.mark.asyncio
async def test_ensure_atom_embeddings_batch_skips_blank_view_and_failed_vectors():
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.executemany = AsyncMock()
    with patch.object(am_mod, "compute_embeddings", return_value=[None]):
        written = await ensure_atom_embeddings_batch(conn, [("a1", "", "visit")])
    # "place" view is blank (not sent); "place_action" = "visit" failed → nothing written
    assert written == 0
    conn.executemany.assert_not_awaited()


# ── land_questions_for_segment with prefetched vectors ─────────────────────────────────────


@pytest.mark.asyncio
async def test_question_without_vector_skips_vector_lookup_and_uses_claim_by_name():
    paa_rows = [("sukhbaatar square", "US", ["What is Sukhbaatar Square"])]
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[{"atom_id": "atom-1", "place": "Sukhbaatar Square", "action": "visit"}])
    conn.execute = AsyncMock()
    with patch("services.acp_contract.atom_ranking.land_question_on_atom", AsyncMock()) as m_land:
        count = await land_questions_for_segment(
            conn, "Sukhbaatar Square", "visit", ["atom-1"], paa_rows,
            question_vectors={}, atoms_embedded=True,
        )
    m_land.assert_not_awaited()
    assert count == 1  # claim-by-name fallback still counts it
    assert conn.execute.call_args.args[4] == "tokens"


# ── precompute_question_landings pre-pass ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_precompute_embeds_all_segments_up_front_once():
    pool = MagicMock()
    conn = AsyncMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    conn.fetch = AsyncMock(side_effect=[
        [
            {"segment_id": "seg-1", "canonical_place": "Sukhbaatar Square",
             "canonical_action": "visit", "questions_count": None, "atom_ids": ["atom-1"]},
            {"segment_id": "seg-2", "canonical_place": "Gandan Monastery",
             "canonical_action": "visit", "questions_count": None, "atom_ids": ["atom-2"]},
        ],
        [
            {"keyword": "sukhbaatar square", "market": "US", "people_also_ask": ["Is Sukhbaatar Square free"]},
            {"keyword": "gandan monastery", "market": "US", "people_also_ask": json.dumps(["Gandan Monastery hours"])},
        ],
        [  # atom rows for the pre-pass
            {"atom_id": "atom-1", "place": "Sukhbaatar Square", "action": "visit"},
            {"atom_id": "atom-2", "place": "Gandan Monastery", "action": "visit"},
        ],
    ])
    conn.executemany = AsyncMock()
    vectors = {"Is Sukhbaatar Square free": [1.0], "Gandan Monastery hours": [2.0]}
    progress = []

    with patch("services.acp_contract.atom_ranking.ensure_atom_embeddings_batch",
               AsyncMock(return_value=4)) as m_atoms, \
         patch("services.acp_contract.atom_ranking.embed_questions_cached",
               AsyncMock(return_value=vectors)) as m_q, \
         patch("services.acp_contract.atom_ranking.land_questions_for_segment",
               AsyncMock(return_value=1)) as m_land, \
         patch("services.acp_contract.atom_ranking.filter_candidates_by_landing",   # AA-694: Jev gate
               AsyncMock(return_value={})):
        counts = await precompute_question_landings(pool, progress=progress.append)

    m_atoms.assert_awaited_once()
    assert sorted(a[0] for a in m_atoms.call_args.args[1]) == ["atom-1", "atom-2"]
    m_q.assert_awaited_once()
    assert m_q.call_args.args[1] == set(vectors)
    assert m_land.await_count == 2
    for call in m_land.call_args_list:
        assert call.kwargs["question_vectors"] is vectors
        assert call.kwargs["atoms_embedded"] is True
        assert call.kwargs["candidates"]          # AA-694: the filtered shortlist is passed through
    assert counts == {"seg-1": 1, "seg-2": 1}
    assert progress[-1] == {"step": "landing_questions", "done": 2, "total": 2}


# ── A3 job progress forwarding ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a3_job_forwards_thread_progress_to_job_context():
    from services.jobs import a3_atomize_job

    ctx = MagicMock()
    ctx.payload = {"tour_id": "t1", "version_id": "v1", "rewritten": {}}
    ctx.job_id = "job-1"
    ctx.progress = AsyncMock()

    async def fake_background(**kw):
        kw["progress"]({"step": "embedding_atoms", "done": 96, "total": 200})

    with patch("services.export.handler._run_a3_atomize_background", fake_background):
        await a3_atomize_job.run(ctx)
        await asyncio.sleep(0)  # let the scheduled progress write run

    fields = [c.kwargs for c in ctx.progress.await_args_list]
    assert {"step": "embedding_atoms", "done": 96, "total": 200} in fields
    assert {"phase": "done"} in fields


@pytest.mark.asyncio
async def test_recompute_reports_ranking_and_route_steps():
    """AA-687 — after the landing, ranking (one pass per market) and route detection report their
    own steps, so the Jobs page does not sit on "landing_questions · n/n · ETA 0s"."""
    from services.export import handler
    from services.seo_intelligence.seed_builder import DFS_LOCATION_MAP

    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    seen = []
    with patch("services.acp_contract.segment_matching.run_segment_matching", AsyncMock(return_value={})), \
         patch("services.acp_contract.atom_ranking.precompute_question_landings", AsyncMock(return_value={})), \
         patch("services.acp_contract.atom_ranking.run_atom_ranking", AsyncMock(return_value={})), \
         patch("services.acp_contract.route_detection.run_route_detection", AsyncMock(return_value={})):
        await handler.recompute_segment_score_route("tour-1", pool, progress=seen.append)
    n = len(DFS_LOCATION_MAP)
    assert [s for s in seen if s["step"] == "ranking_markets"][-1] == {"step": "ranking_markets", "done": n, "total": n}
    assert seen[-1] == {"step": "route_detection", "done": 1, "total": 1}
