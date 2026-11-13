"""S279 thresholds L1 — the readers switched to the settings registry keep today's values and follow a changed value.

Parity (the change-control evidence of the gates move): with no stored value every switched reader returns exactly the
value it had before the move (the literal, the Terraform env value or the thresholds.yaml value: 600 / 5.8 / 0.9, the
T9 3 attempts, A1 3 retries, the T2 version budget 2 / 3 / 1 / 0.25 USD, the 25 USD writing-memory month, the 50 USD
research run, 50 backfill tours, 2,000 wave tours, the 3 USD top-up, the DataForSEO price table, 20 posts a day, the
soft over-quota rule, the 7-day retirement notice, the 180 / 90 / 30 / 90-day retention windows and the 30-day erasure
window). Follow: a value the registry serves (monkeypatched ``settings.get``) reaches each reader. No DB."""
import pytest

from shared import settings

KEYS_BEFORE = {
    "cis.t7.blog_min_seed_words": 600, "cis.t7.blog_chars_per_seed_word": 5.8, "cis.t9.repeat_drop_near_exact": 0.9,
    "cis.t9.max_attempts": 3, "cis.a1.attempts_before_review": 3, "cis.t2.version_writer_max": 2,
    "cis.t2.version_repair_max": 3,
    "cis.t2.version_voice_max": 1, "cis.t2.version_usd_max": 0.25, "cis.writing_memory.month_cap_usd": 25,
    "cis.spend.segment_research_max_usd_per_run": 50, "cis.spend.place_backfill_max_tours": 50,
    "cis.spend.rerun_wave_max_tours": 2000, "cis.spend.question_topup_cap_usd": 3,
    "cis.publish.daily_limit_default": 20, "cis.billing.rewrite_over_quota_mode": "soft",
    "cis.lifecycle.tour_retirement_notice_days": 7, "cis.retention.llm_call_log_days": 180,
    "cis.retention.tenant_api_usage_days": 90, "cis.retention.job_succeeded_days": 30,
    "cis.retention.job_failed_days": 90, "cis.erasure.tenant_retention_days": 30,
}


@pytest.fixture
def no_env(monkeypatch):
    for k in settings.READER.keys():
        env = settings.definition(k).env_override
        if env:
            monkeypatch.delenv(env, raising=False)
    return monkeypatch


@pytest.fixture
def served(no_env):
    """Serve the given values through the registry (as an approved platform row would)."""
    values: dict = {}
    real = settings.READER.get

    def fake(key, tenant_id=None):
        return values[key] if key in values else real(key, tenant_id)
    no_env.setattr(settings.READER, "get", fake)
    return values


@pytest.mark.parametrize("key", sorted(KEYS_BEFORE))
def test_defaults_are_todays_values(no_env, key):
    assert settings.get(key) == KEYS_BEFORE[key]


def test_parity_of_the_switched_readers(no_env):
    from api.services import cost_incidents
    from services.acp_content_writing import repeat_drop, service as t9
    from services.acp_publish import daily_limit
    from services.acp_shared import blog_seed, question_topup
    from services.content_generation import graph
    from services.content_generation.t2_orchestrator import VersionBudget
    from services.jobs import erasure_job, segment_research_job
    from services.places import backfill
    from services.retention import policy
    from services.tour_lifecycle import fanout
    from services.writing_memory import config as wm
    from shared import cost_guard

    assert (blog_seed.min_seed_words(), blog_seed.chars_per_seed_word(), repeat_drop.near_exact_share()) == \
        (600, 5.8, 0.9)
    assert (t9.max_attempts(), t9.MAX_ATTEMPTS, graph.max_retries(), graph.MAX_RETRIES) == (3, 3, 3, 3)
    b = VersionBudget()
    assert (b.writer_max, b.repair_max, b.voice_max, b.usd_max) == (2, 3, 1, 0.25)
    assert (wm.month_cap_usd(), wm.MONTH_CAP_USD) == (25.0, 25.0)
    assert segment_research_job.max_usd_per_run() == segment_research_job.MAX_USD_PER_RUN == 50.0
    assert backfill.max_tours() == backfill.MAX_TOURS == 50
    assert question_topup.topup_cap_usd() == question_topup.TOPUP_CAP_USD == 3.0
    assert daily_limit.platform_default_limit() == daily_limit.DEFAULT_DAILY_PUBLISH_LIMIT == 20
    assert daily_limit.DAILY_PUBLISH_LIMIT_MAX == 1000
    assert fanout.platform_notice_days() == fanout.DEFAULT_NOTICE_DAYS == 7
    assert cost_guard.dfs_call_estimate_usd() == cost_guard.DFS_CALL_ESTIMATE_USD == {
        "search_volume": 0.09, "search_volume_bulk": 0.09, "keywords_for_keywords": 0.09, "serp_advanced": 0.002,
        "serp_advanced_paa_deep": 0.01, "search_volume_bulk_queue": 0.06, "serp_advanced_queue": 0.0006}
    assert cost_guard.dfs_research_prices("standard") == (0.06, 0.0006)
    windows = {k: s.windows() for k, s in policy.current_specs().items()}
    assert windows["retention_llm_call_log"] == {"raw": 180} and windows["retention_tenant_api_usage"] == {"raw": 90}
    assert windows["retention_job"] == {"succeeded": 30, "failed": 90}
    assert {k: s.windows() for k, s in policy.SPECS.items()} == windows
    assert cost_incidents.llm_raw_retention_days() == cost_incidents.LLM_RAW_RETENTION_DAYS == 180
    assert erasure_job.tenant_retention_days() == erasure_job.tenant_retention_days({}) == 30


def test_a_served_value_reaches_each_reader(served):
    from api.services import cost_incidents
    from services.acp_content_writing import repeat_drop, service as t9
    from services.acp_publish import daily_limit
    from services.acp_shared import blog_seed, question_topup
    from services.content_generation import graph
    from services.content_generation.t2_orchestrator import VersionBudget
    from services.jobs import erasure_job, segment_research_job
    from services.places import backfill
    from services.retention import policy
    from services.tour_lifecycle import fanout
    from services.writing_memory import config as wm
    from shared import cost_guard

    served.update({
        "cis.t7.blog_min_seed_words": 900, "cis.t7.blog_chars_per_seed_word": 3.364,
        "cis.t9.repeat_drop_near_exact": 0.95, "cis.t9.max_attempts": 2, "cis.a1.attempts_before_review": 4,
        "cis.t2.version_writer_max": 1, "cis.t2.version_usd_max": 0.4, "cis.writing_memory.month_cap_usd": 10,
        "cis.spend.segment_research_max_usd_per_run": 20, "cis.spend.place_backfill_max_tours": 12,
        "cis.spend.question_topup_cap_usd": 1, "cis.publish.daily_limit_default": 5,
        "cis.lifecycle.tour_retirement_notice_days": 14, "cis.retention.llm_call_log_days": 365,
        "cis.retention.job_failed_days": 120, "cis.erasure.tenant_retention_days": 45,
        "cis.spend.dfs_call_estimate_usd": {"search_volume_bulk_queue": 0.07, "serp_advanced_queue": 0.001},
    })
    assert (blog_seed.min_seed_words(), blog_seed.chars_per_seed_word()) == (900, 3.364)
    assert blog_seed.seed_floor()["sized"] == 1398                     # the S279 measured ratio: floor 1,398 words
    assert repeat_drop.near_exact_share() == 0.95 and t9.max_attempts() == 2 and graph.max_retries() == 4
    b = VersionBudget()
    assert (b.writer_max, b.repair_max, b.usd_max) == (1, 3, 0.4)
    assert wm.month_cap_usd() == 10.0 and segment_research_job.max_usd_per_run() == 20.0
    assert backfill.max_tours() == 12 and question_topup.topup_cap_usd() == 1.0
    assert question_topup.plan([], cap_usd=5.0)["cap_usd"] <= 1.0
    assert daily_limit.platform_default_limit() == 5 and fanout.platform_notice_days() == 14
    assert cost_guard.dfs_research_prices("standard") == (0.07, 0.001)
    assert cost_guard.dfs_research_prices("live") == (0.09, 0.002)    # a label the table lacks: the compiled default
    spec = policy.spec_for("retention_job")
    assert spec.windows() == {"succeeded": 30, "failed": 120}
    assert policy.spec_for("retention_llm_call_log").windows() == {"raw": 365}
    assert policy.SPECS["retention_llm_call_log"].windows() == {"raw": 180}     # the compiled default stays
    assert cost_incidents.llm_raw_retention_days() == 365
    assert erasure_job.tenant_retention_days() == 45
    assert erasure_job.tenant_retention_days({"ERASURE_TENANT_RETENTION_DAYS": "0"}) == 0
    served["cis.t9.max_attempts"] = 9                                   # never above the loop's structural ceiling
    assert t9.max_attempts() == 3


def test_segment_research_max_usd_follows_the_setting(served):
    from services.jobs import segment_research_job as srj

    class _Budget:
        def remaining_usd(self):
            return None
    served["cis.spend.segment_research_max_usd_per_run"] = 20
    assert srj.max_usd_for({}, _Budget()) == 20.0 and srj.max_usd_for({"max_usd": 50}, _Budget()) == 20.0
    from api.routers.admin_segment_research import ResearchScope
    with pytest.raises(ValueError):
        ResearchScope(markets=["US"], max_usd=25)
    assert ResearchScope(markets=["US"], max_usd=15).max_usd == 15


@pytest.mark.asyncio
async def test_rewrite_over_quota_hard_mode_refuses(served):
    from fastapi import HTTPException

    from api.routers import v1_tours

    class _Conn:
        async def fetchrow(self, sql, *a):
            return {"plan": "content.starter", "quota": 2}

        async def fetchval(self, sql, *a):
            return 3                                   # the third rewrite of a 2-rewrite month

    async def plan_limit(conn, tenant_id):
        return "content.starter", 2
    served["cis.billing.rewrite_over_quota_mode"] = "soft"
    import unittest.mock as um
    with um.patch.object(v1_tours, "_get_tenant_plan_limit", plan_limit):
        await v1_tours._check_and_consume_rewrite_quota(_Conn(), "00000000-0000-4000-8000-00000000a279")
        served["cis.billing.rewrite_over_quota_mode"] = "hard"
        with pytest.raises(HTTPException) as e:
            await v1_tours._check_and_consume_rewrite_quota(_Conn(), "00000000-0000-4000-8000-00000000a279")
    assert e.value.status_code == 429 and "quota" in e.value.detail


def test_wave_size_follows_the_setting(served):
    from api.routers.admin_waves import WaveCreate
    assert WaveCreate(name="zz", tour_ids=["t"] * 2001).tour_ids    # the request bound is the setting's maximum
    with pytest.raises(ValueError):
        WaveCreate(name="zz", tour_ids=["t"] * 5001)


def test_tenant_ai_budget_defaults_fail_closed():
    from api.services import tenant_ai_budget
    assert tenant_ai_budget.DEFAULTS["hard_stop"] is True
