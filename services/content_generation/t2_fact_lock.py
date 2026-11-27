"""AA-1144 (S285 decision D8) — the fact lock of an agency's hand edit of its T2 tour copy.

The agency may reword its copy of a tour freely; it may not change what the tour IS. Adventure Asia operates the
Master, and TripPlanner (catalog.v_tenant_tour_v1) and CRM quotes show the agency's copy to the traveller, so a fact
changed here would reach a customer while AA runs something else (advisory r3 permissions P5).

What counts as a fact is what the FactSheet tour builder already extracts from a text (services/quality/s1_facts.py:
day markers, figures bound to a unit, place names, "not included" lines); this module reuses those extractors, it adds
no new parser. An edit (PATCH /v1/tours/versions/{id}, action=edit) is checked against its base: the version it edits
(the AA-written T2 copy, or an earlier accepted edit), the Master text (published_tours aa_*) and the raw source
(raw_tours src_*, duration, inclusions, exclusions; not price_raw, AA-1188). Refused (HTTP 422 ``fact_locked``) if it:

* sets a key that is not wording (WORDING_FIELDS): duration, price, inclusions, a day list ... (``field``);
* changes the day structure of the itinerary: another set of "Day N" markers than the base itinerary (``days``);
* brings a figure the base never states: "3 days" where AA says 4, "$450", "5 hours", "07:30" (``figure``); a
  duration spelled out ("a two-day walk", "three nights") is read as its figure. A day / night / week figure is
  checked against the Master's length (``master_length``) and the figures AA wrote (the version, the Master) only,
  never the raw source (S285 live check: a raw "10 days" let "This ten-day trek" through on a seven-day Master);
* names a place the base never names, in the highlights or the itinerary (``place``); the place base is the parent's
  highlights and itinerary plus the Master and raw texts, never the parent's name / summary / SEO lines, so a place
  first slipped into the summary (where places are free) cannot clear it for the itinerary in a second edit;
* drops or adds a "not included" item group (flights, visa, tips, meals ...) (``not_included``);
* newly says an item group is included that the source excludes, or that the Master does not state as included
  (the version or the Master text says it is included, or the raw inclusions list names it; "international flights"
  needs international or all flights there) ("flights included", "visa included") (``included``). Naming the item
  elsewhere ("fly to the coast") never clears a claim (S285 live check: "International flights included." went
  through).

Kept free: every sentence that keeps those facts, dropping a figure or a place (less detail is not a different
tour), reordering, tone, brand words in the name / subtitle / summary / SEO lines (places are checked only where a
place is an itinerary fact). The new content is the edited version with the edited wording fields laid over it, so
an edit that sends only some fields never drops the others (the Content studio sends no itinerary).

Every edit is audited (acp_shared.audit_log, written on the main pool after the tenant transaction, best effort like
the Facts Entries audit): who (the member from the token), which fields, the sha256 of each old and new value; a
refused edit is audited too, with what was locked. The checks above are pure; ``check_against_base`` (one read on the
route's connection) and ``audit`` (one write on the main pool) are the only I/O.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Optional

import structlog

from services.acp_content_writing.hygiene import fold, phrase_tokens, proper_nouns
from services.content_generation.day_parse import parse_duration, source_day_markers
from services.quality import s1_facts

logger = structlog.get_logger()

FACT_LOCKED = "fact_locked"
# The keys of tenant_tour_versions.rewritten_content an agency may reword (t2_rewrite_job writes these).
WORDING_FIELDS = ("name", "subtitle", "summary", "highlights", "itineraries", "seo_title", "seo_meta")
# Where a place name is a fact of the tour (the name / summary / SEO lines may carry the agency's own brand words).
PLACE_FIELDS = ("highlights", "itineraries")
# Figure roles that are facts (s1_facts.number_role): an ordinal ("12th-century") is not. A label stays: the reader
# takes "USD 390" for a label (a capitalised word before the figure), and a new "Day 5" is a new day anyway.
_FACT_ROLES = ("figure", "clock", "label")
MESSAGE = ("This edit changes a tour fact (days, places, figures or what is or is not included). Only the wording "
           "can change; the facts come from Adventure Asia.")
# Durations spelled out ("a two-day walk", "three nights", "twelve hours"): the FactSheet reader sees digit figures
# only, and copy usually spells a duration out. "one" is left out ("one day you will ..." is an idiom, not a figure).
_NUMBER_WORDS = {w: str(i) for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen "
    "eighteen nineteen twenty".split()) if i >= 2}
_NUMBER_WORDS.update({"thirty": "30", "forty": "40", "fifty": "50"})
_WORD_FIGURE_RE = re.compile(r"\b(?P<num>" + "|".join(_NUMBER_WORDS) + r")[^\S\n]*[-–][^\S\n]*(?P<unit>"
                             r"days?|nights?|hours?|hrs?|weeks?)\b|\b(?P<num2>" + "|".join(_NUMBER_WORDS)
                             + r")[^\S\n]+(?P<unit2>days?|nights?|hours?|hrs?|weeks?)\b", re.I)
# A clause that says an item is included ("Price includes flights", "Visa included", "covers all meals").
_INCLUDED_RE = re.compile(r"\binclud(?:e|es|ed|ing)\b|\bcover(?:s|ed)?\b|\bprovided\b|\bcomes\s+with\b|"
                          r"\bwe\s+(?:arrange|provide)\b", re.I)
# Clause ends inside a sentence ("Flights are not included, but the visa is included" holds two claims) and the
# exceptions an inclusion claim carries ("everything is covered except the visa": the visa is not claimed).
_CLAUSE_SPLIT_RE = re.compile(r";|\bbut\b|\bwhile\b|\bwhereas\b", re.I)
_EXCEPT_RE = re.compile(r"\bexcept\b|\bother\s+than\b|\bapart\s+from\b|\bbesides\b", re.I)
# S285 live check: the units of the tour's length. A figure in one of them is checked against the Master's duration
# and the texts AA wrote (the version and the Master), never the raw source: a raw text that says "10 days" (another
# departure, a visa rule) let "This ten-day trek" through on a seven-day Master.
LENGTH_UNITS = ("day", "night", "week")
# The scope words of an inclusion claim: "international flights included" is not cleared by "domestic flights".
_SCOPE_RE = re.compile(r"\b(international|domestic|internal|inbound|outbound)\b[^\S\n]+(?:[\w-]+[^\S\n]+)?$", re.I)
_SCOPE_OF = {"international": "international", "inbound": "international", "outbound": "international",
             "domestic": "domestic", "internal": "domestic"}


@dataclass(frozen=True)
class Locked:
    field: str    # the rewritten_content key the change is in ("content" for a not-included item group)
    fact: str     # field | days | figure | place | not_included
    value: str    # what changed, as the agency wrote it (or the base value it dropped)

    def as_dict(self) -> dict:
        return asdict(self)


def text_of(value: Any) -> str:
    """A content value as one text (a highlights list joined by newlines), as the FactSheet reads a field."""
    return s1_facts.field_text(value)


def merge_edit(parent: dict, edited: dict) -> tuple[dict, list[Locked]]:
    """(new content, locked keys): ``parent`` with the edited wording fields laid over it. A key outside
    WORDING_FIELDS that the edit sets to another value than the parent's is a locked fact."""
    out = dict(parent)
    locked = []
    for key, value in edited.items():
        if key in WORDING_FIELDS:
            out[key] = value
        elif parent.get(key) != value:
            locked.append(Locked(str(key)[:64], "field", text_of(value)[:120]))
    return out, locked


def _word_figures(text: str) -> list[tuple[str, Optional[str], str]]:
    """(number, unit, span) of every spelled-out duration in ``text`` ("two-day" -> ("2", "day"))."""
    out = []
    for m in _WORD_FIGURE_RE.finditer(text or ""):
        word = (m["num"] or m["num2"]).lower()
        unit = (m["unit"] or m["unit2"]).lower()
        out.append((_NUMBER_WORDS[word], s1_facts._UNIT_OF.get(unit), m.group(0)))
    return out


def _figures(text: str) -> list[tuple[str, Optional[str], str]]:
    """(number, unit, span) of every fact figure in ``text`` (a range gives both ends), spelled-out durations
    included."""
    return [(n, m["unit"], m["span"]) for m in s1_facts.number_mentions(text) if m["role"] in _FACT_ROLES
            for n in m["numbers"]] + _word_figures(text)


def _figure_known(number: str, unit: Optional[str], base: set[tuple[str, Optional[str]]]) -> bool:
    """A figure with a unit must be in the base with that unit ("3 nights" never clears "3 days"); a bare figure
    matches the same number with any unit."""
    return (number, unit) in base or (unit is None and any(n == number for n, _u in base))


def _day_numbers(text: str) -> set[int]:
    out: set[int] = set()
    for first, last, _idx in source_day_markers(text):
        out.update(range(first, last + 1))
    return out


def _words(text: str) -> set[str]:
    return set(phrase_tokens(text)) | {fold(w) for w in text.split()}


def _places(text: str, lower: set[str]) -> dict[str, str]:
    """{token key: name as written} of the capitalised names in ``text``; the key leaves out the ordinary words a
    sentence start or a heading adds (s1_facts.clean_name)."""
    out = {}
    for sent in s1_facts.sentences(text):
        for raw in proper_nouns(sent):
            key = " ".join(phrase_tokens(s1_facts.clean_name(raw, lower)))
            if key:
                out.setdefault(key, raw)
    return out


def _not_included(texts: Iterable[str]) -> set[str]:
    """The item groups (s1_facts.ITEM_GROUPS) of every "not included" clause of the texts ("Lodges are included, but
    flights are not included" excludes flights only)."""
    out: set[str] = set()
    for text in texts:
        for sent in s1_facts.sentences(text):
            for clause in _CLAUSE_SPLIT_RE.split(sent):
                if s1_facts._NOT_INCLUDED_RE.search(clause):
                    out.update(s1_facts.item_groups(clause))
    return out


def _clauses(text: str) -> list[str]:
    """The clauses of every sentence of ``text``, each cut before an "except" / "other than"."""
    return [_EXCEPT_RE.split(c)[0] for sent in s1_facts.sentences(text) for c in _CLAUSE_SPLIT_RE.split(sent)
            if c.strip()]


def _scoped_groups(text: str) -> set[tuple[str, str]]:
    """(item group, scope) of every item ``text`` names; the scope is "international" / "domestic" when a scope word
    stands right before the item ("international flights", "domestic return flights"), else "" (the whole group)."""
    out: set[tuple[str, str]] = set()
    for group, rx in s1_facts._ITEM_RES.items():
        for m in rx.finditer(text or ""):
            scope = _SCOPE_RE.search(text[max(0, m.start() - 40):m.start()])
            out.add((group, _SCOPE_OF[scope.group(1).lower()] if scope else ""))
    return out


def _claims(texts: Iterable[str]) -> set[tuple[str, str]]:
    """(group, scope) of every item the texts say is included: a clause (up to any "except") that has an inclusion
    verb and is neither a "not included" nor an optional one."""
    out: set[tuple[str, str]] = set()
    for text in texts:
        for clause in _clauses(text):
            if (_INCLUDED_RE.search(clause) and not s1_facts._NOT_INCLUDED_RE.search(clause)
                    and not s1_facts._OPTIONAL_RE.search(clause)):
                out |= _scoped_groups(clause)
    return out


def _listed(source_inclusions: str) -> set[tuple[str, str]]:
    """(group, scope) of every line of the raw inclusions list (its lines carry no "included" word), optional and
    "not included" lines left out."""
    return {pair for line in s1_facts.item_lines(source_inclusions)
            if not s1_facts._NOT_INCLUDED_RE.search(line) and not s1_facts._OPTIONAL_RE.search(line)
            for pair in _scoped_groups(line)}


def _excluded(texts: Iterable[str], source_exclusions: str) -> set[tuple[str, str]]:
    """(group, scope) the base excludes: every unconditional line of the raw exclusions ("meals not mentioned in the
    itinerary" depends on the itinerary) and every "not included" clause of the texts. Any excluded meal excludes
    "meals" (a page saying "meals included" would contradict it)."""
    out = {pair for line in s1_facts.item_lines(source_exclusions) if not s1_facts._CONDITIONAL_RE.search(line)
           for pair in _scoped_groups(line)}
    for text in texts:
        for sent in s1_facts.sentences(text):
            for clause in _CLAUSE_SPLIT_RE.split(sent):
                if s1_facts._NOT_INCLUDED_RE.search(clause):
                    out |= _scoped_groups(clause)
    if any(g in s1_facts.MEAL_GROUPS for g, _scope in out):
        out.add(("meals", ""))
    return out


def _covers(pairs: set[tuple[str, str]], group: str, scope: str) -> bool:
    """``pairs`` state the item: the same group with the same scope, or with no scope (all of the group)."""
    return (group, scope) in pairs or (group, "") in pairs


def _check_included(changed_texts: list[str], parent_texts: list[str], master_texts: list[str],
                    source_inclusions: str, excluded_texts: list[str], source_exclusions: str) -> list[Locked]:
    """A new inclusion claim is locked unless the Master states it: the version or the Master text says the item is
    included, or the raw inclusions list names it (with the same scope, or the whole group). It is locked as well when
    the base excludes it ("flights included" where the source lists flights as excluded; "meals included" where it
    excludes any meal; an unscoped claim against any scoped exclusion). Naming the item elsewhere ("fly to the
    coast", "drive back") never clears a claim (S285 live check: "International flights included." went through).
    A claim the version already makes is kept (it was checked when it was written)."""
    new_claims = _claims(changed_texts) - _claims(parent_texts)
    if not new_claims:
        return []
    excluded = _excluded(parent_texts + excluded_texts, source_exclusions)
    support = _claims(parent_texts + master_texts) | _listed(source_inclusions)
    out = set()
    for group, scope in new_claims:
        clash = _covers(excluded, group, scope) or (not scope and any(g == group for g, _s in excluded))
        if clash or not _covers(support, group, scope):
            out.add(group)
    return [Locked("content", "included", g) for g in sorted(out)]


def master_length(master_itinerary: str, parent_itinerary: str, duration: str) -> set[tuple[str, str]]:
    """The (number, unit) figures that state the Master's length: its day count (the last "Day N" of the Master
    itinerary, else of the version's; the raw duration only when neither has day markers, both ends of a range), its
    nights (the raw duration's when it gives the same day count, else one fewer) and its weeks when the days are whole
    weeks."""
    dur = parse_duration(duration)
    marked = _day_numbers(master_itinerary) or _day_numbers(parent_itinerary)
    if marked:
        days = [max(marked)]
    elif dur.range:
        days = list(dur.range)
    else:
        days = [dur.day_count] if dur.day_count else []
    out: set[tuple[str, str]] = set()
    for d in days:
        out.add((str(d), "day"))
        nights = dur.nights if dur.nights and (dur.range or dur.day_count == d) else d - 1
        if nights > 0:
            out.add((str(nights), "night"))
        if d % 7 == 0:
            out.add((str(d // 7), "week"))
    return out


def _check_days(parent: dict, new: dict, master_itinerary: str) -> list[Locked]:
    base = _day_numbers(text_of(parent.get("itineraries"))) or _day_numbers(master_itinerary)
    got = _day_numbers(text_of(new.get("itineraries")))
    if not base or got == base:
        return []
    diff = sorted(base ^ got)
    return [Locked("itineraries", "days", ", ".join(f"Day {d}" for d in diff[:10]))]


def _check_field(field: str, text: str, base_figures: set, base_places: set[str], base_words: set[str],
                 lower: set[str], length_base: set) -> list[Locked]:
    out = []
    for number, unit, span in _figures(text):
        known = (number, unit) in length_base if unit in LENGTH_UNITS else _figure_known(number, unit, base_figures)
        if not known:
            out.append(Locked(field, "figure", span))
    if field in PLACE_FIELDS:
        for key, name in _places(text, lower).items():
            if key not in base_places and not set(key.split()) <= base_words:
                out.append(Locked(field, "place", name))
    return out


def check_edit(parent: dict, edited: dict, base_texts: Iterable[str], master_itinerary: str = "",
               source_exclusions: str = "", *, master_texts: Optional[Iterable[str]] = None,
               source_inclusions: str = "", duration: str = "") -> tuple[dict, list[Locked]]:
    """(new content, locked facts) of an agency edit of ``parent`` (the version it edits). ``base_texts``: the Master
    and raw source texts the tour's facts come from; ``master_texts``: the Master's own texts among them (None: every
    base text but the exclusions); ``source_exclusions`` / ``source_inclusions``: the raw lists (the copy may say an
    excluded item is not included, never that it is included; a listed item may be called included); ``duration``: the
    raw duration (the Master's length when no itinerary has day markers). An empty locked list means the edit may be
    saved."""
    new, locked = merge_edit(parent, edited)
    source = [t for t in base_texts if t]
    excl = (source_exclusions or "").strip()
    # The raw exclusions list names its groups without a "not included" word: never a base of an inclusion.
    positive = [t for t in source if t.strip() != excl]
    master = [t for t in master_texts if t] if master_texts is not None else positive
    parent_texts = [text_of(parent.get(f)) for f in WORDING_FIELDS]
    base = "\n".join(parent_texts + source)
    base_figures = {(n, u) for n, u, _s in _figures(base)}
    # The tour's length: the Master's duration, or a day / night / week figure AA wrote (the version, the Master).
    length_base = ({(n, u) for n, u, _s in _figures("\n".join(parent_texts + master)) if u in LENGTH_UNITS}
                   | master_length(master_itinerary, text_of(parent.get("itineraries")), duration))
    # Places: only where a place is a fact (the parent's highlights / itinerary) plus the Master and raw texts.
    place_base = "\n".join([text_of(parent.get(f)) for f in PLACE_FIELDS] + source)
    base_words = _words(place_base)
    lower = s1_facts._lower_vocab({"base": base, "new": "\n".join(text_of(new.get(f)) for f in WORDING_FIELDS)})
    base_places = set(_places(place_base, lower))
    changed = [f for f in WORDING_FIELDS if text_of(new.get(f)) != text_of(parent.get(f))]
    for f in changed:
        locked += _check_field(f, text_of(new.get(f)), base_figures, base_places, base_words, lower, length_base)
    if "itineraries" in changed:
        locked += _check_days(parent, new, master_itinerary)
    if changed:
        was = _not_included(parent_texts)
        now = _not_included(text_of(new.get(f)) for f in WORDING_FIELDS)
        allowed = was | _not_included(source) | set(s1_facts.item_groups(source_exclusions))
        locked += [Locked("content", "not_included", g) for g in sorted((was - now) | (now - allowed))]
        locked += _check_included([text_of(new.get(f)) for f in changed], parent_texts, master, source_inclusions,
                                  positive, source_exclusions)
    return new, locked


def refusal(locked: list[Locked]) -> dict:
    """The 422 detail: {code, message, locked: [{field, fact, value}]} (at most 20 entries)."""
    return {"code": FACT_LOCKED, "message": MESSAGE, "locked": [x.as_dict() for x in locked[:20]]}


def _sha(value: Any) -> Optional[str]:
    if value is None:
        return None
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def audit_changes(parent: dict, new: dict) -> list[dict]:
    """The audit view of an edit: one {field, old_sha256, new_sha256} per changed key (never the text itself)."""
    keys = sorted(set(parent) | set(new), key=str)
    return [{"field": str(k)[:64], "old_sha256": _sha(parent.get(k)), "new_sha256": _sha(new.get(k))}
            for k in keys if parent.get(k) != new.get(k)]


# The base of an edit besides the version itself: the Master text and the raw source of the tour (tenant pool: CIS 239
# master read on published_tours / raw_tours; admin pool: cis_app). AA-1188 (DB-01): not raw_tours.price_raw, so the
# accept / refuse answer is no oracle on the source price text and an agency edit cannot bring it into its copy.
BASE_SQL = """
    SELECT pt.aa_name, pt.aa_subtitle, pt.aa_summary, pt.aa_description, pt.aa_highlights, pt.aa_itineraries,
           rt.src_name, rt.src_subtitle, rt.src_summary, rt.src_description, rt.src_highlights, rt.src_itineraries,
           rt.duration, rt.inclusions, rt.exclusions
      FROM gold_aa_internal.published_tours pt
      LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
     WHERE pt.id = $1::uuid
"""


# The Master's own texts in BASE_SQL (an inclusion claim and a length figure need the Master, not the raw source).
MASTER_KEYS = ("aa_name", "aa_subtitle", "aa_summary", "aa_description", "aa_highlights", "aa_itineraries")


def content_dict(value: Any) -> dict:
    """rewritten_content as a dict (asyncpg returns jsonb as text without a codec); {} when unreadable."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return dict(value) if isinstance(value, dict) else {}


async def check_against_base(conn, published_tour_id: Any, parent: dict, edited: dict) -> tuple[dict, list[Locked]]:
    """check_edit with the tour's Master and raw texts read on ``conn`` (no row: the version is the only base)."""
    row = await conn.fetchrow(BASE_SQL, published_tour_id)
    row = dict(row) if row else {}
    texts = [text_of(v) for v in row.values()]
    return check_edit(parent, edited, texts, master_itinerary=text_of(row.get("aa_itineraries")),
                      source_exclusions=text_of(row.get("exclusions")),
                      master_texts=[text_of(row.get(k)) for k in MASTER_KEYS],
                      source_inclusions=text_of(row.get("inclusions")), duration=text_of(row.get("duration")))


async def audit(pool, *, tenant_id: str, actor: str, version_id: str, details: dict) -> None:
    """One audit row per edit (TOUR_VERSION_EDITED) or refusal (TOUR_VERSION_EDIT_REFUSED, details carry
    ``locked``). Best effort: a lost row is logged, the edit stands."""
    from services.acp_shared.audit_log import TenantAuditAction, write_audit_log
    action = (TenantAuditAction.TOUR_VERSION_EDIT_REFUSED if "locked" in details
              else TenantAuditAction.TOUR_VERSION_EDITED)
    try:
        await write_audit_log(pool, tenant_id=str(tenant_id), actor=actor[:64], action=action,
                              resource_type="tenant_tour_version", resource_id=str(version_id), details=details)
    except Exception as exc:  # noqa: BLE001 - the edit already committed (or was refused); never undo it for a log
        logger.warning("t2_edit_audit_failed", tenant_id=str(tenant_id), version_id=str(version_id),
                       error=str(exc)[:200])


__all__ = ["BASE_SQL", "FACT_LOCKED", "LENGTH_UNITS", "Locked", "MASTER_KEYS", "MESSAGE", "PLACE_FIELDS",
           "WORDING_FIELDS", "audit", "audit_changes", "check_against_base", "check_edit", "content_dict",
           "master_length", "merge_edit", "refusal", "text_of"]
