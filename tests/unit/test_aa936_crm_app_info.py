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

    def slow(request):
        raise httpx.ConnectTimeout("slow")

    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as c:
        out = await pa.crm_jobs(f"Bearer {COGNITO}", c)
    assert out == {"status": "unavailable", "reason": "ConnectTimeout", "kinds": []}
