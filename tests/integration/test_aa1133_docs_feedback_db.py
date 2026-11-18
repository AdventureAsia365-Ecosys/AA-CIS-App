"""AA-1133 (S284) — api/services/docs_feedback.py against Postgres (CIS 454 acp_shared.docs_feedback, replayed by the
conftest): the upsert keeps one row per person and page and reports created / replaced, the note is masked and
trimmed, and the admin summary counts per page, newest notes first, hides pages the caller may not open.

The test signs in as cis_app (the runtime role) for the writes and reads, like the API. Invented pages only; runs in
a transaction that is rolled back.
Chain: needs CIS 454 (the migration-only PR of this lane) applied first."""
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.services import docs_feedback  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


def _entry(path: str, access: str = "staff") -> dict:
    return {"repo": "AA-Ecosys", "path": path, "id": "GUIDE-90", "title": f"Invented {path}", "access": access}


MANIFEST = {"entries": [_entry("docs/guides/zz-lantern.md"), _entry("docs/legal/zz-ledger.md", "owner")]}


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        # The replayed test DB has no runtime schema grants (Dev has them): grant USAGE for this transaction only.
        await c.execute("GRANT USAGE ON SCHEMA acp_shared TO cis_app")
        await c.execute("SET LOCAL ROLE cis_app")
        yield c
    finally:
        await tr.rollback()
        await c.close()


@pytest.mark.asyncio
async def test_upsert_replaces_the_answer(conn):
    first = await docs_feedback.record(conn, repo="AA-Ecosys", path="docs/guides/zz-lantern.md", lang="en",
                                       helpful=True, comment=None, actor="admin:zz-1", role="support")
    again = await docs_feedback.record(conn, repo="AA-Ecosys", path="docs/guides/zz-lantern.md", lang="vi",
                                       helpful=False, comment="  Old menu for 123456789012.  ", actor="admin:zz-1",
                                       role="support")
    assert first["created"] is True and again["created"] is False
    row = await conn.fetchrow("SELECT helpful AS h, comment AS c, lang AS l FROM acp_shared.docs_feedback "
                              "WHERE path = 'docs/guides/zz-lantern.md' AND actor = 'admin:zz-1'")
    assert (row["h"], row["c"], row["l"]) == (False, "Old menu for [redacted-account-id].", "vi")


@pytest.mark.asyncio
async def test_summary_counts_and_hides_unreadable_pages(conn, monkeypatch):
    from api.services import docs_reader

    monkeypatch.setattr(docs_reader, "visible_entries",
                        lambda m, owner_read: [e for e in m["entries"] if e["access"] == "staff" or owner_read])
    for actor, helpful, note in (("admin:zz-1", False, "Step two is old."), ("admin:zz-2", True, None),
                                 ("admin:zz-3", False, None)):
        await docs_feedback.record(conn, repo="AA-Ecosys", path="docs/guides/zz-lantern.md", lang="en",
                                   helpful=helpful, comment=note, actor=actor, role="content")
    await docs_feedback.record(conn, repo="AA-Ecosys", path="docs/legal/zz-ledger.md", lang="en", helpful=False,
                               comment="Owner note.", actor="admin:zz-1", role="admin")
    view = await docs_feedback.summary(conn, MANIFEST, owner_read=False)
    mine = [p for p in view["pages"] if p["path"].startswith("docs/") and "zz-" in p["path"]]
    assert [(p["path"], p["yes"], p["no"]) for p in mine] == [("docs/guides/zz-lantern.md", 1, 2)]
    assert [n["comment"] for n in mine[0]["notes"]] == ["Step two is old."]
    assert view["hidden_pages"] >= 1
    owner_view = await docs_feedback.summary(conn, MANIFEST, owner_read=True)
    assert {p["path"] for p in owner_view["pages"]} >= {"docs/guides/zz-lantern.md", "docs/legal/zz-ledger.md"}
