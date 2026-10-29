"""
services.acp_content_writing.facts — AA-529 Facts Entry source for T9's content_seed builder.

A Facts Entry is a hand-written, sourced claim that doesn't live in a tour's own itinerary text
(reference price, travel season, visa/entry requirement, estimated transfer time between places,
etc.) — the 4th citable-source type `aa-social-media` (the reference repo, AA-525 Phần 8.2/11.B.3)
calls "Facts Entry", the one AA-CIS-App had no equivalent of before this. Confirmed real gap: a
piece opening on the claim "$100 in Laos goes further than you'd expect" failed F1_grounding on
both attempts and stayed held forever — the claim is true and useful, but PRICE is never in an
itinerary's own atom text, so there was never anything to cite it against (AA-529 Linear issue,
piece c771a4d5/7ca09d4b, tenant wanderlux-travel).

Architecture (Nghiep, 05/09/2026, AA-529 issue's own appended decision — HET TREO): TWO scopes,
not one flat table —
  - scope='platform': AA-admin writes ONE shared set of objective facts, usable by EVERY tenant
    writing about ANY trip (weather/season, visa, typical transfer times between places...).
  - scope='tenant': each tenant writes their OWN facts, visible ONLY to that tenant (their own
    pricing, their own cancellation/booking terms, deals specific to their business).
T9 always pulls ALL platform facts + the WRITING tenant's own tenant facts — never another
tenant's. A tenant with zero facts of their own still gets every platform fact; nothing here is
required before a tenant can write.

AA-814 (S257): the write side is services/acp_shared/facts_store.py (admin page /admin/facts for platform facts,
Content studio /v1/facts for a tenant's own). This module is the read side for the writers: expired facts are left
out in SQL, and pick_facts() keeps a tenant's fact over the platform facts on the same subject (kind, title,
country). Each seed entry names its source and owner.
"""
from __future__ import annotations

from uuid import UUID

from services.acp_shared.facts_store import subject_key

# S233 — facts.country (migration 220): a fact about one country is offered only to pieces about
# that country (the S232 Laos e-visa fact reached Tibet posts). NULL = any country. Replaces the
# per-fact Jev relevance call on the T axis (S232: no Jev on T).
# AA-814 (CIS 302): kind / valid_until — a fact after its valid_until never reaches a writer, and pick_facts() keeps
# the tenant's fact over the platform facts on the same subject.
_FACTS_QUERY = """
    SELECT fact_id, scope, tenant_id, kind, title, body, stated_on, valid_until, provenance, country, updated_at
    FROM acp_shared.facts
    WHERE (scope = 'platform' OR (scope = 'tenant' AND tenant_id = $1))
      AND (country IS NULL OR $2::text IS NULL OR lower(country) = lower($2::text))
      AND (valid_until IS NULL OR valid_until >= current_date)
    ORDER BY scope, stated_on DESC NULLS LAST, fact_id
"""


async def fetch_facts_for_writing(tenant_id: UUID, pool, country: str | None = None) -> list[dict]:
    """ALL scope='platform' facts + the given tenant's own scope='tenant' facts — never another
    tenant's (the `scope = 'tenant' AND tenant_id = $1` half of the WHERE is what enforces that; a
    platform fact matches on `scope = 'platform'` alone, regardless of $1). `tenant_id` is passed
    as the real UUID object (not str()) — it's compared against `acp_shared.facts.tenant_id`, a
    UUID column, with no other type ambiguity on the same placeholder (unlike the fetch_review_list
    "uuid = text" bug this codebase already hit once, AA-501's own live-verify finding — this
    query never reuses $1 against a text column). Empty list is a normal, common outcome (no facts
    written yet for anyone) — never an error, same soft-fail convention every other optional T9
    signal (DFS keyword, brand rubric) already follows."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(_FACTS_QUERY, tenant_id, country)
    return pick_facts([dict(r) for r in rows])


def _rank(fact: dict) -> tuple:
    """Sort key, best first: a tenant fact, then the latest stated_on, then the latest edit, then the id."""
    stated = fact.get("stated_on")
    edited = fact.get("updated_at")
    return (0 if fact.get("scope") == "tenant" else 1,
            -(stated.toordinal()) if stated else 0,
            -(edited.timestamp()) if edited else 0.0,
            str(fact.get("fact_id")))


def pick_facts(facts: list[dict]) -> list[dict]:
    """AA-814 precedence: one fact per subject (facts_store.subject_key: kind, title, country). When the writing
    tenant has its own fact on a subject, the platform facts on that subject are dropped; within one scope the latest
    stated (then latest edited) fact wins. The input order is kept for the facts that stay, so the seed reads as
    before for a tenant with no overrides."""
    best: dict[tuple, dict] = {}
    for f in facts:
        key = subject_key(f)
        if key not in best or _rank(f) < _rank(best[key]):
            best[key] = f
    kept = {id(f) for f in best.values()}
    return [f for f in facts if id(f) in kept]


def format_facts_block(facts: list[dict]) -> str:
    """Renders fetched facts as a labeled block prompts.py can append to the CONTENT SEED, one
    `[Fact id=<fact_id>]` per entry — same labeling shape service.py::_fetch_route_segments()'s
    `[Moment id=<id>]` already uses for atoms/Segments, so the model sees one consistent citation
    pattern rather than two unrelated ones. Returns "" (not None) for an empty list — callers can
    safely do `if facts_text:` without a None-check."""
    if not facts:
        return ""
    return "\n\n".join(_fact_entry(f) for f in facts)


def _fact_entry(f: dict) -> str:
    """AA-814: the entry also names its source and whose fact it is ("agency fact" = the writing tenant's own,
    "platform fact" = AA's), so the seed shows where a cited claim comes from. The head line stays exactly
    `[Fact id=<id>]`: claim_rules, hygiene, sentence_repair and service._FACT_BLOCK_RE parse it."""
    entry = f"[Fact id={f['fact_id']}]\n{f['title']}: {f['body']}"
    source = " ".join(str(f.get("provenance") or "").split())
    if f.get("scope") in ("tenant", "platform") or source:
        owner = {"tenant": "agency fact", "platform": "platform fact"}.get(f.get("scope"))
        label = "; ".join(p for p in (source, owner) if p)
        entry += f"\nSource: {label}"
    return entry


__all__ = ["fetch_facts_for_writing", "format_facts_block", "pick_facts"]
