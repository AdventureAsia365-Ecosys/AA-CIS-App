"""AA-920 WM-26 — the post-activation check of a release with content ($0, no model call, app ADR 0010).

Shadow is small after the approved cost cuts (20 inputs catch a 5% violation rate with 64% probability), so the net
after activation is this check: the writes made since a release went active are read back and

  carried       the write's stamp names this release with its fingerprint (`items_sha256`, insertion.stamp): it was
                written with exactly the approved rule items. A write whose stamp names another release (memory off,
                a write started before the activation) is `not_carried`; one that names this release but rendered
                nothing (`ignored`: the items failed their check, fail open) is `not_rendered`. Neither is a
                violation; both are counted and reported.
  QA trace      hard stop 3 (`eval_checks.qa_artefacts`, the same grep as R2 and the shadow, with the release's rule
                texts): a gate name, an A1 code, a taxonomy class, a citation tag, a memory / ledger key
                (`generation_meta`, `rule_ids`, `writing_release`, …) or a rule's text in the reader-facing text.
  isolation     hard stop 2 (`eval_checks.isolation_probe`): another tenant's brand name or >= 2 distinctive 6-word
                runs of its brand pack in the text. n-gram only, not the F10 embedding check.
  fingerprint   a carried write whose stamped `items_sha256` differs from the release's (the text sent is not the
                text approved).

Verdict per release: `violation` (any carried write fails one of the last three), `pass` (>= N carried writes, none
failing), `pending` (fewer than N yet), `not_applicable` (release 0 / an empty release renders nothing). With
`apply`, a violation moves the writer's pointer back at once (`releases.rollback`; a tenant release with no previous
is retired alone; a platform release with no previous turns the writer's memory off), writes one audit row (ids,
codes, counts; never text) and alerts (admin bell + the `ops_alert_email` job, template `writing_memory_rollback`).
The offending writes are named by id in the audit row and the result; no app row is changed (the piece keeps its
own gate verdicts).

Callers: the `writing_memory_post_activation` job (every active release with content, apply on; the api ops sampler
queues it hourly while one exists) and POST /admin/writing-memory/releases/{id}/post-activation-check (one release,
DRY RUN unless `apply`). Writers read: A1 `silver_aa_internal.generated_content` (S1 writer v2 rows), T2
`gold_aa_internal.tenant_tour_versions` + its `shared.job` result, T9 `acp_shared.content_piece` (approved / held).
Every tenant read filters the owner explicitly (cis_app's policies are USING (true)).
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

import structlog

from services.writing_memory import config as cfg
from services.writing_memory import eval_checks as ec

logger = structlog.get_logger()

VERDICTS = ("pass", "pending", "violation", "not_applicable")
PLATFORM_TENANTS = ("00000000-0000-0000-0000-000000000001",)   # the AA master tenant: never a "foreign" pack
_A1_FIELDS = ("aa_name", "aa_subtitle", "aa_summary", "aa_description", "aa_highlights", "aa_itineraries",
              "seo_title", "seo_meta")
_A1_SOURCE = ("src_name", "src_subtitle", "src_summary", "src_description", "src_highlights", "src_itineraries")


def _j(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


# ── the writes since an activation (read-only) ─────────────────────────────────────────────────────────────────────

_T9_SQL = """
    SELECT cp.piece_id::text AS ref, cp.tenant_id::text AS tenant_id, cp.created_at, cp.content_text, cp.seo_title,
           cp.meta_description, cp.writer_input
      FROM acp_shared.content_piece cp
     WHERE cp.created_at >= $1 AND ($2::timestamptz IS NULL OR cp.created_at < $2)
       AND cp.status IN ('approved', 'held')
       AND (CASE WHEN $3 THEN cp.writer_input->>'channel' = 'blog'
                 ELSE coalesce(cp.writer_input->>'channel', '') <> 'blog' END)
       AND ($4::uuid IS NULL OR cp.tenant_id = $4::uuid)
     ORDER BY cp.created_at, cp.piece_id
     LIMIT $5
"""

_A1_SQL = """
    SELECT gc.id::text AS ref, NULL::text AS tenant_id, gc.created_at, gc.aa_name, gc.aa_subtitle, gc.aa_summary,
           gc.aa_description, gc.aa_highlights, gc.aa_itineraries, gc.seo_title, gc.seo_meta,
           gc.metadata->'writing_release' AS stamp, rt.src_name, rt.src_subtitle, rt.src_summary, rt.src_description,
           rt.src_highlights, rt.src_itineraries
      FROM silver_aa_internal.generated_content gc
      LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = gc.tour_id
     WHERE gc.created_at >= $1 AND ($2::timestamptz IS NULL OR gc.created_at < $2)
       AND gc.metadata->>'writer_version' = 'v2'
     ORDER BY gc.created_at, gc.id
     LIMIT $3
"""

_T2_SQL = """
    SELECT v.id::text AS ref, v.tenant_id::text AS tenant_id, v.created_at, v.rewritten_content,
           j.result->'writing_release' AS stamp
      FROM gold_aa_internal.tenant_tour_versions v
      JOIN shared.job j ON j.id = v.job_id AND j.kind = 't2_rewrite'
     WHERE v.created_at >= $1 AND ($2::timestamptz IS NULL OR v.created_at < $2)
       AND ($3::uuid IS NULL OR v.tenant_id = $3::uuid)
     ORDER BY v.created_at, v.id
     LIMIT $4
"""


def _owner(release: dict) -> Optional[str]:
    return release.get("tenant_id") if release.get("scope") == "tenant" else None


async def writes_since(conn, release: dict, *, limit: int = cfg.POST_ACTIVATION_SCAN_MAX) -> list[dict]:
    """The writes of the release's writer (and owner, for a tenant release) since its activation, oldest first:
    {ref, source, tenant_id, created_at, stamp, text, allowed_text}. A retired release: up to its retirement."""
    writer, since, until = release["writer"], release["activated_at"], release.get("retired_at")
    owner = _owner(release)
    out: list[dict] = []
    if writer in ("t9_blog", "t9_social"):
        from services.writing_memory.eval_writers import allowed_text
        for r in await conn.fetch(_T9_SQL, since, until, writer == "t9_blog", owner, limit):
            wi = _j(r["writer_input"]) or {}
            text = "\n".join(t for t in (r["content_text"], r["seo_title"], r["meta_description"]) if t)
            try:
                allowed = allowed_text(writer, {"writer_input": wi, "tenant_id": r["tenant_id"]})
            except Exception:  # noqa: BLE001 — an old piece without a writer context: its own brand only
                allowed = ""
            out.append({"ref": r["ref"], "source": "content_piece", "tenant_id": r["tenant_id"],
                        "created_at": r["created_at"], "stamp": wi.get("writing_release"), "text": text,
                        "allowed_text": allowed})
    elif writer == "a1":
        for r in await conn.fetch(_A1_SQL, since, until, limit):
            text = "\n".join(ec.flatten_text(_j(r[k]) if k == "aa_highlights" else r[k]) for k in _A1_FIELDS
                             if r[k] is not None)
            src = "\n".join(ec.flatten_text(_j(r[k]) if k == "src_highlights" else r[k]) for k in _A1_SOURCE
                            if r[k] is not None)
            out.append({"ref": r["ref"], "source": "generated_content", "tenant_id": None,
                        "created_at": r["created_at"], "stamp": _j(r["stamp"]), "text": text, "allowed_text": src})
    elif writer == "t2":
        for r in await conn.fetch(_T2_SQL, since, until, owner, limit):
            out.append({"ref": r["ref"], "source": "tenant_tour_version", "tenant_id": r["tenant_id"],
                        "created_at": r["created_at"], "stamp": _j(r["stamp"]),
                        "text": ec.flatten_text(_j(r["rewritten_content"]) or {}), "allowed_text": ""})
    return out


_TENANTS_SQL = "SELECT tenant_id::text AS tenant_id FROM shared.tenants WHERE cancelled_at IS NULL ORDER BY 1"


async def tenant_fingerprints(conn) -> dict[str, dict]:
    """{tenant_id: tenant_fingerprint(brand pack)} of every live tenant but the AA master tenant: the foreign texts of
    the isolation probe (a write is checked against every tenant but its own owner)."""
    from services.acp_shared.brand_pack import fetch_brand_pack_conn
    out: dict[str, dict] = {}
    for r in await conn.fetch(_TENANTS_SQL):
        tid = r["tenant_id"]
        if tid in PLATFORM_TENANTS:
            continue
        try:
            pack = await fetch_brand_pack_conn(conn, tid)
        except Exception:  # noqa: BLE001 — a tenant without a pack has nothing to leak
            pack = None
        if pack:
            out[tid] = ec.tenant_fingerprint(pack)
    return out


async def release_rule_texts(conn, release: dict) -> list[tuple[str, str]]:
    ids = list(release.get("rule_ids") or [])
    if not ids:
        return []
    rows = await conn.fetch("SELECT rule_id::text AS rule_id, rule_text FROM shared.writing_rule "
                            "WHERE rule_id = ANY($1::uuid[])", ids)
    return [(r["rule_id"], r["rule_text"] or "") for r in rows]


# ── pure: one write, one release ───────────────────────────────────────────────────────────────────────────────────

def carriage(release: dict, stamp: Any) -> str:
    """How a write's stamp relates to the release: carried / not_carried / not_rendered / fingerprint_mismatch."""
    stamp = _j(stamp)
    part = (stamp or {}).get(release["scope"]) if isinstance(stamp, dict) else None
    if not isinstance(part, dict) or str(part.get("release_id")) != str(release["release_id"]):
        return "not_carried"
    if stamp.get("ignored"):
        return "not_rendered"
    if part.get("items_sha256") != release.get("items_sha256"):
        return "fingerprint_mismatch"
    return "carried"


def check_write(release: dict, write: dict, *, rule_texts: list[tuple[str, str]],
                foreign: dict[str, dict]) -> dict:
    """One write: {ref, source, carriage, qa_artefacts, isolation, violation (codes)}. Ids, codes and counts only."""
    how = carriage(release, write.get("stamp"))
    row: dict[str, Any] = {"ref": write["ref"], "source": write["source"], "carriage": how, "violation": []}
    if how in ("not_carried", "not_rendered"):
        return row
    if how == "fingerprint_mismatch":
        row["violation"].append("fingerprint_mismatch")
    text = write.get("text") or ""
    arte = ec.qa_artefacts(text, rule_texts=rule_texts)
    others = {t: fp for t, fp in foreign.items() if t != write.get("tenant_id")}
    iso = ec.isolation_probe(text, foreign=others, allowed_text=write.get("allowed_text") or "")
    row["qa_artefacts"] = arte[:20]
    row["isolation"] = {k: iso[k] for k in ("leak", "tenants", "weak_tenants", "name_hits", "shingle_hits")}
    if arte:
        row["violation"].append("qa_trace")
    if iso["leak"]:
        row["violation"].append("tenant_leak")
    return row


def verdict(release: dict, rows: list[dict], *, n: int = cfg.POST_ACTIVATION_N) -> dict:
    """The release's verdict from its checked writes (pure)."""
    counts = {k: sum(1 for r in rows if r["carriage"] == k)
              for k in ("carried", "not_carried", "not_rendered", "fingerprint_mismatch")}
    bad = [r for r in rows if r["violation"]]
    carried = counts["carried"] + counts["fingerprint_mismatch"]
    if release.get("content_class") == "empty" and not release.get("rule_ids"):
        v = "not_applicable"
    elif bad:
        v = "violation"
    elif carried >= n:
        v = "pass"
    else:
        v = "pending"
    codes: dict[str, int] = {}
    for r in bad:
        for c in r["violation"]:
            codes[c] = codes.get(c, 0) + 1
    return {"verdict": v, "n": n, "checked": len(rows), **counts, "violations": len(bad), "violation_codes": codes,
            "violating_refs": [{"source": r["source"], "ref": r["ref"], "codes": r["violation"]} for r in bad][:50]}


# ── the run ────────────────────────────────────────────────────────────────────────────────────────────────────────

def _iso(v):
    return v.isoformat() if isinstance(v, datetime) else v


async def check_release(pool, release_id: str, *, n: int = cfg.POST_ACTIVATION_N, apply: bool = False,
                        actor: str = "system:post_activation") -> dict:
    """Check one release (DRY RUN unless `apply`). Returns the report; with `apply` and a violation, `action` says
    what moved: rolled_back (to `active_release_id`), retired_alone, memory_off, or the refusal."""
    from services.writing_memory import releases as rel

    n = max(1, min(int(n), cfg.POST_ACTIVATION_SCAN_MAX))
    async with pool.acquire() as conn:
        release = await rel.get_release(conn, release_id)
        if release is None:
            raise rel.ReleaseError(404, "not_found", "Unknown release")
        base = {"release_id": release["release_id"], "writer": release["writer"], "scope": release["scope"],
                "tenant_id": release.get("tenant_id"), "release_no": release["release_no"],
                "content_class": release["content_class"], "status": release["status"],
                "activated_at": _iso(release.get("activated_at")), "items_sha256": release.get("items_sha256"),
                "apply": apply, "action": "none", "cost_usd": 0.0}
        if release.get("activated_at") is None:
            return {**base, "verdict": "not_applicable", "reason": "never activated", "n": n, "checked": 0}
        if release["content_class"] == "empty" and not release.get("rule_ids"):
            return {**base, **verdict(release, [], n=n)}
        writes = await writes_since(conn, release)
        texts = await release_rule_texts(conn, release)
        foreign = await tenant_fingerprints(conn)
    rows = [check_write(release, w, rule_texts=texts, foreign=foreign) for w in writes]
    report = {**base, **verdict(release, rows, n=n)}
    if report["verdict"] != "violation":
        return report
    logger.warning("wm_post_activation_violation", release_id=release["release_id"], writer=release["writer"],
                   scope=release["scope"], codes=report["violation_codes"], apply=apply)
    if not apply:
        report["action"] = "dry_run"
        return report
    if release["status"] != "active":
        report["action"] = "not_active"
        return report
    report.update(await _move_pointer(pool, release, report, actor=actor))
    await _alert(pool, release, report)
    return report


async def _move_pointer(pool, release: dict, report: dict, *, actor: str) -> dict:
    """The automatic rollback, in one transaction with its audit row."""
    from services.acp_shared.audit_log import write_audit_log
    from services.writing_memory import releases as rel

    codes = ",".join(sorted(report["violation_codes"]))
    reason = f"post_activation: {codes} on {report['violations']} of {report['carried']} writes"
    out: dict[str, Any] = {}
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                try:
                    now = await rel.rollback(conn, release["release_id"], actor=actor, reason=reason)
                    out = {"action": "rolled_back", "active_release_id": now["release_id"],
                           "active_release_no": now["release_no"]}
                except rel.ReleaseError as e:
                    if e.code != "no_previous":
                        raise
                    if release["scope"] == "tenant":
                        await rel.retire_without_previous(conn, release["release_id"], actor=actor, reason=reason)
                        out = {"action": "retired_alone", "active_release_id": None}
                    else:
                        await rel.set_memory_enabled(conn, release["writer"], False, actor=actor)
                        out = {"action": "memory_off", "active_release_id": release["release_id"]}
                await write_audit_log(
                    conn, tenant_id=release.get("tenant_id") or rel.PLATFORM_TENANT_ID, actor=actor[:128],
                    action="writing_memory.release_post_activation_failed", resource_type="writing_memory_release",
                    resource_id=str(release["release_id"]),
                    details={"writer": release["writer"], "scope": release["scope"],
                             "release_no": release["release_no"], "codes": report["violation_codes"],
                             "carried": report["carried"], "violations": report["violations"],
                             "refs": report["violating_refs"][:20], **out})
    except rel.ReleaseError as e:
        logger.error("wm_post_activation_rollback_refused", release_id=release["release_id"], code=e.code)
        return {"action": f"refused:{e.code}", "action_detail": e.detail[:300]}
    logger.warning("wm_post_activation_rolled_back", release_id=release["release_id"], **out)
    return out


_BELL_SQL = """
    INSERT INTO shared.notifications (tenant_id, actor_type, event_type, entity_type, entity_id, payload, target_roles)
    VALUES ($1::uuid, 'system', 'writing_memory_rollback', 'writing_memory_release', $2, $3::jsonb, ARRAY['admin'])
"""


async def _alert(pool, release: dict, report: dict) -> None:
    """Best effort: the admin bell and the operator e-mail (ops_alert_email). The audit row is the record."""
    from services.writing_memory.releases import PLATFORM_TENANT_ID
    from shared import ops_alerts

    payload = {"message": (f"Writing memory: {release['writer']} release {release['release_no']} "
                           f"({release['scope']}) failed its post-activation check ({report['action']})"),
               "writer": release["writer"], "scope": release["scope"], "release_no": release["release_no"],
               "release_id": release["release_id"], "action": report["action"],
               "codes": report["violation_codes"], "violations": report["violations"], "carried": report["carried"]}
    try:
        async with pool.acquire() as conn:
            await conn.execute(_BELL_SQL, PLATFORM_TENANT_ID, str(release["release_id"]),
                               json.dumps(payload, default=str))
            await ops_alerts.enqueue_writing_memory_rollback_email(conn, details=payload)
    except Exception as e:  # noqa: BLE001 — the pointer already moved; an alert failure is logged, never raised
        logger.warning("wm_post_activation_alert_failed", release_id=release["release_id"], error=str(e)[:200])


_CONTENT_ACTIVE_SQL = """
    SELECT release_id::text AS release_id FROM shared.writing_memory_release
     WHERE status = 'active' AND (content_class <> 'empty' OR cardinality(rule_ids) > 0)
     ORDER BY writer, scope, tenant_id NULLS FIRST
"""


async def content_releases(conn) -> list[str]:
    """The active releases a check applies to (anything but an empty release)."""
    return [r["release_id"] for r in await conn.fetch(_CONTENT_ACTIVE_SQL)]


async def check_all(pool, *, n: int = cfg.POST_ACTIVATION_N, apply: bool = True,
                    actor: str = "job:writing_memory_post_activation") -> dict:
    """Every active release with content. Today (no approved rule) this checks nothing and changes nothing."""
    async with pool.acquire() as conn:
        ids = await content_releases(conn)
    reports = []
    for rid in ids:
        try:
            reports.append(await check_release(pool, rid, n=n, apply=apply, actor=actor))
        except Exception as e:  # noqa: BLE001 — one release's failure never skips the others
            logger.error("wm_post_activation_check_failed", release_id=rid, error=f"{type(e).__name__}: {str(e)[:200]}")
            reports.append({"release_id": rid, "verdict": "error", "error": type(e).__name__})
    return {"releases": len(ids), "reports": reports, "cost_usd": 0.0,
            "verdicts": {v: sum(1 for r in reports if r.get("verdict") == v) for v in (*VERDICTS, "error")}}


__all__ = ["VERDICTS", "carriage", "check_all", "check_release", "check_write", "content_releases", "verdict",
           "writes_since"]
