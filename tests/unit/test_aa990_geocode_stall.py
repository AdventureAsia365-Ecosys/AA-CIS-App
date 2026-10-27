"""AA-990 / AA-905 — the place_geocode stall (S252 analysis RC1–RC5): the run that stops on the Geoapify daily
credit limit queues the next one for the credit reset; the locate answer is read leniently (fences, prose, wrapper,
re-spelt keys) and an unreadable one is asked again as two halves; each name without a point says why; a card's
multi-country string keys on one of the ISO countries it names; the data coverage page flags names due for more than
24 h.

Fakes only: no DB, no network, no paid call."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from services.places import enqueue as enq
from services.places import locate, pipeline


# ── (c) tolerant JSON ─────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    '```json\n{"Paro": [27.43, 89.41, "Bhutan"]}\n```',
    'Here are the coordinates:\n{"Paro": [27.43, 89.41, "Bhutan"]}',
    '{"Paro": [27.43, 89.41, "Bhutan"]}\nNote: {uncertain} values were left null.',     # prose with a brace after
    '{"places": {"Paro": [27.43, 89.41, "Bhutan"]}}',                                   # one-key wrapper
    'I think {maybe} this: {"Paro": [27.43, 89.41, "Bhutan"]}',                          # a non-JSON brace first
])
def test_locate_answer_is_read_through_fences_prose_and_a_wrapper(raw):
    assert locate.parse_answer(raw, ["Paro"]) == {"Paro": locate.Proposal(27.43, 89.41, "Bhutan")}


@pytest.mark.parametrize("raw", ["", "I cannot help with that.", '{"Paro": [27.43, 89.41, "Bhu', "[1, 2]"])
def test_unreadable_locate_answer_raises(raw):
    with pytest.raises(locate.AnswerUnreadable, match="no JSON object in the locate answer"):
        locate.read_answer(raw, ["Paro"])


def test_wrapper_is_kept_when_its_key_is_a_label():
    # a one-name batch whose own key holds an object is a malformed value, not a wrapper
    assert locate.read_answer('{"Paro": {"lat": 27.4}}', ["Paro"]) == {"Paro": locate.REASON_MALFORMED}


# ── (d) one reason per name ───────────────────────────────────────────────────────────────────────────────────────

def test_each_name_without_a_point_says_why():
    raw = ('{"Nowhere": null, "Bad": ["x", 1, "Laos"], "Listvyanka": [51.85, 104.87, "Russia"], '
           '"Taiga Camp": [55.0, 100.0, "Siberia"], "K2 Base Camp": [35.8, 76.5, "India and Pakistan"], '
           '"Hoi An": [15.88, 108.33, "Vietnam"], "Night Market": [19.89, 102.13, "Laos"]}')
    labels = ["Nowhere", "Bad", "Listvyanka", "Taiga Camp", "K2 Base Camp", "Hội An", "Night Market (Luang Prabang)",
              "Dropped"]
    out = locate.read_answer(raw, labels, {"Night Market (Luang Prabang)": "Night Market"})
    assert out["Nowhere"] == locate.REASON_NULL
    assert out["Bad"] == locate.REASON_MALFORMED
    # any ISO country is taken (the place's own country; point_ok checks bbox + polygon next) — no "AA list"
    assert out["Listvyanka"] == locate.Proposal(51.85, 104.87, "Russian Federation")
    # only text that names no ISO country at all (a region, a compound) gets the reason
    assert out["Taiga Camp"] == f"{locate.UNKNOWN_COUNTRY}:Siberia"
    assert out["K2 Base Camp"] == "unknown_country:India and Pakistan"
    assert out["Hội An"] == locate.Proposal(15.88, 108.33, "Vietnam")                      # re-spelt within name_norm
    assert out["Night Market (Luang Prabang)"] == locate.Proposal(19.89, 102.13, "Laos")   # answered by plain name
    assert out["Dropped"] == locate.REASON_MISSING


def test_plain_name_is_not_used_when_two_labels_share_it():
    raw = '{"Night Market": [19.89, 102.13, "Laos"]}'
    labels = ["Night Market (Luang Prabang)", "Night Market (Vientiane)"]
    out = locate.read_answer(raw, labels, {n: "Night Market" for n in labels})
    assert set(out.values()) == {locate.REASON_MISSING}


class _NoGeocoder:
    calls_external = False
    name = "model_only"


@pytest.mark.asyncio
async def test_unreadable_batch_is_asked_again_as_two_halves():
    items = [locate.Item((n,), n, "Bhutan") for n in ("Paro", "Punakha", "Haa", "Bumthang")]
    calls = []

    async def generate(system, user):
        calls.append(user)
        if len(calls) == 1:
            return '{"Paro": [27.43, 89.41, "Bhutan"], "Punakha": [27.5'     # cut at max_tokens
        names = [i.name for i in items if f"- {i.name} |" in user]
        return "{" + ", ".join(f'"{n}": [27.4, 89.5, "Bhutan"]' for n in names) + "}"

    out = await locate.locate_many(items, generate, _NoGeocoder())
    assert len(calls) == 3
    assert all(out[i.key].point is not None and out[i.key].error is None for i in items)


@pytest.mark.asyncio
async def test_a_half_still_unreadable_counts_its_names_as_failed_only():
    items = [locate.Item((n,), n, "Bhutan") for n in ("Paro", "Punakha", "Haa", "Bumthang")]
    calls = []

    async def generate(system, user):
        calls.append(user)
        if len(calls) == 3:
            return '{"Haa": [27.3, 89.2, "Bhu'
        if len(calls) == 1:
            return ""
        return '{"Paro": [27.43, 89.41, "Bhutan"], "Punakha": [27.58, 89.86, "Bhutan"]}'

    out = await locate.locate_many(items, generate, _NoGeocoder())
    assert out[("Paro",)].point and out[("Punakha",)].point
    for k in (("Haa",), ("Bumthang",)):
        assert out[k].point is None and out[k].error.startswith("locate: no JSON object")


@pytest.mark.asyncio
async def test_one_name_batch_is_not_split():
    calls = []

    async def generate(system, user):
        calls.append(user)
        return "sorry"

    out = await locate.locate_many([locate.Item(("paro",), "Paro", "Bhutan")], generate, _NoGeocoder())
    assert len(calls) == 1 and out[("paro",)].error.startswith("locate: no JSON object")


# ── (d) a card's multi-country string keys on ONE of the countries it names (ISO, shared/countries) ──────────────

@pytest.mark.parametrize("card_country, tour_country, want", [
    ("Nepal and China", "Nepal", "Nepal"),
    ("Nepal and China", "China", "China"),
    ("Nepal and China", "Thailand", "Nepal"),          # the tour's is not named: the first named, never the tour's
    ("India, Pakistan", "India", "India"),
    ("India, Pakistan", "Pakistan", "Pakistan"),       # every ISO country counts, not an "AA list"
    ("Myanmar (Burma)", "Thailand", "Myanmar"),
    ("Laos / Thailand", "Thailand", "Thailand"),
    ("thailand", "Laos", "Thailand"),                  # a plain country is canonical, never the tour's
    ("Tanzania", "India", "Tanzania"),                 # cross-border: the place's own country
    ("Russia", "Mongolia", "Russian Federation"),      # an alias -> the shared.countries name
    ("Himalayas", "Nepal", "Himalayas"),               # names no country: kept as written, not forced to the tour's
    (None, "Laos", ""),
])
def test_canonical_card_country(card_country, tour_country, want):
    assert pipeline.canonical(card_country, tour_country) == want


@pytest.mark.parametrize("card_country, tour_country", [
    ("Nepal and China", "China"), ("Nepal and China", "Thailand"), ("Myanmar (Burma)", "Thailand"),
    ("Tanzania", "India"), ("Viet Nam", "Laos"),
])
def test_component_key_and_new_card_country_follow_one_rule(card_country, tour_country):
    """pipeline.canonical (the place-name key) and place_cards.normalise_card_country (a new card's country) agree
    whenever the text names a country (shared.countries.one_country)."""
    from services.acp_shared.place_cards import normalise_card_country
    assert pipeline.canonical(card_country, tour_country) == normalise_card_country(card_country, tour_country)


# ── (a) resume after the credit reset ─────────────────────────────────────────────────────────────────────────────

def test_next_credit_reset_is_the_next_utc_midnight_plus_delay_and_jitter():
    at = enq.next_credit_reset(datetime(2026, 10, 26, 15, 10, tzinfo=timezone.utc), jitter_s=0)
    assert at == datetime(2026, 10, 27, 0, 10, tzinfo=timezone.utc)
    late = enq.next_credit_reset(datetime(2026, 10, 26, 23, 59, 59, tzinfo=timezone.utc), jitter_s=42)
    assert late == datetime(2026, 10, 27, 0, 10, 42, tzinfo=timezone.utc)
    for _ in range(20):
        j = enq.next_credit_reset(datetime(2026, 10, 26, 3, 0, tzinfo=timezone.utc))
        assert datetime(2026, 10, 27, 0, 10, tzinfo=timezone.utc) <= j \
            <= datetime(2026, 10, 27, 0, 15, tzinfo=timezone.utc)


class _JobDb:
    def __init__(self, queued=None):
        self.queued, self.sql = queued, []

    async def fetch(self, sql, *args):
        self.sql.append((sql, args))
        return [{"id": self.queued}] if self.queued else []

    async def fetchval(self, sql, *args):
        self.sql.append((sql, args))
        return None


@pytest.mark.asyncio
async def test_credit_reset_enqueue_is_keyed_per_reset_day(monkeypatch):
    seen = []

    async def fake_enqueue(db, kind, payload, **kw):
        seen.append((kind, payload, kw))
        return "new", True
    monkeypatch.setattr(enq, "enqueue", fake_enqueue)
    now = datetime(2026, 10, 26, 15, 10, tzinfo=timezone.utc)
    job, at, created = await enq.enqueue_after_credit_reset(_JobDb(), reason="place_geocode:credit_reset", now=now)
    assert (job, created) == ("new", True) and at.date().isoformat() == "2026-10-27"
    kind, payload, kw = seen[0]
    assert kind == "place_geocode" and kw["run_after"] == at
    assert kw["idempotency_key"] == "place_geocode:credit_reset:2026-10-27"


@pytest.mark.asyncio
async def test_credit_reset_moves_an_already_queued_geocode_job(monkeypatch):
    async def fake_enqueue(*a, **kw):
        raise AssertionError("no second row while one is queued")
    monkeypatch.setattr(enq, "enqueue", fake_enqueue)
    db = _JobDb(queued="q1")
    job, at, created = await enq.enqueue_after_credit_reset(db, reason="r")
    assert (job, created) == ("q1", False)
    sql, args = db.sql[0]
    assert "SET run_after = $2" in sql and "status = 'queued'" in sql and args == ("place_geocode", at)


class _KeyedJobDb:
    """No queued geocode row; `rows` maps job id -> status, `keys` idempotency key -> job id (queue.enqueue's
    ON CONFLICT: a used key returns its row whatever the status)."""

    def __init__(self, rows, keys):
        self.rows, self.keys = dict(rows), dict(keys)

    async def fetch(self, sql, *args):
        return []

    async def fetchval(self, sql, *args):
        if sql == enq._STATUS_SQL:
            return self.rows.get(args[0])
        return None

    async def enqueue(self, db, kind, payload, *, idempotency_key=None, **kw):
        if idempotency_key in self.keys:
            return self.keys[idempotency_key], False
        job_id = f"new{len(self.rows)}"
        self.rows[job_id] = "queued"
        if idempotency_key:
            self.keys[idempotency_key] = job_id
        return job_id, True


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["cancelled", "failed", "succeeded"])
async def test_a_dead_reset_row_under_the_day_key_gets_a_new_queued_row(monkeypatch, status):
    now = datetime(2026, 10, 26, 15, 10, tzinfo=timezone.utc)
    day_key = f"place_geocode:credit_reset:{enq.next_credit_reset(now, jitter_s=0):%Y-%m-%d}"
    db = _KeyedJobDb({"old": status}, {day_key: "old"})
    monkeypatch.setattr(enq, "enqueue", db.enqueue)
    job, _, created = await enq.enqueue_after_credit_reset(db, reason="r", now=now, caller_job_id="caller")
    assert created and job != "old" and db.rows[job] == "queued"
    assert db.keys[f"{day_key}:caller"] == job


@pytest.mark.asyncio
async def test_the_reset_row_stopping_again_queues_a_new_row_not_itself(monkeypatch):
    # an admin retry ran the reset row early with no credits left: the day key points at the caller, running
    now = datetime(2026, 10, 26, 15, 10, tzinfo=timezone.utc)
    day_key = f"place_geocode:credit_reset:{enq.next_credit_reset(now, jitter_s=0):%Y-%m-%d}"
    db = _KeyedJobDb({"self": "running"}, {day_key: "self"})
    monkeypatch.setattr(enq, "enqueue", db.enqueue)
    job, _, created = await enq.enqueue_after_credit_reset(db, reason="r", now=now, caller_job_id="self")
    assert created and job not in ("self",) and db.rows[job] == "queued"


@pytest.mark.asyncio
async def test_both_keys_used_falls_back_to_a_keyless_row(monkeypatch):
    # the same job stopped twice (a retried attempt) after its suffixed row was cancelled too
    now = datetime(2026, 10, 26, 15, 10, tzinfo=timezone.utc)
    day_key = f"place_geocode:credit_reset:{enq.next_credit_reset(now, jitter_s=0):%Y-%m-%d}"
    db = _KeyedJobDb({"old": "cancelled", "old2": "cancelled"}, {day_key: "old", f"{day_key}:c": "old2"})
    monkeypatch.setattr(enq, "enqueue", db.enqueue)
    job, _, created = await enq.enqueue_after_credit_reset(db, reason="r", now=now, caller_job_id="c")
    assert created and job not in ("old", "old2") and db.rows[job] == "queued"


@pytest.mark.asyncio
async def test_a_still_queued_day_key_row_is_reused(monkeypatch):
    # the row went queued between the queued-row check and the insert (a race): reuse it
    now = datetime(2026, 10, 26, 15, 10, tzinfo=timezone.utc)
    day_key = f"place_geocode:credit_reset:{enq.next_credit_reset(now, jitter_s=0):%Y-%m-%d}"
    db = _KeyedJobDb({"q": "queued"}, {day_key: "q"})
    monkeypatch.setattr(enq, "enqueue", db.enqueue)
    assert (await enq.enqueue_after_credit_reset(db, reason="r", now=now, caller_job_id="c"))[0::2] == ("q", False)
    assert len(db.rows) == 1


class _Pool:
    @asynccontextmanager
    async def acquire(self):
        yield SimpleNamespace()


class _Geo:
    name = "geoapify"
    calls_external = True
    cost_per_request = 0.0
    requests = 0

    async def aclose(self):
        pass


def _job_env(monkeypatch, result, remaining, started=datetime(2026, 10, 26, 15, 0, tzinfo=timezone.utc)):
    from services.jobs import place_geocode_job as job
    from services.places import store

    calls = []

    async def fake_reset(pool, **kw):
        calls.append(kw)
        return "next", datetime(2026, 10, 27, 0, 12, tzinfo=timezone.utc), True

    async def fake_pending(pool, generate, geocoder, **kw):
        return dict(result)

    async def zero(*a, **kw):
        return 0

    async def none(*a, **kw):
        return None

    async def due(conn):
        return remaining
    monkeypatch.setattr(job, "_utcnow", lambda: started)
    monkeypatch.setattr(job, "get_geocoder", lambda: _Geo())
    monkeypatch.setattr(job, "load_run_budget", none)
    monkeypatch.setattr(job.llm, "make_generate", lambda *a, **kw: None)
    monkeypatch.setattr(job, "geocode_pending", fake_pending)
    monkeypatch.setattr(job, "enqueue_after_credit_reset", fake_reset)
    monkeypatch.setattr(store, "log_geocode_calls", none)
    monkeypatch.setattr(store, "pending_names_due", due)
    monkeypatch.setattr(store, "geocoder_requests_today", zero)
    ctx = SimpleNamespace(payload={}, pool=_Pool(), job_id="j", progress=None, add_cost=lambda c: None,
                          set_result=lambda r: None)
    return job, ctx, calls


@pytest.mark.asyncio
async def test_credit_stop_with_names_due_queues_the_next_run(monkeypatch):
    job, ctx, calls = _job_env(monkeypatch, {"names": 400, "deferred": 400, "stopped_credit_limit": True,
                                             "relinked": {}}, remaining=3956)
    out = await job._run_locked(ctx)
    assert out["status"] == "stopped_credit_limit" and out["pending_names_due"] == 3956
    assert out["next_job"] == "next" and out["next_run_after"] == "2026-10-27T00:12:00+00:00"
    assert calls == [{"reason": "place_geocode:credit_reset", "now": datetime(2026, 10, 26, 15, 0, tzinfo=timezone.utc),
                      "caller_job_id": "j"}]


@pytest.mark.asyncio
async def test_a_run_crossing_utc_midnight_resumes_after_the_reset_of_the_day_it_counted(monkeypatch):
    # started 23:58Z on yesterday's credit count, stopped at 00:01Z: the reset is 00:10Z today, not tomorrow
    job, ctx, calls = _job_env(monkeypatch, {"names": 400, "deferred": 400, "stopped_credit_limit": True,
                                             "relinked": {}}, remaining=10,
                               started=datetime(2026, 10, 26, 23, 58, tzinfo=timezone.utc))
    await job._run_locked(ctx)
    assert calls[0]["now"] == datetime(2026, 10, 26, 23, 58, tzinfo=timezone.utc)
    assert enq.next_credit_reset(calls[0]["now"], jitter_s=0) == datetime(2026, 10, 27, 0, 10, tzinfo=timezone.utc)
    assert enq.next_credit_reset(datetime(2026, 10, 27, 0, 1, tzinfo=timezone.utc), jitter_s=0) \
        == datetime(2026, 10, 28, 0, 10, tzinfo=timezone.utc)   # what reading the clock at the stop would give


@pytest.mark.asyncio
async def test_no_resume_without_names_due_or_when_the_geocoder_is_unavailable(monkeypatch):
    job, ctx, calls = _job_env(monkeypatch, {"names": 3, "stopped_credit_limit": True, "relinked": {}}, remaining=0)
    out = await job._run_locked(ctx)
    assert out["status"] == "stopped_credit_limit" and "next_job" not in out and calls == []
    job, ctx, calls = _job_env(monkeypatch, {"names": 3, "stopped_geocoder_unavailable": True, "relinked": {}},
                               remaining=5)
    out = await job._run_locked(ctx)
    assert out["status"] == "stopped_geocoder_unavailable" and "next_job" not in out and calls == []


# ── (b) the stall is visible on the data coverage page ──────────────────────────────────────────────────────────

def test_names_due_over_24h_is_a_must_row_on_the_data_coverage_page():
    from api.services.data_coverage import queries, registry
    assert "names_due_over_24h" in queries.GEO and "now() - interval '24 hours'" in queries.GEO
    row = next(r for r in registry.ROWS if r.id == "geo.names_stalled")
    assert row.kind == "must" and row.polarity == "defect"
    assert row.value(lambda src, name: 7 if (src, name) == ("geo", "names_due_over_24h") else None) == 7
