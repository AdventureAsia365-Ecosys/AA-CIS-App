"""AA-1168 follow-up (S286 settings-rule audit) — the AI disclosure wording is the setting cis.content.ai_disclosure
(CIS 463), not a code literal:

  1. ai_marking reads the wording in force (HTML paragraph, social message, plain-text trailer), the piece language
     picks the line (English fallback), and the seeded wording is served when the setting cannot be read;
  2. the line can be reworded, never removed: the settings API refuses an empty or malformed value (422 on a
     proposal, 409 on approval);
  3. Facebook: the retry lookup matches the message the earlier attempt stored (publish_log.sent_message, CIS 464)
     even after the wording changed, and falls back to the rebuilt message for a row without one; a retry that
     finds nothing records the new text before posting it;
  4. GET /v1/content-writing/ai-disclosure serves the wording to the agency studio export.
No DB; invented wording and text only.
"""
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_content_writing import ai_disclosure_setting as ads
from services.acp_content_writing import ai_marking
from services.acp_content_writing.export import render_content_text_to_fragment, render_content_text_to_plain
from services.acp_publish.idempotency import Claim
from shared import settings
from tests.unit.safe_http_fakes import TARGET, fake_request

CREATED = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
MARK = {"ai_generated": True, "generator": ai_marking.GENERATOR, "model": "writer-model-x",
        "generated_at": "2026-11-02T09:01:00+00:00", "human_edited": False}
REWORDED = {"text": {"en": "Drafted with a writing model.", "vi": "Soạn bằng mô hình viết."},
            "social_tag": "#ModelDrafted"}
TEXT = "A slow boat past the paper mill."


@pytest.fixture
def reworded(monkeypatch):
    monkeypatch.setattr(settings, "get", lambda key, tenant_id=None: REWORDED if key == ads.KEY else None)


@pytest.fixture
def unreadable(monkeypatch):
    def boom(key, tenant_id=None):
        raise KeyError(key)
    monkeypatch.setattr(settings, "get", boom)


# ── 1. the wording in force ───────────────────────────────────────────────────────────────────────────────────

def test_every_channel_shows_the_wording_in_force(reworded):
    assert ai_marking.disclosure_text() == "Drafted with a writing model."
    assert "<em>Drafted with a writing model.</em>" in ai_marking.disclosure_html(MARK)
    assert ai_marking.social_message(TEXT, MARK) == f"{TEXT}\n\nDrafted with a writing model. #ModelDrafted"
    assert "\n---\nDrafted with a writing model.\nAI-Content: " in ai_marking.text_with_disclosure(TEXT, MARK)
    assert "<em>Drafted with a writing model.</em>" in render_content_text_to_fragment(TEXT, ai=MARK)


def test_a_vietnamese_piece_gets_the_vietnamese_line(reworded):
    assert ai_marking.social_message(TEXT, MARK, "vi") == f"{TEXT}\n\nSoạn bằng mô hình viết. #ModelDrafted"
    assert "<em>Soạn bằng mô hình viết.</em>" in render_content_text_to_fragment(TEXT, ai=MARK, lang="vi-VN")
    assert "\n---\nSoạn bằng mô hình viết.\n" in render_content_text_to_plain(TEXT, ai=MARK, lang="vi")
    assert ai_marking.disclosure_text("th") == "Drafted with a writing model."   # no line: English


def test_the_seeded_wording_is_the_fallback(unreadable):
    assert ai_marking.disclosure_text("vi") == ads.DEFAULT_TEXT_VI
    assert ai_marking.social_message(TEXT) == SENT_BEFORE
    assert ai_marking.DISCLOSURE_TEXT == ads.DEFAULT["text"]["en"]
    assert ai_marking.SOCIAL_DISCLOSURE_TAG == ads.DEFAULT["social_tag"]


def test_a_stored_value_that_would_blank_the_line_is_ignored(monkeypatch):
    monkeypatch.setattr(settings, "get", lambda key, tenant_id=None: {"text": {"en": " "}, "social_tag": "#x1"})
    assert ai_marking.disclosure_text() == ai_marking.DISCLOSURE_TEXT
    assert ai_marking.disclosure_html().endswith(f"<em>{ai_marking.DISCLOSURE_TEXT}</em></p>")


def test_the_setting_module_is_the_one_the_migration_pr_ships():
    """The code PR carries a byte copy of services/acp_content_writing/ai_disclosure_setting.py (the migration PR
    ships it with CIS 463); once both are on main this test just reads the one file."""
    assert ads.KEY == "cis.content.ai_disclosure" and ads.value_problem(ads.DEFAULT) is None


# ── 2. the settings API refuses a value that breaks the disclosure ────────────────────────────────────────────

@pytest.mark.parametrize("value", [
    {"text": {"en": ""}, "social_tag": "#AIgenerated"},
    {"text": {"vi": "Chỉ tiếng Việt."}, "social_tag": "#AIgenerated"},
    {"text": {"en": "A line."}, "social_tag": "AIgenerated"},
    {"text": {"en": "A line."}, "social_tag": ""},
])
def test_a_proposal_that_breaks_the_shape_is_refused_with_422(value):
    from api.services import settings_registry as reg
    d = MagicMock(key=ads.KEY, label="AI disclosure wording", value_type="json", min_value=None, max_value=None)
    with patch.object(reg.settings, "validate", return_value=None), \
            pytest.raises(reg.SettingsError) as err:
        reg._check_value(d, value)
    assert err.value.status == 422 and "AI disclosure wording" in err.value.detail


def test_a_valid_rewording_passes_and_other_keys_are_not_shape_checked():
    from api.services import settings_registry as reg
    assert reg.shape_problem(ads.KEY, REWORDED) is None
    assert reg.shape_problem("cis.publish.daily_limit_default", {"anything": "goes"}) is None


@pytest.mark.asyncio
async def test_an_approval_rechecks_the_shape_with_409():
    from api.services import settings_registry as reg
    d = MagicMock(key=ads.KEY, label="AI disclosure wording")
    with patch.object(reg.settings, "validate", return_value=None), pytest.raises(reg.SettingsError) as err:
        await reg._recheck_on_approval(MagicMock(), d, {"id": "x", "value": {"text": {"en": "ok"}}})
    assert err.value.status == 409


# ── 3. Facebook retry after a wording change ──────────────────────────────────────────────────────────────────

def _fb():
    fb = MagicMock()
    fb.create_post = AsyncMock(return_value=MagicMock(post_id="1_2", post_url="https://example.test/1_2"))
    fb.find_recent_post = AsyncMock(return_value=MagicMock(post_id="1_9", post_url="https://example.test/1_9"))
    return fb


PIECE = {"content_text": TEXT, "created_at": CREATED, "ai_provenance": None}
SENT_BEFORE = f"{TEXT}\n\n{ai_marking.DISCLOSURE_TEXT} {ai_marking.SOCIAL_DISCLOSURE_TAG}"


@pytest.mark.asyncio
async def test_retry_lookup_matches_the_stored_message_after_a_wording_change(reworded):
    from services.acp_publish import dispatch
    fb = _fb()
    with patch.object(dispatch, "_facebook", return_value=fb):
        found = await dispatch.find_remote("facebook", {}, PIECE, "k", sent_message=SENT_BEFORE)
    assert found == ("1_9", "https://example.test/1_9")
    asked = fb.find_recent_post.call_args.args
    assert asked[0] == SENT_BEFORE                                    # the text the earlier attempt sent, first
    assert asked[1] == dispatch.facebook_message(PIECE) != SENT_BEFORE  # and the one built with the new wording


@pytest.mark.asyncio
async def test_a_row_without_a_stored_message_looks_for_the_rebuilt_one(reworded):
    from services.acp_publish import dispatch
    fb = _fb()
    with patch.object(dispatch, "_facebook", return_value=fb):
        await dispatch.find_remote("facebook", {}, PIECE, "k", sent_message=None)
        await dispatch.find_remote("facebook", {}, PIECE, "k", sent_message=dispatch.facebook_message(PIECE))
    for call in fb.find_recent_post.call_args_list:
        assert call.args == (dispatch.facebook_message(PIECE),)       # no duplicate candidate


@pytest.mark.asyncio
async def test_publish_posts_the_stored_message_as_is(reworded):
    from services.acp_publish import dispatch
    fb = _fb()
    with patch.object(dispatch, "_facebook", return_value=fb):
        await dispatch.publish_to_channel("facebook", {}, PIECE, "k", message="Stored text. #Tag1")
        await dispatch.publish_to_channel("facebook", {}, PIECE, "k")
    sent = [c.args[0].message for c in fb.create_post.call_args_list]
    assert sent == ["Stored text. #Tag1", dispatch.facebook_message(PIECE)]
    assert dispatch.post_text("facebook", PIECE) == dispatch.facebook_message(PIECE)
    assert dispatch.post_text("blog", PIECE) is None


@pytest.mark.asyncio
async def test_facebook_adapter_matches_any_candidate_in_one_feed_read():
    from services.acp_publish.facebook import FacebookAdapter
    adapter = FacebookAdapter(page_id="1", page_access_token="tok")
    feed = {"data": [{"id": "1_5", "message": "Other post"}, {"id": "1_7", "message": SENT_BEFORE + "\n"}]}
    fake = fake_request(200, body_text=json.dumps(feed))
    with patch(TARGET, fake):
        found = await adapter.find_recent_post("Not posted", SENT_BEFORE)
    assert found.post_id == "1_7" and fake.call_count == 1
    with patch(TARGET, fake_request(200, body_text=json.dumps({"data": [{"id": "1_8", "message": ""}]}))):
        assert await adapter.find_recent_post("", "  ") is None      # no candidate never matches an empty post


@asynccontextmanager
async def _scope():
    yield (MagicMock(), False)


@pytest.mark.asyncio
async def test_a_retry_that_finds_nothing_records_the_new_text_before_posting():
    from api.routers import v1_publish as pub
    order = []
    claimed = Claim("claimed", publish_id="p1", prior_attempt=True, prior_message=SENT_BEFORE)
    record = AsyncMock(side_effect=lambda *a, **k: order.append(("record", k["sent_message"])))
    post = AsyncMock(side_effect=lambda *a, **k: order.append(("post", k["message"])) or ("1_3", "u"))
    with patch.object(pub, "find_remote", AsyncMock(return_value=None)) as find, \
            patch.object(pub, "record_sent_message", record), patch.object(pub, "publish_to_channel", post):
        out = await pub._find_or_post(_scope, "t1", "pc1", "facebook", {}, PIECE, "k", claimed, "New text. #T2")
    assert out == ("1_3", "u")
    assert find.call_args.kwargs["sent_message"] == SENT_BEFORE
    assert order == [("record", "New text. #T2"), ("post", "New text. #T2")]


@pytest.mark.asyncio
async def test_a_retry_that_finds_the_earlier_post_never_posts_or_records():
    from api.routers import v1_publish as pub
    claimed = Claim("claimed", publish_id="p1", prior_attempt=True, prior_message=SENT_BEFORE)
    with patch.object(pub, "find_remote", AsyncMock(return_value=("1_9", "u9"))), \
            patch.object(pub, "record_sent_message", AsyncMock()) as record, \
            patch.object(pub, "publish_to_channel", AsyncMock()) as post:
        assert await pub._find_or_post(_scope, "t1", "pc1", "facebook", {}, PIECE, "k", claimed,
                                       "New text. #T2") == ("1_9", "u9")
    record.assert_not_awaited()
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_first_attempt_posts_without_a_second_record():
    """The first attempt's text is stored by claim() itself (the INSERT), so no extra write before the post."""
    from api.routers import v1_publish as pub
    with patch.object(pub, "find_remote", AsyncMock()) as find, \
            patch.object(pub, "record_sent_message", AsyncMock()) as record, \
            patch.object(pub, "publish_to_channel", AsyncMock(return_value=("1_4", "u4"))) as post:
        await pub._find_or_post(_scope, "t1", "pc1", "facebook", {}, PIECE, "k",
                                Claim("claimed", publish_id="p1"), "Text. #T2")
    find.assert_not_awaited()
    record.assert_not_awaited()
    assert post.call_args.kwargs["message"] == "Text. #T2"


@pytest.mark.asyncio
async def test_claim_stores_the_message_on_a_new_row_and_returns_the_stored_one_on_a_retry():
    from services.acp_publish import idempotency as idem

    def conn_with(rows):
        conn = MagicMock()
        tx = MagicMock()
        tx.__aenter__ = AsyncMock(return_value=None)
        tx.__aexit__ = AsyncMock(return_value=False)
        conn.transaction = MagicMock(return_value=tx)
        conn.fetch = AsyncMock(return_value=rows)
        conn.fetchval = AsyncMock(return_value="new-id")
        conn.execute = AsyncMock(return_value="UPDATE 1")
        return conn

    fresh = conn_with([])
    c = await idem.claim(fresh, piece_id="pc", tenant_id="t", channel="facebook", key="K", sent_message="Msg #T")
    assert c.publish_id == "new-id" and fresh.fetchval.call_args.args[-1] == "Msg #T"
    assert "sent_message" in fresh.fetchval.call_args.args[0]

    stale = conn_with([{"publish_id": "p1", "idempotency_key": "K", "status": "requested", "stale": True,
                        "sent_message": SENT_BEFORE}])
    c = await idem.claim(stale, piece_id="pc", tenant_id="t", channel="facebook", key="K", sent_message="Msg #T")
    assert c.prior_attempt and c.prior_message == SENT_BEFORE
    assert all("sent_message" not in call.args[0] for call in stale.execute.call_args_list)   # kept as it was

    failed = conn_with([{"publish_id": "p2", "idempotency_key": "K", "status": "failed", "stale": False,
                         "sent_message": SENT_BEFORE}])
    c = await idem.claim(failed, piece_id="pc", tenant_id="t", channel="facebook", key="K", sent_message="Msg #T")
    assert c.prior_attempt and c.prior_message == SENT_BEFORE
    upd = failed.execute.call_args.args
    assert upd[-2:] == (True, "Msg #T")                                # CASE keeps the stored text for a prior

    other = conn_with([{"publish_id": "p3", "idempotency_key": "OLD", "status": "failed", "stale": False,
                        "sent_message": "Old revision text"}])
    c = await idem.claim(other, piece_id="pc", tenant_id="t", channel="facebook", key="K", sent_message="Msg #T")
    assert not c.prior_attempt and c.prior_message is None
    assert other.execute.call_args.args[-2:] == (False, "Msg #T")     # another revision: the new text replaces it


# ── 4. the studio read ────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_studio_reads_the_wording_in_force(reworded):
    from api.routers import v1_content_writing
    assert await v1_content_writing.ai_disclosure(tenant={"sub": "t"}) == REWORDED


@pytest.mark.asyncio
async def test_the_studio_read_serves_the_seed_when_the_setting_is_unreadable(unreadable):
    from api.routers import v1_content_writing
    out = await v1_content_writing.ai_disclosure(tenant={"sub": "t"})
    assert out == ads.DEFAULT and v1_content_writing.AiDisclosure(**out).social_tag == "#AIgenerated"
