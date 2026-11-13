"""S279 thresholds L1 — api/services/settings_registry.py and the settings reader against real Postgres (CIS 407).

propose -> approve (a recorded self-approval while two-person approval is off, the tech-lead e-mail queued for a
high-impact key), the reader serving the approved value after the refresh, the optimistic lock (409 on a stale base
version), one open proposal per key (409), the 422 refusals (bounds, reason length, evidence), reject (a note
required), the history with the value each row replaced, the audit rows (settings.value_*), two-person approval
switched on (409 for the proposer, a second person approves) and the list view (value, source, last change,
proposals).

Needs CIS 407 (shared.setting_definition / setting_value). It ships in the migration-only PR before this code; until
that PR is on the branch the module is skipped (it then runs on every CI round: the conftest replays 407).
Invented values only; every test runs in one transaction that is rolled back."""
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.services import settings_registry as reg  # noqa: E402
from shared import ops_alerts, settings  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
KEY = "cis.publish.daily_limit_default"          # high impact, maker_checker, 1..1000, default 20
REASON = "zz test: lower the platform default for the trial"
A, B = "admin:zz-admin-a", "admin:zz-admin-b"


class _OneConnPool:
    """The service's pool.acquire() on the test's one connection (inside its rolled-back transaction)."""

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
    if await c.fetchval("SELECT to_regclass('shared.setting_value') IS NULL AS ok"):
        await c.close()
        pytest.skip("CIS 407 is not on this branch yet (migration-only PR first)")
    tr = c.transaction()
    await tr.start()
    mails = []

    async def self_approved(conn, *, details):
        mails.append(details)
    monkeypatch.setattr(ops_alerts, "enqueue_setting_self_approved_email", self_approved)
    p = _OneConnPool(c)
    p.mails = mails
    settings.READER.install(None)
    settings.READER.invalidate()
    try:
        yield p
    finally:
        await tr.rollback()
        await c.close()
        settings.READER._snapshot = None           # the next test starts from the compiled defaults
        settings.READER.invalidate()


async def _audits(pool):
    return [r["action"] for r in await pool.conn.fetch(
        "SELECT action FROM acp_shared.audit_log WHERE resource_type = 'setting' ORDER BY created_at, action")]


@pytest.mark.asyncio
async def test_propose_and_self_approve_then_the_reader_serves_it(pool):
    out = await reg.propose(pool, KEY, value=12, reason=REASON, actor=A, base_version=0, apply=True,
                            can_approve=True)
    assert (out["status"], out["version"], out["self_approved"], out["decided_by"]) == ("approved", 1, True, A)
    assert pool.mails and pool.mails[0]["key"] == KEY and pool.mails[0]["before"] == 20 and pool.mails[0]["after"] == 12
    assert await settings.READER.refresh(force=True, pool=pool) is True
    res = settings.resolve(KEY)
    assert (res.value, res.source, res.version) == (12, "platform", 1)
    assert sorted(await _audits(pool)) == ["settings.value_approved", "settings.value_proposed"]
    # the next change names the version it replaces; a stale one is refused
    with pytest.raises(reg.SettingsError) as e:
        await reg.propose(pool, KEY, value=15, reason=REASON, actor=A, base_version=0)
    assert e.value.status == 409 and e.value.code == "stale_version"
    v2 = await reg.propose(pool, KEY, value=15, reason=REASON, actor=A, base_version=1)
    assert v2["status"] == "proposed" and v2["version"] == 2
    with pytest.raises(reg.SettingsError) as e:
        await reg.propose(pool, KEY, value=16, reason=REASON, actor=A, base_version=1)
    assert e.value.code == "open_proposal"
    done = await reg.decide(pool, v2["id"], actor=B, approve=True)
    assert done["status"] == "approved" and done["self_approved"] is False
    statuses = {r["version"]: r["status"] for r in await pool.conn.fetch(
        "SELECT version, status FROM shared.setting_value WHERE key = $1", KEY)}
    assert statuses == {1: "superseded", 2: "approved"}
    h = await reg.history(pool, KEY)
    assert [(r["version"], r["value"], r["previous_value"]) for r in h["history"]] == [(2, 15, 12), (1, 12, 20)]
    listing = await reg.list_settings(pool, app="cis")
    row = next(r for r in listing["settings"] if r["key"] == KEY)
    assert (row["value"], row["source"], row["version"], row["default_value"]) == (15, "platform", 2, 20)
    assert row["last_change"]["version"] == 2 and row["proposals"] == []
    assert listing["two_person_approval"] is False and "content" in listing["areas"]


@pytest.mark.asyncio
async def test_refusals(pool):
    for value, code in ((0, "out_of_bounds"), (1001, "out_of_bounds"), ("20", "out_of_bounds")):
        with pytest.raises(reg.SettingsError) as e:
            await reg.propose(pool, KEY, value=value, reason=REASON, actor=A, base_version=0)
        assert (e.value.status, e.value.code) == (422, code)
    with pytest.raises(reg.SettingsError) as e:
        await reg.propose(pool, KEY, value=10, reason="too short", actor=A, base_version=0)
    assert e.value.code == "reason_too_short"
    with pytest.raises(reg.SettingsError) as e:
        await reg.propose(pool, "cis.t7.blog_chars_per_seed_word", value=3.364, reason=REASON, actor=A,
                          base_version=0)
    assert e.value.code == "evidence_required"
    with pytest.raises(reg.SettingsError) as e:
        await reg.propose(pool, KEY, value=10, reason=REASON, actor=A, base_version=0,
                          tenant_id="00000000-0000-4000-8000-00000000a279")
    assert e.value.code == "not_tenant_scoped"
    with pytest.raises(reg.SettingsError) as e:
        await reg.propose(pool, KEY, value=10, reason=REASON, actor=A, base_version=0, apply=True, can_approve=False)
    assert e.value.status == 403
    with pytest.raises(reg.SettingsError) as e:
        await reg.propose(pool, "cis.zz.nope", value=1, reason=REASON, actor=A, base_version=0)
    assert e.value.status == 404
    ok = await reg.propose(pool, "cis.t7.blog_chars_per_seed_word", value=3.364, reason=REASON, actor=A,
                           base_version=0, evidence_ref="replay:zz-s279-seed-floor")
    assert ok["evidence_ref"] == "replay:zz-s279-seed-floor"


@pytest.mark.asyncio
async def test_reject_needs_a_note_and_two_person_approval_refuses_the_proposer(pool):
    p = await reg.propose(pool, KEY, value=30, reason=REASON, actor=A, base_version=0)
    with pytest.raises(reg.SettingsError) as e:
        await reg.decide(pool, p["id"], actor=B, approve=False)
    assert e.value.code == "note_required"
    r = await reg.decide(pool, p["id"], actor=B, approve=False, note="zz not now")
    assert r["status"] == "rejected"
    with pytest.raises(reg.SettingsError) as e:
        await reg.decide(pool, p["id"], actor=B, approve=True)
    assert e.value.code == "already_decided"
    # switch two-person approval on: the flag is maker_checker (S279 review); while it is off the admin may apply it
    on = await reg.propose(pool, reg.TWO_PERSON_KEY, value=True, reason=REASON, actor=A, base_version=0, apply=True,
                           can_approve=True)
    assert on["status"] == "approved" and on["self_approved"] is True
    # the flag is read in the decision transaction, not from the (stale) process cache: no refresh here
    assert settings.get(reg.TWO_PERSON_KEY) is False and await reg.two_person_on(pool.conn) is True
    p2 = await reg.propose(pool, KEY, value=25, reason=REASON, actor=A, base_version=0)
    with pytest.raises(reg.SettingsError) as e:
        await reg.decide(pool, p2["id"], actor=A, approve=True)
    assert e.value.code == "second_person_required"
    assert (await reg.decide(pool, p2["id"], actor=B, approve=True))["status"] == "approved"
    # the person it checks cannot switch it off alone; a second person can
    off = await reg.propose(pool, reg.TWO_PERSON_KEY, value=False, reason=REASON, actor=A, base_version=1)
    with pytest.raises(reg.SettingsError) as e:
        await reg.decide(pool, off["id"], actor=A, approve=True)
    assert e.value.code == "second_person_required"
    with pytest.raises(reg.SettingsError) as e:
        await reg.propose(pool, KEY, value=26, reason=REASON, actor=A, base_version=2, apply=True, can_approve=True)
    assert e.value.code == "second_person_required"
    assert (await reg.decide(pool, off["id"], actor=B, approve=True))["status"] == "approved"
    assert await reg.two_person_on(pool.conn) is False
    with pytest.raises(reg.SettingsError) as e:
        await reg.decide(pool, "not-a-uuid", actor=B, approve=True)
    assert e.value.status == 404


# ── S279 L2 review: the rules between quality keys are re-checked on approval (CIS 412 definitions) ─────────────
EVIDENCE = "replay:zz-invented-run-0001"


async def _needs_quality_keys(pool):
    if not await pool.conn.fetchval("SELECT count(*) AS ok FROM shared.setting_definition "
                                    "WHERE key = 'cis.quality.pass_line'"):
        pytest.skip("CIS 412 (the quality definitions) is not on this branch yet")


@pytest.mark.asyncio
async def test_two_quality_proposals_that_pass_alone_cannot_both_be_approved(pool):
    await _needs_quality_keys(pool)
    cap = await reg.propose(pool, "cis.quality.a1_missing_field_cap", value=6.5, reason=REASON, actor=A,
                            base_version=0, evidence_ref=EVIDENCE)
    line = await reg.propose(pool, "cis.quality.pass_line", value=6.2, reason=REASON, actor=A, base_version=0,
                             evidence_ref=EVIDENCE)
    assert (cap["status"], line["status"]) == ("proposed", "proposed")       # each fits the values in force alone
    assert (await reg.decide(pool, cap["id"], actor=B, approve=True))["status"] == "approved"
    with pytest.raises(reg.SettingsError) as e:
        await reg.decide(pool, line["id"], actor=B, approve=True)
    assert e.value.status == 409 and e.value.code == "cross_check" and "pass line" in e.value.detail
    assert await pool.conn.fetchval("SELECT status AS ok FROM shared.setting_value WHERE id = $1::uuid",
                                    line["id"]) == "proposed"           # refused, still open: reject or re-propose


@pytest.mark.asyncio
async def test_a_future_dated_quality_approval_counts_when_the_next_one_is_approved(pool):
    await _needs_quality_keys(pool)
    top = await reg.propose(pool, "cis.quality.ui_score_band_top", value=7.5, reason=REASON, actor=A,
                            base_version=0, evidence_ref=EVIDENCE,
                            effective_from=await pool.conn.fetchval("SELECT now() + interval '7 days' AS ok"))
    assert (await reg.decide(pool, top["id"], actor=B, approve=True))["status"] == "approved"
    line = await reg.propose(pool, "cis.quality.pass_line", value=8.0, reason=REASON, actor=A, base_version=0,
                             evidence_ref=EVIDENCE)
    with pytest.raises(reg.SettingsError) as e:
        await reg.decide(pool, line["id"], actor=B, approve=True)
    assert e.value.code == "cross_check" and "future-dated" in e.value.detail
