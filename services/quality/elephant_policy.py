"""S271 W0 (root ADR 0020 D8, AA-900 l16) — the elephant-riding policy detector, one copy for S1 and T2.

The policy (business owner, 08/10/2026): a tour whose product offers elephant riding is kept AS IT IS and goes to a
human review; the system never strips the ride and never regenerates the text to hide it.

- **S1 (A1 Master):** a hit on the generated page OR on the raw source adds ``FACT_CHECK_MANUAL_CHECK`` in
  ``brand_audit_node.pre_audit_checks`` (manual_check, so the tour goes to the Review Queue and is never
  auto-published); ``flag_fix_node`` makes no LLM edit on such a tour (it could rewrite the ride away).
- **T2 (tenant rewrite):** a hit on the Master text or the raw source is ``policy_hold`` v0 (D8): the version is the
  Master text verbatim with the SEO title / meta fitted by code, 0 LLM calls, a review row; the tenant still receives
  it (``services/jobs/t2_rewrite_job.py``).

Before S271 the only detector was ``elephant[-\\s]?back|elephant\\s+ride|elephant\\s+trek`` over the generated
highlights: the policy's own wording ("elephant riding", "riding an elephant", "ride an elephant") did not match, and
the itinerary, summary, description and source were never read (audit S271, tour-pipeline-audit F-elephant).

What matches (case-insensitive, plural and inflected forms):

- ``elephant riding`` / ``elephant ride(s)`` / ``elephant trek(s|king)``;
- ``elephant-back`` / ``elephantback``; ``elephant back`` only before a ride noun (``elephant back ride / safari /
  trek``) or after ``on`` / ``by`` ("watch the mahouts bring the elephants back" is not a ride);
- ``ride / rides / riding / rode / ridden [on|atop] [an|a|the|your|our|baby] elephant(s)``, ``mount an elephant``,
  ``back of an elephant``;
- ``elephant safari(s)`` in context: a ride when its clause says so (on / atop an elephant, on the back, riding),
  never when the clause puts it in a vehicle ("by 4x4 jeep", "jeep elephant safari"), and otherwise a ride unless
  the tour's country is one where the phrase names a jeep safari to watch the herds (``JEEP_SAFARI_COUNTRIES``:
  Sri Lanka's Minneriya / Kaudulla / Udawalawe). An Indian or Nepali park "elephant safari" is a ride.

What does not: a mention negated RIGHT next to it ("no elephant riding", "without (an) elephant ride", "without
riding the elephants", "we never ride elephants", "we do not offer elephant rides", "elephant riding is not offered",
"elephant-riding-free"), and elephant mentions with no ride ("bathe the elephants", "an elephant sanctuary", "watch
wild elephants"). The negation check is fail safe (a missed ride breaks the policy, a false hit costs one human
review): marketing idioms stay hits ("Don't miss the elephant ride", "You will never forget riding an elephant",
"No trip is complete without an elephant ride", "You cannot leave without riding an elephant", "non-stop fun with
elephant rides", "an elephant ride is not to be missed").

AA-1047 W1 (follow-up of the W0 review, #1355): the detector has its own code, ``POLICY_CODE``
(``POLICY_ELEPHANT_RIDING``), no longer the LLM brand audit's shared ``FACT_CHECK_MANUAL_CHECK``. Before, a tour with
elephant riding AND an unrelated LLM fact check (a rare-wildlife / location mismatch) carried one code and was read
as held by the policy alone (``policy_keep``), so the other check was approved away. A gate that exempts the policy
reads ``is_policy_only_hold`` (the policy code AND a detector hit; any other manual-check code keeps the hold),
never the code alone. Next to the LLM audit's ``FACT_CHECK_MANUAL_CHECK`` it reads the audit's issues: an issue
about the ride (``names_riding``: riding, not the animal) is the policy, an issue naming another product-truth
concern keeps the hold, a voice / SEO / format issue is neither.

Pure: no DB, no LLM, no I/O.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional

# The brand-audit code a hit adds (brand_audit_node._MANUAL_CHECK_CODES: manual_check, Review Queue). AA-1047: its own
# code; LEGACY_CODE is what a hit added before (stored versions written up to S271 carry it).
POLICY_CODE = "POLICY_ELEPHANT_RIDING"
LEGACY_CODE = "FACT_CHECK_MANUAL_CHECK"
# An LLM brand-audit issue that names some other product-truth concern (a claim the source does not support, a
# mismatch, a wrong place / duration ...). A voice / SEO / format issue ("meta 162 chars", a city-list subtitle, hype)
# names none of these and does not count: on a v2 draft those codes are report-only, but their issue text stays in
# brand_audit_issues (is_policy_only_hold).
_PRODUCT_TRUTH_ISSUE_RE = re.compile(
    r"\b(?:source|sources|sourced|unsupported|unverified|verif\w*|claim\w*|mismatch\w*|invent\w*|fabricat\w*"
    r"|inaccura\w*|incorrect\w*|wrong\w*|contradict\w*|inconsisten\w*|mislead\w*|fact\w*|product[-\s]truth"
    r"|(?:does|do|did)\s+not\s+(?:match|mention|state|include|offer|support)|doesn['’]?t\s+(?:match|mention|state"
    r"|include|offer|support)|not\s+(?:mentioned|stated|included|offered|supported)\b"
    r"|sightings?|guarantee\w*|location|duration|misplac\w*|rare|endangered)",
    re.IGNORECASE,
)
# The policy-hold reason a T2 version and its review row carry (D8).
POLICY_HOLD = "elephant_riding"

_ELE = r"elephants?"
_ART = r"(?:(?:an|a|the|your|our|their|baby|young|gentle|trained|domestic(?:ated)?)\s+){0,2}"
_RIDE_VERB = r"(?:ride|rides|riding|rode|ridden)"
ELEPHANT_RIDING_RE = re.compile(
    r"\b(?:"
    # "elephant riding / ride(s) / trek(s) / trekking"
    rf"{_ELE}[-\s]+(?:{_RIDE_VERB}|treks?|trekking|trekked)\b"
    # "elephant-back" / "elephantback" (written as one word, it only means riding)
    r"|elephant-?back(?:ed)?\b"
    # "elephant back ride / safari / trek" (two words: only with the ride noun after; "bring the elephants back"
    # is not a ride)
    rf"|{_ELE}\s+back\s+(?:{_RIDE_VERB}|safaris?|treks?|trekking|excursions?)\b"
    # "on / by elephant back"
    r"|(?:on|by)\s+elephant\s+back\b"
    # "elephant safari(s)": a ride only in context (riding_mentions: _safari_is_a_ride)
    r"|(?P<safari>elephants?[-\s]+safaris?)\b"
    # "ride on an elephant", "riding the elephants", "rode a baby elephant"
    rf"|{_RIDE_VERB}\s+(?:on\s+|atop\s+)?{_ART}{_ELE}\b"
    # "mount an elephant", "the back of an elephant"
    rf"|(?:mount|mounts|mounting|mounted)\s+{_ART}{_ELE}\b"
    rf"|back\s+of\s+{_ART}{_ELE}\b"
    r")",
    re.IGNORECASE,
)

# A negation RIGHT before the match (fail safe: a looser "a no/not somewhere in the clause" rule dropped real offers
# such as "Don't miss the elephant ride" or "You will never forget riding an elephant"). Only the negation word, at
# most one verb of offering and an article may sit between: "no elephant riding", "without (an) elephant ride(s)",
# "without riding the elephants", "we never ride elephants", "we do not offer elephant rides", "instead of an
# elephant ride", "non-elephant-riding".
_NEG_ADJACENT_RE = re.compile(
    r"(?:\b(?:no|not|never|nor|without|instead\s+of|rather\s+than|avoid(?:s|ing)?|ban(?:s|ned)?(?:\s+on)?"
    r"|free\s+of|don['’]?t|doesn['’]?t|do\s+not|does\s+not|won['’]?t|will\s+not|cannot|can['’]?t)"
    r"(?:\s+(?:offer|offers|include|includes|allow|allows|support|supports|condone|condones|promote|promotes"
    r"|provide|provides|any|more|longer))?"
    r"(?:\s+(?:an|a|the|any))?\s+|\bnon-)$",
    re.IGNORECASE,
)
# Idioms that read as negations but sell the ride: "no trip is complete without an elephant ride", "you cannot
# leave without riding an elephant". The negation check never applies after one of them.
_NEG_IDIOM_RE = re.compile(
    r"\b(?:complete|leave|leaving|go\s+home|going\s+home|depart|departing|miss|missed)\s+without"
    r"(?:\s+(?:an|a|the|any))?\s+$",
    re.IGNORECASE,
)
# A negation right after the match ("elephant riding is not offered", "elephant rides are not included",
# "elephant-riding-free"), but not "an elephant ride is not to be missed".
_NOT_AN_IDIOM = r"(?!\s+(?:to\s+be\s+missed|only|just)\b)"
_NEG_AFTER_RE = re.compile(
    rf"^(?:\s*-\s*free\b|\s+(?:is|are|was|were|will\s+be)\s+(?:not|never|no\s+longer)\b{_NOT_AN_IDIOM}"
    rf"|\s+(?:isn['’]?t|aren['’]?t)\b{_NOT_AN_IDIOM})",
    re.IGNORECASE,
)
_CLAUSE_BREAK_RE = re.compile(r"[.;:!?\n()\[\]]")
# "Elephant safari": an Indian / Nepali park "elephant safari" is a ride on an elephant, a Sri Lankan one
# (Minneriya, Kaudulla, Udawalawe) is a jeep safari to watch the herds. It counts as riding when its clause says so
# (on / atop / astride an elephant, on the back, a ride verb), never when the clause puts it in a vehicle (after it:
# "by 4x4 jeep", "in an open vehicle"; right before it: "jeep elephant safari"), and otherwise unless the tour is in
# a country where the phrase means a jeep safari.
_SAFARI_RIDE_CUE_RE = re.compile(
    rf"\b(?:(?:on|atop|astride|aboard)\s+(?:the\s+back\s+of\s+)?{_ART}{_ELE}\b|(?:on|from)\s+(?:the\s+)?back\b"
    r"|riding|rode|ridden|mahout-led\s+ride)",
    re.IGNORECASE,
)
# Another animal's or vehicle's ride in the same clause is not a riding cue ("elephant safari, then bike riding").
_OTHER_RIDE_RE = re.compile(
    r"\b(?:bike|bicycle|cycle|horse|horseback|boat|camel|pony|quad|jeep|tuk[-\s]?tuk)\s+(?:riding|rode|ridden)\b",
    re.IGNORECASE,
)
_SAFARI_VEHICLE_RE = re.compile(
    r"\b(?:jeeps?|4x4|4wd|vehicles?|trucks?|land\s*cruisers?|safari\s+cars?|game\s+drives?|open[-\s]top|by\s+car)\b",
    re.IGNORECASE,
)
_SAFARI_VEHICLE_BEFORE_RE = re.compile(r"\b(?:jeeps?|4x4|4wd|vehicle|truck)\s+$", re.IGNORECASE)
JEEP_SAFARI_COUNTRIES = frozenset({"sri lanka"})


def _clause_before(text: str, start: int) -> str:
    """The part of the clause before ``start`` (back to the last sentence / clause break)."""
    head = text[:start]
    cut = max((m.end() for m in _CLAUSE_BREAK_RE.finditer(head)), default=0)
    return head[cut:]


def _clause_after(text: str, end: int) -> str:
    """The part of the clause after ``end`` (up to the next sentence / clause break)."""
    m = _CLAUSE_BREAK_RE.search(text, end)
    return text[end:m.start() if m else len(text)]


def _is_negated(text: str, m: "re.Match[str]") -> bool:
    before = _clause_before(text, m.start())
    if _NEG_IDIOM_RE.search(before):
        return False
    if _NEG_ADJACENT_RE.search(before):
        return True
    return bool(_NEG_AFTER_RE.match(text[m.end():m.end() + 40]))


def _safari_is_a_ride(text: str, m: "re.Match[str]", country: Optional[str]) -> bool:
    before, after = _clause_before(text, m.start()), _clause_after(text, m.end())
    if _SAFARI_VEHICLE_RE.search(after) or _SAFARI_VEHICLE_BEFORE_RE.search(before):
        return False
    clause = _OTHER_RIDE_RE.sub(" ", before + " " + after)
    if _SAFARI_RIDE_CUE_RE.search(clause):
        return True
    return (country or "").strip().lower() not in JEEP_SAFARI_COUNTRIES


def riding_mentions(text: str, country: Optional[str] = None) -> list[str]:
    """Every elephant-riding mention in ``text`` that is not negated in its clause (the matched words).
    ``country`` (the tour's) decides a bare "elephant safari" (``JEEP_SAFARI_COUNTRIES``)."""
    out: list[str] = []
    text = text or ""
    for m in ELEPHANT_RIDING_RE.finditer(text):
        if _is_negated(text, m):
            continue
        if m.group("safari") and not _safari_is_a_ride(text, m, country):
            continue
        out.append(m.group(0))
    return out


def mentions_riding(text: str, country: Optional[str] = None) -> bool:
    return bool(riding_mentions(text, country))


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "\n".join(_as_text(v) for v in value)
    if isinstance(value, dict):
        return "\n".join(_as_text(v) for v in value.values())
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


# The fields read on a generated page / a T2 version / the Master text (graph `generated`, rewritten_content,
# t2_rewrite_job.source_tour_dict): every customer-facing text field. `seo_keywords_used` is metadata (lesson
# S218 AA-738: never scan the keyword list as content).
CONTENT_FIELDS = ("name", "subtitle", "summary", "description", "highlights", "itineraries", "seo_title",
                  "seo_meta", "trip_type")
# The raw source fields (grounding.SOURCE_FIELDS shape; raw_tours src_* columns). `exclusions` is not read: a ride
# listed there is a thing the tour does NOT include.
SOURCE_FIELDS = ("name", "subtitle", "summary", "description", "highlights", "itineraries", "inclusions",
                 "activities", "price")


def field_hits(doc: Optional[dict], fields: Iterable[str] = CONTENT_FIELDS,
               country: Optional[str] = None) -> list[dict]:
    """[{field, mention}] for every non-negated riding mention in ``doc``'s ``fields`` (first mention per field)."""
    hits: list[dict] = []
    for f in fields:
        found = riding_mentions(_as_text((doc or {}).get(f)), country)
        if found:
            hits.append({"field": f, "mention": found[0]})
    return hits


def scan(generated: Optional[dict] = None, source: Optional[dict] = None, *,
         country: Optional[str] = None) -> list[dict]:
    """Every hit over the generated / rewritten fields and the source fields:
    [{"where": "generated" | "source", "field", "mention"}]. Empty = no elephant riding anywhere.
    ``country`` defaults to the source's (then the page's) ``country`` key."""
    country = country or (source or {}).get("country") or (generated or {}).get("country")
    out = [{"where": "generated", **h} for h in field_hits(generated, CONTENT_FIELDS, country)]
    out += [{"where": "source", **h} for h in field_hits(source, SOURCE_FIELDS, country)]
    return out


def names_riding(text: Optional[str]) -> bool:
    """An audit issue / note that is about elephant RIDING (the detector's phrases, negated or not: an issue that
    says "elephant riding must not be promoted" is about the ride too). The animal alone is not riding ("wild
    elephant sightings", "an elephant sanctuary"); a bare "elephant safari" counts only with a ride cue in its
    clause (on an elephant, on the back, riding), whatever the country."""
    text = text or ""
    for m in ELEPHANT_RIDING_RE.finditer(text):
        if not m.group("safari"):
            return True
        clause = _OTHER_RIDE_RE.sub(" ", _clause_before(text, m.start()) + " " + _clause_after(text, m.end()))
        if _SAFARI_RIDE_CUE_RE.search(clause):
            return True
    return False


def other_product_truth_issues(issues: Optional[Iterable[Any]]) -> list[str]:
    """The audit issues that name a product-truth concern other than elephant riding (an issue that names the
    ride is the policy's; a voice / SEO / format issue names no concern)."""
    texts = [_as_text(i) for i in (issues or ()) if _as_text(i).strip()]
    return [t for t in texts if not names_riding(t) and _PRODUCT_TRUTH_ISSUE_RE.search(t)]


def is_policy_only_hold(brand_audit_status: Optional[str], brand_audit_codes: Iterable[str],
                        generated: Optional[dict] = None, source: Optional[dict] = None, *,
                        issues: Optional[Iterable[Any]] = None) -> bool:
    """A version held ONLY by this policy: brand audit manual_check, the detector hits the version text or its
    source, and no other manual-check reason is on it. Read by the approve gate (an unedited approve = the policy's
    human review), the re-validation gate (a human edit of an elephant tour is that review too) and the final
    contract. By codes:

    - ``POLICY_CODE`` alone: the policy.
    - ``POLICY_CODE`` + the LLM audit's ``FACT_CHECK_MANUAL_CHECK`` (since the split only the LLM emits that code):
      the policy only when the audit explained its fact check as the ride (at least one issue that names elephant
      riding, ``names_riding``) and no issue names another product-truth concern (``other_product_truth_issues``:
      "wild elephant sightings are not in the source" is another concern, "meta 162 chars" is none). An unexplained
      fact check (no riding issue) keeps the hold.
    - ``LEGACY_CODE`` alone (a version stored before AA-1047, when the detector added that code with no issue text):
      the pre-split rule, a detector hit is the policy, unless an issue names another product-truth concern.
    - anything else: not the policy alone.
    """
    if brand_audit_status != "manual_check" or not scan(generated, source):
        return False
    codes = set(brand_audit_codes or ())
    if codes == {POLICY_CODE}:
        return True
    if codes == {POLICY_CODE, LEGACY_CODE}:
        texts = [_as_text(i) for i in (issues or ())]
        return any(names_riding(t) for t in texts) and not other_product_truth_issues(issues)
    if codes == {LEGACY_CODE}:
        return not other_product_truth_issues(issues)
    return False


def _getter(row: Any):
    def get(key: str) -> Any:
        try:
            return row[key]
        except (KeyError, IndexError, TypeError):
            return None
    return get


def raw_source_from_row(row: Any) -> dict:
    """The raw-source part of a row that carries raw_tours' src_* columns under their own names (``src_name`` ...,
    ``inclusions``, ``activities``, ``price_raw``, ``country``), keyed like SOURCE_FIELDS (+ ``country``). Missing
    columns read as empty."""
    get = _getter(row)
    return {
        "name": get("src_name"), "subtitle": get("src_subtitle"), "summary": get("src_summary"),
        "description": get("src_description"), "highlights": get("src_highlights"),
        "itineraries": get("src_itineraries"), "inclusions": get("inclusions"), "activities": get("activities"),
        "price": get("price_raw"), "country": get("country"),
    }


def version_content_from_row(row: Any) -> dict:
    """The page text of a row that carries generated_content's aa_* / seo_* columns under their own names, keyed
    like CONTENT_FIELDS. Missing columns read as empty."""
    get = _getter(row)
    return {
        "name": get("aa_name"), "subtitle": get("aa_subtitle"), "summary": get("aa_summary"),
        "description": get("aa_description"), "highlights": get("aa_highlights"),
        "itineraries": get("aa_itineraries"), "seo_title": get("seo_title"), "seo_meta": get("seo_meta"),
    }
