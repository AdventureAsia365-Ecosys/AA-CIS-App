"""AA-712 — Geoapify adapter: acceptance rule on the recorded S236 bake-off responses, query normalisation,
retry / backoff, the daily credit guard and key hygiene. No live call: HTTP is mocked, fixtures are recorded."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from structlog.testing import capture_logs

from services.places import geocoder as gc
from services.places import geomatch as gm
from services.places import guards, locate

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "aa712_geoapify_bakeoff.json"
ROWS = json.loads(FIXTURE.read_text())["rows"]
KEY = "test-geoapify-key-0123456789"


def _cands(row):
    return gm.candidates({"results": row["results"]})


# ── the recorded bake-off ──────────────────────────────────────────────────────────────────────

def test_fixture_is_the_bakeoff_sample_and_holds_no_key():
    raw = FIXTURE.read_text()
    assert "apikey" not in raw.lower() and len(ROWS) == 71
    real = [r for r in ROWS if not r["generic"]]
    assert len(real) == 64 and sum(1 for r in real if r["results"]) == 55
    assert sum(1 for r in real if r["results"] and r["labels"]["0"] in "NR") == 38


def _evaluate(top_n):
    """(correct accepted among the 38 rows whose top result is right, wrong accepted, rescued, accepted)."""
    correct = wrong = rescued = accepted = 0
    for r in ROWS:
        if r["generic"] or not r["results"]:
            continue
        v = gm.accept(r["name"], _cands(r), *r["ref"], guards.iso2(r["country"]), top_n=top_n)
        if v.accepted is None:
            continue
        accepted += 1
        label = r["labels"].get(str(v.rank))
        assert label, f"row {r['i']}: accepted rank {v.rank} has no hand label"
        if label in "NR":
            if r["labels"]["0"] in "NR":
                correct += 1
            else:
                rescued += 1
        else:
            wrong += 1
    return correct, wrong, rescued, accepted


def test_acceptance_rule_reproduces_the_bakeoff_numbers():
    # report: "name sim ≥ 0.5 AND ≤ 50 km" kept 34 of 38 correct results and let 3 wrong ones through
    correct, wrong, rescued, _ = _evaluate(top_n=3)
    assert 32 <= correct <= 36 and wrong <= 3
    assert rescued >= 1          # the top-3 scan finds Bangkok Chinatown / Phi Phi below a wrong #1
    # top-1 only is the floor: the scan never loses a correct row
    c1, w1, _, _ = _evaluate(top_n=1)
    assert correct >= c1 and wrong <= max(w1, 3)


def test_the_three_correct_rows_it_rejects_have_a_bad_legacy_reference():
    missed = []
    for r in ROWS:
        if r["generic"] or not r["results"] or r["labels"]["0"] not in "NR":
            continue
        v = gm.accept(r["name"], _cands(r), *r["ref"], guards.iso2(r["country"]))
        if v.accepted is None:
            missed.append((r["name"], r["ref_by"]))
    # Khone Phapheng / Laos Buffalo Dairy / Ritigala: the stored point is 100–670 km off (ref_by null);
    # in production the reference is the model's fresh point
    assert all(by == "null" for _, by in missed) and len(missed) <= 4


def test_rank_confidence_is_recorded_not_used_as_a_gate():
    by_name = {r["name"]: r for r in ROWS}
    for name in ("Jokhang Temple and Barkhor Street", "Khuvsgul Lake", "Li Phi Waterfall",
                 "Yol Valley (Vulture Valley)"):
        r = by_name[name]
        v = gm.accept(name, _cands(r), *r["ref"], guards.iso2(r["country"]))
        assert v.accepted is not None and (v.accepted.confidence or 0) < 0.8, name
    # a confident homonym (Bhangarh, Madhya Pradesh, 0.9; Chiang Mai Chinatown, 1.0) is refused by distance
    for name in ("Bhangarh village", "Chinatown"):
        r = by_name[name]
        v = gm.accept(name, _cands(r), *r["ref"], guards.iso2(r["country"]))
        assert v.reasons[0] == "0:distance" and (_cands(r)[0].confidence or 0) >= 0.9


def test_city_results_only_for_settlement_queries_and_admin_types_never():
    by_name = {r["name"]: r for r in ROWS}
    r = by_name["cave complexes near Vang Vieng"]                    # Vang Vieng town for a cave: fallback
    assert gm.accept(r["name"], _cands(r), *r["ref"], "la").accepted is None
    r = by_name["Laomei River (Dream Valley), New Taipei City"]       # county centroid
    assert gm.accept(r["name"], _cands(r), *r["ref"], "tw").reasons[0] == "0:type"
    r = by_name["Guangning"]                                          # the place is a town: city accepted
    assert gm.accept(r["name"], _cands(r), *r["ref"], "cn").accepted.result_type == "city"


def test_long_features_get_50_km():
    by_name = {r["name"]: r for r in ROWS}
    for name in ("Manas River", "Cheow Lan Lake"):                    # 39 km / 33 km, both right
        r = by_name[name]
        assert gm.radius_km(name) == 50 and gm.accept(name, _cands(r), *r["ref"]).accepted is not None
    assert gm.radius_km("Tashilhunpo Monastery") == 25


def test_normalising_the_query_fixes_the_hahoe_homonym():
    r = next(x for x in ROWS if x["name"] == "Hahoe Folk Village, Andong")
    assert gm.accept(r["name"], _cands(r), *r["ref"], "kr").accepted.name == "Andong Folk Village"   # wrong
    q = gm.normalise(r["name"])
    assert q == ["Hahoe Folk Village"]
    assert gm.accept(q[0], _cands(r), *r["ref"], "kr").accepted.name == "Hahoe Village"


# ── normalisation ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("Gorakshep (5,164m)", ["Gorakshep"]),
    ("Everest Base Camp (5,364 m)", ["Everest Base Camp"]),
    ("Thorong La 5416m", ["Thorong La"]),
    ("Cho La Pass to Dzongla", ["Cho La Pass", "Dzongla"]),
    ("Koto to Meta", ["Koto", "Meta"]),
    ("Hanok Village, Jeonju", ["Hanok Village"]),
    ("Laomei River (Dream Valley), New Taipei City", ["Laomei River"]),
    ("Yol Valley (Vulture Valley)", ["Yol Valley"]),
    ("Taktshang Monastery", ["Taktshang Monastery"]),
    ("ger camp", []), ("local market", []), ("village center", []), ("fishing village", []),
    ("living reefs", []), ("", []),
])
def test_normalise(raw, expected):
    assert gm.normalise(raw) == expected


def test_mount_hill_island_rock_viewpoint_are_poi_queries():
    for q in ("Mount Fuji", "Mt Kinabalu", "Chocolate Hills", "Phi Phi Islands", "Ayers Rock", "Doi Suthep Viewpoint"):
        assert not gm.is_settlement(q), q
    fuji_city = gm.Candidate(35.16, 138.68, "jp", "Fuji", "city", 1.0, "full_match", "p", "osm")
    assert gm.accept("Mount Fuji", [fuji_city], 35.36, 138.73, "jp").reasons == ["0:type"]


def test_x_to_y_drops_an_endpoint_with_no_proper_noun():
    assert gm.normalise("Road to Mandalay") == ["Mandalay"]
    assert gm.normalise("Hot Springs to Beppu") == ["Beppu"]
    assert gm.normalise("Cho La Pass to Dzongla") == ["Cho La Pass", "Dzongla"]
    assert gm.normalise("Bridge to Bridge") == []


def test_hard_kinds_and_native_script():
    assert gm.is_hard("Zatrwa La Pass") and gm.is_hard("Tashilhunpo Monastery") and gm.is_hard("Hahoe Village")
    assert not gm.is_hard("Bikaner") and not gm.is_hard("Ho Chi Minh City")
    assert gm.has_native_script("太平山") and gm.has_native_script("하회마을") and not gm.has_native_script("Hahoe")


# ── adapter with mocked HTTP ───────────────────────────────────────────────────────────────────

def _resp(status=200, payload=None, headers=None):
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {}
    r.json.return_value = payload if payload is not None else {"results": []}
    return r


def _geo(responses, **kw):
    http = MagicMock()
    http.get = AsyncMock(side_effect=responses)
    sleeps = []

    async def sleep(s):
        sleeps.append(s)
    g = gc.GeoapifyGeocoder(http=http, api_key=KEY, sleep=sleep, **kw)
    g._throttle.interval = 0.0      # tests only: the throttle itself is covered by test_rate_*
    return g, http, sleeps


def _row(name):
    return next(x for x in ROWS if x["name"] == name)


@pytest.mark.asyncio
async def test_confirm_sends_the_rule_request_and_stores_audit_fields():
    r = _row("Tashilhunpo Monastery")
    g, http, _ = _geo([_resp(payload={"results": r["results"]})])
    got = await g.confirm(r["name"], *r["ref"], "China")
    assert isinstance(got, gc.Confirmed) and got.located_by == "geoapify" and got.country == "China"
    assert got.meta["result_type"] == "amenity" and "confidence" in got.meta and got.meta["rank"] == 0
    assert "OpenStreetMap" in got.meta["attribution"] and "Geoapify" in got.meta["attribution"]
    url, params = http.get.call_args[0][0], http.get.call_args[1]["params"]
    assert url == gc.GEOAPIFY_URL
    assert params == {"text": "Tashilhunpo Monastery", "format": "json", "limit": "3", "lang": "en",
                      "filter": "countrycode:cn", "apiKey": KEY}
    assert g.requests == 1 and KEY not in json.dumps(got.meta)


@pytest.mark.asyncio
async def test_generic_phrases_send_nothing():
    g, http, _ = _geo([])
    got = await g.confirm("ger camp", 47.2, 102.8, "Mongolia")
    assert isinstance(got, gc.Unconfirmed) and got.meta["outcome"] == "skipped_generic"
    assert g.requests == 0 and not http.get.called


@pytest.mark.asyncio
async def test_x_to_y_tries_each_endpoint():
    meta_hit = {"results": [{"name": "Meta", "result_type": "city", "lat": 28.62, "lon": 84.25, "country_code": "np",
                             "rank": {"confidence": 0.4}}]}
    g, http, _ = _geo([_resp(payload={"results": []}), _resp(payload=meta_hit)])
    got = await g.confirm("Koto to Meta", 28.63, 84.22, "Nepal")
    assert isinstance(got, gc.Confirmed) and got.meta["query"] == "Meta" and g.requests == 2
    assert [c[1]["params"]["text"] for c in http.get.call_args_list] == ["Koto", "Meta"]


@pytest.mark.asyncio
async def test_taiwan_retries_with_the_native_name():
    native = {"results": [{"name": "太平山國家森林遊樂區", "result_type": "amenity", "lat": 24.50, "lon": 121.53,
                           "country_code": "tw", "rank": {"confidence": 0}}]}
    g, http, _ = _geo([_resp(payload={"results": []}), _resp(payload=native)])
    got = await g.confirm("Taipingshan Villa", 24.517, 121.633, "Taiwan", native_name="太平山")
    assert isinstance(got, gc.Confirmed) and got.meta["query"] == "太平山" and g.requests == 2
    # not for other countries, and not for a Latin "native" name
    g2, _, _ = _geo([_resp(payload={"results": []})])
    assert isinstance(await g2.confirm("Paro", 27.43, 89.41, "Bhutan", native_name="སྤ་རོ"), gc.Unconfirmed)
    assert g2.requests == 1


@pytest.mark.parametrize("cand_name,rtype,km_off,ok", [
    ("太平", "city", 0.10, False),          # bake-off row 58 homonym: a city for a mountain lodge
    ("太平", "amenity", 0.10, True),        # same name, a POI result: half the query, accepted
    ("太", "amenity", 0.05, False),         # 1 character never matches
    ("太平山莊", "amenity", 0.20, True),    # ~22 km
    ("太平山莊", "amenity", 0.40, False),   # ~45 km: beyond 25 km (not a long feature)
])
def test_native_acceptance_follows_the_bakeoff_rule(cand_name, rtype, km_off, ok):
    lat, lng = 24.497, 121.534
    c = gm.Candidate(lat + km_off, lng, "tw", cand_name, rtype, 0.0, None, "p", "osm")
    v = gc._accept_native("太平山莊", [c], lat, lng, "tw", ref_name="Taipingshan Villa")
    assert (v.accepted is not None) is ok, v.reasons


def test_native_city_only_for_a_settlement_and_long_features_get_50_km():
    lat, lng = 37.0, 127.0
    city = gm.Candidate(lat + 0.05, lng, "kr", "하회마을", "city", 0.0, None, "p", "osm")
    assert gc._accept_native("하회마을", [city], lat, lng, "kr", ref_name="Hahoe Village").accepted is not None
    temple = gm.Candidate(lat + 0.05, lng, "kr", "불국사", "city", 0.0, None, "p", "osm")
    assert gc._accept_native("불국사", [temple], lat, lng, "kr", ref_name="Bulguksa").accepted is None
    lake = gm.Candidate(lat + 0.35, lng, "tw", "日月潭", "water", 0.0, None, "p", "osm")   # ~39 km
    assert gc._accept_native("日月潭", [lake], lat, lng, "tw", ref_name="Sun Moon Lake").accepted is not None
    assert gc._accept_native("日月潭", [lake], lat, lng, "tw", ref_name="Ita Thao").accepted is None


@pytest.mark.asyncio
async def test_unconfirmed_keeps_what_was_tried():
    r = _row("Rangaji Temple")
    g, _, _ = _geo([_resp(payload={"results": r["results"]})])
    got = await g.confirm(r["name"], *r["ref"], "India")
    assert isinstance(got, gc.Unconfirmed) and got.meta["outcome"] == "no_match"
    assert got.meta["tried"][0]["rejected"] == ["0:name", "1:name", "2:name"]


# ── rate limit / backoff ───────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retries_429_and_5xx_with_backoff_and_retry_after():
    r = _row("Erawan Waterfall")
    g, http, sleeps = _geo([_resp(429, headers={"Retry-After": "5"}), _resp(503),
                            _resp(payload={"results": r["results"]})])
    with capture_logs() as logs:
        got = await g.confirm(r["name"], *r["ref"], "Thailand")
    assert isinstance(got, gc.Confirmed) and g.requests == 3     # every attempt is a credit
    assert sleeps == [5.0, 2.0]                                 # Retry-After honoured, then 1s·2^1
    assert [x["event"] for x in logs].count("geoapify_retry") == 2


@pytest.mark.asyncio
async def test_gives_up_after_max_retries_and_never_retries_4xx():
    g, http, sleeps = _geo([_resp(500)] * 4, max_retries=3)
    with pytest.raises(gc.GeocoderError, match="HTTP 500 after 4 attempts"):
        await g.confirm("Paro", 27.43, 89.41, "Bhutan")
    assert g.requests == 4 and sleeps == [1.0, 2.0, 4.0]
    g, http, sleeps = _geo([_resp(400)])
    with pytest.raises(gc.GeocoderError, match="HTTP 400"):
        await g.confirm("Paro", 27.43, 89.41, "Bhutan")
    assert g.requests == 1 and sleeps == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failure_is_unavailable_not_a_per_name_error(status):
    g, http, sleeps = _geo([_resp(status)])
    with pytest.raises(gc.GeocoderUnavailable, match=f"HTTP {status}"):
        await g.confirm("Paro", 27.43, 89.41, "Bhutan")
    assert g.requests == 1 and sleeps == []


@pytest.mark.asyncio
async def test_locate_defers_every_name_on_auth_failure_without_a_try():
    calls = []

    async def generate(system, user):
        calls.append(user)
        return '{"Paro": [27.43, 89.41, "Bhutan"], "Punakha": [27.58, 89.86, "Bhutan"]}'

    g, http, _ = _geo([_resp(401)])
    items = [locate.Item((n,), n, "Bhutan") for n in ("Paro", "Punakha")]
    items += [locate.Item((f"n{i}",), f"Name {i}", "Bhutan") for i in range(locate.BATCH)]  # a 2nd batch
    out = await locate.locate_many(items, generate, g)
    deferred = [k for k, o in out.items() if o.deferred]
    assert all(out[k].error is None and out[k].point is None and out[k].reason == locate.DEFER_UNAVAILABLE
               for k in deferred)
    # Paro hit the 401; Punakha and the whole 2nd batch are deferred. Names the model itself answered null
    # for in the 1st batch keep that answer (it does not depend on the geocoder).
    assert ("Paro",) in deferred and ("Punakha",) in deferred and ("n39",) in deferred
    assert all(o.reason == "unknown_to_model" for k, o in out.items() if k not in deferred)
    assert len(calls) == 1 and http.get.call_count == 1      # no 2nd paid model call, one request


@pytest.mark.asyncio
async def test_missing_key_defers_before_any_model_call(monkeypatch):
    import shared.secrets as sec

    def boom():
        raise RuntimeError("ResourceNotFoundException: aa-cis/dev/geoapify")
    monkeypatch.setattr(sec, "get_geoapify_key", boom)
    g = gc.GeoapifyGeocoder(http=MagicMock())
    generate = AsyncMock(return_value="{}")
    with capture_logs() as logs:
        out = await locate.locate_many([locate.Item(("paro",), "Paro", "Bhutan")], generate, g)
    assert out[("paro",)].deferred and out[("paro",)].reason == locate.DEFER_UNAVAILABLE
    assert not generate.called and g.requests == 0
    assert "aa-cis/dev/geoapify" not in json.dumps(logs, default=str)   # type only, no secret name / detail
    monkeypatch.setattr(sec, "get_geoapify_key", lambda: "  ")
    with pytest.raises(gc.GeocoderUnavailable, match="empty"):
        gc.GeoapifyGeocoder(http=MagicMock()).ensure_ready()


def test_rate_is_capped_at_5_per_second(monkeypatch):
    assert gc._Throttle(5).interval == pytest.approx(0.2)
    monkeypatch.setenv("GEOAPIFY_RATE_PER_S", "50")
    g = gc.get_geocoder("geoapify", api_key=KEY)
    assert g._throttle.interval == pytest.approx(0.2)


@pytest.mark.parametrize("raw", ["0", "-1", "nan", "abc"])
def test_rate_zero_or_negative_never_turns_the_throttle_off(monkeypatch, raw):
    monkeypatch.setenv("GEOAPIFY_RATE_PER_S", raw)
    assert gc.get_geocoder("geoapify", api_key=KEY)._throttle.interval == pytest.approx(0.2)
    assert gc._Throttle(0).interval == pytest.approx(0.2) and gc._Throttle(-3).interval == pytest.approx(0.2)
    monkeypatch.setenv("GEOAPIFY_RATE_PER_S", "2")
    assert gc.get_geocoder("geoapify", api_key=KEY)._throttle.interval == pytest.approx(0.5)


# ── daily credit guard ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_credit_guard_stops_before_the_limit():
    r = _row("Erawan Waterfall")
    g, http, _ = _geo([_resp(payload={"results": r["results"]})] * 3, daily_credit_limit=2500,
                      credits_used_today=2499)
    assert g.credits_left() == 1
    assert isinstance(await g.confirm(r["name"], *r["ref"], "Thailand"), gc.Confirmed)
    with pytest.raises(gc.CreditLimitReached):
        await g.confirm(r["name"], *r["ref"], "Thailand")
    assert g.requests == 1 and http.get.call_count == 1


def test_credit_limit_is_configurable(monkeypatch):
    monkeypatch.setenv("GEOAPIFY_DAILY_CREDIT_LIMIT", "100")
    assert gc.get_geocoder("geoapify", api_key=KEY).daily_credit_limit == 100
    monkeypatch.delenv("GEOAPIFY_DAILY_CREDIT_LIMIT")
    assert gc.get_geocoder("geoapify", api_key=KEY).daily_credit_limit == 2500


@pytest.mark.asyncio
async def test_locate_defers_the_rest_when_credits_run_out():
    async def generate(system, user):
        return ('{"Paro": [27.43, 89.41, "Bhutan"], "Punakha": [27.58, 89.86, "Bhutan"], '
                '"Haa": [27.37, 89.28, "Bhutan"]}')

    paro = {"results": [{"name": "Paro", "result_type": "city", "lat": 27.43, "lon": 89.41, "country_code": "bt"}]}
    g, _, _ = _geo([_resp(payload=paro)], daily_credit_limit=1)
    items = [locate.Item((n,), n, "Bhutan") for n in ("Paro", "Punakha", "Haa")]
    out = await locate.locate_many(items, generate, g)
    assert out[("Paro",)].point.located_by == "geoapify" and out[("Paro",)].point.precision == "point"
    assert all(out[(n,)].deferred and out[(n,)].point is None and out[(n,)].error is None for n in ("Punakha", "Haa"))


# ── keys never in logs or errors ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_key_in_errors_or_logs(caplog):
    import httpx
    url = f"{gc.GEOAPIFY_URL}?text=Paro&apiKey={KEY}"
    g, _, _ = _geo([httpx.ConnectError(f"cannot connect to {url}")] * 2, max_retries=1)
    with capture_logs() as logs, caplog.at_level(logging.DEBUG):
        with pytest.raises(gc.GeocoderError) as e:
            await g.confirm("Paro", 27.43, 89.41, "Bhutan")
        out = await locate.locate_many(
            [locate.Item(("paro",), "Paro", "Bhutan")],
            AsyncMock(return_value='{"Paro": [27.43, 89.41, "Bhutan"]}'),
            _geo([httpx.ConnectError(f"boom {url}")] * 4)[0])
    assert KEY not in str(e.value) and KEY not in json.dumps(logs, default=str) and KEY not in caplog.text
    assert KEY not in (out[("paro",)].error or "") and out[("paro",)].error.startswith("geoapify:")


def test_httpx_request_log_line_is_redacted(caplog):
    gc._install_log_redaction()
    with caplog.at_level(logging.INFO, logger="httpx"):
        logging.getLogger("httpx").info('HTTP Request: %s %s "%s %d %s"', "GET",
                                        f"{gc.GEOAPIFY_URL}?text=Paro&apiKey={KEY}", "HTTP/1.1", 200, "OK")
    assert "apiKey=***" in caplog.text and KEY not in caplog.text


def test_redact_helper():
    assert gc.redact(f"x?apiKey={KEY}&y=1") == "x?apiKey=***&y=1"
    assert gc.redact(f"key is {KEY}", KEY) == "key is ***"


# ── provider selection + secret ────────────────────────────────────────────────────────────────

def test_default_provider_is_geoapify_and_removed_ones_fail_loudly(monkeypatch):
    monkeypatch.delenv("GEOCODER_PROVIDER", raising=False)
    assert gc.provider_name() == "geoapify"
    assert gc.get_geocoder(api_key=KEY).name == "geoapify"
    monkeypatch.setenv("GEOCODER_PROVIDER", "model_only")
    assert not gc.get_geocoder().calls_external
    for removed in ("mapbox_permanent", "amazon_location"):
        monkeypatch.setenv("GEOCODER_PROVIDER", removed)
        with pytest.raises(gc.GeocoderNotSupported, match="not supported"):
            gc.get_geocoder()
    monkeypatch.setenv("GEOCODER_PROVIDER", "geoapfy")                    # typo → no external call
    assert gc.get_geocoder().name == "model_only"


def test_geoapify_key_from_env_or_secret(monkeypatch):
    from shared import secrets
    monkeypatch.setattr(secrets, "_cache", {})
    monkeypatch.setenv("GEOAPIFY_API_KEY", " envkey ")
    assert secrets.get_geoapify_key() == "envkey"
    monkeypatch.delenv("GEOAPIFY_API_KEY")
    seen = []
    monkeypatch.setattr(secrets, "_fetch_secret_sdk", lambda sid: seen.append(sid) or "plainkey\n")
    assert secrets.get_geoapify_key() == "plainkey" and seen == ["aa-cis/dev/geoapify"]
    monkeypatch.setattr(secrets, "_cache", {})
    monkeypatch.setattr(secrets, "_fetch_secret_sdk", lambda sid: '{"api_key": "jsonkey"}')
    assert secrets.get_geoapify_key() == "jsonkey"


# ── what locate stores ─────────────────────────────────────────────────────────────────────────

def test_unconfirmed_hard_place_is_an_area_on_the_review_list():
    item = locate.Item(("x",), "Zatrwa La Pass", "Nepal")
    p = locate._located(item, 27.66, 86.78, "Nepal", gc.Unconfirmed({"provider": "geoapify", "outcome": "no_match"}))
    assert (p.located_by, p.precision, p.review_reason, p.provider) == ("llm", "area", "unconfirmed_hard_type",
                                                                         "geoapify")
    town = locate._located(locate.Item(("y",), "Bikaner", "India"), 28.0, 73.3, "India", None)
    assert town.precision == "area" and town.review_reason is None
    ok = locate._located(item, 27.66, 86.78, "Nepal",
                         gc.Confirmed(27.67, 86.77, "Nepal", "pid", "geoapify", {"provider": "geoapify"}))
    assert ok.precision == "point" and ok.review_reason is None and ok.meta["provider"] == "geoapify"


def test_parse_answer_keeps_native_names_only_for_taiwan_and_korea():
    raw = ('{"Taipingshan": [24.5, 121.5, "Taiwan", "太平山"], "Hahoe": [36.54, 128.52, "South Korea", "하회마을"], '
           '"Paro": [27.43, 89.41, "Bhutan", "སྤ་རོ"], "Jiufen": [25.1, 121.84, "Taiwan", "Jiufen"]}')
    out = locate.parse_answer(raw, ["Taipingshan", "Hahoe", "Paro", "Jiufen"])
    assert out["Taipingshan"].native_name == "太平山" and out["Hahoe"].native_name == "하회마을"
    assert out["Paro"].native_name is None and out["Jiufen"].native_name is None


def test_sentry_breadcrumbs_drop_the_key():
    from api.core.sentry import before_breadcrumb
    crumb = {"category": "httplib", "message": f"GET {gc.GEOAPIFY_URL}?text=Paro&apiKey={KEY}",
             "data": {"url": gc.GEOAPIFY_URL, "http.query": f"text=Paro&apiKey={KEY}&lang=en",
                      "nested": [f"apikey={KEY}"]}}
    out = before_breadcrumb(crumb, {})
    assert KEY not in json.dumps(out) and "apiKey=***&lang=en" in out["data"]["http.query"]


# ── pipeline: deferral accounting ──────────────────────────────────────────────────────────────

class _Conn:
    async def fetch(self, *a, **k):
        return []

    def transaction(self):
        import contextlib

        @contextlib.asynccontextmanager
        async def tx():
            yield
        return tx()


class _Pool:
    def acquire(self):
        import contextlib

        @contextlib.asynccontextmanager
        async def acq():
            yield _Conn()
        return acq()


def _due(n):
    return [{"name_norm": f"n{i}", "country_norm": "bhutan", "locality_norm": "", "display_name": f"Name {i}",
             "country": "Bhutan", "tour_name": "", "locality": None, "place_card_id": None} for i in range(n)]


def _patch_store(monkeypatch, due):
    from services.places import pipeline, store
    marked = []

    async def mark_name(conn, key, **kw):
        marked.append((key, kw))
        return "pending"
    monkeypatch.setattr(store, "due_names", AsyncMock(return_value=due))
    monkeypatch.setattr(store, "known_destinations", AsyncMock(return_value={}))
    monkeypatch.setattr(store, "mark_name", mark_name)
    monkeypatch.setattr(store, "upsert_destination", AsyncMock(return_value=("d1", "inserted")))
    monkeypatch.setattr(store, "relink", AsyncMock(return_value={}))
    monkeypatch.setattr(pipeline, "apply_outlier_rule", AsyncMock(return_value=(0, [])))
    return marked


@pytest.mark.asyncio
async def test_geocode_pending_stops_on_auth_failure_without_marking_names(monkeypatch):
    from services.places import pipeline
    marked = _patch_store(monkeypatch, _due(50))

    async def answer_all(system, user):
        names = [ln[2:].split(" | ")[0] for ln in user.splitlines() if ln.startswith("- ")]
        return json.dumps({n: [27.43, 89.41, "Bhutan"] for n in names})
    generate = AsyncMock(side_effect=answer_all)
    g, _, _ = _geo([_resp(403)])
    out = await pipeline.geocode_pending(_Pool(), generate, g, limit=50)
    assert out["stopped_geocoder_unavailable"] and not out.get("stopped_credit_limit")
    assert out["deferred"] == 50 and out["failed"] == 0 and out["unknown"] == 0
    assert generate.call_count == 1 and marked == []


@pytest.mark.asyncio
async def test_geocode_pending_locates_only_what_todays_credits_can_check(monkeypatch):
    from services.places import pipeline
    _patch_store(monkeypatch, _due(10))
    seen = []

    async def generate(system, user):
        seen.append(user.count("\n- "))
        return "{}"          # the model knows none: no geocoder call needed
    g, _, _ = _geo([], daily_credit_limit=3)
    out = await pipeline.geocode_pending(_Pool(), generate, g, limit=10)
    assert seen == [3] and out["deferred"] == 7 and out["stopped_credit_limit"] and out["unknown"] + out["failed"] == 3
