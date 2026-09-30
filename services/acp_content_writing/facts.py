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

No write-side service function here on purpose — this issue is schema/backend-integration only,
UI (both an Admin page for platform facts and a Tenant page for their own) is deferred to the
upcoming Admin/Tenant redesign epic per the issue's own explicit instruction. The one real row
needed for this build's own live-verify was inserted directly via SQL (S3-mediated ECS exec
pattern), not through any app code path.
"""
from __future__ import annotations

from uuid import UUID

_FACTS_QUERY = """
    SELECT fact_id, scope, tenant_id, title, body, stated_on, provenance
    FROM acp_shared.facts
    WHERE scope = 'platform' OR (scope = 'tenant' AND tenant_id = $1)
    ORDER BY scope, stated_on DESC NULLS LAST, fact_id
"""


async def fetch_facts_for_writing(tenant_id: UUID, pool) -> list[dict]:
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
        rows = await conn.fetch(_FACTS_QUERY, tenant_id)
    return [dict(r) for r in rows]


def format_facts_block(facts: list[dict]) -> str:
    """Renders fetched facts as a labeled block prompts.py can append to the CONTENT SEED, one
    `[Fact id=<fact_id>]` per entry — same labeling shape service.py::_fetch_route_segments()'s
    `[Moment id=<id>]` already uses for atoms/Segments, so the model sees one consistent citation
    pattern rather than two unrelated ones. Returns "" (not None) for an empty list — callers can
    safely do `if facts_text:` without a None-check."""
    if not facts:
        return ""
    return "\n\n".join(
        f"[Fact id={f['fact_id']}]\n{f['title']}: {f['body']}" for f in facts
    )


# AA-700 T9-1 — every platform fact + the tenant's own facts go into every writer prompt. Jev asks per
# fact whether it belongs in *this* post; an enforced, confident no leaves it out. Shadow/grey/error
# keep it. Tenant content → asked only for Jev allow-listed tenants (design C2).
T9_STAGE = "t9_facts"
T9_Q = "t9_fact_relevant"
_T9_CONCURRENCY = 4


async def select_relevant_facts(facts: list[dict], *, moment: str, angle: dict, trip: str | None,
                                tenant_id) -> tuple[list[dict], int]:
    """Returns (facts to inject, how many Jev left out). Never raises."""
    if not facts:
        return facts, 0
    import asyncio
    import hashlib

    from shared.llm_client.decide import decide

    angle_state = {"name": (angle or {}).get("name", ""), "why_it_works": (angle or {}).get("why_it_works", "")}
    ctx = hashlib.md5(f"{moment}|{angle_state['name']}|{angle_state['why_it_works']}|{trip}".encode()).hexdigest()[:12]
    sem = asyncio.Semaphore(_T9_CONCURRENCY)

    async def one(f: dict):
        state = {"moment": moment[:1500], "angle": angle_state, "trip": trip or "",
                 "fact": {"title": f.get("title") or "", "body": f.get("body") or ""}}
        async with sem:
            d = await decide(T9_STAGE, f"t9:{f.get('fact_id')}:{ctx}", state, [T9_Q], tenant_id=str(tenant_id))
        return d.rejected(T9_Q)

    try:
        rejected = await asyncio.gather(*[one(f) for f in facts])
    except Exception:
        return facts, 0
    kept = [f for f, r in zip(facts, rejected) if not r]
    return kept, len(facts) - len(kept)


__all__ = ["fetch_facts_for_writing", "format_facts_block", "select_relevant_facts"]
