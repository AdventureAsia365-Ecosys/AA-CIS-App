"""AA-712 — place pipeline pure logic + adapters with fakes (no DB, no model, no geocoder call)."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.places import components as cm
from services.places import geocoder as gc
from services.places import guards, locate, names

MIGRATION = Path(__file__).resolve().parents[2] / "api" / "migrations" / "226_geo_place_pipeline.sql"


# ── names ──────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("Hội An", "hoi an"), ("Hoi-An", "hoi an"), ("  HOI AN ", "hoi an"), ("The Old Quarter", "old quarter"),
    ("the", "the"), ("A", "a"), ("Điện Biên Phủ", "dien bien phu"), ("Khövsgöl Nuur", "khovsgol nuur"),
    ("Xi’an", "xian"), ("Straße", "strasse"), ("", ""), (None, ""), ("Theatre Square", "theatre square"),
])
def test_name_norm(raw, expected):
    assert names.name_norm(raw) == expected


def test_migration_carries_the_same_accent_table_as_python():
    sql = MIGRATION.read_text(encoding="utf-8")
    m = re.search(r"translate\(.*?'ss'\).*?'’', ''''\),\s*'([^']+)',\s*'([^']+)'\)", sql, re.S)
    assert m, "translate() literal not found in migration 226"
    assert (m.group(1), m.group(2)) == (names.ACCENTS_FROM, names.ACCENTS_TO)


def test_component_id_is_stable_and_ignores_case_and_accents():
    a = names.component_id("src-1", 3, "Hội An", "Vietnam", "culinary")
    assert a == names.component_id("src-1", 3, "hoi an", "vietnam ", "Culinary")
    assert a != names.component_id("src-1", 4, "Hội An", "Vietnam", "culinary")
    assert a != names.component_id("src-2", 3, "Hội An", "Vietnam", "culinary")
    assert a == "%s" % names.component_id("src-1", 3, "Hoi-An", "Vietnam", "culinary")


def test_locality_key_only_for_generic_names():
    assert names.locality_key("Paro", None) == ""
    assert names.locality_key("Night Market", "Luang Prabang") == "luang prabang"
    assert names.locality_key("Night Market", None) is None


# ── components ─────────────────────────────────────────────────────────────────────────────────

def _atom(i, day, place, at="culture", text=None, **kw):
    return {"atom_id": f"atom_{i}", "itinerary_day": day, "place": place, "activity_type": at,
            "text": text or f"{place} — visit", **kw}


def test_components_merge_skip_and_link_fields():
    atoms = [
        _atom(1, 1, "Paro Dzong", text="Paro Dzong — short"),
        _atom(2, 1, "Paro Dzong", text="Paro Dzong — a much longer description of the fortress"),
        _atom(3, 1, "Hotel Olathang", at="stay"),                 # logistics
        _atom(4, 2, "Thimphu", at="transit"),                     # transit never pinned
        _atom(5, 2, "Night Market", at="food"),                   # generic → locality from day town
        _atom(6, 3, "Night Market", at="food"),                   # generic, no town → unresolvable
        _atom(7, 2, "Tiger's Nest", at="trek", card_id="c1", card_display_name="Paro Taktsang",
              card_country="Bhutan", card_locality="Paro"),
        _atom(8, None, "Punakha"),                                # no day
    ]
    comps, counters = cm.build_components("src", "Bhutan", atoms, {2: "Thimphu"})
    by = {(c.day_index, c.name): c for c in comps}
    paro = by[(1, "Paro Dzong")]
    assert paro.atom_ids == ["atom_1", "atom_2"] and "much longer" in paro.text_extract
    assert paro.activity == "cultural_heritage" and paro.intensity_level == "leisurely"
    assert by[(2, "Night Market")].locality_norm == "thimphu"
    assert by[(3, "Night Market")].locality_norm is None and counters["generic_without_locality"] == 1
    taktsang = by[(2, "Tiger's Nest")]
    assert taktsang.link_name == "Paro Taktsang" and taktsang.name_norm == "paro taktsang"
    assert taktsang.place_card_id == "c1" and taktsang.activity == "trekking"
    assert taktsang.component_id == names.component_id("src", 2, "Tiger's Nest", "Bhutan", "trekking")
    assert counters["skipped"] == {"logistics_place": 1, "transit": 1, "no_day": 1}
    assert counters["components"] == len(comps) == 4


def test_components_hash_ignores_order_and_sees_text_changes():
    a = [_atom(1, 1, "A"), _atom(2, 2, "B")]
    assert cm.components_hash(a) == cm.components_hash(list(reversed(a)))
    assert cm.components_hash(a) != cm.components_hash([_atom(1, 1, "A", text="changed"), _atom(2, 2, "B")])


@pytest.mark.parametrize("field,value", [("card_id", "c1"), ("card_display_name", "Hoi An Ancient Town"),
                                         ("card_country", "Vietnam"), ("card_locality", "Hoi An"),
                                         ("card_destination_id", "d1")])
def test_components_hash_sees_a_card_that_arrives_after_atomize(field, value):
    # review finding 4: a card linked later must make place_extract rebuild (card identity link)
    before = [_atom(1, 1, "Ancient Town")]
    assert cm.components_hash(before) != cm.components_hash([_atom(1, 1, "Ancient Town", **{field: value})])


@pytest.mark.asyncio
async def test_place_cards_build_enqueues_place_extract_when_cards_were_linked(monkeypatch):
    from services.jobs import place_cards_job as job
    from services.places import enqueue as enq
    ctx = SimpleNamespace(payload={"tour_id": "t1"}, pool=_Pool(SimpleNamespace(fetch=AsyncMock(return_value=[]))))
    extract = AsyncMock(return_value=("pe1", True))
    monkeypatch.setattr(enq, "enqueue_place_extract", extract)
    monkeypatch.setattr(job, "ensure_place_cards", AsyncMock(return_value={"linked_existing": 0, "built": 2}))
    out = await job.run(ctx)
    assert out["place_extract_job"] == "pe1"
    assert extract.await_args.kwargs["tour_id"] == "t1" and extract.await_args.kwargs["reason"] == "place_cards_build"
    extract.reset_mock()
    monkeypatch.setattr(job, "ensure_place_cards", AsyncMock(return_value={"linked_existing": 0, "built": 0}))
    assert "place_extract_job" not in await job.run(ctx) and not extract.await_count


@pytest.mark.parametrize("at,text,expected", [
    ("other", "Safari in the national park", "wildlife_nature"), ("other", "Kayak the lagoon", "water_activities"),
    ("", "something else", "local_immersion"), ("transit", "x", None), ("bike", "x", "adventure_sport"),
])
def test_map_activity(at, text, expected):
    assert cm.map_activity(at, text) == expected


# ── guards ─────────────────────────────────────────────────────────────────────────────────────

def test_guard_countries_cover_every_raw_tours_country():
    # review finding 5: a live tour's country outside the guard list never gets a graph
    sql = (MIGRATION.parent / "150_country_check_constraint.sql").read_text()
    block = sql[sql.index("chk_raw_tours_country CHECK"):]
    allowed = set(re.findall(r"'([A-Z][A-Za-z ]+)'", block[:block.index("]")]))
    assert len(allowed) == 18
    assert {guards.canonical_country(c) for c in allowed} == allowed
    for c in allowed:
        iso = guards.iso2(c)
        assert iso in guards.BBOX and iso in guards.ISO2_ISO3 and iso in guards.AA_ISO2
    assert guards.point_ok(-8.65, 115.2, "Indonesia") is None           # Bali
    assert guards.point_ok(3.14, 101.69, "Malaysia") is None            # Kuala Lumpur
    assert guards.point_ok(42.87, 74.59, "Kyrgyzstan") is None          # Bishkek
    assert guards.point_ok(39.65, 66.96, "Uzbekistan") is None          # Samarkand
    assert gc.GeoapifyGeocoder.params("Ubud", guards.iso2("Indonesia"))["filter"] == "countrycode:id"
    assert {"id", "my", "kg", "uz"} <= set(guards.AA_ISO2)


def test_point_guard_country_and_bbox():
    assert guards.point_ok(18.79, 98.98, "Thailand") is None                 # Chiang Mai
    assert guards.point_ok(18.79, 98.98, "Vietnam") == "outside_country_bbox"
    assert guards.point_ok(48.85, 2.35, "France") == "not_aa_country"
    assert guards.point_ok(0, 0, "Laos") == "out_of_range"
    assert guards.point_ok(29.65, 91.1, "Tibet") is None                      # Lhasa → China
    assert guards.canonical_country("SRI-LANDKA") == "Sri Lanka"


def _stop(day, dest, lat, lng, transit=False):
    return guards.DayStop(day, dest, lat, lng, transit)


def test_outlier_rule_is_transit_aware():
    delhi, leh, roi_et = (28.61, 77.21), (34.16, 77.58), (16.05, 103.65)
    # Delhi → Leh by flight (≈ 620 km): not an outlier
    assert guards.outlier_observations([_stop(1, "delhi", *delhi), _stop(2, "leh", *leh, transit=True)]) == []
    # same jump by road with no transit word: both ends are outliers of each other
    obs = guards.outlier_observations([_stop(1, "delhi", *delhi), _stop(2, "leh", *leh)])
    assert {o[0] for o in obs} == {"delhi", "leh"} and all(o[2] > 600 for o in obs)
    # a wrong point between two near stops (Chiang Mai geocoded into the far south): only it is flagged
    cm_, phuket = (18.79, 98.98), (7.88, 98.39)
    obs = guards.outlier_observations([_stop(1, "a", *cm_), _stop(2, "bad", *phuket), _stop(3, "b", *cm_)])
    assert [o[0] for o in obs] == ["bad"]
    # a transit day anywhere inside the jump (day 2 flight, wrong-looking day 3) excuses it
    assert guards.outlier_observations([_stop(1, "a", *cm_), _stop(2, "b", *cm_, transit=True),
                                        _stop(3, "c", *phuket), _stop(4, "d", *phuket)]) == []
    # one near neighbour is enough to not be an outlier
    assert guards.outlier_observations([_stop(1, "a", *cm_), _stop(2, "b", 18.8, 99.0), _stop(3, "c", *phuket)]) \
        == [("c", 3, pytest.approx(guards.km(*cm_, *phuket), abs=0.1))]
    assert guards.km(*cm_, *roi_et) < 600   # the S202 Roi Et case is caught by the model-first locate, not here


def test_suspect_and_overwrite_rules():
    assert guards.is_suspect(2, "geoapify") and guards.is_suspect(1, "llm")
    assert not guards.is_suspect(1, "geoapify") and not guards.is_suspect(5, "llm", "manual")
    assert guards.may_overwrite("llm", "ok", "geoapify")
    assert guards.may_overwrite("mapbox", "ok", "geoapify")                   # legacy TP Temporary row: re-confirm
    assert not guards.may_overwrite("geoapify", "ok", "llm")
    assert not guards.may_overwrite("mapbox", "ok", "llm")
    assert not guards.may_overwrite("llm", "ok", "llm")
    assert not guards.may_overwrite("llm", "ok", "mapbox")                    # Mapbox no longer confirms
    assert guards.may_overwrite(None, "ok", "llm")                            # legacy, unlocated row
    assert not guards.may_overwrite("llm", "manual", "geoapify", force=True)  # admin fix wins always
    assert guards.may_overwrite("geoapify", "ok", "geoapify", force=True)


# ── locate + geocoder adapter ──────────────────────────────────────────────────────────────────

def test_parse_answer_keeps_known_places_and_rejects_the_rest():
    raw = 'Sure: {"Chiang Mai": [18.79, 98.98, "thailand"], "Paro": [27.43, 89.41, "Narnia"], ' \
          '"Nowhere": null, "Bad": ["x", 1, "Laos"]}'
    out = locate.parse_answer(raw, ["Chiang Mai", "Paro", "Nowhere", "Bad", "Missing"])
    assert out["Chiang Mai"] == locate.Proposal(18.79, 98.98, "Thailand")
    assert all(out[k] is None for k in ["Paro", "Nowhere", "Bad", "Missing"])
    with pytest.raises(ValueError):
        locate.parse_answer("no json here", ["A"])


def test_batches_keep_labels_distinct():
    items = [locate.Item((f"k{i}",), "Paro", "Bhutan") for i in range(3)] + [locate.Item(("x",), "Leh", "India")]
    bs = locate.batches(items, size=40)
    assert [len(b) for b in bs] == [2, 1, 1]


class _FakeGeocoder:
    calls_external = True
    name = "fake"
    cost_per_request = 0.005

    def __init__(self, results=None, fail=False):
        self.results, self.fail, self.requests = results or {}, fail, 0

    async def confirm(self, name, lat, lng, country, *, native_name=None):
        self.requests += 1
        if self.fail:
            raise RuntimeError("HTTP 401")
        return self.results.get(name)


@pytest.mark.asyncio
async def test_locate_many_confirm_mismatch_and_failures():
    async def generate(system, user):
        return ('{"Delhi": [28.61, 77.21, "India"], "Laya": [28.07, 89.7, "Bhutan"], "Ghost": null, '
                '"Siem Reap": [13.36, 103.86, "Cambodia"], "Mars": [18.79, 98.98, "Vietnam"]}')

    geo = _FakeGeocoder({"Delhi": gc.Confirmed(28.6, 77.2, "India", "dX", "geoapify"),
                         "Laya": gc.Confirmed(28.0, 89.7, "Nepal", "n", "geoapify")})   # other country: ignored
    items = [locate.Item(("delhi",), "Delhi", "India"), locate.Item(("laya",), "Laya", "Bhutan"),
             locate.Item(("ghost",), "Ghost", "Laos"), locate.Item(("sr",), "Siem Reap", "Thailand"),
             locate.Item(("mars",), "Mars", "Vietnam")]
    out = await locate.locate_many(items, generate, geo)
    assert out[("delhi",)].point.located_by == "geoapify" and out[("delhi",)].point.place_id == "dX"
    assert out[("laya",)].point == locate.Located(28.07, 89.7, "Bhutan", "llm")
    assert out[("laya",)].point.meta["outcome"] == "rejected_country_guard"
    assert out[("ghost",)].point is None and out[("ghost",)].reason == "unknown_to_model"
    assert out[("sr",)].point.country == "Cambodia"                          # cross-border night kept
    assert out[("mars",)].point is None and out[("mars",)].reason == "outside_country_bbox"
    assert geo.requests == 3                                                 # guard-rejected / unknown: no call

    failing = await locate.locate_many(items[:1], generate, _FakeGeocoder(fail=True))
    assert failing[("delhi",)].point is None and "401" in failing[("delhi",)].error   # retried later

    async def broken(system, user):
        raise RuntimeError("throttled")
    out = await locate.locate_many(items[:1], broken, None)
    assert out[("delhi",)].error.startswith("locate:")


@pytest.mark.asyncio
async def test_locate_many_propagates_budget_stop():
    from shared.cost_guard import BudgetExceeded

    async def over(system, user):
        raise BudgetExceeded("over", "bedrock", "per_run")
    with pytest.raises(BudgetExceeded):
        await locate.locate_many([locate.Item(("a",), "A", "Laos")], over, None)


def test_geocoder_factory(monkeypatch):
    monkeypatch.setenv("GEOCODER_PROVIDER", "mapbox")                       # typo / unknown → no paid call
    assert gc.get_geocoder().name == "model_only" and not gc.get_geocoder().calls_external
    monkeypatch.setenv("GEOCODER_PROVIDER", "geoapify")
    monkeypatch.setenv("GEOCODER_COST_PER_REQUEST_USD", "0.004")
    g = gc.get_geocoder(api_key="k")
    assert g.name == "geoapify" and g.cost_per_request == 0.004 and g.calls_external


@pytest.mark.asyncio
async def test_model_only_never_calls_out():
    g = gc.ModelOnlyGeocoder()
    assert await g.confirm("Paro", 27.4, 89.4, "Bhutan") is None and g.requests == 0


@pytest.mark.asyncio
async def test_throttle_spaces_calls():
    t = gc._Throttle(50.0)
    loop = asyncio.get_running_loop()
    start = loop.time()
    for _ in range(4):
        await t.wait()
    assert loop.time() - start >= 3 * 0.02 - 0.005


# ── worker: background kinds never take the last 2 free slots ─────────────────────────────────

def test_background_kinds_are_dropped_from_caps_when_only_reserved_slots_are_free():
    from shared.jobs import worker
    from shared.jobs.registry import kinds
    worker.load_kinds()
    w = worker.Worker(MagicMock(), max_parallel=8)
    background = {n for n, k in kinds().items() if k.background}
    assert {"place_extract", "place_geocode", "tour_graph_build"} <= background
    assert background <= set(w.claim_caps(3))
    assert not background & set(w.claim_caps(2)) and not background & set(w.claim_caps(1))
    assert "a3_atomize" in w.claim_caps(1)
    # a tiny worker still runs background work
    assert "place_extract" in worker.Worker(MagicMock(), max_parallel=1).claim_caps(1)


# ── enqueue (fake db) ──────────────────────────────────────────────────────────────────────────

class _Db:
    def __init__(self, reuse=None, queued_extract=None):
        self.reuse, self.queued_extract, self.inserted = reuse, queued_extract, []

    async def fetchval(self, sql, *args):
        if "UPDATE shared.job" in sql:
            return self.reuse
        if "payload->>'tour_id'" in sql:
            return self.queued_extract
        return None

    async def fetchrow(self, sql, *args):
        self.inserted.append(args)
        return {"id": f"job-{len(self.inserted)}"}


@pytest.mark.asyncio
async def test_debounced_enqueue_reuses_a_queued_job():
    from services.places import enqueue as eq
    import services.jobs  # noqa: F401 — registers kinds
    assert await eq.enqueue_debounced(_Db(reuse="j0"), eq.GRAPH_KIND, reason="r") == ("j0", False)
    db = _Db()
    job, created = await eq.enqueue_debounced(db, eq.GRAPH_KIND, reason="r")
    assert created and db.inserted[0][0] == eq.GRAPH_KIND and db.inserted[0][6] is not None   # run_after set


@pytest.mark.asyncio
async def test_after_atomize_keys_on_version_and_atomize_job():
    from services.places import enqueue as eq
    import services.jobs  # noqa: F401
    db = _Db()
    out = await eq.after_atomize(db, tour_id="t", version_id="v", atomize_job_id="a1")
    assert out["place_extract_job_reused"] is False
    assert db.inserted[0][2] == "place_extract:v:a1"
    db2 = _Db(queued_extract="q1")
    assert (await eq.after_liveness_change(db2, tour_id="t", new_status="active",
                                           created_by="x"))["place_extract_job"] == "q1"


# ── cost guard: kind-scoped day spend ──────────────────────────────────────────────────────────

class _Conn:
    def __init__(self, rows, provider_day, kind_day):
        self.rows, self.provider_day, self.kind_day = rows, provider_day, kind_day

    async def fetch(self, sql, *args):
        return self.rows

    async def fetchval(self, sql, *args):
        return self.kind_day if "JOIN shared.job" in sql else self.provider_day


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False
        return _Ctx()


@pytest.mark.asyncio
async def test_kind_scoped_daily_budget_ignores_the_providers_busy_day():
    from shared.cost_guard import BudgetExceeded, load_run_budget
    rows = [{"scope": "job:place_extract", "per_run_usd": 5, "per_day_usd": 40, "hard_stop": True, "alert_pct": 80},
            {"scope": "global", "per_run_usd": None, "per_day_usd": 10, "hard_stop": True, "alert_pct": 80}]
    rows[1]["per_day_usd"] = 600
    b = await load_run_budget(_Pool(_Conn(rows, provider_day=500.0, kind_day=1.0)), "bedrock", "place_extract",
                              day_scope="kind")
    assert b.per_day_usd == 40 and b.day_spent_at_start == 1.0
    b.check(0.04)
    b2 = await load_run_budget(_Pool(_Conn(rows, provider_day=500.0, kind_day=39.99)), "bedrock",
                               "place_extract", day_scope="kind")
    with pytest.raises(BudgetExceeded) as e:
        b2.check(0.04)
    assert e.value.limit == "per_day"


@pytest.mark.asyncio
async def test_kind_scoped_budget_still_obeys_the_provider_wide_daily_hard_stop():
    # review finding 3: the global Bedrock daily cap is checked against the provider's spend
    from shared.cost_guard import BudgetExceeded, load_run_budget
    rows = [{"scope": "job:place_extract", "per_run_usd": 5, "per_day_usd": 40, "hard_stop": True, "alert_pct": 80},
            {"scope": "global", "per_run_usd": None, "per_day_usd": 100, "hard_stop": True, "alert_pct": 80}]
    b = await load_run_budget(_Pool(_Conn(rows, provider_day=99.99, kind_day=1.0)), "bedrock", "place_extract",
                              day_scope="kind")
    assert b.global_day_usd == 100 and b.remaining_usd() == pytest.approx(0.01)
    with pytest.raises(BudgetExceeded) as e:
        b.check(0.04)
    assert e.value.limit == "per_day_global"
    rows[1]["hard_stop"] = False   # an alert-only global cap never stops the kind
    b2 = await load_run_budget(_Pool(_Conn(rows, provider_day=99.99, kind_day=1.0)), "bedrock", "place_extract",
                               day_scope="kind")
    b2.check(0.04)


def test_geocoder_is_a_budget_provider():
    from shared.cost_guard import PROVIDERS
    assert "geocoder" in PROVIDERS


# ── job handlers (store faked) ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_place_extract_skips_a_tour_that_is_not_live(monkeypatch):
    from services.jobs import place_extract_job as job
    from services.places import store
    monkeypatch.setattr(store, "live_tour", AsyncMock(return_value=None))
    ctx = SimpleNamespace(payload={"tour_id": "t"}, pool=_Pool(object()), job_id="j", progress=AsyncMock())
    assert await job.run(ctx) == {"tour_id": "t", "status": "skipped_not_live"}


@pytest.mark.asyncio
async def test_place_extract_needs_a_tour_id():
    from services.jobs import place_extract_job as job
    from shared.jobs.registry import NonRetryable
    with pytest.raises(NonRetryable):
        await job.run(SimpleNamespace(payload={}, pool=None, job_id="j"))


@pytest.mark.asyncio
async def test_follow_ups_queue_geocode_only_when_names_wait(monkeypatch):
    from services.jobs import place_extract_job as job
    calls = []

    async def fake(pool, kind, **kw):
        calls.append(kind)
        return f"{kind}-1", True
    monkeypatch.setattr(job, "enqueue_debounced", fake)
    await job.follow_ups(None, {"status": "ok", "tour_id": "t", "pending_names_due": 0}, "x")
    assert calls == ["tour_graph_build"]
    await job.follow_ups(None, {"status": "partial", "tour_id": "t", "pending_names_due": 3}, "x")
    assert calls[1:] == ["place_geocode", "tour_graph_build"]
    calls.clear()
    await job.follow_ups(None, {"status": "skipped_unchanged", "tour_id": "t"}, "x")
    assert calls == []


@pytest.mark.asyncio
async def test_budgeted_geocoder_checks_before_each_request():
    from services.jobs.place_geocode_job import BudgetedGeocoder
    from shared.cost_guard import BudgetExceeded, RunBudget
    budget = RunBudget(provider="geocoder", scope="job:place_geocode", per_run_usd=0.01, per_day_usd=None)
    g = BudgetedGeocoder(_FakeGeocoder(), budget)
    await g.confirm("a", 1, 1, "Laos")
    await g.confirm("b", 1, 1, "Laos")
    with pytest.raises(BudgetExceeded):
        await g.confirm("c", 1, 1, "Laos")
    assert g.requests == 2


# ── admin backfill endpoint ────────────────────────────────────────────────────────────────────

def _backfill_env(monkeypatch, busy, tours, geo_cost=0.0):
    from api.routers import admin as admin_module
    from services.places import backfill as bf
    monkeypatch.setattr(admin_module, "ADMIN_SECRET", "s")
    monkeypatch.setattr(bf, "wave_busy", AsyncMock(return_value=busy))
    monkeypatch.setattr(bf, "plan", AsyncMock(return_value=tours))
    monkeypatch.setattr(bf, "estimate", AsyncMock(return_value={"expected_cost_usd": 0.1,
                                                                 "geocoder_cost_usd": geo_cost}))
    request = MagicMock()
    request.app.state.pool = _Pool(object())
    return request


@pytest.mark.asyncio
async def test_backfill_dry_run_enqueues_nothing(monkeypatch):
    from api.routers.admin_places import BackfillRequest, backfill
    from services.places import enqueue as eq
    enq = AsyncMock()
    monkeypatch.setattr(eq, "enqueue_place_extract", enq)
    req = _backfill_env(monkeypatch, {"s1_rewrite": 3}, [{"tour_id": "t", "source_id": "s", "build": None}])
    out = await backfill(BackfillRequest(country="Laos"), req, x_admin_secret="s")
    assert out["dry_run"] and out["would_refuse"] and len(out["tours"]) == 1
    enq.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_refuses_during_a_wave_and_above_the_geocoder_cap(monkeypatch):
    from fastapi import HTTPException
    from api.routers.admin_places import BackfillRequest, backfill
    req = _backfill_env(monkeypatch, {"a3_atomize": 1}, [])
    with pytest.raises(HTTPException) as e:
        await backfill(BackfillRequest(dry_run=False), req, x_admin_secret="s")
    assert e.value.status_code == 409
    req = _backfill_env(monkeypatch, {}, [{"tour_id": "t", "source_id": "s"}], geo_cost=60.0)
    with pytest.raises(HTTPException) as e:
        await backfill(BackfillRequest(dry_run=False), req, x_admin_secret="s")
    assert e.value.status_code == 409


@pytest.mark.asyncio
async def test_backfill_real_run_enqueues_one_job_per_tour(monkeypatch):
    from api.routers.admin_places import BackfillRequest, backfill
    from services.places import enqueue as eq
    enq = AsyncMock(side_effect=[("j1", True), ("j2", True)])
    monkeypatch.setattr(eq, "enqueue_place_extract", enq)
    req = _backfill_env(monkeypatch, {}, [{"tour_id": "t1", "source_id": "s1"}, {"tour_id": "t2", "source_id": "s2"}])
    out = await backfill(BackfillRequest(dry_run=False), req, x_admin_secret="s", x_admin_user_id="u")
    assert out["enqueued"] == 2 and [j["job_id"] for j in out["jobs"]] == ["j1", "j2"]
    kw = enq.await_args_list[0].kwargs
    assert kw["created_by"] == "backfill:admin:u" and kw["idempotency_key"].startswith("place_extract:backfill:")


def test_backfill_limit_is_capped_at_50():
    from pydantic import ValidationError
    from api.routers.admin_places import BackfillRequest
    with pytest.raises(ValidationError):
        BackfillRequest(limit=51)
