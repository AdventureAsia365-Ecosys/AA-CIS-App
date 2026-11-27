"""AA-1187 — which existing tour an uploaded Excel row is (one rule for the Upload preview and Commit).

Before: the preview called a row "already exists" when any tour had the same Name, while Commit matched Name +
Provider only. A re-upload with a different Provider (a typo, another spelling, or a Provider filled in where the
tour had none: 43 live tours have no Provider, S295 data audit) was announced as a duplicate and then created a
second tour with a new key.

Now both paths call :func:`match_existing`, and a row is the existing tour with the same Name (case and outer spaces
ignored) when, in this order of preference:

1. the Provider is the same (the old key; the country may have been corrected since), else
2. the country is the same, or one side has no country (a Provider change never makes a new tour), else
3. there is no match: the same Name in another country is another tour.

Among several candidates of one rank the newest ingest wins (as before). A matched row goes to Duplicate Review
(``silver_aa_internal.upload_staging``), where the person chooses Merge (field by field, AA-1146), Bypass, or Restore
for a deleted tour. A row matched on Name + country with another Provider is also offered Create a new tour (two
operators really do sell tours of the same name, near_duplicate.py; the staging list's ``matched_other_provider``), and
its Merge never changes the Provider unless the person takes the sheet's value (the Provider is a conflict of the
merge plan, uploads.py ``_merge_inputs``). The in-file check (two rows of one file) keeps its Name + Provider key:
neither row is in the database yet.

The Provider stays an internal field (DB-01): it shows on the staff preview and Duplicate Review only.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

MATCH_SQL = """
SELECT rt.tour_id, rt.source_group_id, rt.src_name, rt.provider, rt.country,
       lower(trim(coalesce(rt.provider, ''))) = $3 AS same_provider,
       EXISTS (SELECT 1 FROM silver_aa_internal.raw_tours k
               WHERE COALESCE(k.source_group_id, k.tour_id) = COALESCE(rt.source_group_id, rt.tour_id)
                 AND k.source_status = 'active' AND k.deleted_at IS NULL) AS live,
       (SELECT max(k.deleted_at) FROM silver_aa_internal.raw_tours k
         WHERE COALESCE(k.source_group_id, k.tour_id) = COALESCE(rt.source_group_id, rt.tour_id)) AS key_deleted_at
FROM silver_aa_internal.raw_tours rt
WHERE rt.tenant_id = $1::uuid
  AND lower(trim(rt.src_name)) = $2
  AND (lower(trim(coalesce(rt.provider, ''))) = $3
       OR $4::text IS NULL OR rt.country IS NULL OR lower(trim(rt.country)) = $4)
ORDER BY (lower(trim(coalesce(rt.provider, ''))) = $3) DESC,
         (lower(trim(rt.country)) = $4) DESC NULLS LAST,
         rt.ingest_at DESC
LIMIT 1
"""


def _provider_key(value: Any) -> str:
    return str(value or "").lower().strip()


def same_provider(a: Any, b: Any) -> bool:
    """Two Provider values name the same operator as the match compares them (case and outer spaces ignored; an empty
    Provider is its own value). Duplicate Review uses it for a row matched on Name + country: it then offers Create a
    new tour, and a Merge asks before it changes the Provider (excel_merge ``ask``)."""
    return _provider_key(a) == _provider_key(b)


def match_key(row: Mapping[str, Any]) -> tuple[str, str, Optional[str]]:
    """(name, provider, country) as the match compares them; an empty country is None (unknown, matches any)."""
    name = str(row.get("src_name") or "").lower().strip()
    provider = _provider_key(row.get("provider"))
    country = str(row.get("country") or "").lower().strip() or None
    return name, provider, country


async def match_existing(conn: Any, tenant_id: str, row: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """The existing raw tour this row is (module docstring), or None for a new tour.

    The result carries ``tour_id``, ``source_group_id``, the matched ``src_name`` / ``provider`` / ``country``,
    ``same_provider``, ``live`` (the key still has a live version) and ``key_deleted_at``."""
    name, provider, country = match_key(row)
    if not name:
        return None
    return await conn.fetchrow(MATCH_SQL, tenant_id, name, provider, country)


def preview_message(match: Mapping[str, Any], row: Mapping[str, Any]) -> str:
    """The Upload preview's line for a matched row: what Duplicate Review will offer, and why it matched."""
    if not match.get("live") and match.get("key_deleted_at") is not None:
        when = match["key_deleted_at"].date().isoformat()
        return f"This tour was deleted on {when} — Duplicate Review will offer Restore or Create a new tour."
    if match.get("same_provider"):
        return "This tour already exists in the system."
    was = match.get("provider") or "none"
    now = row.get("provider") or "none"
    return (f"This tour already exists in the system with another provider ({was}; this row: {now}). "
            "Duplicate Review will offer Merge (the provider changes only if you choose it) or Create a new tour; "
            "nothing new is made without that choice.")
