"""AA-977 test helpers: an in-memory stand-in for gold_aa_internal.v_aa_content_published answering exactly the
statements of services/aa_content/reader.py, and a fake main pool. No DB, no network."""
from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from services.aa_content import reader

T0 = datetime(2026, 10, 24, 8, 0, 0, 123456, tzinfo=timezone.utc)

VN_EN = uuid.UUID("10000000-0000-4000-8000-000000000001")
VN_VI = uuid.UUID("10000000-0000-4000-8000-000000000002")
TPL_EN = uuid.UUID("10000000-0000-4000-8000-000000000003")
RETIRED_EN = uuid.UUID("10000000-0000-4000-8000-000000000004")
DRAFT_ONLY = uuid.UUID("10000000-0000-4000-8000-000000000005")


def published_row(content_id: uuid.UUID, *, version: int = 1, kind: str = "prep_pack", locale: str = "en",
                  group_key: str = "prep-vietnam", title: str = "Before you go: Vietnam",
                  body: str = "Visa: apply only on the official portal (https://evisa.gov.vn).",
                  applies_to: Optional[dict] = None, published_at: datetime = T0,
                  retired_at: Optional[datetime] = None) -> dict:
    """A view row as the store would have published it (same served body and content_hash)."""
    row = {"content_id": content_id, "kind": kind, "locale": locale, "group_key": group_key, "version": version,
           "title": title, "body_markdown": body,
           "applies_to": json.dumps(applies_to if applies_to is not None else {"country_codes": ["VN"]}),
           "published_at": published_at, "retired_at": retired_at}
    row["content_hash"] = reader.hash_of(reader.served_body(row))
    return row


class AaContentView:
    """The rows of v_aa_content_published (published versions only: a draft is simply absent)."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.sql: list[tuple[str, tuple]] = []
        self.readonly: list[bool] = []

    def add(self, content_id: uuid.UUID, **kw: Any) -> dict:
        row = published_row(content_id, **kw)
        self.rows.append(row)
        return row

    @asynccontextmanager
    async def transaction(self, isolation: Optional[str] = None, readonly: bool = False):
        self.readonly.append(readonly)
        yield

    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        self.sql.append((sql, args))
        assert sql == reader.LIST_SQL, sql
        after, kind, locale, limit = args
        latest: dict[uuid.UUID, dict] = {}
        for row in self.rows:
            if row["retired_at"] is not None:
                continue
            if after is not None and not row["content_id"] > after:
                continue
            if (kind is not None and row["kind"] != kind) or (locale is not None and row["locale"] != locale):
                continue
            if row["content_id"] not in latest or row["version"] > latest[row["content_id"]]["version"]:
                latest[row["content_id"]] = row
        return [latest[k] for k in sorted(latest)][:limit]

    async def fetchrow(self, sql: str, *args: Any) -> Optional[dict]:
        self.sql.append((sql, args))
        assert sql == reader.VERSION_SQL, sql
        content_id, version = args
        return next((r for r in self.rows if r["content_id"] == content_id and r["version"] == version), None)


class _Acquire:
    def __init__(self, pool: "FakeMainPool") -> None:
        self.pool = pool

    async def _get(self) -> Any:
        if self.pool.fail is not None:
            raise self.pool.fail
        self.pool.acquired += 1
        return self.pool.conn

    def __await__(self):
        return self._get().__await__()

    async def __aenter__(self) -> Any:
        return await self._get()

    async def __aexit__(self, *exc: Any) -> None:
        self.pool.released += 1


class FakeMainPool:
    """``await pool.acquire(timeout=)`` + ``release`` (reader) and ``async with pool.acquire()`` (admin router)."""

    def __init__(self, conn: Any = None, fail: Optional[BaseException] = None) -> None:
        self.conn, self.fail = conn, fail
        self.acquired = 0
        self.released = 0

    def acquire(self, timeout: Optional[float] = None) -> _Acquire:
        return _Acquire(self)

    async def release(self, conn: Any) -> None:
        self.released += 1


def seeded() -> AaContentView:
    """VN prep pack EN (v1 + v2) and VI (v1), an EN starter template, a retired EN pack (v1); DRAFT_ONLY is not in
    the view at all (its only version is a draft)."""
    view = AaContentView()
    view.add(VN_EN, version=1)
    view.add(VN_EN, version=2, title="Before you go: Vietnam (2027)")
    view.add(VN_VI, locale="vi", title="Trước khi đi: Việt Nam",
             body="Thị thực: chỉ nộp trên cổng chính thức (https://evisa.gov.vn).")
    view.add(TPL_EN, kind="starter_template", group_key="tpl-welcome", title="Welcome", body="Hello {name}.",
             applies_to={})
    view.add(RETIRED_EN, group_key="prep-old", title="Old pack", retired_at=T0)
    return view


__all__ = ["T0", "VN_EN", "VN_VI", "TPL_EN", "RETIRED_EN", "DRAFT_ONLY", "AaContentView", "FakeMainPool",
           "published_row", "seeded"]
