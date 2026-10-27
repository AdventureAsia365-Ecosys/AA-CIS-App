"""AA-741 G1 — tenant attribution through the gateway.

The job worker binds the job's tenant (call_log.bind_tenant) next to its job id; record_call*()
and decide() fall back to it when the caller passes no tenant_id. Mock-only, no DB — the
real-Postgres path is tests/integration/test_aa741_g1_tenant_attribution.py.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.jobs.worker import job_tenant_id
from shared.llm_client import call_log
from shared.llm_client import decide as d

TENANT = "11111111-2222-4333-8444-555555555555"
OTHER = "99999999-2222-4333-8444-555555555555"


def _kwargs(**extra):
    return dict(stage="s1_judge", role="judge", model="haiku-4-5", tokens_in=1, tokens_out=1,
                cost_usd=0.01, quality_signal={"ok": True}, **extra)


async def _logged_tenant(**extra):
    conn = AsyncMock()
    with patch("shared.llm_client.call_log.asyncpg.connect", AsyncMock(return_value=conn)), \
         patch("shared.llm_client.call_log.get_database_url", return_value="postgres://fake"):
        await call_log.record_call(**_kwargs(**extra))
    return conn.execute.call_args.args[1]   # $1 = tenant_id


# ── call_log ───────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_binding_keeps_tenant_null():
    assert await _logged_tenant() is None


@pytest.mark.asyncio
async def test_bound_tenant_fills_a_call_that_passed_none():
    with call_log.bind_tenant(TENANT):
        assert await _logged_tenant() == TENANT
        assert await _logged_tenant(tenant_id=None) == TENANT
        assert await _logged_tenant(tenant_id="None") == TENANT      # str(None) at a call site
    assert call_log.current_tenant_id() is None                     # reset after the block


@pytest.mark.asyncio
async def test_explicit_tenant_wins_over_the_binding():
    with call_log.bind_tenant(TENANT):
        assert await _logged_tenant(tenant_id=OTHER) == OTHER


@pytest.mark.asyncio
async def test_binding_reaches_tasks_and_threads():
    with call_log.bind_tenant(TENANT):
        task = asyncio.create_task(asyncio.to_thread(call_log.current_tenant_id))
    assert await task == TENANT                                   # created inside, awaited after


@pytest.mark.asyncio
async def test_pool_path_uses_the_binding():
    conn = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    with call_log.bind_tenant(TENANT):
        await call_log.record_call_with_pool(pool, **_kwargs())
    assert conn.execute.call_args.args[1] == TENANT


# ── worker: which tenant a job works for ───────────────────────────────────────────────────────

@pytest.mark.parametrize("payload,expected", [
    ({"tenant_id": TENANT, "version_id": "v"}, TENANT),                       # T2
    ({"request_id": "r", "context": {"tenant_id": TENANT}}, TENANT),          # T9 nested
    ({"request_id": "r", "tenant_id": TENANT, "goal": "promotion"}, TENANT),  # T8 angles (AA-764)
    ({"tenant_id": TENANT}, TENANT),                                          # T7 slate (AA-764)
    ({"tenant_id": call_log.PLATFORM_TENANT_ID, "tour_id": "t"}, None),       # S1 platform
    ({"tenant_id": "aa_internal", "tour_id": "t"}, None),                     # S1 platform slug (AA-993)
    ({"tour_ids": ["t"]}, None),                                              # no tenant
    ({"tenant_id": None}, None),
    ({"tenant_id": "None"}, None),
    (None, None),
])
def test_job_tenant_id(payload, expected):
    assert job_tenant_id(payload) == expected


def test_platform_id_matches_decide():
    assert call_log.PLATFORM_TENANT_ID == d.PLATFORM_TENANT_ID


# ── decide(): the bound tenant drives the allow-list guard and the logs ────────────────────────

def _q():
    return d.Question(key="kw_belongs", stage="t3", kind="noul", instructions="?", criteria=None,
                      mode="enforce", accept_floor=0.8, reject_ceiling=0.2, threshold_version=1)


def _conn(allowlist):
    conn = MagicMock()

    async def fetch(sql, *args):
        if "decision_question" in sql:
            q = _q()
            return [{"question_key": q.key, "stage": q.stage, "kind": q.kind,
                     "instructions": q.instructions, "criteria": None, "mode": q.mode,
                     "accept_floor": q.accept_floor, "reject_ceiling": q.reject_ceiling,
                     "threshold_version": 1, "question_hash": "h"}]
        if "FROM shared.decision_log" in sql:
            return []
        return [{"t": t} for t in allowlist]

    conn.fetch = AsyncMock(side_effect=fetch)
    conn.executemany = AsyncMock()
    return conn


@pytest.fixture(autouse=True)
def _fresh_decide_cache():
    d._questions_loaded_at = 0.0
    d._allowlist_loaded_at = 0.0
    d.reset_decision_memo()
    yield
    d.reset_decision_memo()


@pytest.mark.asyncio
async def test_decide_uses_the_bound_tenant_when_the_caller_passes_none():
    """Before G1 a T-stage decide() deep in a tenant job without tenant_id= was refused as
    'tenant stage called without tenant_id'; now the job's tenant is used — asked when it is
    on the allow-list, and the decision_log row carries it."""
    conn = _conn(allowlist=[TENANT])
    answer = {"model": "jev", "usage": {"input_tokens": 1},
              "answers": {"kw_belongs": {"type": "noul", "noul": 0.9}}}
    with patch.object(d, "_call_jev", new=AsyncMock(return_value=answer)) as m_call, \
         patch.object(d, "_price_in_per_mtok", return_value=0.042), \
         patch.object(d, "record_call_with_pool", new=AsyncMock()) as m_log:
        with call_log.bind_tenant(TENANT):
            dec = await d._decide(d._SingleConn(conn), "t3", "piece:1", "text", ["kw_belongs"], None)
    m_call.assert_awaited_once()
    assert dec.accepted("kw_belongs")
    assert conn.executemany.await_args.args[1][0][3] == TENANT     # decision_log.tenant_id
    assert m_log.await_args.kwargs["tenant_id"] == TENANT          # llm_call_log.tenant_id


@pytest.mark.asyncio
async def test_decide_refuses_a_bound_tenant_not_on_the_allow_list():
    conn = _conn(allowlist=[OTHER])
    with patch.object(d, "_call_jev", new=AsyncMock()) as m_call, \
         patch.object(d, "record_call_with_pool", new=AsyncMock()):
        with call_log.bind_tenant(TENANT):
            dec = await d._decide(d._SingleConn(conn), "a1_claims", "piece:1", "text",
                                  ["kw_belongs"], None)
    m_call.assert_not_awaited()
    v = dec.verdicts["kw_belongs"]
    assert v.zone == "skipped" and v.error == "tenant not on Jev allow-list"


# ── shadow calls run on a plain thread: job + tenant are carried over explicitly ───────────────

def test_shadow_thread_keeps_the_job_and_tenant():
    import threading
    from unittest.mock import patch as _patch
    from shared.llm_client.client import LLMClient
    from shared.llm_client.models import LLMRequest, LLMResponse
    from shared.llm_client.role_config import StageConfig

    with _patch("shared.llm_client.client.boto3"), _patch("shared.llm_client.client.openai"):
        c = LLMClient()
    cfg = StageConfig("s1_judge", "judge", "openai", "gpt-5.6-luna", None, shadow_model_id="gpt-4.1")
    primary = LLMResponse(content="{}", model_used="satellite-gpt-5.6-luna", provider="p")
    seen, done = {}, threading.Event()

    def _rec(**_kw):
        seen["job"], seen["tenant"] = call_log.current_job_id(), call_log.current_tenant_id()
        done.set()

    with _patch.object(c, "_call_key", return_value=primary), \
            _patch("shared.llm_client.shadow.record_shadow_sync", side_effect=_rec):
        with call_log.bind_job("11111111-1111-1111-1111-111111111111"), call_log.bind_tenant(TENANT):
            c._start_shadow(LLMRequest(system_prompt="s", user_prompt="u", stage="s1_judge"),
                            cfg, primary, "acc3", "acc1")
        assert done.wait(5)
    assert seen == {"job": "11111111-1111-1111-1111-111111111111", "tenant": TENANT}
