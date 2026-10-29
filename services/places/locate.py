"""AA-712 — where a place is: the model proposes coordinates, the configured geocoder confirms.

AA-997 (S254 geocode audit, fix plan steps 1, 2, 5, 6): COORDINATES COME FROM THE GEOCODER ONLY. The model's point is a
hint (bias, and the distance reference when the tour has no located stop near that day); the audit measured model-placed
rows at 53-60 % correct against ~91 % for Geoapify-placed ones. Per name, in order:
  a. the name, searched in the model's country (China covers Hong Kong / Macao), accepted by geomatch.accept against
     the tour ROUTE (guards.route_km: the located overnight stops of days d-1..d+1 of every tour using the name, 50 km /
     100 km for long features) — or, with no route, against the model's point (the S236 bake-off rule);
  b. else the TOWN the model says the place is in (the answer's town element), searched as a settlement under the same
     reference → the town's point, precision 'area', review_reason `centroid_fallback` (never drawn as the site's pin);
  c. else no point: `unconfirmed_by_geocoder`, final (status 'unknown' at once — asking the same again does not help).
  A model null (it does not know the place) gets a geocoder-only search of the name in the item's country when the tour
  has a route to check it against (§7 "geocoder-only fallback"); without one, or when it finds nothing, the null is
  final too (`unknown_to_model`). `model_only` (tests / emergency switch, no geocoder) still stores the model's point,
  as 'area' with review_reason `llm_point` (or `unconfirmed_hard_type`).
The rest of this docstring is the AA-712 / AA-990 design; its "otherwise the model's point is kept" (step 2) is
replaced by the rules above (model_only excepted).
Port of AA-TripPlanner-Web backend/extraction/locate.py (AA-675 follow-up, @ 6523816).

Mapbox forward geocoding by name alone is unreliable for Asian place names, even limited to the
tour's country (29/09/2026, Dev): "Chiang Mai" -> a village in Roi Et, "Delhi" -> a village in
Madhya Pradesh. The model knows these places. So:

  1. one a3_place_locate call per batch (BATCH names) returns [lat, lng, country] per name, or null
     when it does not know the place (it must not guess);
  2. the geocoder (services/places/geocoder.py, Geoapify since S236) searches the name in that
     country; a candidate passing the acceptance rule (services/places/geomatch.py: name match, type,
     ≤ 25 km / 50 km of the model's point) is taken (located_by 'geoapify', precision 'point'),
     otherwise the model's point is kept ('llm', precision 'area'; a hard place kind — temple, pass,
     lake, village… — is put on the /admin/places review list). AA-847: a linear feature (river, road, trail) is
     precision 'line' either way — drawn from geo.place_shape (job place_shape), never a pin;
  3. hard guards (guards.point_ok): any ISO 3166-1 country (AA-990: the PLACE's own country, which may differ from
     the tour's — a tour sold as India that climbs Kilimanjaro has Tanzanian places), inside that country's bounding
     box and polygon (10 km border buffer); a confirming result in another country than the model's is ignored.

A failed model batch or a geocoder error leaves the names pending (retried later), never a guess.
AA-990: the answer is read leniently (code fences, prose around it, a one-key wrapper object, keys re-spelt within
`name_norm` or answered without the " (Locality)" part of a label); an unreadable answer is asked again once as two
halves before its names count a try; and each name that does not get a point carries its own reason
(`unknown_to_model` = the model said null, `missing_in_answer`, `malformed_answer`, `unknown_country:<text>` = the
model's country names no ISO 3166-1 country; any ISO country is taken, subject to guards.point_ok).
The geocoder's daily credit guard, and a setup / auth failure (no key, secret unreadable, HTTP 401/403),
defer the rest of the run: no try consumed, no further paid model call.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Awaitable, Callable, Optional

import structlog

from services.places import geomatch, guards
from shared.cost_guard import BudgetExceeded
from services.places.names import name_norm
from services.places.geocoder import (CONFIRMED_BY, STRATEGY_V1, Confirmed, CreditLimitReached,
                                      GeocoderUnavailable, Unconfirmed, default_strategy)

logger = structlog.get_logger()

BATCH = 40
MAX_TOKENS = 4000
CONFIDENCE = {"geoapify": 0.9, "mapbox": 0.9, "manual": 1.0, "llm": 0.6}
NATIVE_NAME_COUNTRIES = ("Taiwan", "South Korea")
REVIEW_UNCONFIRMED_HARD = "unconfirmed_hard_type"
# AA-847: the review reason the first validity branch wrote on HIDDEN linear features (geo_status 'suspect'). Owner
# decision S246: lines are drawn, never hidden, so nothing writes it any more; store._write_point un-hides a row that
# still carries it when the name is located again as a line.
REVIEW_LINEAR_FEATURE = "linear_feature"
# AA-997 review reasons (shared.destinations.review_reason, listed on /admin/places)
REVIEW_LLM_POINT = "llm_point"                # only the model placed it (model_only; or found by the relocate job)
REVIEW_CENTROID = "centroid_fallback"         # the town's point stands in for the site
REVIEW_OFF_ROUTE = "off_route"                # far off the tour's route (relocate job, existing rows)
DEFER_CREDIT_LIMIT = "credit_limit"
DEFER_UNAVAILABLE = "geocoder_unavailable"
# AA-990: why a name got no point although the batch answered (geo.place_name.last_error). `unknown_to_model` used to
# cover all four; a try is still counted for each (bounded at store.mark_name max_tries), only the reason is honest.
REASON_NULL = "unknown_to_model"          # the model said null: it does not know the place
REASON_MISSING = "missing_in_answer"      # the answer has no key for this name (dropped or re-spelt beyond name_norm)
REASON_MALFORMED = "malformed_answer"     # the value is not [lat, lng, country, ...]
# f"{UNKNOWN_COUNTRY}:<the model's text>": the text names no ISO 3166-1 country ('Siberia', 'India and Pakistan');
# any ISO country is accepted here (shared/countries) and then checked by guards.point_ok (bbox + polygon).
UNKNOWN_COUNTRY = "unknown_country"
REASON_UNCONFIRMED = "unconfirmed_by_geocoder"   # AA-997: the model proposed a point, no geocoder answer confirmed it
FINAL_REASONS = frozenset({REASON_NULL, REASON_UNCONFIRMED})   # status 'unknown' at once (no retry loop)

SYSTEM_PROMPT = (
    "You are a geographer for Adventure Asia. For each place named on a tour itinerary you give "
    "its coordinates and country. You answer only for places you actually know; for anything "
    "you are unsure of you return null. You never invent a location."
)


@dataclass(frozen=True)
class Item:
    key: tuple            # (name_norm, country_norm, locality_norm) — geo.place_name key
    name: str             # display name to locate
    country: str          # a hint only: the card / tour country; the answer's country is where the place really is
    tour_name: str = ""
    locality: Optional[str] = None
    # AA-997: the routes of the tours using the name (guards.Route each), the geocoder's reference; () = none located
    routes: tuple = field(default=(), compare=False, hash=False)

    @property
    def label(self) -> str:
        return f"{self.name} ({self.locality})" if self.locality else self.name


@dataclass(frozen=True)
class Located:
    lat: float
    lng: float
    country: str
    located_by: str       # geoapify | llm (mapbox: legacy TripPlanner rows only)
    place_id: Optional[str] = None
    meta: Optional[dict] = field(default=None, compare=False, hash=False)   # geocoder audit (geocoder_meta)
    review_reason: Optional[str] = field(default=None, compare=False, hash=False)
    kind: Optional[str] = field(default=None, compare=False, hash=False)   # geomatch.feature_kind (AA-847)

    @property
    def confidence(self) -> float:
        return CONFIDENCE.get(self.located_by, 0.5)

    @property
    def precision(self) -> str:
        """'point' for a geocoder-confirmed (or admin) point; 'area' for the model's own point — shown as
        an area, never as a pinpoint. AA-847: an area feature (geomatch.feature_kind) is 'area' even when a geocoder
        confirmed it; a LINEAR feature is 'line' (owner decision S246: drawn as a line from geo.place_shape, never a
        pin, a circle or hidden) — Geoapify's point for "Ho Chi Minh Trail" is one spot on a 1,000 km trail, kept only
        as the anchor the shape is searched around."""
        if self.kind == geomatch.LINEAR:
            return "line"
        if self.kind == geomatch.AREA:
            return "area"
        return "point" if self.located_by in CONFIRMED_BY or self.located_by == "manual" else "area"

    @property
    def provider(self) -> Optional[str]:
        return (self.meta or {}).get("provider")


@dataclass(frozen=True)
class Outcome:
    point: Optional[Located]      # None: the model does not know it / guard rejected
    error: Optional[str] = None   # set when the attempt failed (model or geocoder error) → retry later
    reason: Optional[str] = None  # why point is None without an error (unknown / guard)
    deferred: bool = False        # credit guard / geocoder unavailable: stays pending, no try counted (reason)

    @property
    def final(self) -> bool:
        """AA-997: no point and no use asking again (store: status 'unknown' at once)."""
        return self.point is None and not self.deferred and self.error is None and self.reason in FINAL_REASONS


@dataclass(frozen=True)
class Proposal:
    lat: float
    lng: float
    country: str
    native_name: Optional[str] = None   # 中文 / 한글 name (Taiwan, South Korea) — the geocoder's 2nd query
    town: Optional[str] = None          # AA-997: the town / village the place is in (the town fallback)


def build_prompt(items: list[Item]) -> str:
    lines = "\n".join(f"- {i.label} | tour country: {i.country} | tour: {i.tour_name}" for i in items)
    return f"""Places (name | the tour's country | the tour's name, for context):
{lines}

Return ONLY a JSON object mapping each place name, copied exactly, to
[latitude, longitude, country, town] or null. Country is where the place really is (a Thailand tour may
sleep in Siem Reap, Cambodia). Town is the town or village the place is in or next to (the place itself
when it is a town). Use the town, village, park or landmark itself; for a hotel, lodge or camp, the town
it is in. For a place in Taiwan or South Korea, add its name in the local script (Chinese / Hangul) as a
last element when you know it.
Example: {{"Paro": [27.43, 89.41, "Bhutan", "Paro"], "Taroko Gorge": [24.16, 121.62, "Taiwan", "Hualien",
"太魯閣"], "Tiger's Nest": [27.49, 89.36, "Bhutan", "Paro"], "Unknown Lodge": null}}"""


class AnswerUnreadable(ValueError):
    """The model's answer holds no JSON object (empty, a refusal, or cut at max_tokens)."""


_FENCE = re.compile(r"```[a-zA-Z]*")
_MAX_OBJECT_STARTS = 50


def json_object(raw: Optional[str]) -> dict:
    """The first JSON object in a model answer: code fences and prose before / after it are ignored (AA-990: the old
    first-"{"-to-last-"}" slice failed a whole batch on trailing prose holding a brace). Raises AnswerUnreadable."""
    s = _FENCE.sub("", raw or "")
    decoder = json.JSONDecoder()
    pos, err = s.find("{"), "no '{'"
    for _ in range(_MAX_OBJECT_STARTS):
        if pos < 0:
            break
        try:
            obj, _end = decoder.raw_decode(s, pos)
        except json.JSONDecodeError as e:
            err = str(e)
        else:
            if isinstance(obj, dict):
                return obj
        pos = s.find("{", pos + 1)
    raise AnswerUnreadable(f"no JSON object in the locate answer: {err[:80]}")


def _proposal(v) -> "Proposal | str":
    try:
        lat, lng, raw_country = float(v[0]), float(v[1]), v[2]
    except (TypeError, ValueError, IndexError, KeyError):
        return REASON_MALFORMED
    if not isinstance(raw_country, str) or not raw_country.strip():
        return REASON_MALFORMED
    country = guards.lenient_country(raw_country)       # AA-997: "Tibet, China" -> China
    if not country:
        return f"{UNKNOWN_COUNTRY}:{raw_country.strip()[:40]}"
    native, town = None, None
    for extra in v[3:5] if isinstance(v, (list, tuple)) else ():
        if not isinstance(extra, str) or not extra.strip():
            continue
        if geomatch.has_native_script(extra):
            native = native or extra.strip()
        else:
            town = town or extra.strip()[:120]
    if not (country in NATIVE_NAME_COUNTRIES and native):
        native = None
    return Proposal(lat, lng, country, native, town)


def read_answer(raw: str, labels: list[str], plain: Optional[dict[str, str]] = None) -> dict[str, "Proposal | str"]:
    """Model JSON -> {label: Proposal | reason}. A key matches a label exactly (case / outer spaces), else by
    `name_norm`, else — when `plain` gives the label's plain name ("Night Market" for "Night Market (Hoi An)") and no
    other label of the batch shares it — by that plain name. A one-key object wrapping the map ({"places": {...}})
    is unwrapped. Raises AnswerUnreadable when there is no JSON object at all (the batch is asked again / retried)."""
    data = json_object(raw)
    label_norms = {name_norm(n) for n in labels} | {n.strip().lower() for n in labels}
    if len(data) == 1:
        (k, inner), = data.items()
        is_label = str(k).strip().lower() in label_norms or name_norm(str(k)) in label_norms
        if isinstance(inner, dict) and not is_label:
            data = inner
    exact = {str(k).strip().lower(): v for k, v in data.items()}
    normed: dict[str, object] = {}
    for k, v in data.items():
        normed.setdefault(name_norm(str(k)), v)
    plain = plain or {}
    plain_count: dict[str, int] = {}
    for n in labels:
        pn = name_norm(plain.get(n) or "")
        if pn:
            plain_count[pn] = plain_count.get(pn, 0) + 1
    missing = object()
    out: dict[str, Proposal | str] = {}
    for n in labels:
        v = exact.get(n.strip().lower(), missing)
        if v is missing and name_norm(n):
            v = normed.get(name_norm(n), missing)
        pn = name_norm(plain.get(n) or "")
        if v is missing and pn and plain_count.get(pn) == 1:
            v = normed.get(pn, missing)
        if v is missing:
            out[n] = REASON_MISSING
        elif v is None:
            out[n] = REASON_NULL
        else:
            out[n] = _proposal(v)
    return out


def parse_answer(raw: str, labels: list[str]) -> dict[str, Optional[Proposal]]:
    """Model JSON -> {label: Proposal(lat, lng, canonical country, native name?) | None}; anything
    malformed, or a country that names no ISO country, -> None; a native name is kept only in native script for
    Taiwan / South Korea. The country is the model's (where the place really is), not the tour's (AA-990).
    Raises ValueError (AnswerUnreadable) when the answer has no JSON object at all (the batch is retried).
    :func:`read_answer` says why a label got None."""
    return {k: (v if isinstance(v, Proposal) else None) for k, v in read_answer(raw, labels).items()}


def batches(items: list[Item], size: int = BATCH) -> list[list[Item]]:
    """Batches of ≤ size items with distinct labels (the answer is keyed by label)."""
    out: list[list[Item]] = []
    for it in items:
        for b in out:
            if len(b) < size and all(x.label.lower() != it.label.lower() for x in b):
                b.append(it)
                break
        else:
            out.append([it])
    return out


GenerateFn = Callable[[str, str], Awaitable[str]]


async def locate_many(items: list[Item], generate: GenerateFn, geocoder) -> dict[tuple, Outcome]:
    """Outcome per item key. `generate` raises to stop (e.g. BudgetExceeded propagates)."""
    out: dict[tuple, Outcome] = {}
    stopped: Optional[str] = None     # DEFER_* once the geocoder cannot check any more names this run
    external = geocoder is not None and getattr(geocoder, "calls_external", False)
    ensure_ready = getattr(geocoder, "ensure_ready", None) if external else None
    if ensure_ready is not None:
        try:
            ensure_ready()            # no paid model call when the key cannot even be read
        except GeocoderUnavailable as e:
            logger.error("place_geocoder_unavailable", provider=geocoder.name, error=str(e)[:200])
            stopped = DEFER_UNAVAILABLE
    for batch in batches(items):
        if stopped:   # no more paid model calls for names the geocoder cannot check
            for i in batch:
                out[i.key] = Outcome(None, reason=stopped, deferred=True)
            continue
        try:
            answer, errors = await _ask_batch(batch, generate)
        except BudgetExceeded:
            raise
        except Exception as e:  # noqa: BLE001 — a failed batch stays pending
            logger.warning("place_locate_batch_failed", names=len(batch), error=str(e)[:200])
            for i in batch:
                out[i.key] = Outcome(None, error=f"locate: {str(e)[:200]}")
            continue
        for i in batch:
            if i.label in errors:
                out[i.key] = Outcome(None, error=errors[i.label])
                continue
            if stopped:
                out[i.key] = Outcome(None, reason=stopped, deferred=True)
                continue
            try:
                out[i.key] = await _resolve(i, answer.get(i.label, REASON_MISSING), geocoder, external)
            except CreditLimitReached as e:
                logger.warning("place_geocoder_credit_limit", provider=geocoder.name, error=str(e)[:200])
                stopped = DEFER_CREDIT_LIMIT
            except GeocoderUnavailable as e:
                logger.error("place_geocoder_unavailable", provider=geocoder.name, error=str(e)[:200])
                stopped = DEFER_UNAVAILABLE
            except BudgetExceeded:
                raise
            except Exception as e:  # noqa: BLE001 — retried later, never a guess
                logger.warning("place_geocoder_failed", provider=geocoder.name, error=str(e)[:200])
                out[i.key] = Outcome(None, error=f"{geocoder.name}: {str(e)[:200]}")
                continue
            if stopped:
                out[i.key] = Outcome(None, reason=stopped, deferred=True)
    return out


async def _resolve(item: Item, answer, geocoder, external: bool) -> Outcome:
    """One name: the geocoder's point for the name, else for its town (area), else none (AA-997, module docstring).
    Raises the geocoder's CreditLimitReached / GeocoderUnavailable / errors (locate_many handles them)."""
    prop = answer if isinstance(answer, Proposal) else None
    if prop is not None:      # "China" for a point in Hong Kong / Macao: theirs (it fails China's polygon)
        prop = replace(prop, country=guards.refine_country(prop.lat, prop.lng, prop.country))
    bad = guards.point_ok(prop.lat, prop.lng, prop.country) if prop else None
    if not external:          # model_only: the model's point, flagged (no geocoder to ask)
        if prop is None:
            return Outcome(None, reason=answer)
        if bad:
            return Outcome(None, reason=bad)
        return Outcome(_located(item, prop.lat, prop.lng, prop.country, None))
    if prop is None or bad:
        # the model does not know it (or gave an impossible point): one geocoder-only search when the tour's route can
        # check the answer; a malformed / missing answer stays retryable as before
        why = answer if prop is None else bad
        if (prop is None and answer != REASON_NULL) or not guards.has_route(item.routes):
            return Outcome(None, reason=why)
        country = prop.country if prop else (guards.canonical_country(item.country) or item.country)
        res = await geocoder.confirm(item.name, None, None, country, routes=item.routes)
        hit = _confirmed(item, res, country)
        return Outcome(hit) if hit else Outcome(None, reason=REASON_NULL if prop is None else why)
    # AA-997 v2: GEOCODER_STRATEGY=v2 adds query variants + hint bias for new names (kwarg sent only then, so a
    # geocoder written before v2 keeps working); the town fallback below stays a plain settlement search
    strategy = default_strategy()
    kw = {"strategy": strategy} if strategy != STRATEGY_V1 else {}
    res = await geocoder.confirm(item.name, prop.lat, prop.lng, prop.country, native_name=prop.native_name,
                                 routes=item.routes, **kw)
    hit = _confirmed(item, res, prop.country)
    if hit:
        return Outcome(hit)
    town = prop.town
    if town and geomatch.name_sim(town, item.name) < 0.999 and not geomatch.is_generic_phrase(town):
        tres = await geocoder.confirm(town, prop.lat, prop.lng, prop.country, routes=item.routes, settlement=True)
        thit = _confirmed(item, tres, prop.country, town=town)
        if thit:
            return Outcome(thit)
    return Outcome(None, reason=REASON_UNCONFIRMED)


def _confirmed(item: Item, res, country: str, *, town: Optional[str] = None) -> Optional[Located]:
    """The geocoder's accepted point when it is in the asked country (or its CN / HK / MO family) and passes the hard
    guard; Hong Kong / Macao points asked as China are stored as Hong Kong / Macao. `town`: the point is the town's
    (precision 'area', review_reason centroid_fallback; a linear name keeps 'line': its shape is searched there)."""
    if not isinstance(res, Confirmed) or not guards.same_country(country, res.country):
        return None
    ctry = guards.refine_country(res.lat, res.lng, res.country)
    if guards.point_ok(res.lat, res.lng, ctry) is not None:
        return None
    if town is None:
        kind = geomatch.feature_kind(item.name, (res.meta or {}).get("result_type"))
        return Located(res.lat, res.lng, ctry, res.located_by, res.place_id, meta=dict(res.meta), kind=kind)
    kind = geomatch.LINEAR if geomatch.feature_kind(item.name) == geomatch.LINEAR else geomatch.AREA
    meta = {**res.meta, "outcome": "town_fallback", "town": town}
    return Located(res.lat, res.lng, ctry, res.located_by, None, meta=meta, review_reason=REVIEW_CENTROID, kind=kind)


async def _ask(batch: list[Item], generate: GenerateFn) -> dict[str, "Proposal | str"]:
    return read_answer(await generate(SYSTEM_PROMPT, build_prompt(batch)), [i.label for i in batch],
                       {i.label: i.name for i in batch if i.locality})


async def _ask_batch(batch: list[Item], generate: GenerateFn) -> tuple[dict[str, "Proposal | str"], dict[str, str]]:
    """(answers by label, errors by label). AA-990: an unreadable answer (empty, refusal, cut at max_tokens — a big
    batch is the likely one to be cut) is asked again ONCE as two halves (≤ 2 more calls) before its names count a
    try; a half that is still unreadable gives its names a `locate: …` error. A model call that raises (throttled,
    budget) propagates: nothing to split."""
    try:
        return await _ask(batch, generate), {}
    except AnswerUnreadable as e:
        if len(batch) < 2:
            return {}, {i.label: f"locate: {str(e)[:200]}" for i in batch}
        logger.warning("place_locate_answer_unreadable_split", names=len(batch), error=str(e)[:200])
    answers: dict[str, Proposal | str] = {}
    errors: dict[str, str] = {}
    mid = len(batch) // 2
    for half in (batch[:mid], batch[mid:]):
        try:
            answers.update(await _ask(half, generate))
        except AnswerUnreadable as e:
            errors.update({i.label: f"locate: {str(e)[:200]}" for i in half})
    return answers, errors


def _located(item: Item, lat: float, lng: float, country: str, res) -> Located:
    """The confirmed point when the geocoder accepted a candidate in the model's country, else the model's point
    ('llm', area precision). AA-997: the model's point is stored only by `model_only` (no geocoder configured); it is
    flagged `llm_point` (`unconfirmed_hard_type` for a hard kind) so /admin/places lists it."""
    hit = _confirmed(item, res, country)
    if hit:
        return hit
    meta = None
    if isinstance(res, Confirmed):     # accepted by the rule but in another country / outside its bbox
        meta = {**res.meta, "outcome": "rejected_country_guard"}
    elif isinstance(res, Unconfirmed):
        meta = dict(res.meta)
    kind = geomatch.feature_kind(item.name)
    country = guards.refine_country(lat, lng, country)
    # a line's geometry is searched by name (place_shape: Overpass) around this point: no pin review for it
    review = None if kind == geomatch.LINEAR else (
        REVIEW_UNCONFIRMED_HARD if geomatch.is_hard(item.name) else REVIEW_LLM_POINT)
    return Located(lat, lng, country, "llm", meta=meta, review_reason=review, kind=kind)
