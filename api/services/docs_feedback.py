"""AA-1133 — page feedback of the /admin/docs reader (tech lead S283 c-s283-docs-feedback: a CIS table).

``acp_shared.docs_feedback`` (CIS 454) holds the latest "Was this page helpful?" answer and optional note of one staff
member on one page: one row per (repo, path, actor), a new answer replaces the old one. The reader's POST writes it
for a page the manifest lists for the caller (an owner-only page is a 404 for everyone else, as in the reader); the
admin view reads the per-page counts, newest first, with the latest notes, and lists only pages the admin may see.

actor is the audit actor ``admin:<admin_users.id>`` (or ``machine`` for the admin secret without a person), never an
e-mail. The note is pii:free_text (DM-6.1); 12-digit runs are masked on the way in, like every docs text.
Retention: proposed 24 months, no deletion job yet (root data standards Part B section 7).
"""
from __future__ import annotations

from typing import Any, Optional

from api.services import docs_reader

COMMENT_CHARS = 1000      # technical-constant: the column check of CIS 454 (docs_feedback_comment_check)
NOTES_PER_PAGE = 5        # technical-constant: latest notes shown per page in the admin view
PAGES_SHOWN = 200         # technical-constant: pages listed by the admin view (a page each, most "no" first)

UPSERT_SQL = """
    INSERT INTO acp_shared.docs_feedback (repo, path, lang, helpful, comment, actor, role)
    VALUES ($1, $2, $3, $4, $5, $6, $7)
    ON CONFLICT (repo, path, actor) DO UPDATE
       SET helpful = EXCLUDED.helpful, comment = EXCLUDED.comment, lang = EXCLUDED.lang, role = EXCLUDED.role,
           updated_at = now()
    RETURNING created_at, updated_at, (xmax = 0) AS inserted
"""

SUMMARY_SQL = """
    SELECT repo, path,
           count(*) FILTER (WHERE helpful) AS yes,
           count(*) FILTER (WHERE NOT helpful) AS no,
           max(updated_at) AS last_at
      FROM acp_shared.docs_feedback
     GROUP BY repo, path
     ORDER BY count(*) FILTER (WHERE NOT helpful) DESC, max(updated_at) DESC
     LIMIT $1
"""

NOTES_SQL = """
    SELECT repo, path, lang, helpful, comment, role, updated_at
      FROM (SELECT f.*, row_number() OVER (PARTITION BY repo, path ORDER BY updated_at DESC) AS n
              FROM acp_shared.docs_feedback f
             WHERE comment IS NOT NULL) x
     WHERE n <= $1
     ORDER BY repo, path, updated_at DESC
"""


def clean_comment(comment: Optional[str]) -> Optional[str]:
    """The note as stored: trimmed, at most COMMENT_CHARS, 12-digit runs masked; None when empty."""
    if comment is None:
        return None
    text = docs_reader._ACCOUNT_ID.sub(docs_reader.ACCOUNT_ID_MARKER, comment.strip())[:COMMENT_CHARS]
    return text or None


def listed_page(m: dict, owner_read: bool, repo: str, path: str) -> Optional[dict]:
    """The manifest entry of (repo, path) when this caller may read it, else None."""
    return next((e for e in docs_reader.visible_entries(m, owner_read) if e["repo"] == repo and e["path"] == path),
                None)


async def record(conn: Any, *, repo: str, path: str, lang: str, helpful: bool, comment: Optional[str], actor: str,
                 role: Optional[str]) -> dict:
    row = await conn.fetchrow(UPSERT_SQL, repo, path, lang, helpful, clean_comment(comment), actor[:64],
                              (role or None) and role[:32])
    return {"repo": repo, "path": path, "helpful": helpful, "created": bool(row["inserted"]),
            "updated_at": row["updated_at"].isoformat()}


async def summary(conn: Any, m: dict, owner_read: bool) -> dict:
    """Per page: yes / no counts, the last answer time and the latest notes; pages the admin may not see (owner tier,
    or no longer published) are counted, never named."""
    visible = {(e["repo"], e["path"]): e for e in docs_reader.visible_entries(m, owner_read)}
    rows = await conn.fetch(SUMMARY_SQL, PAGES_SHOWN)
    notes: dict[tuple[str, str], list[dict]] = {}
    for r in await conn.fetch(NOTES_SQL, NOTES_PER_PAGE):
        notes.setdefault((r["repo"], r["path"]), []).append(
            {"lang": r["lang"], "helpful": r["helpful"], "comment": r["comment"], "role": r["role"],
             "at": r["updated_at"].isoformat()})
    pages, hidden = [], 0
    for r in rows:
        entry = visible.get((r["repo"], r["path"]))
        if entry is None:
            hidden += 1
            continue
        pages.append({"repo": r["repo"], "path": r["path"], "id": entry["id"], "title": entry["title"],
                      "access": entry["access"], "yes": r["yes"], "no": r["no"], "last_at": r["last_at"].isoformat(),
                      "notes": notes.get((r["repo"], r["path"]), [])})
    return {"pages": pages, "hidden_pages": hidden, "totals": {"yes": sum(p["yes"] for p in pages),
                                                              "no": sum(p["no"] for p in pages)}}
