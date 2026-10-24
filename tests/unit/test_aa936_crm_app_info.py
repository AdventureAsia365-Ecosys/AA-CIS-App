"""AA-936 (card CRM-08 "CIS AppInfo('crm') + jobs tab"): the partner CRM on Platform › Apps, with fakes (no network).

- the CRM card is probed like Booking (public /crm/health + /crm/health/ready);
- its jobs come from the CRM's crm.ops.v1 GET /jobs/summary with the staff member's OWN Cognito Bearer (the route
  class the CRM accepts on /crm/ops/v1); a legacy session (no `iss`) gets "cognito_session_required" and no call;
- the cost page gets a crm row (App = crm) from the registry.
"""
import base64
import json

import httpx
import pytest

from api.core.app_registry import APPS, app_for_cost_tag
from api.services import platform_apps as pa


def _jwt(claims: dict) -> str:
    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{b64({'alg': 'RS256'})}.{b64(claims)}.sig"


COGNITO = _jwt({"iss": "https://cognito-idp.us-west-1.amazonaws.com/pool", "token_use": "access"})
LEGACY = _jwt({"sub": "admin", "role": "admin"})


def test_registry_has_the_crm():
    crm = APPS["crm"]
    assert crm.repo == "AA-Partner" and crm.health_paths == ("/crm/health", "/crm/health/ready")
    assert crm.extra["ops_base"].endswith("/crm/ops/v1") and crm.llm_app is None
    assert app_for_cost_tag("crm") == "crm"


def _transport(health=(200, {"status": "ok", "git_sha": "c" * 40}),
               ready=(200, {"status": "ok", "db": "ok", "schema_version": "crm-005"})):
    def handler(request):
        code, body = health if request.url.path == "/crm/health" else ready
        return httpx.Response(code, json=body)
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_crm_card_is_probed_like_booking():
    async with httpx.AsyncClient(transport=_transport()) as c:
        card = await pa._crm_card(c)
    assert card["id"] == "crm" and card["status"] == "ok" and card["ready"]["schema_version"] == "crm-005"
    assert card["health_url"].endswith("/crm/health") and card["commit_url"].endswith("c" * 40)
    async with httpx.AsyncClient(transport=_transport(ready=(503, {"status": "not_ready"}))) as c:
        degraded = await pa._crm_card(c)
    assert degraded["status"] == "degraded" and degraded["probe"]["reason"] == "/crm/health/ready answered 503"


@pytest.mark.asyncio
async def test_crm_jobs_relays_the_staff_token():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"generated_at": "t", "kinds": [
            {"kind": "crm_intake_process", "queued": 1, "running": 0, "failed_24h": 0, "oldest_queued_age_s": 4}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        out = await pa.crm_jobs(f"Bearer {COGNITO}", c)
    assert out["status"] == "ok" and out["kinds"][0]["kind"] == "crm_intake_process"
    assert seen["url"].endswith("/crm/ops/v1/jobs/summary") and seen["auth"] == f"Bearer {COGNITO}"


@pytest.mark.asyncio
async def test_crm_jobs_needs_a_cognito_session_and_never_raises():
    def boom(request):
        raise AssertionError("no call without a Cognito token")

    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as c:
        for header in (None, "", "Basic x", f"Bearer {LEGACY}", "Bearer not-a-jwt"):
            out = await pa.crm_jobs(header, c)
            assert out == {"status": "unavailable", "reason": "cognito_session_required", "kinds": []}
    refused = httpx.MockTransport(lambda r: httpx.Response(403, json={"detail": "missing_group"}))
    async with httpx.AsyncClient(transport=refused) as c:
        out = await pa.crm_jobs(f"Bearer {COGNITO}", c)
    assert out["status"] == "refused" and out["reason"] == "missing_group" and out["http_status"] == 403
    broken = httpx.MockTransport(lambda r: httpx.Response(502, text="bad gateway"))
    async with httpx.AsyncClient(transport=broken) as c:
        assert (await pa.crm_jobs(f"Bearer {COGNITO}", c))["status"] == "error"
    html_200 = httpx.MockTransport(lambda r: httpx.Response(200, text="<html>proxy page</html>"))
    async with httpx.AsyncClient(transport=html_200) as c:
        out = await pa.crm_jobs(f"Bearer {COGNITO}", c)
    assert out == {"status": "error", "http_status": 200, "reason": "invalid_json", "kinds": []}
    list_200 = httpx.MockTransport(lambda r: httpx.Response(200, json=[1, 2]))
    async with httpx.AsyncClient(transport=list_200) as c:
        assert (await pa.crm_jobs(f"Bearer {COGNITO}", c))["reason"] == "unexpected_shape"

    def slow(request):
        raise httpx.ConnectTimeout("slow")

    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as c:
        out = await pa.crm_jobs(f"Bearer {COGNITO}", c)
    assert out == {"status": "unavailable", "reason": "ConnectTimeout", "kinds": []}


@pytest.mark.asyncio
async def test_crm_llm_usage_totals_the_rollup_per_model_and_never_raises():
    from datetime import date

    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"rows": [
            {"day": "2026-10-01", "stage": "crm_draft", "model": "m1", "calls": 3, "cost_usd": "0.010000"},
            {"day": "2026-10-02", "stage": "crm_draft", "model": "m1", "calls": 2, "cost_usd": "0.005000"},
            {"day": "2026-10-02", "stage": "crm_judge", "model": "m2", "calls": 1, "cost_usd": "0.020000"},
            {"bad": "row", "calls": "x"},
        ]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        out = await pa.crm_llm_usage(f"Bearer {COGNITO}", date(2026, 10, 1), date(2026, 11, 1), c)
    assert "/crm/ops/v1/llm/usage?from=2026-10-01&to=2026-10-31" in seen["url"] and seen["auth"] == f"Bearer {COGNITO}"
    assert out["status"] == "ok" and out["calls"] == 6 and out["cost_usd"] == 0.035
    assert [m["model"] for m in out["by_model"]] == ["m2", "m1"]  # the malformed row is skipped
    none = await pa.crm_llm_usage(None, date(2026, 10, 1), date(2026, 11, 1))
    assert none == {"status": "unavailable", "reason": "cognito_session_required", "calls": 0, "cost_usd": 0.0,
                    "by_model": []}


@pytest.mark.asyncio
async def test_cost_page_crm_row_takes_the_crm_gateway_spend():
    from datetime import datetime, timezone

    from api.services import platform_cost as pc

    class Pool:
        async def fetch(self, sql, *args):
            return []

        async def fetchrow(self, sql, *args):
            return None

        async def fetchval(self, sql, *args):
            return None

    async def no_store(pool, start, end):
        return None

    calls = []

    async def crm_llm(start, end):
        calls.append((start, end))
        return {"status": "ok", "calls": 4, "cost_usd": 0.5, "by_model": [{"model": "m1", "calls": 4, "cost_usd": 0.5}]}

    now = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
    orig = pc.ce.read_app_cost_range
    pc.ce.read_app_cost_range = no_store
    try:
        out = await pc.cost_by_app(Pool(), None, redis=None, refresh_if_stale=False, now=now, crm_llm=crm_llm)

        async def down(start, end):
            return {"status": "unavailable", "reason": "ConnectTimeout"}

        missing = await pc.cost_by_app(Pool(), None, redis=None, refresh_if_stale=False, now=now, crm_llm=down)
    finally:
        pc.ce.read_app_cost_range = orig
    crm = next(r for r in out["apps"] if r["app"] == "crm")
    assert crm["ai_usd"] == 0.5 and crm["ai_calls"] == 4 and crm["ai_outside_aws_usd"] == 0.5
    assert crm["ai_by_source"][0]["provider"] == "crm_gateway:m1" and out["crm_llm"] == {"status": "ok"}
    assert calls and calls[0][0].isoformat() == "2026-10-01"
    assert missing["crm_llm"] == {"status": "unavailable", "reason": "ConnectTimeout"}
    assert next(r for r in missing["apps"] if r["app"] == "crm")["ai_usd"] == 0.0
