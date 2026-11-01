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


@pytest.fixture(autouse=True)
def _reset_daily_limit_column_cache():
    """AA-993: services/acp_publish/daily_limit.py caches "does shared.tenants.daily_publish_limit exist" in the
    process-global ``_column`` (a False answer for 300 s). Clear it around every test so no test sees an answer
    another test left behind: the result would depend on test order and on which xdist worker ran what."""
    from services.acp_publish import daily_limit
    daily_limit._forget_column()
    yield
    daily_limit._forget_column()


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


@pytest.fixture(autouse=True)
def _llm_breaker_off(request, monkeypatch):
    """AA-1002 (S4): the LLM circuit breaker keeps state across calls (in process, and in shared.llm_provider_health
    when a DB answers). Gateway tests that script provider failures expect every call to reach the provider, as before
    S4, so the breaker is off (LLM_CIRCUIT_BREAKER=off = the S3 behaviour) unless a test opts in with the marker
    `real_breaker`; either way it starts empty and never touches a database."""
    from shared.llm_client import breaker
    breaker.reset()
    if "real_breaker" not in request.keywords:
        monkeypatch.setenv(breaker.SWITCH_ENV, "off")
    else:
        monkeypatch.delenv(breaker.SWITCH_ENV, raising=False)

    def _no_db(fn):
        raise RuntimeError("no database in unit tests")
    monkeypatch.setattr(breaker, "_db", _no_db)
    # AA-1018: the connection-circuit alert writes a bell through its own DB connection; none in unit tests.
    monkeypatch.setattr(breaker, "on_connection_open", lambda health, models: None)
    # AA-1018 part 2: the access_state write uses its own DB connection too; none in unit tests.
    monkeypatch.setattr(breaker, "set_access_state", lambda connection_key, state: None)
    # AA-1016 / AA-900 (AA-1018 part 2): the tenant AI budget and the prompt register read the DB on the call path;
    # in unit tests no tenant has a budget and no prompt version is registered (tests that need them set loaders).
    from shared.llm_client import prompt_registry, tenant_budget
    tenant_budget.invalidate()
    prompt_registry.invalidate()
    monkeypatch.setattr(tenant_budget, "loader_tenants", lambda: frozenset())
    monkeypatch.setattr(tenant_budget, "loader_state", lambda tenant_id: None)
    monkeypatch.setattr(prompt_registry, "loader", lambda: set())
    yield
    breaker.reset()
    tenant_budget.invalidate()
    prompt_registry.invalidate()


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


# AA-986 L3: the tenant routes read the served state of their tours (shared/tour_liveness.served_states /
# request_tours: one query each on the route's connection). Unit tests script faked asyncpg connections call by call;
# by default these two reads answer "no state" (every tour served, nothing refused) without touching the fake, so the
# scripts stay as they were. The real reads, on both pools, are tested on Postgres
# (tests/integration/test_aa986_l3_tenant_served.py). Mark a test `real_served_state` to keep the real functions.
@pytest.fixture(autouse=True)
def _served_state_unknown(request, monkeypatch):
    if "real_served_state" in request.keywords:
        return
    from shared import tour_liveness

    async def _no_states(conn, tour_ids):
        return {}

    async def _no_tours(conn, tenant_id, request_ids):
        return {}

    monkeypatch.setattr(tour_liveness, "served_states", _no_states)
    monkeypatch.setattr(tour_liveness, "request_tours", _no_tours)


# AA-1002 (gateway slice S2): every catalog-key call asks shared/llm_client/vendor_route.py whether an Anthropic API
# deployment comes first, which reads the connection registry (a DB read, cached in process). Unit tests have no DB:
# by default the registry is EMPTY (today's path), so no test opens a connection and none depends on what another
# test left in the cache. Tests of the registry patch `connections.run_sync` themselves (their patch wins).
@pytest.fixture(autouse=True)
def _empty_llm_connection_registry(monkeypatch):
    from shared.llm_client import connections, deployments

    def _empty(fn):
        return {} if fn is connections._fetch_all else []

    def _reset():
        connections.invalidate()
        deployments.invalidate()
        connections._connections = {}
        deployments._by_model = {}
    _reset()
    monkeypatch.setattr(connections, "run_sync", _empty)
    yield
    _reset()


# AA-642 (S263 lane L2, B1): decide() writes Jev's access_state, the DataForSEO client asks shared/llm_client/connections.provider_enabled before each call
# (a DB read through its own connection, cached in process) and every provider client writes its access_state. Unit
# tests have no DB: by default no provider row exists (= on) and the state writes go nowhere; a test that needs a row
# or the writes patches `provider_row_loader` / `provider_state_writer` itself (its patch wins).
@pytest.fixture(autouse=True)
def _no_provider_row_reads(monkeypatch):
    from shared.llm_client import connections

    async def _no_row(connection_key):
        return None

    async def _no_write(connection_key, state, probed):
        return None

    async def _no_jev_write(db, state):
        return None

    from services.places import geocoder
    from shared.llm_client import decide
    monkeypatch.setattr(geocoder, "_registry_enabled", None)
    connections.reset_provider_state()
    decide.reset_jev_access()
    monkeypatch.setattr(connections, "provider_row_loader", _no_row)
    monkeypatch.setattr(connections, "provider_state_writer", _no_write)
    monkeypatch.setattr(decide, "jev_access_writer", _no_jev_write)
    yield
    connections.reset_provider_state()
    decide.reset_jev_access()
