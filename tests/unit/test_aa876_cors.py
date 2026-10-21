"""AA-876 — no CORS on the deployed API; localhost front-ends only for local dev / CI."""
from fastapi.testclient import TestClient

import api.main as main
from api.main import app, cors_allow_origins


def test_deployed_task_allows_no_origin():
    assert cors_allow_origins({"AWS_EXECUTION_ENV": "AWS_ECS_FARGATE"}) == []


def test_local_allows_only_localhost():
    origins = cors_allow_origins({})
    assert origins == ["http://localhost:3000", "http://localhost:3001"]
    assert not any("lumiguides" in o for o in origins)


def test_module_origins_follow_the_process_env(monkeypatch):
    monkeypatch.delenv("AWS_EXECUTION_ENV", raising=False)
    assert cors_allow_origins() == main._LOCAL_CORS_ORIGINS


def test_no_public_host_gets_a_cors_grant():
    # This process is not on ECS, so the middleware carries the localhost list only: a preflight from the
    # (former) public admin host gets no Access-Control-Allow-Origin.
    client = TestClient(app)
    res = client.options("/health", headers={
        "Origin": "https://aa-cis.lumiguides.it.com", "Access-Control-Request-Method": "GET",
    })
    assert "access-control-allow-origin" not in {k.lower() for k in res.headers}
