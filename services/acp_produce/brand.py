"""
services.acp_produce.brand — shared brand-rubric fetch for every N7 LLM call
(E2/E3/E4/E5 writer/repair AND F9 judge), AA-404 writer-side wire.

`fetch_brand_rubric_text()` lived in `slot_runner.py` from AA-404 F9 fix #1
(PR #158) until this module existed — that fix wired real per-tenant brand
content (`shared.tenant_brand_rules`) into ONLY the F9 judge
(`gate_brand_seo_audit()`/`gate_brand_seo_audit_social()`, gates.py), because
`slot_runner.py` was already the one real caller wiring `brand_rubric_text`
through to `run_piece_through_produce_gates()`. AA-404's own F9 deep-dive
(`docs/implementation-notes/AA-404-F9-deep-dive.md`, TL;DR #1) confirmed this
was a real gap: the writer modules (`generation.py` E2, `adapt.py` E3,
`faq.py` E4, `repair.py` E5) kept hardcoding the generic
`AA_BRAND_IDENTITY_PROMPT` constant — so the judge started scoring against a
more specific, real per-tenant rubric while the writer kept aiming at the old,
vaguer generic target. This module is the fix: one fetch function every N7
module can import without a layering problem.

Why this couldn't just live in `generation.py` (or any other single writer
module) and be imported from there by the others: `slot_runner.py` already
imports `generation.py`, `adapt.py`, and `faq.py` directly (it's the
orchestrator that chains E1-E5). If any of those three needed to import
`fetch_brand_rubric_text` from one of the OTHERS (or from `slot_runner.py`
itself), that would either invert the orchestrator's own import direction or
create a cycle. A small shared leaf module — no dependents in this package,
only `services.content_generation.brand_standards` (the generic fallback
constant) and `asyncpg`/`structlog` — is the same fix shape AA-404 F9 fix #1
itself already reasoned through for the `admin_pipeline.py::_resolve_brand_
rule()` question ("api.routers.* is a higher layer than services.
acp_produce.*, importing downward would invert every other module's
dependency direction") — this module goes the other way: a genuinely shared
leaf sits BELOW every module that needs it, not inside one of its peers.

`slot_runner.py` still fetches this ONCE per slot (unchanged from F9 fix #1)
and threads the resulting `str` down as a plain parameter to E2/E3/E4/E5 —
none of those modules call `fetch_brand_rubric_text()` themselves. That keeps
the "1 DB fetch per slot, not per piece/module" property F9 fix #1 already
established, while still letting every module import the function directly
(for its own tests, or any future caller that isn't `slot_runner.py`)
without a cycle.
"""
from __future__ import annotations

import asyncpg
import structlog

from services.acp_shared.brand_pack import compile_rubric, fetch_brand_pack_conn

logger = structlog.get_logger()


async def fetch_brand_rubric_text(db: asyncpg.Connection, tenant_id: str) -> str:
    """AA-404 F9 fix #1 (originally `slot_runner.py`, moved here for the
    writer-side wire) — real per-tenant brand voice from
    `shared.tenant_brand_rules`, replacing the hardcoded
    `AA_BRAND_IDENTITY_PROMPT` constant every E2/E3/E4/E5/F9 call used before
    this fix (see `gates.py::gate_brand_seo_audit()`'s docstring for the full
    history of why that was the case up to F9 fix #1).

    Same default-brand resolution shape as
    `api/routers/admin_pipeline.py::_resolve_brand_rule()`'s no-`brand_
    identity_id`/no-`brand_name` branch (`tenant_id` + `brand_name = 'default'`
    + `is_active = true`) — reused, not reinvented; this codebase's
    multi-brand resolver (AA-198) already established this exact convention.
    A standalone query rather than importing that router-private function
    directly: `api.routers.*` is a higher layer than `services.acp_produce.*`
    (importing downward would invert the dependency direction every other
    module in this package already follows, and risks the same
    `api/__init__.py` import-cycle trap `admin_produce.py`'s own module
    docstring already documents for `slot_runner`).

    AA-771 (S233): an empty `system_prompt` no longer falls back to
    `AA_BRAND_IDENTITY_PROMPT` — the rubric head is compiled from the tenant's
    own T0 fields (`brand_pack.compile_rubric`); with no T0 brand content at
    all the judge gets `NEUTRAL_RUBRIC` (logged as a warning)."""
    # AA-771 (S233) — a tenant without a hand-written system_prompt is judged against a rubric
    # compiled from its own T0 fields, never against Adventure Asia's identity (the S232 F9
    # judge scored every pilot tenant against AA_BRAND_IDENTITY_PROMPT). No T0 data at all →
    # a neutral rubric that judges clarity and the CTA only.
    pack = await fetch_brand_pack_conn(db, tenant_id)
    head = pack.get("system_prompt") or compile_rubric(pack)
    if not head:
        logger.warning("brand_rubric_neutral", tenant_id=tenant_id,
                       reason="no active shared.tenant_brand_rules row with brand content")
        return NEUTRAL_RUBRIC

    parts = [head]
    if pack.get("style_guide"):
        parts.append(f"STYLE GUIDE:\n{pack['style_guide']}")
    if pack.get("forbidden_words"):
        parts.append("FORBIDDEN WORDS/PHRASES: " + ", ".join(pack["forbidden_words"]))
    if pack.get("good_examples"):
        parts.append(
            "GOOD EXAMPLES (real, on-brand — do not flag writing like this as generic):\n"
            f"{pack['good_examples']}"
        )
    return "\n\n".join(parts)


NEUTRAL_RUBRIC = (
    "BRAND IDENTITY — not set up for this tenant yet. Judge only clarity, specificity, honesty "
    "and whether the call to action is clear; do not mark the piece off-brand for its voice."
)


__all__ = ["fetch_brand_rubric_text", "NEUTRAL_RUBRIC"]
