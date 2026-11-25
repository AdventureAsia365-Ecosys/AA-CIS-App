"""AA-1169 — Writing Lab › Eval runs: services/writing_memory/eval_review.py (the pure views) and
api/routers/admin_eval_runs.py (the three reads, role-gated, read-only).

No database: the SQL itself runs in tests/integration/test_aa1169_eval_runs_sql.py. Invented names and places only.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI

from services.writing_memory import eval_review as er

NOW = datetime(2026, 11, 25, 9, 0, tzinfo=timezone.utc)
RUN = "00000000-0000-0000-0000-0000000a1169"
ITEM = "00000000-0000-0000-0000-0000000b1169"
ROW_B = "00000000-0000-0000-0000-0000000c1169"
ROW_A = "00000000-0000-0000-0000-0000000d1169"
SCORED = "00000000-0000-0000-0000-0000000e1169"

OUTPUT = {
    "seo_title": "Walking the Lantern Hills of Norvale",
    "meta_description": "A slow walk through the Lantern Hills.",
    "slug": "lantern-hills-norvale",
    "content_text": ("Day 1 starts at the Velin market.\n\n"
                     "By evening the group reaches the ridge camp above Norvale.\n\n"
                     "It is one of the rarest views in the whole valley."),
}
CLAIMS = [
    {"class": "unsupported", "claim": "It is one of the rarest views in the whole valley.", "why": "not in source"},
    {"class": "unsupported", "claim": "By evening the group reaches the ridge camp above Norvale.", "why": "timing"},
]


def _run(**kw):
    base = {"run_id": RUN, "run_kind": "r2", "writer": "t9_blog", "prompt_version": "v1", "created_at": NOW,
            "finished_at": NOW, "verdict": "informative", "stop_reason": "completed", "cost_usd": "1.25",
            "cost_cap_usd": "5.0000", "tour_count": 2, "judge_kappa": None, "golden_set_id": None,
            "release_id": None, "triggered_by": "test", "prereg_id": "proof-x", "prereg_arm": "B", "results": 2,
            "passes": 1, "fails": 1, "errors": 0, "outputs": 1}
    return {**base, **kw}


def _list_row(**kw):
    base = {"result_id": ROW_B, "item_id": ITEM, "sample_id": "t9b-901", "scope": "platform", "split": "holdout",
            "kind": "real", "country": "Norvale", "days": 5, "label_verdict": "fail", "arm": "candidate",
            "mode": "regenerate", "phase": "holdout", "verdict": "fail", "hard_stop": False,
            "failure_codes": ["claims"], "cost_usd": "0.05", "judge_claims": CLAIMS, "has_output": True,
            "scored_result_id": None}
    return {**base, **kw}


# ── pure views ────────────────────────────────────────────────────────────────────────────────────────────────────

def test_run_view_sends_iso_dates_and_floats():
    v = er.run_view({**_run(), "total": 7})
    assert v["created_at"] == NOW.isoformat() and v["cost_usd"] == 1.25 and v["cost_cap_usd"] == 5.0
    assert "total" not in v and v["prereg_id"] == "proof-x"


def test_claim_classes_types_each_claim_without_sending_its_text():
    assert er.claim_classes(CLAIMS) == {"rarity": 1, "route_timing": 1}
    assert er.claim_classes(None) == {} and er.claim_classes('[{"claim": "Day 2 has 40 steps."}]') == {"number": 1}


def test_items_view_groups_rows_per_item_without_text():
    rows = [_list_row(), _list_row(result_id=ROW_A, arm="control", mode="rejudge", verdict="pass", judge_claims=None,
                                   has_output=False, scored_result_id=SCORED, failure_codes=[])]
    (item,) = er.items_view(rows)
    assert item["sample_id"] == "t9b-901" and [r["arm"] for r in item["rows"]] == ["candidate", "control"]
    b, a = item["rows"]
    assert b["claims"] == 2 and b["claim_classes"] == {"rarity": 1, "route_timing": 1} and b["cost_usd"] == 0.05
    assert a["claims"] == 0 and a["scored_result_id"] == SCORED and a["has_output"] is False
    assert "It is one of" not in repr(item)                     # no claim text in the list view
    s = er.run_summary([item])
    assert s["candidate"] == {"rows": 1, "judged": 1, "passes": 0, "claims": 2, "hard_stops": 0, "pass_share": 0.0}
    assert s["control"]["pass_share"] == 1.0


def test_output_fields_follow_the_judge_reading_order():
    fields = er.output_fields("t9_blog", OUTPUT)
    assert [f["field"] for f in fields][-1] == "content_text"
    assert fields[0]["text"] == OUTPUT["seo_title"]
    assert er.output_fields("t9_blog", None) == []


def test_typed_claims_are_placed_in_reading_order():
    claims = er.typed_claims(CLAIMS, OUTPUT)
    assert [c["policy_class"] for c in claims] == ["route_timing", "rarity"]    # the evening line comes first
    assert all(c["field"] == "body" and isinstance(c["pos"], int) for c in claims)
    assert claims[1]["why"] == "not in source" and claims[1]["judge_class"] == "unsupported"
    lost = er.typed_claims([{"claim": "A sentence that is not there."}], OUTPUT)
    assert lost[0]["pos"] is None                             # not found: kept, never dropped


def test_gate_replay_runs_only_on_a_t9_draft(monkeypatch):
    from services.writer_proof import first_pass
    seen = []
    monkeypatch.setattr(first_pass, "gate_replay",
                        lambda ctx, out: seen.append(ctx) or {"passed": False, "first_failure": "g", "blocking": ["g"]})
    inp = {"context": {"channel": "blog", "trip_name": "Lantern Hills"}}
    assert er.gate_replay("t9_blog", inp, OUTPUT)["first_failure"] == "g" and seen == [inp["context"]]
    assert er.gate_replay("t2", inp, OUTPUT) is None and er.gate_replay("t9_blog", inp, None) is None
    assert er.gate_replay("t9_blog", {"no": "context"}, OUTPUT) == {"error": "WriterFailed"}


def test_item_view_puts_rows_side_by_side_with_the_scored_output(monkeypatch):
    from services.writer_proof import first_pass
    monkeypatch.setattr(first_pass, "gate_replay", lambda ctx, out: {"passed": True, "first_failure": None,
                                                                     "blocking": []})
    item = {"item_id": ITEM, "sample_id": "t9b-901", "writer": "t9_blog", "channel": "blog", "scope": "platform",
            "tenant_id": None, "split": "holdout", "kind": "real", "tiers": ["trek_hike"], "country": "Norvale",
            "days": 5, "label_verdict": "fail", "label_source": "agent_panel", "label": '{"verdict": "fail"}',
            "input": {"context": {"channel": "blog", "trip_name": "Lantern Hills"}}, "input_sha256": "0" * 64,
            "pick_reason": None}
    rows = [
        {"result_id": ROW_A, "arm": "control", "mode": "rejudge", "phase": "holdout", "verdict": "fail",
         "score": None, "failure_codes": ["claims"], "hard_stop": False, "judge_model": "judge-x",
         "judge_version": "j1", "cost_usd": "0.01", "created_at": NOW, "output": None, "output_sha256": None,
         "details": {"judge_claims": CLAIMS[:1], "source_mode": "regenerate", "frozen": False, "unrelated": 1},
         "scored_result_id": SCORED},
        {"result_id": ROW_B, "arm": "candidate", "mode": "regenerate", "phase": "holdout", "verdict": "pass",
         "score": "0.9", "failure_codes": [], "hard_stop": False, "judge_model": "judge-x", "judge_version": "j1",
         "cost_usd": "0.05", "created_at": NOW, "output": OUTPUT, "output_sha256": "1" * 64,
         "details": {"qa_artefacts": [], "isolation": {"leak": False}, "judge": {"model_key": "judge-x"}},
         "scored_result_id": None},
    ]
    v = er.item_view(er.run_view(_run()), item, rows, {SCORED: {"content_text": "It is one of the rarest views in "
                                                                                "the whole valley."}})
    assert v["item"]["label"] == {"verdict": "fail"} and "input" not in v["item"] and v["input"] == item["input"]
    a, b = v["results"]
    assert a["output_source"] == "scored" and a["claims"][0]["policy_class"] == "rarity"
    assert a["checks"] == {"source_mode": "regenerate", "frozen": False}          # only the known check keys
    assert b["output_source"] == "own" and b["score"] == 0.9 and b["judge"] == {"model_key": "judge-x"}
    assert b["gate_replay"] == {"passed": True, "first_failure": None, "blocking": []}
    assert b["checks"] == {"qa_artefacts": [], "isolation": {"leak": False}}


# ── the endpoints ─────────────────────────────────────────────────────────────────────────────────────────────────

def _app(*, fetch=None, fetchrow=None):
    from api.core import lab_read_log
    from api.core.staff_authz import staff_policy
    from api.routers import admin_eval_runs as r
    from api.routers.admin import verify_admin_secret

    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=fetch or (lambda *a: []))
    conn.fetchrow = AsyncMock(side_effect=fetchrow or (lambda *a: None))

    @asynccontextmanager
    async def tx(**kw):
        assert kw == {"readonly": True}
        yield
    conn.transaction = MagicMock(side_effect=lambda **kw: tx(**kw))
    pool = MagicMock()

    @asynccontextmanager
    async def acq():
        yield conn
    pool.acquire = MagicMock(side_effect=lambda: acq())
    app = FastAPI()
    app.include_router(r.router)
    app.state.pool = pool
    app.dependency_overrides[staff_policy] = lambda: None
    app.dependency_overrides[verify_admin_secret] = lambda: None
    app.dependency_overrides[lab_read_log.log_lab_tenant_text_read] = lambda: None
    return app, conn


def test_list_endpoint_filters_and_pages():
    from fastapi.testclient import TestClient

    async def fetch(sql, *args):
        assert sql is er.RUNS_SQL and args == ("t9_blog", "r2", "proof-x", 20, 40)
        return [{**_run(), "total": 41}]
    app, _ = _app(fetch=fetch)
    body = TestClient(app).get("/admin/writing-memory/eval-runs?writer=t9_blog&run_kind=r2&prereg=proof-x"
                               "&limit=20&offset=40").json()
    assert body["total"] == 41 and body["runs"][0]["run_id"] == RUN and body["limit"] == 20
    c = TestClient(app)
    assert c.get("/admin/writing-memory/eval-runs?writer=t7").status_code == 422
    assert c.get("/admin/writing-memory/eval-runs?limit=201").status_code == 422


def test_run_endpoint_reads_the_run_then_its_rows():
    from fastapi.testclient import TestClient

    async def fetchrow(sql, rid):
        return _run() if sql is er.RUN_SQL and rid == RUN else None

    async def fetch(sql, rid):
        assert sql is er.RUN_ROWS_SQL and rid == RUN
        return [_list_row()]
    app, _ = _app(fetch=fetch, fetchrow=fetchrow)
    body = TestClient(app).get(f"/admin/writing-memory/eval-runs/{RUN}").json()
    assert body["run"]["run_id"] == RUN and body["items"][0]["rows"][0]["claims"] == 2
    assert body["summary"]["candidate"]["judged"] == 1
    assert TestClient(app).get(f"/admin/writing-memory/eval-runs/{ITEM}").status_code == 404
    assert TestClient(app).get("/admin/writing-memory/eval-runs/not-a-uuid").status_code == 422


def test_item_endpoint_fetches_the_scored_output_of_a_rejudged_row(monkeypatch):
    from fastapi.testclient import TestClient

    from services.writer_proof import first_pass
    monkeypatch.setattr(first_pass, "gate_replay", lambda ctx, out: {"passed": True, "first_failure": None,
                                                                     "blocking": []})

    async def fetchrow(sql, oid):
        if sql is er.RUN_SQL:
            return _run()
        return {"item_id": ITEM, "sample_id": "t9b-901", "writer": "t9_blog", "channel": "blog",
                "scope": "platform", "tenant_id": None, "split": "holdout", "kind": "real", "tiers": [],
                "country": None, "days": None, "label_verdict": "pass", "label_source": "synthetic", "label": {},
                "input": {"context": {"channel": "blog"}}, "input_sha256": "0" * 64, "pick_reason": None}

    async def fetch(sql, *args):
        if sql is er.ITEM_ROWS_SQL:
            assert args == (RUN, ITEM)
            return [{"result_id": ROW_A, "arm": "control", "mode": "rejudge", "phase": "holdout", "verdict": "pass",
                     "score": None, "failure_codes": [], "hard_stop": False, "judge_model": None,
                     "judge_version": None, "cost_usd": 0, "created_at": NOW, "output": None,
                     "output_sha256": None, "details": {}, "scored_result_id": SCORED}]
        assert sql is er.SCORED_SQL and args == ([SCORED],)
        return [{"result_id": SCORED, "run_id": RUN, "output": OUTPUT}]
    app, conn = _app(fetch=fetch, fetchrow=fetchrow)
    body = TestClient(app).get(f"/admin/writing-memory/eval-runs/{RUN}/items/{ITEM}").json()
    (row,) = body["results"]
    assert row["output_source"] == "scored" and row["output_fields"][-1]["field"] == "content_text"
    assert row["gate_replay"]["passed"] is True and body["item"]["sample_id"] == "t9b-901"


def test_item_endpoint_404_when_the_item_is_not_in_the_run():
    from fastapi.testclient import TestClient

    async def fetchrow(sql, oid):
        return _run() if sql is er.RUN_SQL else None
    app, _ = _app(fetchrow=fetchrow)
    assert TestClient(app).get(f"/admin/writing-memory/eval-runs/{RUN}/items/{ITEM}").status_code == 404


def test_without_a_pool_the_endpoints_say_so():
    from fastapi.testclient import TestClient

    from api.core.staff_authz import staff_policy
    from api.routers import admin_eval_runs as r
    from api.routers.admin import verify_admin_secret
    app = FastAPI()
    app.include_router(r.router)
    app.dependency_overrides[staff_policy] = lambda: None
    app.dependency_overrides[verify_admin_secret] = lambda: None
    assert TestClient(app).get("/admin/writing-memory/eval-runs").status_code == 503


# ── who may call ──────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path,cap", [
    ("/admin/writing-memory/eval-runs", "lab.read"),
    ("/admin/writing-memory/eval-runs/{run_id}", "lab.read"),
    ("/admin/writing-memory/eval-runs/{run_id}/items/{item_id}", "lab.tenant_text.read"),
])
def test_the_reads_are_lab_reads(path, cap):
    from api.core.staff_authz import POLICY
    rule = POLICY[("GET", path)]
    assert cap in repr(rule) and "tenant_content.read" in repr(rule)


def test_the_item_read_is_logged_and_the_list_reads_are_not():
    from api.core import lab_read_log
    from api.routers import admin_eval_runs as r

    def logged(path):
        (route,) = [x for x in r.router.routes if x.path == path]
        return any(d.call is lab_read_log.log_lab_tenant_text_read for d in route.dependant.dependencies)
    assert logged("/admin/writing-memory/eval-runs/{run_id}/items/{item_id}")
    assert not logged("/admin/writing-memory/eval-runs") and not logged("/admin/writing-memory/eval-runs/{run_id}")


def test_the_reads_never_write():
    for sql in (er.RUNS_SQL, er.RUN_SQL, er.RUN_ROWS_SQL, er.ITEM_SQL, er.ITEM_ROWS_SQL, er.SCORED_SQL):
        up = sql.upper()
        assert "INSERT" not in up and "UPDATE " not in up and "DELETE" not in up
