"""Unit-test environment — the same dummy values CI sets (.github/workflows/ci.yml, "Run unit
tests"), applied only when the variable is not already set, so `pytest tests/unit` behaves the same
locally as in CI. Set at import time: some modules read these while being imported (JWT_SECRET).

Before this file, 3 tests passed in CI but failed locally because they built an OpenAI client or
opened a DB connection without the env CI provides (aa324 ×2, aa652).
"""
import os

_CI_UNIT_ENV = {
    "ENVIRONMENT": "test",
    "AWS_REGION": "us-west-1",
    "AWS_DEFAULT_REGION": "us-west-1",
    "DATABASE_URL": "postgresql://test:test@localhost:5432/test",
    "OPENAI_API_KEY": "sk-test-dummy",
    "JWT_SECRET": "test-jwt-secret-ci-only-not-real",
    "DATAFORSEO_LOGIN": "test",
    "DATAFORSEO_PASSWORD": "test",
    "LANGFUSE_SECRET_KEY": "test",
    "LANGFUSE_PUBLIC_KEY": "test",
    "LANGFUSE_HOST": "http://localhost:3000",
    # AA-902: the bronze bucket name comes from the environment (shared/aws_client/bronze.py).
    "BRONZE_BUCKET": "aa-cis-bronze-test",
    # AA-902: satellite account IDs come from the task environment (shared/llm_client/bedrock_satellite.py).
    "BEDROCK_ACCT_PRIMARY": "acc3-test-account",
    "BEDROCK_ACCT_FALLBACK": "acc1-test-account",
}
for _k, _v in _CI_UNIT_ENV.items():
    os.environ.setdefault(_k, _v)


import pytest  # noqa: E402


# AA-902: AWS's documentation example account, the only 12-digit account the resource-ID check allows.
FAKE_ACC2_ACCOUNT_ID = "111122223333"


@pytest.fixture(autouse=True)
def _no_sts_caller_identity(monkeypatch):
    """No unit test calls STS: the caller's account (acc2 on ECS) is a fixed fake."""
    monkeypatch.setattr("shared.aws_client.bronze.caller_account_id", lambda: FAKE_ACC2_ACCOUNT_ID)


@pytest.fixture(autouse=True)
def _reset_jev_decision_memo():
    """AA-742: decide() keeps an in-process memo of Jev answers + pending cache-hit counts; clear it
    between tests so one test's verdict never answers another's question."""
    from shared.llm_client import decide
    decide.reset_decision_memo()
    yield
    decide.reset_decision_memo()


async def _no_seed(conn, atom_id, tenant_id):
    """AA-768 — T8/T9 unit tests script conn.fetchrow side-effects in call order; with no seed the
    callers fall back to the atom label exactly as before. The real composition is tested in
    test_aa768_atom_seed.py (marker `real_atom_seed`)."""
    return None


async def _label_route_seeds(conn, atoms, tenant_id):
    """S234 — the route walk's merged seeds, label-only: one block per atom, its label."""
    return [(a, t) for a, t in atoms]


@pytest.fixture(autouse=True)
def _atom_seed_label_only(request, monkeypatch):
    if "real_atom_seed" in request.keywords:
        return
    from services.acp_angle_gate import service as angle_service
    from services.acp_content_writing import service as writing_service
    monkeypatch.setattr(angle_service, "atom_seed_text", _no_seed)  # T8 falls back to row["text"]
    monkeypatch.setattr(writing_service, "atom_seed_text", _no_seed)  # T9 falls back to the label
    monkeypatch.setattr(writing_service, "route_moment_seeds", _label_route_seeds)


async def _fake_public_dns(host, port, family=0):
    """AA-875 — no unit test does a real DNS lookup through shared.safe_http: every name resolves to
    one public address. Tests of the DNS rules replace safe_http._getaddrinfo themselves."""
    import socket
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", port))]


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch):
    monkeypatch.setattr("shared.safe_http._getaddrinfo", _fake_public_dns)


# AA-986 L2: process_export publishes through services.tour_lifecycle.publish (a transaction, key reads, the cascade).
# Unit tests that drive process_export with a faked asyncpg connection opt in to this stand-in: the repository
# insert only, the call the service makes first. The real publish is tested on Postgres
# (tests/integration/test_aa986_l2_tour_lifecycle.py).
async def _aa986_fake_publish(conn, data, *, tenant_slug="aa_internal", actor="pipeline"):
    from types import SimpleNamespace

    from shared.repository.published_catalog_repository import PublishedCatalogRepository
    catalog_id = await PublishedCatalogRepository(conn, tenant_slug).insert(data)
    return SimpleNamespace(extra={"catalog_id": catalog_id}, events=[])


@pytest.fixture
def plain_publish(monkeypatch):
    monkeypatch.setattr("services.tour_lifecycle.publish", _aa986_fake_publish)
