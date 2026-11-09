"""AA-1039 R6 slice 2 (root ADR 0019 d21, clarifications TL-21): the AA-subject gate of a tenant content piece.

A rule check, no LLM ($0). It runs when T9 finishes a piece (stage ``write``, services/acp_content_writing/service.py
``_finalize_piece``) and again right before a publish (stage ``publish``, api/routers/v1_publish.py), and checks:

1. **An AA subject.** The piece's angle-gate request is about a served AA tour (its trip, else its atom's tour, the
   tour of ``shared.tour_liveness.REQUEST_TOURS_SQL``), a live AA atom (``acp_contract.v_active_tour_atoms``) or a
   Slate Segment with a live member. Brand content passes without one when it asks nobody to buy: no sales link and
   a goal outside :data:`SALES_GOALS`.
2. **Sales links point to AA.** A sales link (:func:`is_sales_link`: the request's CTA, a booking word in the link's
   text or path, or a product word with a commercial tour host or a product listing path) must go to an AA tour page
   (a trip page ``/trip/<served tour id>`` on a trip page host, one of the tenant's own pages of an AA tour in
   ``acp_deliver.tenant_tour_pages``, or an Adventure Asia host) or to the tenant's TripPlanner site
   (:func:`tripplanner_hosts`). Any other link (a museum, a map, a social profile) passes: d21 limits sales links
   only. Links are read from the HTML the publisher sends (:func:`extract_links`).

A piece that fails gets one open ``subject_gate`` row in ``acp_shared.content_compliance_flag`` (CIS 360): admin
Tenant content QA › Compliance lists it, and the publish route refuses it with 409 and the row's ``tenant_message``
(content-style English, no internal code). A later pass of the gate resolves the row (``passed``); staff may release
it for the exact text (``released`` with the text's sha256), and the gate then lets that text through.

Every DB read runs on the CIS main pool (cis_app): the flag table has no tenant-pool grant and the Segment tables are
platform tables. Before CIS 360 is applied the flag writes are skipped (UndefinedTable), the verdict still holds.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Iterable, Optional
from urllib.parse import urlparse

import asyncpg
import markdown
import structlog

from shared import tour_liveness as _tl

logger = structlog.get_logger()

KIND = "subject_gate"
NO_AA_SUBJECT = "no_aa_subject"
OFF_CATALOG_LINK = "off_catalog_link"

# Goals that ask the reader to buy (services/acp_angle_gate/goals.py keys): a piece with one of them is never brand
# content, so it needs an AA subject even without a link.
SALES_GOALS = frozenset({"promotion", "lead_generation", "conversion"})

# A link reads as a sales link when its text (the anchor text, or the words just before a bare URL) or its URL path
# has a booking word, or a product word together with a commercial host or a product listing path (``/tours/<x>``).
# A product word alone ("guided tours of the museum", "/blog/best-trips") is not a sales link.
_BOOKING_WORDS = re.compile(
    r"\b(book(?:ing|ings|ed)?|buy|purchase|reserv(?:e|ation|ations)|enquir(?:e|y|ies)|inquir(?:e|y|ies)|quote"
    r"|pric(?:e|es|ing)|deals?|checkout|cart)\b",
    re.IGNORECASE,
)
_PRODUCT_WORDS = re.compile(
    r"\b(tours?|trips?|packages?|holidays?|vacations?|itinerar(?:y|ies)|offers?|excursions?|cruises?)\b",
    re.IGNORECASE,
)
# Hosts that sell tours (an online travel agency or a tour marketplace): a product word to one of them is a sales link.
# Env COMMERCIAL_TOUR_HOSTS adds more.
DEFAULT_COMMERCIAL_HOSTS = (
    "viator.com", "getyourguide.com", "klook.com", "tripadvisor.com", "booking.com", "expedia.com", "agoda.com",
    "airbnb.com", "trip.com", "kkday.com", "tiqets.com", "musement.com", "headout.com", "civitatis.com",
    "traveloka.com", "tourradar.com", "intrepidtravel.com", "gadventures.com", "bookmundi.com", "withlocals.com",
)
# A bare URL anywhere (also right after "(", a quote or "="). Text and the raw piece are scanned with it.
_BARE_URL = re.compile(r"(?<![\w])(https?://[^\s<>\"')\]]{1,2000})", re.IGNORECASE)
# A link written without a scheme: "www.host.tld[/path]" or "host.tld/path" (Facebook and WordPress turn these into
# links). Not inside a longer host, a path, a query or an e-mail address.
_HOST = r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}"
_SCHEMELESS = re.compile(
    rf"(?<![\w@.\-/:=?&#%+~])((?:www\.{_HOST}(?:/[^\s<>\"')\]]{{0,2000}})?)|(?:{_HOST}/[^\s<>\"')\]]{{0,2000}}))",
    re.IGNORECASE)
_SCHEMELESS_HREF = re.compile(rf"^(?:www\.{_HOST}(?:[/?#].*)?|{_HOST}[/?#].*)$", re.IGNORECASE | re.DOTALL)
_SCHEME = re.compile(r"^([a-z][a-z0-9+.\-]*):", re.IGNORECASE)
# Block tags end a line of text (the words before a bare URL are read on its own line).
_BLOCK_TAGS = frozenset({"p", "br", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "td", "th",
                         "table", "blockquote", "pre", "hr", "dt", "dd", "section", "article"})
_CONTEXT_CHARS = 80
_SENTENCE_END = re.compile(r"[.!?](?:\s|$)")
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_TRIP_PATH = re.compile(rf"^/trip/({_UUID})/?$")
_TRAILING = ".,;:!?"

# Adventure Asia's own hosts (an AA tour page there is always allowed). Env AA_SALES_LINK_HOSTS adds more.
DEFAULT_AA_HOSTS = ("adventure.asia",)
# The hosts trip pages are served on (api/routers/v1_trip_page.py TRIP_PAGE_BASE_URL, the admin host and the CRM
# client host it 307s to). Env TRIP_PAGE_HOSTS adds more.
DEFAULT_TRIP_PAGE_HOSTS = ("aa-cis.lumiguides.it.com", "go-dev.lumiguides.it.com")
# The TripPlanner site (one platform site today: the Dev custom domain, served by ECS behind CloudFront; the
# per-tenant white-label site of ADR 0019 R5 adds the tenant's own host here). Env TRIPPLANNER_SITE_HOSTS adds more.
# The former Vercel host left with the Vercel project (AA-1038): a retired *.vercel.app name can be claimed by anyone,
# so it must never stay an allowed sales target.
DEFAULT_TRIPPLANNER_HOSTS = ("tripplanner.lumiguides.it.com",)

_MAX_LINKS = 200


def _host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _env_hosts(name: str) -> tuple[str, ...]:
    return tuple(h.strip().lower().removeprefix("www.") for h in (os.environ.get(name) or "").split(",") if h.strip())


def aa_hosts() -> frozenset[str]:
    return frozenset(DEFAULT_AA_HOSTS + _env_hosts("AA_SALES_LINK_HOSTS"))


def trip_page_hosts() -> frozenset[str]:
    from api.routers.v1_trip_page import trip_page_base_url
    return frozenset(DEFAULT_TRIP_PAGE_HOSTS + _env_hosts("TRIP_PAGE_HOSTS") + (_host(trip_page_base_url()),))


def tripplanner_hosts() -> frozenset[str]:
    extra = ()
    revalidate = os.environ.get("TRIPPLANNER_REVALIDATE_URL")
    if revalidate:
        extra = (_host(revalidate),)
    return frozenset(h for h in DEFAULT_TRIPPLANNER_HOSTS + _env_hosts("TRIPPLANNER_SITE_HOSTS") + extra if h)


def _host_in(host: str, hosts: Iterable[str]) -> bool:
    return any(host == h or host.endswith("." + h) for h in hosts)


def _norm_url(url: str) -> str:
    p = urlparse(url.strip())
    return f"{_host(url)}{(p.path or '/').rstrip('/') or '/'}"


@dataclass(frozen=True)
class Link:
    url: str
    text: str = ""
    cta: bool = False


def commercial_hosts() -> frozenset[str]:
    return frozenset(DEFAULT_COMMERCIAL_HOSTS + _env_hosts("COMMERCIAL_TOUR_HOSTS"))


def resolve_href(href: Optional[str]) -> Optional[str]:
    """The absolute http(s) URL a browser opens for an ``href`` (entities already decoded by the HTML parser), or None
    for a link that cannot leave the tenant's page (relative, ``#``, ``mailto:``, ``tel:``, ...). Like a browser:
    tabs and newlines are dropped, ``\\`` counts as ``/``, the scheme is case-blind, ``//host`` and ``https:host``
    get ``https://``, and a scheme-less ``www.host`` / ``host.tld/path`` is read as a link to that host."""
    if href is None:
        return None
    h = "".join(ch for ch in href if ch not in "\t\n\r").strip(" \x00\x0b\x0c")
    if not h:
        return None
    m = _SCHEME.match(h)
    if m:
        scheme = m.group(1).lower()
        if scheme not in ("http", "https"):
            return None
        rest = h[m.end():].replace("\\", "/")
        url = f"{scheme}://{rest.lstrip('/')}"
    else:
        h2 = h.replace("\\", "/")
        if h2.startswith("//"):
            url = "https://" + h2.lstrip("/")
        elif _SCHEMELESS_HREF.match(h2):
            url = "https://" + h2
        else:
            return None
    try:
        return url if urlparse(url).hostname else None
    except ValueError:
        return None


class _Page(HTMLParser):
    """The anchors (resolved href, visible text) and the text outside them of the rendered piece."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.anchors: list[tuple[Optional[str], str]] = []
        self.text: list[str] = []
        self._open: list[tuple[Optional[str], list]] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = next((v for k, v in attrs if k == "href"), None)   # the first href wins, as in a browser
            self._open.append((href, []))
        elif tag in _BLOCK_TAGS:
            self._data("\n")

    def handle_startendtag(self, tag, attrs):
        if tag == "a":
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)
        elif tag in _BLOCK_TAGS:
            self._data("\n")

    def handle_endtag(self, tag):
        if tag == "a" and self._open:
            href, parts = self._open.pop()
            self.anchors.append((href, " ".join("".join(parts).split())))
        elif tag in _BLOCK_TAGS:
            self._data("\n")

    def handle_data(self, data):
        self._data(data)

    def _data(self, data):
        if self._open:
            self._open[-1][1].append(data)
        else:
            self.text.append(data)

    def close(self):
        super().close()
        while self._open:                   # an <a> never closed still links (a browser closes it at the end)
            self.handle_endtag("a")


def rendered_html(text: str) -> str:
    """The HTML the publisher sends: the same markdown render as services/acp_content_writing/export.py
    (``extra`` + ``nl2br``) for WordPress; Facebook gets the raw text, which :func:`extract_links` also reads."""
    return markdown.markdown(text or "", extensions=["extra", "nl2br"])


def _context(text: str, start: int) -> str:
    """The words just before a bare URL: its own line, at most :data:`_CONTEXT_CHARS`, from the last sentence end."""
    line = text[:start].rsplit("\n", 1)[-1][-_CONTEXT_CHARS:]
    ends = list(_SENTENCE_END.finditer(line))
    return " ".join((line[ends[-1].end():] if ends else line).split())


def _plain_urls(text: str) -> list[tuple[str, str]]:
    """The bare and scheme-less URLs of `text` with the words before each (a scheme-less one gets ``https://``)."""
    out, spans = [], []
    for m in _BARE_URL.finditer(text):
        out.append((m.group(1).rstrip(_TRAILING), _context(text, m.start(1))))
        spans.append(m.span(1))
    for m in _SCHEMELESS.finditer(text):
        if not any(a <= m.start(1) < b for a, b in spans):
            out.append(("https://" + m.group(1).rstrip(_TRAILING), _context(text, m.start(1))))
    return [(url, ctx) for url, ctx in ((resolve_href(u), c) for u, c in out) if url]


def extract_links(text: str, cta: Optional[str] = None) -> list[Link]:
    """Every link of the piece, read the way it is published: the anchors of the rendered HTML (markdown inline,
    titled, angle-bracket and reference links, raw ``<a href>`` quoted or not, entities decoded, ``//host`` and
    scheme-less hrefs resolved), the bare and scheme-less URLs of its text and of the raw piece (Facebook posts the raw
    text and links bare URLs), plus the request's CTA links. One entry per URL; an anchor's text wins over the words
    before a bare repeat."""
    text = text or ""
    found: dict[str, Link] = {}
    page = _Page()
    try:
        page.feed(rendered_html(text))
        page.close()
    except Exception:  # noqa: BLE001 — a parser error never hides a link: the raw text is still scanned below
        logger.warning("subject_gate_html_parse_failed", exc_info=True)
    for href, anchor in page.anchors:
        url = resolve_href(href)
        if url:
            found.setdefault(url, Link(url, anchor))
    for source in ("".join(page.text), text):
        for url, ctx in _plain_urls(source):
            found.setdefault(url, Link(url, ctx))
    for url, ctx in _plain_urls(cta or ""):
        found[url] = Link(url, (found.get(url) or Link(url, ctx)).text, cta=True)
    return list(found.values())[:_MAX_LINKS]


def _product_listing(path: str) -> bool:
    """A product word as a directory with a page under it: ``/tours/hanoi-food-walk``, ``/packages/x/``."""
    segments = [seg for seg in path.split("/") if seg]
    return any(_PRODUCT_WORDS.fullmatch(seg) for seg in segments[:-1])


def is_sales_link(link: Link) -> bool:
    """d21's sales link: the request's CTA; a booking word in the link text or path; or a product word (tour, trip,
    package, ...) together with a commercial host or a product listing path. A museum's "/visit/tours" page or a blog
    article "/blog/best-trips" is not one."""
    if link.cta:
        return True
    raw_path = urlparse(link.url).path or ""
    path = raw_path.replace("-", " ").replace("_", " ").replace("/", " ")
    words = f"{link.text or ''} {path}"
    if _BOOKING_WORDS.search(words):
        return True
    if not _PRODUCT_WORDS.search(words):
        return False
    return _host_in(_host(link.url), commercial_hosts()) or _product_listing(raw_path)


@dataclass(frozen=True)
class AllowedTargets:
    aa_hosts: frozenset = frozenset()
    trip_page_hosts: frozenset = frozenset()
    tripplanner_hosts: frozenset = frozenset()
    tenant_pages: frozenset = frozenset()    # normalised host+path of the tenant's pages of served AA tours
    served_trip_ids: frozenset = frozenset()  # tour ids the linked trip pages name that are served


def trip_page_tour_id(url: str, targets: AllowedTargets) -> Optional[str]:
    if not _host_in(_host(url), targets.trip_page_hosts):
        return None
    m = _TRIP_PATH.match(urlparse(url).path or "")
    if not m:
        return None
    try:   # only a real uuid reaches the served-state read ($1::uuid[]); anything else is not a trip page
        return str(uuid.UUID(m.group(1)))
    except ValueError:
        return None


def allowed_sales_target(url: str, targets: AllowedTargets) -> bool:
    host = _host(url)
    if not host:
        return False
    if _host_in(host, targets.aa_hosts) or _host_in(host, targets.tripplanner_hosts):
        return True
    tid = trip_page_tour_id(url, targets)
    if tid is not None:
        return tid in targets.served_trip_ids
    return _norm_url(url) in targets.tenant_pages


@dataclass
class GateResult:
    ok: bool
    codes: list = field(default_factory=list)
    tenant_message: str = ""
    detail: dict = field(default_factory=dict)


def tenant_message(codes: list, refused_hosts: list) -> str:
    """The sentence the tenant reads (content words; never "flag", "QA" or a code)."""
    parts = []
    if OFF_CATALOG_LINK in codes:
        where = f" ({', '.join(refused_hosts[:3])})" if refused_hosts else ""
        parts.append(f"This piece links to a tour that isn't in the Adventure Asia catalogue{where}. Point the "
                     "booking link to an Adventure Asia tour page or your TripPlanner site to publish it.")
    if NO_AA_SUBJECT in codes:
        parts.append("This piece isn't about a tour or place in the Adventure Asia catalogue, and it asks readers to "
                     "book. Base it on an Adventure Asia tour, or remove the booking link, to publish it.")
    return " ".join(parts)


def evaluate(text: str, *, goal: Optional[str], cta: Optional[str], subject_ok: bool,
             targets: AllowedTargets) -> GateResult:
    """The pure rule (no DB): `subject_ok` from :func:`load_subject`, `targets` from :func:`load_targets`."""
    links = extract_links(text, cta)
    sales = [link for link in links if is_sales_link(link)]
    refused = [link for link in sales if not allowed_sales_target(link.url, targets)]
    codes = []
    if refused:
        codes.append(OFF_CATALOG_LINK)
    brand_content = not sales and (goal or "") not in SALES_GOALS
    if not subject_ok and not brand_content:
        codes.append(NO_AA_SUBJECT)
    refused_hosts = sorted({_host(link.url) for link in refused})
    detail = {"links": [link.url for link in links][:50], "sales_links": [link.url for link in sales][:50],
              "refused_links": [link.url for link in refused][:50], "refused_hosts": refused_hosts,
              "subject_ok": subject_ok, "brand_content": brand_content and not subject_ok, "goal": goal}
    return GateResult(not codes, codes, tenant_message(codes, refused_hosts) if codes else "", detail)


def content_sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


# ── DB reads (main pool) ─────────────────────────────────────────────────────────────────────────────────────────────

# AA-1057 GAP 1: `segment_live` is the Subject rule of shared.tour_liveness (a served member of the Segment, a merged
# id read on its live id), the same fragment every other liveness gate on a request uses.
SUBJECT_SQL = f"""
    SELECT r.goal, r.cta, r.atom_id, COALESCE(r.trip_id, ta.tour_id)::text AS tour_id,
           EXISTS (SELECT 1 FROM acp_contract.v_active_tour_atoms v WHERE v.atom_id = r.atom_id) AS atom_live,
           {_tl.segment_served_member_sql('r')} AS segment_live
      FROM acp_shared.angle_gate_request r
      LEFT JOIN acp_contract.tour_atoms ta ON ta.atom_id = r.atom_id
     WHERE r.request_id = $1::uuid AND r.tenant_id = $2::uuid
"""

TENANT_PAGES_SQL = """
    SELECT p.url, p.tour_id::text AS tour_id
      FROM acp_deliver.tenant_tour_pages p
     WHERE p.tenant_id = $1::text
"""

PIECE_SQL = """
    SELECT cp.piece_id::text AS piece_id, cp.tenant_id::text AS tenant_id, cp.content_text, cp.status,
           cp.angle_gate_request_id::text AS request_id, COALESCE(cp.channel, r.channel) AS channel
      FROM acp_shared.content_piece cp
      JOIN acp_shared.angle_gate_request r ON r.request_id = cp.angle_gate_request_id
     WHERE cp.piece_id = $1::uuid
"""


@dataclass(frozen=True)
class Subject:
    goal: Optional[str] = None
    cta: Optional[str] = None
    tour_id: Optional[str] = None
    ok: bool = False


async def load_subject(conn, request_id: str, tenant_id: str) -> Subject:
    row = await conn.fetchrow(SUBJECT_SQL, request_id, tenant_id)
    if row is None:
        return Subject()
    tour_ok = False
    if row["tour_id"]:
        state = (await _tl.served_states(conn, [row["tour_id"]])).get(row["tour_id"])
        tour_ok = state is not None and state.state == "served"
    return Subject(row["goal"], row["cta"], row["tour_id"], bool(tour_ok or row["atom_live"] or row["segment_live"]))


async def load_targets(conn, tenant_id: str, links: list[Link]) -> AllowedTargets:
    base = AllowedTargets(aa_hosts(), trip_page_hosts(), tripplanner_hosts())
    trip_ids = sorted({t for t in (trip_page_tour_id(link.url, base) for link in links) if t})
    page_rows = await conn.fetch(TENANT_PAGES_SQL, str(tenant_id))
    states = await _tl.served_states(conn, trip_ids + [r["tour_id"] for r in page_rows])
    served = {t for t, s in states.items() if s.state == "served"}
    pages = frozenset(_norm_url(r["url"]) for r in page_rows if r["tour_id"] in served)
    return AllowedTargets(base.aa_hosts, base.trip_page_hosts, base.tripplanner_hosts, pages,
                          frozenset(t for t in trip_ids if t in served))


async def check(conn, *, tenant_id: str, request_id: str, text: str) -> GateResult:
    subject = await load_subject(conn, request_id, tenant_id)
    targets = await load_targets(conn, tenant_id, extract_links(text, subject.cta))
    result = evaluate(text, goal=subject.goal, cta=subject.cta, subject_ok=subject.ok, targets=targets)
    result.detail["tour_id"] = subject.tour_id
    result.detail["content_sha256"] = content_sha256(text)
    return result


# ── the flag rows (CIS 360) ──────────────────────────────────────────────────────────────────────────────────────────

RELEASED_SQL = f"""
    SELECT 1 FROM acp_shared.content_compliance_flag
     WHERE piece_id = $1::uuid AND kind = '{KIND}' AND resolution = 'released'
       AND detail ->> 'content_sha256' = $2
     LIMIT 1
"""

OPEN_FLAG_SQL = f"""
    SELECT flag_id::text AS flag_id, tenant_message
      FROM acp_shared.content_compliance_flag
     WHERE piece_id = $1::uuid AND kind = '{KIND}' AND state = 'open'
"""

UPSERT_FLAG_SQL = f"""
    INSERT INTO acp_shared.content_compliance_flag
        (tenant_id, kind, stage, piece_id, tour_id, channel, codes, detail, tenant_message)
    VALUES ($1::uuid, '{KIND}', $2, $3::uuid, $4::uuid, $5, $6::text[], $7::jsonb, $8)
    ON CONFLICT (piece_id) WHERE kind = '{KIND}' AND state = 'open'
    DO UPDATE SET stage = EXCLUDED.stage, codes = EXCLUDED.codes, detail = EXCLUDED.detail,
                  tenant_message = EXCLUDED.tenant_message, tour_id = EXCLUDED.tour_id, updated_at = now()
    RETURNING flag_id::text AS flag_id
"""

RESOLVE_FLAG_SQL = f"""
    UPDATE acp_shared.content_compliance_flag
       SET state = 'resolved', resolution = $2, resolved_at = now(), resolved_by = $3, updated_at = now()
     WHERE piece_id = $1::uuid AND kind = '{KIND}' AND state = 'open'
    RETURNING flag_id::text AS flag_id
"""


def _uuid_or_none(value) -> Optional[str]:
    return str(value) if value else None


async def record(conn, *, piece: dict, result: GateResult, stage: str, actor: str) -> Optional[str]:
    """Write the verdict: an open flag for a failing piece (one per piece, updated in place), the open flag resolved
    as ``passed`` for a passing one. Returns the open flag id, or None. Skipped (None) before CIS 360."""
    try:
        if result.ok:
            await conn.fetch(RESOLVE_FLAG_SQL, piece["piece_id"], "passed", actor[:128])
            return None
        return await conn.fetchval(
            UPSERT_FLAG_SQL, piece["tenant_id"], stage, piece["piece_id"], _uuid_or_none(result.detail.get("tour_id")),
            piece.get("channel"), result.codes, json.dumps(result.detail, default=str), result.tenant_message)
    except asyncpg.UndefinedTableError:
        logger.info("subject_gate_flags_unavailable", piece_id=piece["piece_id"])
        return None


async def released(conn, piece_id: str, text: str) -> bool:
    try:
        return bool(await conn.fetchval(RELEASED_SQL, piece_id, content_sha256(text)))
    except asyncpg.UndefinedTableError:
        return False


async def run(conn, *, piece: dict, stage: str, actor: str) -> GateResult:
    """Check one piece and record the verdict (the write stage and the publish stage). `piece` carries piece_id,
    tenant_id, request_id, content_text and channel. A text staff released passes as is."""
    text = piece.get("content_text") or ""
    if await released(conn, piece["piece_id"], text):
        return GateResult(True, detail={"released": True})
    result = await check(conn, tenant_id=piece["tenant_id"], request_id=piece["request_id"], text=text)
    await record(conn, piece=piece, result=result, stage=stage, actor=actor)
    logger.info("subject_gate", piece_id=piece["piece_id"], tenant_id=piece["tenant_id"], stage=stage, ok=result.ok,
                codes=result.codes)
    return result


async def run_for_piece(pool, piece_id: str, *, stage: str = "write", actor: str = "system:subject_gate") \
        -> Optional[GateResult]:
    """The write-stage hook: best-effort, never raises (a finished piece must not fail because the gate did)."""
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(PIECE_SQL, piece_id)
            if row is None or row["status"] not in ("approved", "held"):
                return None
            return await run(conn, piece=dict(row), stage=stage, actor=actor)
    except Exception:  # noqa: BLE001 — logged; the publish stage checks again
        logger.warning("subject_gate_write_failed", piece_id=str(piece_id), exc_info=True)
        return None


async def open_messages(conn, piece_ids: list[str]) -> dict[str, str]:
    """{piece_id: tenant_message} of the pieces with an open gate flag (the Publish list shows why one waits)."""
    if not piece_ids:
        return {}
    try:
        rows = await conn.fetch(f"""
            SELECT piece_id::text AS piece_id, tenant_message FROM acp_shared.content_compliance_flag
             WHERE piece_id = ANY($1::uuid[]) AND kind = '{KIND}' AND state = 'open'""", piece_ids)
    except asyncpg.UndefinedTableError:
        return {}
    out = {}
    for r in rows:
        r = dict(r)
        if r.get("piece_id") and isinstance(r.get("tenant_message"), str):
            out[str(r["piece_id"])] = r["tenant_message"]
    return out


class PublishHeld(Exception):
    """The publish stage refused the piece: 409 with the content-style sentence."""

    status_code = 409
    code = "content_needs_change"

    def __init__(self, result: GateResult):
        super().__init__(result.tenant_message)
        self.result = result

    @property
    def detail(self) -> str:
        return self.result.tenant_message


__all__ = [
    "AllowedTargets", "GateResult", "KIND", "Link", "NO_AA_SUBJECT", "OFF_CATALOG_LINK", "PublishHeld",
    "SALES_GOALS", "allowed_sales_target", "check", "content_sha256", "evaluate", "extract_links", "is_sales_link",
    "load_subject", "load_targets", "open_messages", "record", "run", "run_for_piece", "tenant_message",
]
