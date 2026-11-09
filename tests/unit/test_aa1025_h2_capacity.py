"""AA-1025 H2 (capacity S12) — request lines, the TPM limiter per connection / model, the claim hold and the wave pause
(shared/llm_client/capacity.py), wired into breaker.guarded, LLMClient, the job worker and the backlog sampler.

No database (conftest stubs the loaders); a fake clock drives the windows. The SQL (the limit table, the wave pause,
the worker's postponed claim) runs against Postgres in tests/integration/test_aa1025_h2_capacity_db.py.
"""
import asyncio
import threading
from datetime import datetime, timedelta, timezone

import pytest

from shared.jobs import backlog
from shared.jobs.registry import kinds
from shared.jobs.worker import Worker, load_kinds
from shared.llm_client import breaker, capacity
from shared.llm_client.breaker import Health
from shared.llm_client.capacity import BULK, DEV, PROD, Hold, Limit, hold_of
from shared.llm_client.connections import ProviderConnection
from shared.llm_client.errors import classify
from shared.llm_client.models import LLMRequest
from tests.unit.test_aa1002_s3_deployment_routing import world  # noqa: F401 — the S3 fake world fixture

T0 = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)
CONN = "zz-conn"


class Clock:
    """A fake monotonic clock; ``wait`` advances it (single-threaded tests never sleep)."""

    def __init__(self, t=1000.0):
        self.t = t
        self.waits = []

    def __call__(self):
        return self.t

    def wait(self, cond, timeout):
        self.waits.append(round(timeout, 3))
        self.t += timeout


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(capacity, "_clock", c)
    monkeypatch.setattr(capacity, "_wait", c.wait)
    return c


def _limits(monkeypatch, *rows):
    monkeypatch.setattr(capacity, "loader", lambda: list(rows))
    capacity.invalidate()


def _conn(**kw):
    return ProviderConnection(connection_key=CONN, kind="anthropic", auth_mode="api_key", label="t",
                              data_scope="vendor_direct", enabled=True, secret_name="aa-llm/dev/zz", **kw)


# ── lines ───────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_every_registered_job_kind_has_a_line():
    """A new kind must be classified (bulk / prod / dev / None), so an LLM kind cannot slip past the backpressure."""
    load_kinds()
    missing = sorted(set(kinds()) - set(capacity.KIND_LINES))
    assert missing == [], f"add these job kinds to capacity.KIND_LINES: {missing}"
    assert set(capacity.KIND_LINES.values()) <= {PROD, BULK, DEV, None}


def test_the_wave_kinds_are_bulk_the_tenant_kinds_prod_the_probes_dev():
    from services.waves.report import WAVE_JOB_KINDS
    assert {capacity.line_of_kind(k) for k in WAVE_JOB_KINDS} == {BULK}
    assert {capacity.line_of_kind(k) for k in ("t2_rewrite", "t8_angles", "t9_write")} == {PROD}
    assert {capacity.line_of_kind(k) for k in ("llm_connection_probe", "llm_contract_smoke")} == {DEV}
    assert capacity.line_of_kind("ops_alert_email") is None and capacity.line_of_kind("zz_unknown") is None


def test_the_line_of_a_call():
    assert capacity.line_for("t9_write") == PROD                         # outside a job: prod
    assert capacity.line_for("adhoc_aa1025") == DEV
    with capacity.bind_kind("s1_rewrite"):
        assert capacity.line_for("s1_generate") == BULK
    with capacity.bind_kind("ops_alert_email"):                         # a kind without LLM line: by the stage
        assert capacity.line_for("x") == PROD
    from shared.llm_client import call_log
    with call_log.bind_eval_stage("eval_zz", "run-1"):
        assert capacity.line_for("x") == DEV


def test_the_token_estimate_is_the_prompt_plus_max_tokens():
    req = LLMRequest(system_prompt="a" * 400, user_prompt="b" * 400, max_tokens=1000)
    assert capacity.estimate_tokens(req) == 800 // 4 + 1 + 1000
    assert capacity.estimate_tokens(object()) == 1


# ── the window (pure) ───────────────────────────────────────────────────────────────────────────────────────────────

def test_a_window_fits_until_the_limit_then_names_the_wait():
    w = capacity._Window(Limit(CONN, tpm_limit=1000))
    assert w.wait_for(600, PROD, 0.0) is None
    w.add(0.0, 600, PROD)
    assert w.wait_for(400, PROD, 10.0) is None
    w.add(10.0, 400, PROD)
    assert w.wait_for(1, PROD, 20.0) == pytest.approx(40.0)          # until the first reservation is 60 s old
    assert w.wait_for(1, PROD, 60.0) is None                         # it left the window


def test_a_request_bigger_than_the_limit_fits_only_an_empty_window():
    w = capacity._Window(Limit(CONN, tpm_limit=100))
    assert w.wait_for(500, PROD, 0.0) is None
    w.add(0.0, 500, PROD)
    assert w.wait_for(500, PROD, 1.0) == pytest.approx(59.0)


def test_bulk_holds_at_most_its_share_and_prod_keeps_the_rest():
    w = capacity._Window(Limit(CONN, tpm_limit=1000, bulk_pct=30))
    w.add(0.0, 300, BULK)
    assert w.wait_for(10, BULK, 1.0) is not None                      # bulk is at its 30 %
    assert w.wait_for(700, PROD, 1.0) is None                        # prod still has the rest


def test_bulk_and_dev_yield_while_a_prod_call_waits():
    w = capacity._Window(Limit(CONN, tpm_limit=1000, dev_pct=50))
    w.waiting[PROD] = 1
    assert w.wait_for(10, BULK, 0.0) is not None
    assert w.wait_for(10, DEV, 0.0) is not None
    assert w.wait_for(10, PROD, 0.0) is None
    w.waiting[PROD] = 0
    assert w.wait_for(10, BULK, 0.0) is None


def test_dev_holds_at_most_its_share():
    w = capacity._Window(Limit(CONN, tpm_limit=1000, dev_pct=10))
    w.add(0.0, 100, DEV)
    assert w.wait_for(1, DEV, 1.0) is not None
    assert w.wait_for(1, BULK, 1.0) is None


def test_the_covering_rows_are_the_connection_and_the_model():
    rows = [Limit(CONN, "*", tpm_limit=10), Limit(CONN, "m1", tpm_limit=5), Limit(CONN, "m2", tpm_limit=5),
            Limit("other", "*", tpm_limit=5), Limit(CONN, "m3", prod_guard=True)]
    assert [(r.model_key, r.tpm_limit) for r in capacity.covering(rows, CONN, "m1")] == [("*", 10), ("m1", 5)]
    assert capacity.covering(rows, CONN, "m3") == [rows[0]]           # a guard-only row limits nothing


# ── reserve (the limiter, fake clock) ───────────────────────────────────────────────────────────────────────────────

def test_request_n_plus_one_waits_for_the_window_instead_of_failing(monkeypatch, clock):
    _limits(monkeypatch, Limit(CONN, "*", tpm_limit=3000))
    with capacity.bind_request(1000, PROD):
        assert [capacity.reserve(CONN, "m") for _ in range(3)] == [0.0, 0.0, 0.0]
        assert capacity.pressure() == frozenset()                     # fitting at once is no pressure
        clock.t += 15
        waited = capacity.reserve(CONN, "m")                          # the 4th: waits ~45 s, then goes
    assert waited == pytest.approx(45.0, abs=1.1)
    assert PROD in capacity.pressure()                                # the worker holds prod claims for a while
    assert PROD not in capacity.pressure(clock.t + capacity.PRESSURE_HOLD_S + 1)


def test_a_reserve_that_fits_on_the_real_clock_is_no_wait_and_no_pressure(monkeypatch):
    """Real time.monotonic (no clock fixture): the two clock reads around the window work differ a little, which must
    not count as a wait, or every covered call would hold the worker's claims for PRESSURE_HOLD_S."""
    _limits(monkeypatch, Limit(CONN, "*", tpm_limit=10 ** 6), Limit(CONN, "m", tpm_limit=10 ** 6))
    slept = []
    monkeypatch.setattr(capacity, "_wait", lambda cond, timeout: slept.append(timeout))
    for line in (PROD, BULK, DEV):
        assert capacity.reserve(CONN, "m", 1000, line) == 0.0
    assert slept == []
    assert capacity.pressure() == frozenset()
    assert {snap["used"] for snap in capacity.windows_snapshot()} == {3000}


def test_past_the_line_wait_it_saturates(monkeypatch, clock):
    monkeypatch.setitem(capacity.WAIT_S, PROD, 5.0)
    _limits(monkeypatch, Limit(CONN, "*", tpm_limit=1000))
    capacity.reserve(CONN, "m", 1000, PROD)
    with pytest.raises(capacity.TokenLimitSaturated, match="token limit reached for the prod line"):
        capacity.reserve(CONN, "m", 10, PROD)
    snap = capacity.windows_snapshot()[0]
    assert snap["used"] == 1000 and snap["waiting"] == {PROD: 0, BULK: 0, DEV: 0}


def test_the_model_row_limits_too(monkeypatch, clock):
    monkeypatch.setitem(capacity.WAIT_S, BULK, 1.0)
    _limits(monkeypatch, Limit(CONN, "*", tpm_limit=100000), Limit(CONN, "m1", tpm_limit=100, bulk_pct=100))
    capacity.reserve(CONN, "m1", 100, BULK)
    with pytest.raises(capacity.TokenLimitSaturated):
        capacity.reserve(CONN, "m1", 10, BULK)
    assert capacity.reserve(CONN, "m2", 10, BULK) == 0.0               # another model: only the connection row


def test_no_estimate_no_limit_or_switched_off_reserves_nothing(monkeypatch, clock):
    _limits(monkeypatch, Limit(CONN, "*", tpm_limit=1))
    assert capacity.reserve(CONN, "m") == 0.0                         # no bound request (embeddings, probes)
    assert capacity.windows_snapshot() == []
    monkeypatch.setenv(capacity.SWITCH_ENV, "off")
    with capacity.bind_request(10, PROD):
        assert capacity.reserve(CONN, "m") == 0.0
    monkeypatch.delenv(capacity.SWITCH_ENV)
    _limits(monkeypatch)
    with capacity.bind_request(10, PROD):
        assert capacity.reserve(CONN, "m") == 0.0


def test_an_unreadable_limit_table_fails_open(monkeypatch, clock):
    def boom():
        raise RuntimeError("relation shared.llm_capacity_limit does not exist")
    monkeypatch.setattr(capacity, "loader", boom)
    capacity.invalidate()
    with capacity.bind_request(10 ** 6, PROD):
        assert capacity.reserve(CONN, "m") == 0.0
    assert capacity.limits() == []


def test_a_bulk_call_waits_for_a_waiting_prod_call_real_threads(monkeypatch):
    """Real time, a 0.3 s window: prod and bulk both wait for the window; prod goes first."""
    monkeypatch.setattr(capacity, "WINDOW_S", 0.3)
    monkeypatch.setattr(capacity, "WAKE_S", 0.02)
    _limits(monkeypatch, Limit(CONN, "*", tpm_limit=100, bulk_pct=100))
    capacity.reserve(CONN, "m", 100, PROD)                            # the window is full
    order = []

    def call(line):
        capacity.reserve(CONN, "m", 100, line)
        order.append(line)
    prod = threading.Thread(target=call, args=(PROD,))
    prod.start()
    while not any(w.waiting[PROD] for w in capacity._windows.values()):
        pass
    bulk = threading.Thread(target=call, args=(BULK,))
    bulk.start()
    prod.join(3)
    bulk.join(3)
    assert order == [PROD, BULK]


# ── through breaker.guarded and LLMClient ───────────────────────────────────────────────────────────────────────────

def test_a_saturated_token_limit_is_a_limiter_saturated_not_a_provider_failure(monkeypatch, clock):
    monkeypatch.setitem(capacity.WAIT_S, PROD, 0.0)
    _limits(monkeypatch, Limit(CONN, "*", tpm_limit=10))
    conn = _conn()
    calls = []
    with capacity.bind_request(10, PROD):
        assert breaker.guarded(conn, "m", lambda: calls.append(1) or "ok") == "ok"
        with pytest.raises(breaker.LimiterSaturated, match="token limit"):
            breaker.guarded(conn, "m", lambda: calls.append(2))
    assert calls == [1]                                               # the second call was never made
    assert classify(breaker.LimiterSaturated("x")) == "throttled"


def test_llm_client_binds_the_line_and_the_estimate(world, monkeypatch):  # noqa: F811 — the fixture
    seen = []
    real = breaker._reserve_tokens

    def spy(conn, model_key):
        seen.append((conn.connection_key, model_key, capacity.bound_request()))
        return real(conn, model_key)
    monkeypatch.setattr(breaker, "_reserve_tokens", spy)
    world.generate(model_tier="sonnet-5-5", max_tokens=100)
    with capacity.bind_kind("s1_rewrite"):
        world.generate(model_tier="sonnet-5-5", max_tokens=100)
    assert [s[2] for s in seen] == [(1 + 100, PROD), (1 + 100, BULK)]
    assert seen[0][:2] == ("acc3", "sonnet-5-5")
    assert capacity.bound_request() is None                           # reset after the request


def test_llm_client_waits_on_a_low_tpm_row_then_answers(world, monkeypatch, clock):  # noqa: F811
    _limits(monkeypatch, Limit("acc3", "*", tpm_limit=250))
    for _ in range(2):
        assert world.generate(model_tier="sonnet-5-5", max_tokens=100).content == "from acc3"
    assert clock.waits == []
    assert world.generate(model_tier="sonnet-5-5", max_tokens=100).content == "from acc3"   # N+1: waited, no error
    assert sum(clock.waits) == pytest.approx(60.0, abs=1.1)
    assert len(world.calls) == 3


# ── the hold (pure) ─────────────────────────────────────────────────────────────────────────────────────────────────

def _open(conn, model, cls, until):
    return Health(conn, model, last_error_at=T0 - timedelta(seconds=1), last_error_class=cls, consecutive_failures=1,
                  circuit_open_until=until, open_streak=1, opened_at=T0 - timedelta(seconds=1))


def test_an_open_prod_guard_connection_holds_prod_and_bulk():
    rows = [Limit(CONN, "*", prod_guard=True)]
    health = [_open(CONN, "m1", breaker.CREDENTIAL, T0 + timedelta(minutes=2))]
    h = hold_of(rows, health, now=T0)
    assert h.prod_open == (f"{CONN} (credential)",) and h.prod_refusing == h.prod_open
    assert h.lines() == {PROD, BULK}
    assert h.wave_pause_reason() == f"prod circuit refusing: {CONN} (credential)"


def test_a_half_open_guard_lets_prod_probe_but_keeps_bulk_waiting():
    rows = [Limit(CONN, "*", prod_guard=True)]
    health = [_open(CONN, "m1", breaker.CREDENTIAL, T0 - timedelta(seconds=5))]
    h = hold_of(rows, health, now=T0)
    assert h.prod_open == () and h.prod_refusing and h.lines() == {BULK}


def test_an_answer_since_the_refusal_closes_the_guard():
    rows = [Limit(CONN, "*", prod_guard=True)]
    health = [_open(CONN, "m1", breaker.CREDENTIAL, T0 - timedelta(seconds=5)),
              Health(CONN, "m2", last_ok_at=T0)]
    assert hold_of(rows, health, now=T0).lines() == frozenset()


def test_a_model_guard_follows_that_deployment_circuit():
    rows = [Limit(CONN, "m1", prod_guard=True)]
    throttled = _open(CONN, "m1", "throttled", T0 + timedelta(seconds=30))
    assert hold_of(rows, [throttled], now=T0).prod_open == (f"{CONN}/m1 (throttled)",)
    assert hold_of(rows, [_open(CONN, "m2", "throttled", T0 + timedelta(seconds=30))], now=T0).lines() == set()


def test_rows_without_prod_guard_never_hold():
    rows = [Limit(CONN, "*", tpm_limit=100)]
    health = [_open(CONN, "m1", breaker.CREDENTIAL, T0 + timedelta(minutes=2))]
    assert hold_of(rows, health, now=T0) == Hold()


def test_the_bulk_pause_a_staff_pause_and_pressure():
    assert hold_of([], [], bulk_paused=True).lines() == {BULK}
    assert hold_of([], [], bulk_paused=True).wave_pause_reason() == "bulk work paused in the app profile"
    assert hold_of([], [], waves_paused_by_staff=1).lines() == {BULK}
    assert hold_of([], [], waves_paused_by_staff=1).wave_pause_reason() is None   # nothing for the worker to pause
    assert hold_of([], [], pressure_lines=[BULK]).lines() == {BULK}
    assert hold_of([], [], pressure_lines=[PROD]).lines() == {PROD, BULK}
    assert hold_of([], [], pressure_lines=[DEV]).lines() == frozenset()            # the probes are never held


def test_held_kinds_and_caps():
    caps = {"s1_rewrite": 6, "t9_write": 4, "llm_connection_probe": 1, "ops_alert_email": 1}
    assert capacity.without_held(caps, {BULK}) == {"t9_write": 4, "llm_connection_probe": 1, "ops_alert_email": 1}
    assert capacity.without_held(caps, {PROD, BULK}) == {"llm_connection_probe": 1, "ops_alert_email": 1}
    assert capacity.without_held(caps, set()) == caps


# ── claim_hold / sync_waves / the worker (fake pool) ────────────────────────────────────────────────────────────────

class FakePool:
    def __init__(self, staff_paused=0, health=()):
        self.staff_paused = staff_paused
        self.health = list(health)
        self.sql = []
        self.paused_rows = [{"id": "w1", "name": "zz wave"}]

    async def fetchval(self, sql, *args):
        self.sql.append(sql)
        return self.staff_paused

    async def fetch(self, sql, *args):
        self.sql.append(sql)
        if "llm_provider_health" in sql:
            return [{c: getattr(h, c) for c in breaker._COLUMNS} for h in self.health]
        if "paused_at = now()" in sql:
            return self.paused_rows
        if "paused_at = NULL" in sql:
            return []
        return []

    async def execute(self, sql, *args):
        self.sql.append(sql)

    def acquire(self):
        pool = self

        class _A:
            async def __aenter__(self):
                return pool

            async def __aexit__(self, *exc):
                return False
        return _A()

    def transaction(self):
        return self.acquire()


def test_claim_hold_reads_the_guard_circuits_and_caches(monkeypatch):
    async def rows(db):
        return [Limit(CONN, "*", prod_guard=True)]
    monkeypatch.setattr(capacity, "loader_async", rows)
    now = datetime.now(timezone.utc)
    pool = FakePool(health=[_open(CONN, "m1", breaker.CREDENTIAL, now + timedelta(minutes=2))])
    h = asyncio.run(capacity.claim_hold(pool))
    assert h.prod_open and h.lines() == {PROD, BULK}
    n = len(pool.sql)
    assert asyncio.run(capacity.claim_hold(pool, with_pressure=False)) is capacity._hold
    assert len(pool.sql) == n                                          # cached HOLD_TTL_S


def test_claim_hold_fails_open(monkeypatch):
    async def boom(db):
        raise RuntimeError("db down")
    monkeypatch.setattr(capacity, "loader_async", boom)

    class Broken(FakePool):
        async def fetchval(self, sql, *args):
            raise RuntimeError("db down")
    assert asyncio.run(capacity.claim_hold(Broken())) == Hold()


def test_sync_waves_pauses_with_one_bell_per_wave_and_resumes():
    pool = FakePool()
    out = asyncio.run(capacity.sync_waves(pool, Hold(prod_refusing=("zz-conn (credential)",))))
    assert out == {"paused": ["w1"], "resumed": []}
    assert any("INSERT INTO shared.notifications" in s for s in pool.sql)
    pool.sql.clear()
    out = asyncio.run(capacity.sync_waves(pool, Hold()))
    assert out == {"paused": [], "resumed": []}
    assert any("paused_at = NULL" in s and "paused_by = $1" in s for s in pool.sql)


def test_sync_waves_before_migration_349_changes_nothing():
    import asyncpg

    class Old(FakePool):
        async def fetch(self, sql, *args):
            raise asyncpg.exceptions.UndefinedColumnError("column paused_at does not exist")
    assert asyncio.run(capacity.sync_waves(Old(), Hold(bulk_paused=True))) == {"paused": [], "resumed": []}


def test_the_worker_leaves_out_the_held_kinds(monkeypatch):
    load_kinds()
    claimed_caps = []

    async def claim(pool, worker_id, caps, lease):
        claimed_caps.append(dict(caps))
        return None

    async def hold(pool, with_pressure=True):
        return Hold(prod_open=("zz-conn (credential)",), prod_refusing=("zz-conn (credential)",))
    synced = []

    async def sync(pool, h):
        synced.append(h)
        return {"paused": [], "resumed": []}

    async def no_newer(pool, rev):
        return False
    monkeypatch.setattr("shared.jobs.queue.claim", claim)
    monkeypatch.setattr("shared.jobs.queue.newer_worker_alive", no_newer)
    monkeypatch.setattr(capacity, "claim_hold", hold)
    monkeypatch.setattr(capacity, "sync_waves", sync)
    w = Worker(pool=FakePool(), max_parallel=8)
    asyncio.run(w._fill_slots())
    caps = claimed_caps[0]
    assert "s1_rewrite" not in caps and "t9_write" not in caps            # bulk and prod wait in the queue
    assert "llm_connection_probe" in caps and "ops_alert_email" in caps   # probes and non-LLM kinds still run
    assert len(synced) == 1 and w._held_lines == {PROD, BULK}


def test_the_backlog_sampler_does_not_count_held_kinds_as_starved():
    now = T0

    class K:
        background = False
        expected_seconds = 600
    queued = [{"kind": "s1_rewrite", "n": 3, "oldest_run_after": now - timedelta(seconds=1200)}]
    workers = [{"worker_id": "w", "alive": True, "fresh": True, "max_parallel": 8, "caps": {"s1_rewrite": 6},
                "task_revision": 1, "started_at": now - timedelta(hours=1), "last_seen_at": now}]
    free = backlog.backlog_sample(queued, [], workers, {"s1_rewrite": K()}, now, now - timedelta(seconds=1200))
    assert "s1_rewrite" in free["starved"]
    held = backlog.backlog_sample(queued, [], workers, {"s1_rewrite": K()}, now, now - timedelta(seconds=1200),
                                  held={"s1_rewrite"})
    assert held["starved"] == {} and held["held"] == ["s1_rewrite"]


# ── admin validation ────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("fields,message", [
    ({}, "set tpm_limit, prod_guard or both"),
    ({"tpm_limit": 0}, "tpm_limit must be"),
    ({"tpm_limit": True}, "tpm_limit must be"),
    ({"tpm_limit": 10, "bulk_pct": 0}, "bulk_pct must be"),
    ({"tpm_limit": 10, "dev_pct": 101}, "dev_pct must be"),
    ({"prod_guard": True, "notes": "x" * 501}, "notes must be"),
])
def test_validate_refuses_what_migration_348_refuses(fields, message):
    with pytest.raises(ValueError, match=message):
        capacity.validate(fields)


def test_validate_keeps_a_good_row():
    assert capacity.validate({"tpm_limit": 2000, "bulk_pct": 30, "notes": "  zz  "}) == {
        "tpm_limit": 2000, "bulk_pct": 30, "dev_pct": None, "prod_guard": False, "notes": "zz"}
    assert capacity.validate({"prod_guard": True})["prod_guard"] is True


def test_the_routes_have_a_policy():
    from api.core import staff_authz
    from api.routers import admin_ai_gateway as gw
    for key in (("GET", "/admin/ai-gateway/capacity"),
                ("PUT", "/admin/ai-gateway/capacity/{connection_key}/{model_key}"),
                ("DELETE", "/admin/ai-gateway/capacity/{connection_key}/{model_key}"),
                ("POST", "/admin/waves/{wave_id}/pause"), ("POST", "/admin/waves/{wave_id}/resume")):
        assert key in staff_authz.POLICY
    assert "capacity" not in gw.PAGE_WRITES          # no page control yet (the card is a follow-up slice)
