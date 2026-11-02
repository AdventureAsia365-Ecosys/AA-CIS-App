"""AA-1025 D3 (root ADR 0009 decision 8 read federated, DM-10.4; tech lead T-14 option b) — the small parts:

- geo.geocode_call rows carry status / latency_ms / app / stage (migration 346): the writer fills them (and falls back
  before 346, then before 319), the Geoapify clients measure the provider time, every geocode call site names its
  stage;
- a Jev call's llm_call_log row has role 'decide' (answered, refused and the daily canary);
- the admin Calls tab / trace show the right outcome: a geocode batch's own status (refused included), its latency,
  app and stage; a Jev ask refused before an answer is 'refused', not 'error'.
No database, no vendor: fakes only; invented places and texts.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from api.services import ai_gateway as gw
from api.services import ai_gateway_calls as calls
from services.places import store
from shared import provider_circuit
from shared.llm_client import app_profile, call_log
from shared.llm_client import decide as d

REPO = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
JOB = "00000000-0000-4000-8000-0000000000aa"


# ── the geocode_call writer ─────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("requests,failed,status", [(5, 0, "ok"), (5, 2, "ok"), (5, 5, "error"), (5, None, "ok"),
                                                     (3, 4, "error"), (2, True, "ok")])
def test_the_batch_status(requests, failed, status):
    assert store.geocode_status(requests, failed) == status


def test_the_v3_row_carries_status_latency_app_and_stage():
    conn = AsyncMock()
    asyncio.run(store.log_geocode_calls(conn, "geoapify", 4, 0.0, JOB, failed_requests=4, stage="place_geocode",
                                        latency_ms=1234))
    sql, *args = conn.execute.call_args.args
    assert sql == store._GEOCODE_CALL_V3_SQL
    assert args[:6] == ["geoapify", 4, 0.0, JOB, 4, "search"]
    assert args[8:] == ["error", 1234, "cis", "place_geocode"]


@pytest.mark.parametrize("kw,expected", [
    ({"status": "refused"}, ["refused", None, "cis", None]),
    ({"status": "shrug"}, ["ok", None, "cis", None]),                     # an unknown status is derived
    ({"latency_ms": -5, "stage": "Place Geocode"}, ["ok", None, "cis", None]),   # the 346 CHECKs, kept here too
    ({"latency_ms": True, "stage": "x" * 65}, ["ok", None, "cis", None]),
    ({"latency_ms": 0, "stage": "adhoc_conn_test"}, ["ok", 0, "cis", "adhoc_conn_test"]),
])
def test_values_the_table_would_refuse_are_dropped(kw, expected):
    conn = AsyncMock()
    asyncio.run(store.log_geocode_calls(conn, "geoapify", 1, 0.0, None, **kw))
    assert list(conn.execute.call_args.args[9:]) == expected


def test_no_request_no_row():
    conn = AsyncMock()
    asyncio.run(store.log_geocode_calls(conn, "geoapify", 0, 0.0, JOB, stage="place_geocode"))
    assert conn.execute.await_count == 0


def test_before_346_the_row_is_the_319_one():
    conn = AsyncMock()
    conn.execute.side_effect = [asyncpg.exceptions.UndefinedColumnError("status"), None]
    asyncio.run(store.log_geocode_calls(conn, "geoapify", 2, 0.0, JOB, failed_requests=1, stage="place_shape"))
    assert [c.args[0] for c in conn.execute.call_args_list] == [store._GEOCODE_CALL_V3_SQL,
                                                                 store._GEOCODE_CALL_V2_SQL]


def test_the_geoapify_clients_sum_their_provider_time():
    from services.places.geocoder import GeoapifyGeocoder
    responses = iter([MagicMock(status_code=503, headers={}), MagicMock(status_code=200, json=lambda: {"results": []})])
    http = MagicMock()
    http.get = AsyncMock(side_effect=lambda *a, **k: next(responses))
    g = GeoapifyGeocoder(http=http, api_key="k", sleep=AsyncMock(), rate_per_s=1000)
    asyncio.run(g.search("Lake Varra", "np"))
    assert g.requests == 2 and isinstance(g.latency_ms, int) and g.latency_ms >= 0


def test_every_geocode_call_site_names_its_stage():
    for path, stage in (("services/jobs/place_geocode_job.py", "stage=KIND"),
                        ("services/jobs/place_relocate_job.py", "stage=KIND"),
                        ("services/jobs/place_shape_job.py", "stage=KIND"),
                        ("services/jobs/llm_probe_job.py", "stage=svc.TEST_STAGE")):
        text = (REPO / path).read_text(encoding="utf-8")
        sites = text.count("log_geocode_calls(")
        assert sites >= 1 and text.count(stage) >= sites, path


# ── the Jev role ────────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_jev_ledger_row_is_role_decide():
    assert d.JEV_ROLE == "decide"
    from shared.aa_llm.ledger import ROLES
    assert "decide" in ROLES                                             # the CHECK of CIS 319
    text = (REPO / "shared" / "llm_client" / "decide.py").read_text(encoding="utf-8")
    assert 'role="validate"' not in text
    canary = (REPO / "api" / "routers" / "admin_llm_ops.py").read_text(encoding="utf-8")
    assert "stage=JEV_CANARY_STAGE, role=decide_mod.JEV_ROLE" in canary


def test_a_refused_jev_ask_is_role_decide_too():
    conn = MagicMock()

    async def fetch(sql, *args):
        if "decision_question" in sql:
            return [{"question_key": "kw_fits", "stage": "a3_research", "kind": "noul",
                     "instructions": "Does it fit?", "criteria": None, "mode": "shadow", "accept_floor": None,
                     "reject_ceiling": None, "threshold_version": 1, "question_hash": "h"}]
        return []
    conn.fetch = AsyncMock(side_effect=fetch)
    conn.fetchrow = AsyncMock(return_value=None)
    conn.executemany = AsyncMock()
    d._questions_loaded_at = 0.0
    d.reset_decision_memo()
    with patch.object(d, "jev_enabled", return_value=False), \
            patch.object(d, "record_call_with_pool", new=AsyncMock()) as m_log:
        asyncio.run(d._decide(d._SingleConn(conn), "a3_research", "kw:XX:reef", "reef", ["kw_fits"], None,
                              use_cache=False))
    row = m_log.await_args.kwargs
    assert (row["role"], row["status"], row["provider"]) == ("decide", "refused", "typesafe")
    d.reset_decision_memo()


# ── what the Calls tab and the trace show ──────────────────────────────────────────────────────────────────────────

def _geo(**kw):
    row = {"at": NOW, "provider": "geoapify", "endpoint": "search", "requests": 4, "credits": 4,
           "failed_requests": 0, "cost_usd": 0.0, "job_id": JOB, "status": None, "latency_ms": None, "app": None,
           "stage": None}
    row.update(kw)
    return row


def test_a_geocode_row_shows_its_own_status_latency_app_and_stage():
    e = gw.geo_event(_geo(status="refused", latency_ms=0, app="cis", stage="place_relocate"))
    assert (e["status"], e["outcome"], e["latency_ms"], e["app"], e["stage"]) == \
        ("refused", "refused", 0, "cis", "place_relocate")


def test_a_row_before_346_is_derived_as_before():
    assert gw.geo_event(_geo(failed_requests=4))["status"] == "error"
    e = gw.geo_event(_geo(failed_requests=1))
    assert (e["status"], e["app"], e["stage"], e["latency_ms"]) == ("ok", "cis", None, None)
    assert gw.geo_status({"status": "billed_unusable", "requests": 1, "failed_requests": 0}) == "ok"


@pytest.mark.parametrize("row,status", [
    ({"zone": "accept", "error": None}, "ok"),
    ({"zone": "skipped", "error": None}, "ok"),                           # question off: nothing asked
    ({"zone": "skipped", "error": "tenant not on Jev allow-list"}, "refused"),
    ({"zone": "error", "error": d.JEV_SWITCHED_OFF}, "refused"),
    ({"zone": "error", "error": d.JEV_BUDGET_EXCEEDED}, "refused"),
    ({"zone": "error", "error": "credit_exhausted (breaker open)"}, "refused"),
    ({"zone": "error", "error": "credit_exhausted"}, "refused"),
    ({"zone": "error", "error": app_profile.refusal_text("cis")}, "refused"),
    ({"zone": "error", "error": "ReadTimeout"}, "error"),
    ({"zone": "error", "error": None}, "error"),
])
def test_the_jev_verdict_status(row, status):
    assert gw.jev_verdict_status(row) == status
    e = gw.jev_event({"at": NOW, "stage": "a3_research", "question_key": "kw_fits", **row})
    assert e["status"] == status and e["outcome"] == (row["zone"] if status == "ok" else status)


def test_every_jev_refusal_text_decide_writes_is_known_to_the_reader():
    health = provider_circuit.Health(connection_key="typesafe", model_key="*", last_error_class="other",
                                     circuit_open_until=NOW + timedelta(minutes=1))
    texts = [d.JEV_SWITCHED_OFF, app_profile.refusal_text("cis"), provider_circuit.refusal_text(health)]
    assert all(t.endswith(gw.JEV_REFUSED_SUFFIX) for t in texts)
    assert d.JEV_BUDGET_EXCEEDED in gw.JEV_REFUSED_ERRORS


def test_the_jev_status_sql_matches_the_python_rule():
    sql = gw.JEV_STATUS_SQL
    assert f"error LIKE '%{gw.JEV_REFUSED_SUFFIX}'" in sql and "zone = 'skipped'" in sql
    assert all(f"'{e}'" in sql for e in gw.JEV_REFUSED_ERRORS)


def _sql(source, **kw):
    sql, args = calls.source_sql(source, calls.parse_filters(**kw), since=NOW - timedelta(hours=24), until=NOW,
                                 cursor=None, limit=101)
    return " ".join(sql.split()), args


def test_the_geocoder_answers_stage_app_and_status_filters():
    sql, args = _sql("geocoder", stage="place_geocode", app="cis", status="refused")
    assert "coalesce(to_jsonb(geocode_call) ->> 'app', 'cis') = $3" in sql
    assert "coalesce(to_jsonb(geocode_call) ->> 'status', CASE WHEN" in sql and "= $4" in sql
    assert "to_jsonb(geocode_call) ->> 'stage' = $5" in sql
    assert args[2:5] == ["cis", "refused", "place_geocode"]
    assert calls.sources_for(calls.parse_filters(stage="place_geocode", source="geocoder")) == ("geocoder",)


def test_the_geocoder_columns_read_through_to_jsonb():
    cols = " ".join(gw._GEO_COLS.split())
    for name in ("status", "app", "stage"):
        assert f"to_jsonb(geocode_call) ->> '{name}' AS {name}" in cols
    assert "(to_jsonb(geocode_call) ->> 'latency_ms')::int AS latency_ms" in cols
    stats = " ".join(gw.GEO_STATS_SQL.split())
    assert "percentile_cont(0.95) WITHIN GROUP (ORDER BY ((to_jsonb(geocode_call) ->> 'latency_ms')::float / " \
           "nullif(requests, 0)))" in stats


def test_a_calls_row_of_the_geocoder_carries_the_new_facts():
    r = {"key": f"geocoder:{17}", "tenant_id": None, **_geo(status="ok", latency_ms=420, app="cis",
                                                            stage="place_geocode")}
    row = calls.cis_row("geocoder", r)
    assert (row["id"], row["stage"], row["latency_ms"], row["outcome"]) == ("geocoder:17", "place_geocode", 420,
                                                                             "ok")


def test_the_geocoder_rows_are_written_under_this_gateway_app():
    assert call_log.APP == "cis"
