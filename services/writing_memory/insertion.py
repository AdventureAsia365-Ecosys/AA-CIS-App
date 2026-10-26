"""AA-919 WM-18 / WM-20 — the writing-memory insertion path of the four writers (app ADR 0010).

A writer call resolves ONE view of its writer's active releases (the platform pointer, plus the tenant's pointer for
a tenant writer) and passes it to the prompt builder, which asks this module for the text of its slot:

  slot            writer          where the builder puts it
  a1_house_rules  A1 (S1 v2)      `s1_writer_v2.w_system_prompt`: after the forbidden-words line, before "Output ONLY"
  t2_block        T2              `batch_prompt.build_t2_system_prompt`: the end of the system prompt (before the
                                  em-dash fold `t2_dashless`)
  t9_platform     T9 blog/social  `generate.write_content` / `rewrite_with_feedback`: appended to SYSTEM_PROMPT
  t9_tenant       T9 blog/social  `prompts.build_user_prompt`: the lines right after the brand's words line

Release 0 (content class `empty`) renders "" in every slot, so the prompt is byte-identical to the prompt without
memory (WM-20, ADR 0010 decision 12); the replay script and the CI test prove it. A release with content is not wired
yet (WM-23 / WM-24 / WM-25 decide what each slot says): its slot renders "" too, the call is logged
(`wm_release_content_ignored`) and the stamp says `ignored: true`; the API refuses to activate such a release
(`content_not_wired`, services/writing_memory/releases.py) until those issues land, so this is a second fence.

The resolver fails open (ADR 0010: memory never blocks a write): any error, a missing table, the switch off or no
active release gives None, and the writer runs exactly as without memory. It filters the tenant release with an
explicit `tenant_id = $2` (cis_app's policy on the release table is USING (true), ADR 0010 decision 10).

The stamp is written by the caller of the writer that consumed the view (critic 29): A1 metadata.writing_release
(only the v2 writer has the hook), T2 shared.job.result.writing_release (every T2 write goes through
build_t2_system_prompt), T9 content_piece.writer_input.writing_release (every T9 write goes through write_content /
rewrite_with_feedback).
"""
from __future__ import annotations

from typing import Optional

import structlog

logger = structlog.get_logger()

WRITERS = ("a1", "t2", "t9_blog", "t9_social")
SLOTS = ("a1_house_rules", "t2_block", "t9_platform", "t9_tenant")

_RESOLVE_SQL = """
    SELECT w.memory_enabled, r.release_id::text AS release_id, r.scope, r.release_no, r.content_class,
           r.rule_ids::text[] AS rule_ids
      FROM shared.writing_memory_writer w
      LEFT JOIN shared.writing_memory_release r
             ON r.writer = w.writer AND r.status = 'active'
            AND (r.scope = 'platform' OR (r.scope = 'tenant' AND r.tenant_id = $2::uuid))
     WHERE w.writer = $1
"""


def writer_for_channel(channel: Optional[str]) -> str:
    """The T9 writer of a channel: blog → t9_blog, every other channel → t9_social."""
    return "t9_blog" if channel == "blog" else "t9_social"


def _part(row) -> dict:
    return {"release_id": row["release_id"], "release_no": int(row["release_no"]),
            "content_class": row["content_class"], "rule_ids": list(row["rule_ids"] or [])}


async def resolve(db, writer: str, tenant_id: Optional[str] = None) -> Optional[dict]:
    """The view a writer call uses: {"writer", "platform": part | None, "tenant": part | None}, a part being
    {release_id, release_no, content_class, rule_ids}. None when memory is off for the writer, no release is active,
    or anything fails (fail open). `db` is a pool or a connection. A1 never reads a tenant release."""
    if writer not in WRITERS:
        return None
    tid = str(tenant_id) if (tenant_id and writer != "a1") else None
    try:
        rows = await db.fetch(_RESOLVE_SQL, writer, tid)
    except Exception as e:  # noqa: BLE001 — memory never blocks a write (fail open)
        logger.warning("wm_release_unavailable", writer=writer, error=f"{type(e).__name__}: {str(e)[:200]}")
        return None
    if not rows or not rows[0]["memory_enabled"]:
        return None
    view: dict = {"writer": writer, "platform": None, "tenant": None}
    for r in rows:
        if r["release_id"] is None:
            continue
        if r["scope"] == "tenant" and tid is None:
            continue                                 # never a tenant release without its tenant (defence in depth)
        view["platform" if r["scope"] == "platform" else "tenant"] = _part(r)
    if view["platform"] is None and view["tenant"] is None:
        return None
    return view


def _parts(view: Optional[dict]) -> list[dict]:
    if not view:
        return []
    return [p for p in (view.get("platform"), view.get("tenant")) if p]


def has_content(view: Optional[dict]) -> bool:
    """True when a resolved release carries rules (anything but the empty class)."""
    return any(p.get("content_class") != "empty" or p.get("rule_ids") for p in _parts(view))


def _content(view: Optional[dict], slot: str) -> str:
    """The text of one slot for this view. Release 0 (empty) and no view: "". A release with content is not wired
    until WM-23 / 24 / 25: "" and a log line (the API also refuses to activate one)."""
    if not has_content(view):
        return ""
    logger.warning("wm_release_content_ignored", slot=slot, writer=(view or {}).get("writer"),
                   releases=[p.get("release_id") for p in _parts(view)])
    return ""


def a1_house_rules(view: Optional[dict]) -> str:
    return _content(view, "a1_house_rules")


def t2_block(view: Optional[dict]) -> str:
    return _content(view, "t2_block")


def t9_platform_block(view: Optional[dict]) -> str:
    return _content(view, "t9_platform")


def t9_tenant_lines(view: Optional[dict]) -> list[str]:
    text = _content(view, "t9_tenant")
    return [text] if text else []


def stamp(view: Optional[dict], **extra) -> Optional[dict]:
    """What a written output records about the memory it was written with (ids and numbers only): None without a
    view, so an output written with memory off keeps its shape (ADR 0010 decision 12)."""
    if not view:
        return None

    def _s(p):
        return None if p is None else {"release_id": p["release_id"], "release_no": p["release_no"]}

    out = {"writer": view.get("writer"), "platform": _s(view.get("platform")), "tenant": _s(view.get("tenant")),
           "ignored": has_content(view)}
    out.update({k: v for k, v in extra.items() if v is not None})
    return out
