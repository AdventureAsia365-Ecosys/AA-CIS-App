"""AA-1018 (reliability part 1) — the report-only failure-domain rule (shared/llm_client/invariant.py) and the
connection circuit / invariant in GET /admin/llm-availability (api/services/llm_availability.py). Invented stage and
model keys; the connection rows mirror the shape of the Dev registry (no account IDs)."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.services import llm_availability as av
from shared.llm_client import invariant

NOW = datetime(2026, 10, 31, 9, 0, tzinfo=timezone.utc)

CONNS = [
    {"connection_key": "acc2", "kind": "bedrock", "enabled": True, "data_scope": "aws_only", "account_id": None,
     "account_id_env": None},
    {"connection_key": "acc3", "kind": "bedrock", "enabled": True, "data_scope": "aws_only", "account_id": None,
     "account_id_env": "BEDROCK_ACCT_PRIMARY"},
    {"connection_key": "anthropic-direct", "kind": "anthropic", "enabled": False, "data_scope": "vendor_direct",
     "account_id": None, "account_id_env": None},
    {"connection_key": "openai-direct", "kind": "openai", "enabled": True, "data_scope": "vendor_direct",
     "account_id": None, "account_id_env": None},
]
DEPS = [
    {"model_key": "writer-x", "connection_key": "acc3", "priority": 10, "enabled": True},
    {"model_key": "writer-x", "connection_key": "anthropic-direct", "priority": 100, "enabled": True},
    {"model_key": "judge-y", "connection_key": "acc3", "priority": 10, "enabled": True},
    {"model_key": "judge-y-direct", "connection_key": "openai-direct", "priority": 10, "enabled": True},
    {"model_key": "embed-z", "connection_key": "acc2", "priority": 20, "enabled": True},
    {"model_key": "legacy-w", "connection_key": "acc3", "priority": 10, "enabled": True},
    {"model_key": "legacy-w", "connection_key": "acc2", "priority": 20, "enabled": False},
]
ALIASES = [{"alias_key": "old-w", "position": 1, "model_key": "legacy-w"},
           {"alias_key": "old-w", "position": 2, "model_key": "judge-y"}]


def _route(stage, role, model, *fallbacks, scopes=("aws_only", "vendor_direct"), active=True):
    return {"stage": stage, "role": role, "model_id": model, "fallback_model_ids": list(fallbacks),
            "allowed_scopes": list(scopes) if scopes is not None else None, "is_active": active}


def _stage(report, stage):
    return next(s for s in report["stages"] if s["stage"] == stage)


def test_the_failure_domain_is_kind_account_and_scope():
    assert invariant.failure_domain(CONNS[0]) == "bedrock:acc2:aws_only"
    assert invariant.failure_domain(CONNS[1]) == "bedrock:BEDROCK_ACCT_PRIMARY:aws_only"
    assert invariant.failure_domain({**CONNS[1], "account_id": "<account>"}) == "bedrock:<account>:aws_only"
    assert invariant.failure_domain(CONNS[3]) == "openai:openai-direct:vendor_direct"


def test_a_writer_whose_warm_path_is_disabled_breaks_the_rule():
    report = invariant.check([_route("w1", "writer", "writer-x")], ALIASES, DEPS, CONNS)
    s = _stage(report, "w1")
    assert s["ok"] is False and s["primary"] == "acc3 (bedrock:BEDROCK_ACCT_PRIMARY:aws_only)"
    assert s["reason"] == "every usable deployment is in bedrock:BEDROCK_ACCT_PRIMARY:aws_only"
    assert report["ok"] is False and report["breaches"] == 1 and report["report_only"] is True


def test_enabling_the_warm_path_satisfies_it():
    conns = [{**c, "enabled": True} for c in CONNS]
    s = _stage(invariant.check([_route("w1", "writer", "writer-x")], ALIASES, DEPS, conns), "w1")
    assert s["ok"] is True and s["other_domains"] == ["anthropic:anthropic-direct:vendor_direct"]


def test_a_fallback_model_in_another_domain_counts():
    s = _stage(invariant.check([_route("j1", "judge", "judge-y", "judge-y-direct")], ALIASES, DEPS, CONNS), "j1")
    assert s["ok"] is True and s["other_domains"] == ["openai:openai-direct:vendor_direct"]


def test_an_aws_only_stage_cannot_count_a_vendor_fallback():
    s = _stage(invariant.check([_route("j1", "judge", "judge-y", "judge-y-direct", scopes=("aws_only",))],
                               ALIASES, DEPS, CONNS), "j1")
    assert s["ok"] is False


def test_no_stage_policy_keeps_vendor_rows_out_of_the_fallbacks():
    s = _stage(invariant.check([_route("j1", "judge", "judge-y", "judge-y-direct", scopes=None)], ALIASES, DEPS, CONNS),
               "j1")
    assert s["ok"] is False


def test_a_connection_out_of_credit_is_not_funded():
    report = invariant.check([_route("j1", "judge", "judge-y", "judge-y-direct")], ALIASES, DEPS, CONNS,
                             blocked={"openai-direct": "quota_exhausted"})
    assert _stage(report, "j1")["ok"] is False
    # an access-denied block is a live outage, not funding: the structure still has the other domain
    report = invariant.check([_route("j1", "judge", "judge-y", "judge-y-direct")], ALIASES, DEPS, CONNS,
                             blocked={"acc3": "access_denied"})
    assert _stage(report, "j1")["ok"] is True


def test_aliases_expand_to_their_targets_in_order():
    s = _stage(invariant.check([_route("e1", "writer", "old-w")], ALIASES, DEPS, CONNS), "e1")
    assert s["primary"].startswith("acc3") and s["ok"] is False      # legacy-w on acc3, judge-y on acc3


def test_a_route_with_nothing_usable_is_a_breach_and_inactive_stages_are_skipped():
    report = invariant.check([_route("x1", "validate", "nothing-here"),
                              _route("x2", "writer", "writer-x", active=False)], ALIASES, DEPS, CONNS)
    assert report["stages_checked"] == 1
    assert _stage(report, "x1")["reason"] == "no enabled, funded deployment on the route"


def test_stage_classes_group_by_role():
    routes = [_route("w1", "writer", "writer-x"), _route("j1", "judge", "judge-y", "judge-y-direct"),
              _route("e1", "embed", "embed-z", scopes=("aws_only",))]
    report = invariant.check(routes, ALIASES, DEPS, CONNS)
    assert report["classes"] == [
        {"role": "embed", "stages": 1, "ok": 0, "breaches": ["e1"]},
        {"role": "judge", "stages": 1, "ok": 1, "breaches": []},
        {"role": "writer", "stages": 1, "ok": 0, "breaches": ["w1"]},
    ]
    assert report["domains"] == ["bedrock:BEDROCK_ACCT_PRIMARY:aws_only", "bedrock:acc2:aws_only",
                                 "openai:openai-direct:vendor_direct"]


@pytest.mark.asyncio
async def test_read_before_the_registry_migrations_is_not_ready():
    import asyncpg

    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=asyncpg.exceptions.UndefinedColumnError("allowed_scopes"))
    assert await invariant.read(conn) == {"ready": False, "report_only": True}


@pytest.mark.asyncio
async def test_read_runs_the_four_registry_queries():
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=[[_route("j1", "judge", "judge-y", "judge-y-direct")], ALIASES, DEPS, CONNS])
    out = await invariant.read(conn, now=NOW)
    assert out["ready"] is True and out["ok"] is True and out["checked_at"] == NOW.isoformat()
    sqls = [c.args[0] for c in conn.fetch.await_args_list]
    assert "shared.llm_role_config" in sqls[0] and "shared.llm_provider_connection" in sqls[3]


# ── llm_availability: connection circuit + failure domain ───────────────────────────────────────────────────────────

def _health_row(model, cls, at, open_s=300, **conn):
    return {"connection_key": "acc3", "kind": "bedrock", "label": "LLM account", "enabled": True,
            "data_scope": "aws_only", "account_id": None, "account_id_env": "BEDROCK_ACCT_PRIMARY",
            "model_key": model, "last_ok_at": None, "last_error_at": at, "last_error_class": cls, "last_error": "e",
            "consecutive_failures": 1, "circuit_open_until": at + timedelta(seconds=open_s), "open_streak": 1,
            "opened_at": at, "probe_until": None, **conn}


def test_availability_shows_the_connection_circuit_and_its_domain():
    rows = [_health_row("model-a", "access_denied", NOW), _health_row("model-b", "access_denied", NOW)]
    (c,) = av.build_connections(rows, now=NOW + timedelta(seconds=10))
    assert c["failure_domain"] == "bedrock:BEDROCK_ACCT_PRIMARY:aws_only"
    circuit = c["circuit"]
    assert circuit["model_key"] == "*" and circuit["state"] == "open" and circuit["refusing"] is True
    assert circuit["error_class"] == "access_denied" and circuit["models"] == ["model-a", "model-b"]
    assert av.blocked_from([c]) == {"acc3": "access_denied"}


def test_one_refused_model_leaves_the_connection_circuit_closed():
    (c,) = av.build_connections([_health_row("model-a", "access_denied", NOW)], now=NOW)
    assert c["circuit"]["state"] == "closed" and c["circuit"]["refusing"] is False and c["circuit"]["models"] == []
    assert av.blocked_from([c]) == {}


def test_an_old_credit_refusal_is_half_open_but_still_refusing():
    rows = [_health_row("model-a", "quota_exhausted", NOW - timedelta(days=1), connection_key="openai-direct",
                        kind="openai", data_scope="vendor_direct", account_id_env=None)]
    (c,) = av.build_connections(rows, now=NOW)
    assert c["circuit"]["state"] == "half_open" and c["circuit"]["refusing"] is True
    assert c["failure_domain"] == "openai:openai-direct:vendor_direct"


def test_an_old_account_block_is_half_open_but_still_refusing_until_a_call_answers():
    # review S262: acc3 refused on two models a day ago, nothing answered since -> still refusing (re-queue holds)
    rows = [_health_row("model-a", "access_denied", NOW - timedelta(days=1)),
            _health_row("model-b", "access_denied", NOW - timedelta(days=1))]
    (c,) = av.build_connections(rows, now=NOW)
    assert c["circuit"]["state"] == "half_open" and c["circuit"]["refusing"] is True
    answered = rows + [_health_row("model-c", None, NOW, last_error_at=None, last_ok_at=NOW - timedelta(hours=1),
                                   circuit_open_until=None, consecutive_failures=0, opened_at=None)]
    (c,) = av.build_connections(answered, now=NOW)
    assert c["circuit"]["state"] == "closed" and c["circuit"]["refusing"] is False


def test_a_connection_without_health_rows_has_a_closed_circuit():
    row = {"connection_key": "acc2", "kind": "bedrock", "label": "main", "enabled": True, "data_scope": "aws_only",
           "account_id": None, "account_id_env": None, "model_key": None}
    (c,) = av.build_connections([row], now=NOW)
    assert c["circuit"]["state"] == "closed" and c["failure_domain"] == "bedrock:acc2:aws_only"
