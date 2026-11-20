"""AA-1168 follow-up — the AI disclosure wording setting (cis.content.ai_disclosure, CIS 463) through the settings
registry and the reader against real Postgres: a proposal that would blank the line or break the tag is refused (422)
before anything is stored; a valid rewording, approved, is what ai_marking and the publish message use after the
refresh; a malformed value written past the API (a hand UPDATE) is ignored and the seeded wording is served.

Needs CIS 463 (the migration-only PR before this code); until it is on the branch the module is skipped. Invented
wording only; every test runs in one transaction that is rolled back."""
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.services import settings_registry as reg  # noqa: E402
from services.acp_content_writing import ai_disclosure_setting as ads  # noqa: E402
from services.acp_content_writing import ai_marking  # noqa: E402
from shared import ops_alerts, settings  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
REASON = "zz test: counsel asked for a shorter disclosure line"
A = "admin:zz-admin-a"
NEW = {"text": {"en": "Drafted with a writing model.", "vi": "Soạn bằng mô hình viết."}, "social_tag": "#ZzDrafted"}


class _OneConnPool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False
        return _Ctx()


@pytest_asyncio.fixture
async def pool(monkeypatch):
    c = await asyncpg.connect(DSN)
    if not await c.fetchval("SELECT EXISTS (SELECT 1 FROM shared.setting_definition WHERE key = $1) AS ok", ads.KEY):
        await c.close()
        pytest.skip("CIS 463 is not on this branch yet (migration-only PR first)")
    tr = c.transaction()
    await tr.start()

    async def no_mail(conn, *, details):
        return None
    monkeypatch.setattr(ops_alerts, "enqueue_setting_self_approved_email", no_mail)
    settings.READER.install(None)
    settings.READER.invalidate()
    try:
        yield _OneConnPool(c)
    finally:
        await tr.rollback()
        await c.close()
        settings.READER._snapshot = None
        settings.READER.invalidate()


@pytest.mark.parametrize("value", [
    {"text": {"en": ""}, "social_tag": "#AIgenerated"},
    {"text": {"vi": "Chỉ có dòng tiếng Việt."}, "social_tag": "#AIgenerated"},
    {"text": {"en": "A line."}, "social_tag": "no hash"},
])
@pytest.mark.asyncio
async def test_a_value_that_breaks_the_disclosure_is_refused_before_it_is_stored(pool, value):
    with pytest.raises(reg.SettingsError) as err:
        await reg.propose(pool, ads.KEY, value=value, reason=REASON, actor=A, base_version=0, apply=True,
                          can_approve=True)
    assert err.value.status == 422
    assert await pool.conn.fetchval("SELECT count(*) AS n FROM shared.setting_value WHERE key = $1", ads.KEY) == 0


@pytest.mark.asyncio
async def test_an_approved_rewording_is_the_line_every_channel_shows(pool):
    out = await reg.propose(pool, ads.KEY, value=NEW, reason=REASON, actor=A, base_version=0, apply=True,
                            can_approve=True)
    assert out["status"] == "approved"
    assert await settings.READER.refresh(force=True, pool=pool) is True
    assert ai_marking.disclosure_text() == "Drafted with a writing model."
    assert ai_marking.disclosure_text("vi") == "Soạn bằng mô hình viết."
    assert ai_marking.social_message("A pier.") == "A pier.\n\nDrafted with a writing model. #ZzDrafted"


@pytest.mark.asyncio
async def test_a_malformed_value_stored_past_the_shape_check_is_ignored(pool, monkeypatch):
    """A value approved before the shape check existed (the code PR deploys after CIS 463 is applied) or by a future
    bug: the generic json check accepts it, the reader refuses it and the seeded wording is served."""
    monkeypatch.setattr(reg, "shape_problem", lambda key, value: None)
    await reg.propose(pool, ads.KEY, value={"text": {"en": "  "}, "social_tag": "#ZzTag"}, reason=REASON, actor=A,
                      base_version=0, apply=True, can_approve=True)
    await settings.READER.refresh(force=True, pool=pool)
    assert ai_marking.disclosure_text() == ai_marking.DISCLOSURE_TEXT
    expected = f"{ai_marking.DISCLOSURE_TEXT} {ai_marking.SOCIAL_DISCLOSURE_TAG}"
    assert ai_marking.social_message("A pier.").endswith(expected)
