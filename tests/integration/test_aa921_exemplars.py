"""AA-921 (WM-27 / WM-29) — CIS 313 (pinned exemplars) and the exemplar code on real Postgres.

conftest replays 313 like every file. Each test runs in one transaction that is rolled back.

  * the file applied: RLS on and forced on both tables with exactly three policies; cis_app's grants; the tenant pool
    holds no privilege;
  * the refresh turns a "use as example" signal that passed the fact filter into one masked candidate of the approved
    piece (idempotent; DRY RUN writes nothing) and a "never use" signal excludes its source;
  * WM-27 Done-when, as cis_app: tenant A's draft pins only tenant A's examples; resolving for tenant B returns none of
    them (selection, release links, the resolved view and the rendered prompt lines);
  * the CIS 313 trigger refuses a cross-tenant link, a link outside the draft's transaction, a `never` example, a link
    on a non-rules release and any UPDATE of a link;
  * WM-29: a pin moves the example into the next draft (and unpin / never take it out), each change audited.
"""
import json
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER, X287_PREDECESSORS, X287_STANDIN_MARK  # noqa: E402
from _rls_fixture_helpers import migration_body  # noqa: E402

from services.writing_memory import exemplars as ex  # noqa: E402
from services.writing_memory import insertion  # noqa: E402
from services.writing_memory import releases as rel  # noqa: E402
from services.writing_memory import rules as rules_mod  # noqa: E402
from services.writing_memory import signals as wm  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
ADMIN = "11111111-1111-1111-1111-111111111111"
SHA = "d" * 64
BODY_A = ("Our guides keep the pace slow and the groups small. Mornings start early, before the heat, and every walk "
          "ends with time to sit and look around. We write plainly and skip the superlatives, because the valley "
          "speaks for itself. Expect quiet tracks, simple meals and long evenings.")
BODY_B = ("Big days, bold views and a crew that loves a sunrise summit. We move fast and we laugh a lot, and every "
          "evening ends around a fire with stories from the trail. Pack light, bring a sense of humour and come ready "
          "to be surprised by what the mountains hold.")


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        for version in X287_PREDECESSORS:
            await c.execute("INSERT INTO shared.schema_versions (version, applied_at, description) "
                            "VALUES ($1, now(), $2) ON CONFLICT (version) DO NOTHING", version, X287_STANDIN_MARK)
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _as_cis_app(conn):
    """Run the rest of the transaction as cis_app (its grants re-made: other modules' X1 teardowns revoke them)."""
    await conn.execute("GRANT USAGE ON SCHEMA shared TO cis_app")
    await conn.execute(migration_body(287))
    for table in ("writing_exemplar", "writing_memory_release_exemplar", "writing_rule", "writing_memory_release",
                  "writing_memory_writer", "prompt_eval_runs"):
        await conn.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON shared.{table} TO cis_app")
    # the reads a draft makes besides (fingerprint: routes, golden set) and its audit row, as on Dev (CIS 244)
    await conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA shared TO cis_app")
    await conn.execute("GRANT USAGE ON SCHEMA acp_shared TO cis_app")
    await conn.execute("GRANT SELECT, INSERT ON acp_shared.audit_log TO cis_app")
    await conn.execute("SET LOCAL ROLE cis_app")


async def _refused(conn, exc, sql, *args, match=None):
    with pytest.raises(exc, match=match):
        async with conn.transaction():
            await conn.execute(sql, *args)


async def _tenant(c):
    tid = uuid.uuid4()
    await c.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) VALUES ($1, $2, $3, 3)",
                    tid, f"AA-921x {tid.hex[:8]}", f"zz921x-{tid.hex[:8]}")
    return str(tid)


async def _piece(c, tid, body, channel="blog", status="approved"):
    req = await c.fetchval("INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, channel, status) "
                           "VALUES ($1::uuid, 'aa921x-atom', $2, 'approved') RETURNING request_id", tid, channel)
    return str(await c.fetchval(
        "INSERT INTO acp_shared.content_piece (tenant_id, angle_gate_request_id, attempt_number, content_text, "
        "status, gate_ledger, repair_log) VALUES ($1::uuid, $2, 1, $3, $4, '[]'::jsonb, '[]'::jsonb) "
        "RETURNING piece_id", tid, req, body, status))


async def _use_signal(c, tid, piece, pref="use", fact="pass"):
    ref = await wm.record_writer_feedback(
        c, writer="t9_blog", scope="tenant", tenant_id=tid, action="sentence_rewrite", actor_type="admin",
        source_table="content_piece", source_id=piece, before_text="Old sentence.", after_text="New sentence.",
        reason_code="voice_off_brand")
    await c.execute("UPDATE shared.writing_signal SET exemplar_pref = $2, fact_status = $3 WHERE signal_id = $1::uuid",
                    ref.signal_id, pref, fact)
    return ref.signal_id


async def _exemplar(c, tid, text, *, writer="t9_blog", channel="blog", status="candidate"):
    m = ex.masked_excerpt(text)
    scope = "tenant" if tid else "platform"
    return str(await c.fetchval(
        "INSERT INTO shared.writing_exemplar (scope, tenant_id, writer, channel, masked_text, text_sha256, word_count, "
        "source_table, source_id, status, status_reason, status_by, status_at) VALUES ($1, $2::uuid, $3, $4, $5, $6, "
        "$7, 'content_piece', $8, $9, $10, $11, CASE WHEN $9 <> 'candidate' THEN now() END) RETURNING exemplar_id",
        scope, tid, writer, channel, m["masked_text"], m["text_sha256"], m["word_count"], str(uuid.uuid4()), status,
        None if status == "candidate" else "pytest", None if status == "candidate" else "pytest"))


async def _rule(c, tid, writer="t9_blog"):
    rid = await rules_mod.insert_candidate(
        c, kind="writer_instruction", scope="tenant" if tid else "platform", tenant_id=tid, writer=writer,
        cluster_key=f"zz921:{uuid.uuid4().hex[:8]}", rule_text="Cut any sentence that only restates the heading.",
        lint={"ok": True})
    await c.execute("UPDATE shared.writing_rule SET status = 'approved', approved_by = $2, approved_at = now() "
                    "WHERE rule_id = $1::uuid", rid, ADMIN)
    return rid


async def _run(conn, writer, kind, verdict="pass", details=None, release=None, golden=None):
    return str(await conn.fetchval(
        "INSERT INTO shared.prompt_eval_runs (pipeline, prompt_version, tour_count, cost_usd, details, triggered_by, "
        "run_kind, writer, golden_set_id, stop_reason, verdict, finished_at, release_id) "
        "VALUES ('writing_memory', 'zz921', 0, 0, $1::jsonb, 'pytest', $2, $3, $4, 'completed', $5, now(), $6::uuid) "
        "RETURNING id", json.dumps(details or {}), kind, writer, golden, verdict, release))


async def _golden(conn):
    await conn.execute("UPDATE shared.eval_golden_set SET status = 'superseded', superseded_at = now() "
                       "WHERE name = 'aa729' AND status = 'active'")
    return await conn.fetchval(
        "INSERT INTO shared.eval_golden_set (name, version, source_uri, source_sha256, seed, item_count, imported_by) "
        "VALUES ('aa729', $2, 'file:zz.json', $1, 729, 0, 'pytest') RETURNING set_id", SHA,
        f"9.{uuid.uuid4().int % 9000 + 1000}.{uuid.uuid4().int % 9000 + 1000}")


async def _activate_release0(conn, writer, tenant=None):
    golden = await _golden(conn)
    r0 = await _run(conn, writer, "r0_replay", details={"inputs": 20, "byte_diffs": 0})
    base = await _run(conn, writer, "baseline_import", "baseline", golden=golden)
    d = await rel.create_draft(conn, writer=writer, scope="tenant" if tenant else "platform", tenant_id=tenant,
                               created_by=f"admin:{ADMIN}")
    await rel.attach_evidence(conn, d["release_id"], r0_run_id=r0, eval_run_id=base, actor=f"admin:{ADMIN}")
    await rel.approve(conn, d["release_id"], approved_by=ADMIN, approval_ref="pytest")
    return await rel.activate(conn, d["release_id"], actor=f"admin:{ADMIN}")


async def _activate_rules(conn, draft):
    ev = await _run(conn, draft["writer"], "r2", release=draft["release_id"], details={"candidate": "release"})
    sh = await _run(conn, draft["writer"], "shadow", release=draft["release_id"])
    await rel.attach_evidence(conn, draft["release_id"], eval_run_id=ev, shadow_run_id=sh, actor=f"admin:{ADMIN}")
    await rel.approve(conn, draft["release_id"], approved_by=ADMIN, approval_ref="pytest")
    return await rel.activate(conn, draft["release_id"], actor=f"admin:{ADMIN}")


# ── the file ────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("table", ["writing_exemplar", "writing_memory_release_exemplar"])
async def test_313_applied_rls_forced_three_policies_no_tenant_privilege(conn, table):
    assert await conn.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = '313'"), \
        "conftest swallowed an error in 313 (see its log)"
    rls = await conn.fetchrow("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = $1::regclass",
                              f"shared.{table}")
    assert rls["relrowsecurity"] and rls["relforcerowsecurity"]
    pols = sorted(r["policyname"] for r in await conn.fetch(
        "SELECT policyname FROM pg_policies WHERE schemaname = 'shared' AND tablename = $1", table))
    assert pols == sorted([f"{table}_cis_app_all", f"{table}_cis_owner_all", "tenant_isolation"])
    assert not await conn.fetchval("SELECT has_table_privilege('aa_app_user', $1, 'SELECT')", f"shared.{table}")


# ── the refresh ─────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_refresh_masks_the_approved_piece_and_honours_never(conn):
    a = await _tenant(conn)
    piece = await _piece(conn, a, BODY_A + " Day 3 costs $450 at 7:30 am in Paro.")
    sid = await _use_signal(conn, a, piece)
    dry = await ex.refresh(conn, dry_run=True)
    assert dry["added"] == 1 and await conn.fetchval("SELECT count(*) FROM shared.writing_exemplar "
                                                     "WHERE tenant_id = $1::uuid", a) == 0
    out = await ex.refresh(conn, dry_run=False)
    assert out["added"] == 1
    row = await conn.fetchrow("SELECT * FROM shared.writing_exemplar WHERE tenant_id = $1::uuid", a)
    assert row["writer"] == "t9_blog" and row["channel"] == "blog" and str(row["source_signal_id"]) == sid
    assert "$450" not in row["masked_text"] and "[PRICE]" in row["masked_text"] and "Paro" not in row["masked_text"]
    assert (await ex.refresh(conn, dry_run=False))["added"] == 0                     # idempotent
    # a held piece gives no candidate; a "never use" signal on the source marks its example never
    held = await _piece(conn, a, BODY_B, status="held")
    await _use_signal(conn, a, held)
    await _use_signal(conn, a, piece, pref="never")
    out = await ex.refresh(conn, dry_run=False)
    assert out["marked_never"] == 1 and out["skipped"].get("not_approved") == 1
    assert await conn.fetchval("SELECT status FROM shared.writing_exemplar WHERE tenant_id = $1::uuid", a) == "never"


# ── WM-27 Done-when: isolation as cis_app ──────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tenant_a_release_pins_only_a_and_never_resolves_for_b(conn):
    a, b = await _tenant(conn), await _tenant(conn)
    ex_a = {await _exemplar(conn, a, BODY_A), await _exemplar(conn, a, BODY_A + " Slow is the point.")}
    ex_b = {await _exemplar(conn, b, BODY_B)}
    await _activate_release0(conn, "t9_blog")                                   # the platform pointer
    await rel.set_memory_enabled(conn, "t9_blog", True, actor=f"admin:{ADMIN}")
    await _activate_release0(conn, "t9_blog", tenant=a)
    await _activate_release0(conn, "t9_blog", tenant=b)
    rule_a = await _rule(conn, a)

    await _as_cis_app(conn)
    draft = await rel.create_draft(conn, writer="t9_blog", scope="tenant", tenant_id=a, rule_ids=[rule_a],
                                   created_by=f"admin:{ADMIN}")
    links = await ex.release_exemplars(conn, draft["release_id"])
    assert {e["exemplar_id"] for e in links} == ex_a and not ({e["exemplar_id"] for e in links} & ex_b)
    assert all(e["tenant_id"] == a for e in links)
    assert not ({e["exemplar_id"] for e in await ex.select_for_release(conn, "t9_blog", "tenant", b)} & ex_a)
    await conn.execute("RESET ROLE")
    await _activate_rules(conn, draft)

    await _as_cis_app(conn)
    view_a = await insertion.resolve(conn, "t9_blog", a)
    assert {e["exemplar_id"] for e in view_a["tenant"]["exemplars"]} == ex_a
    lines_a = insertion.t9_tenant_lines(view_a, "blog")
    assert lines_a[0].startswith("THIS BRAND'S WRITING RULES") and insertion.EXAMPLES_HEADING in lines_a
    assert sum(1 for line in lines_a if line.startswith("Example ")) == 2
    assert insertion.stamp(view_a)["tenant"]["items_sha256"] == draft["items_sha256"]

    view_b = await insertion.resolve(conn, "t9_blog", b)
    got_b = {e["exemplar_id"] for p in (view_b or {}).values() if isinstance(p, dict) for e in p.get("exemplars") or []}
    assert not (got_b & ex_a)
    assert not any(ex.masked_excerpt(BODY_A)["masked_text"][:40] in line
                   for line in insertion.t9_tenant_lines(view_b, "blog"))
    # the platform prompt slot never carries a tenant example
    assert insertion.EXAMPLES_HEADING not in insertion.t9_platform_block(view_a, "blog")


@pytest.mark.asyncio
async def test_an_edited_example_text_renders_nothing(conn):
    a = await _tenant(conn)
    eid = await _exemplar(conn, a, BODY_A)
    await rel.create_draft(conn, writer="t9_blog", scope="tenant", tenant_id=a, created_by=f"admin:{ADMIN}")
    draft = await rel.create_draft(conn, writer="t9_blog", scope="tenant", tenant_id=a,
                                   rule_ids=[await _rule(conn, a)], created_by=f"admin:{ADMIN}")
    row = await insertion.attach_items(conn, draft, "t9_blog")
    assert [e["exemplar_id"] for e in row["exemplars"]] == [eid]
    await conn.execute("UPDATE shared.writing_exemplar SET masked_text = masked_text || ' Extra.' "
                       "WHERE exemplar_id = $1::uuid", eid)
    assert "items" not in await insertion.attach_items(conn, draft, "t9_blog")        # hash mismatch: fail closed


# ── the CIS 313 trigger ────────────────────────────────────────────────────────────────────────────────────────────

_LINK = ("INSERT INTO shared.writing_memory_release_exemplar (release_id, exemplar_id, writer, scope, tenant_id, "
         "channel, position) VALUES ($1::uuid, $2::uuid, 't9_blog', 'tenant', $3::uuid, 'blog', $4)")


@pytest.mark.asyncio
async def test_trigger_refuses_foreign_late_never_and_non_rules_links(conn):
    a, b = await _tenant(conn), await _tenant(conn)
    zero = await rel.create_draft(conn, writer="t9_blog", scope="tenant", tenant_id=a, created_by=f"admin:{ADMIN}")
    draft = await rel.create_draft(conn, writer="t9_blog", scope="tenant", tenant_id=a,
                                   rule_ids=[await _rule(conn, a)], created_by=f"admin:{ADMIN}", exemplars=False)
    foreign = await _exemplar(conn, b, BODY_B)
    never = await _exemplar(conn, a, BODY_A, status="never")
    ok = await _exemplar(conn, a, BODY_A + " Slow is the point.")
    await _refused(conn, asyncpg.CheckViolationError, _LINK, draft["release_id"], foreign, a, 1,
                   match="not a t9_blog blog example of this owner")
    await _refused(conn, asyncpg.CheckViolationError, _LINK, draft["release_id"], never, a, 1, match="never use")
    await _refused(conn, asyncpg.CheckViolationError, _LINK, zero["release_id"], ok, a, 1, match="only a rules")
    await conn.execute(_LINK, draft["release_id"], ok, a, 1)                 # the draft's own transaction: accepted
    await _refused(conn, asyncpg.RestrictViolationError,
                   "UPDATE shared.writing_memory_release_exemplar SET position = 2 WHERE release_id = $1::uuid",
                   draft["release_id"], match="never changed")
    # a release drafted in an earlier transaction (created_at <> now()) takes no new link
    late = await conn.fetchval(
        "INSERT INTO shared.writing_memory_release (writer, scope, tenant_id, release_no, train_kind, content_class, "
        "rule_ids, items_sha256, created_by, created_at) VALUES ('t9_blog', 'tenant', $1::uuid, 7, 'regular', "
        "'rules', ARRAY[$2]::uuid[], $3, 'pytest', now() - interval '1 hour') RETURNING release_id::text",
        a, await _rule(conn, a), SHA)
    other = await _exemplar(conn, a, BODY_A + " Evenings are long.")
    await _refused(conn, asyncpg.RestrictViolationError, _LINK, late, other, a, 1, match="drafts the release")


# ── WM-29: pin / unpin / never ─────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_pin_change_shows_in_the_next_draft_release(conn):
    a = await _tenant(conn)
    old = [await _exemplar(conn, a, BODY_A + f" Note {w}.") for w in ("one", "two")]
    newest = await _exemplar(conn, a, BODY_A + " Note three, the newest one.")
    # one transaction gives every row the same now(): age the two older ones, so "newest first" is not a uuid tie-break
    for hours, eid in zip((2, 1), old):
        await conn.execute("UPDATE shared.writing_exemplar SET created_at = now() - make_interval(hours => $2) "
                           "WHERE exemplar_id = $1::uuid", eid, hours)
    await rel.create_draft(conn, writer="t9_blog", scope="tenant", tenant_id=a, created_by=f"admin:{ADMIN}")
    rule = await _rule(conn, a)

    async def next_draft_ids():
        d = await rel.create_draft(conn, writer="t9_blog", scope="tenant", tenant_id=a, rule_ids=[rule],
                                   created_by=f"admin:{ADMIN}")
        await rel.reject(conn, d["release_id"], actor=f"admin:{ADMIN}", reason="pytest: next draft probe")
        return {e["exemplar_id"] for e in await ex.release_exemplars(conn, d["release_id"])}

    first = await next_draft_ids()
    assert newest in first and len(first) == 2
    pinned_out = (set(old) - first).pop()
    await ex.set_status(conn, pinned_out, "pin", reason="the best voice sample", actor=f"admin:{ADMIN}")
    assert pinned_out in await next_draft_ids()
    await ex.set_status(conn, pinned_out, "never", reason="quotes a guest", actor=f"admin:{ADMIN}")
    assert pinned_out not in await next_draft_ids()
    with pytest.raises(ex.ExemplarError, match="already never"):
        await ex.set_status(conn, pinned_out, "never", reason="again", actor=f"admin:{ADMIN}")
    audits = await conn.fetch("SELECT action, details::text AS d FROM acp_shared.audit_log WHERE resource_id = $1 "
                              "ORDER BY chain_seq", pinned_out)
    assert [r["action"] for r in audits] == ["writing_memory.exemplar_pin", "writing_memory.exemplar_never"]
    assert all("quotes a guest" not in r["d"] for r in audits)                 # ids and statuses only


@pytest.mark.asyncio
async def test_an_example_marked_never_after_the_draft_blocks_activation(conn):
    a = await _tenant(conn)
    await _activate_release0(conn, "t9_blog", tenant=a)
    eid = await _exemplar(conn, a, BODY_A)
    draft = await rel.create_draft(conn, writer="t9_blog", scope="tenant", tenant_id=a,
                                   rule_ids=[await _rule(conn, a)], created_by=f"admin:{ADMIN}")
    await ex.set_status(conn, eid, "never", reason="a guest's words", actor=f"admin:{ADMIN}")
    with pytest.raises(rel.ReleaseError, match="marked never use"):
        await _activate_rules(conn, draft)
