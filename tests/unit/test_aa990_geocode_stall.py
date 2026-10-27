"""AA-990 / AA-905 — the place_geocode stall (S252 analysis RC1–RC5): the run that stops on the Geoapify daily
credit limit queues the next one for the credit reset; the locate answer is read leniently (fences, prose, wrapper,
re-spelt keys) and an unreadable one is asked again as two halves; each name without a point says why; a card's
multi-country string keys on an AA country; the data coverage page flags names due for more than 24 h.

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
           '"Hoi An": [15.88, 108.33, "Vietnam"], "Night Market": [19.89, 102.13, "Laos"]}')
    labels = ["Nowhere", "Bad", "Listvyanka", "Hội An", "Night Market (Luang Prabang)", "Dropped"]
    out = locate.read_answer(raw, labels, {"Night Market (Luang Prabang)": "Night Market"})
    assert out["Nowhere"] == locate.REASON_NULL
    assert out["Bad"] == locate.REASON_MALFORMED
    assert out["Listvyanka"] == "not_aa_country:Russia"
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


# ── (d) a card's multi-country string keys on an AA country ──────────────────────────────────────────────────────

@pytest.mark.parametrize("card_country, tour_country, want", [
    ("Nepal and China", "Nepal", "Nepal"),
    ("Nepal and China", "China", "China"),
    ("Nepal and China", "Thailand", "Nepal"),          # not the tour's: the first AA one
    ("India, Pakistan", "India", "India"),
    ("Myanmar (Burma)", "Thailand", "Myanmar"),
    ("Laos / Thailand", "Thailand", "Thailand"),
    ("thailand", "Laos", "Thailand"),                  # a plain AA country is unchanged
    ("Russia", "Mongolia", "Russia"),                  # non-AA only: kept as before
    (None, "Laos", ""),
])
def test_canonical_card_country(card_country, tour_country, want):
    assert pipeline.canonical(card_country, tour_country) == want


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


def _job_env(monkeypatch, result, remaining):
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
    assert calls == [{"reason": "place_geocode:credit_reset"}]


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
