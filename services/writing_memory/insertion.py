"""AA-919 WM-18 / WM-20, AA-920 WM-23 / WM-24 — the writing-memory insertion path of the four writers (app ADR 0010).

A writer call resolves ONE view of its writer's active releases (the platform pointer, plus the tenant's pointer for
a tenant writer) and passes it to the prompt builder, which asks this module for the text of its slot:

  slot            writer          where the builder puts it                              what it renders
  a1_house_rules  A1 (S1 v2)      `s1_writer_v2.w_system_prompt`: after the forbidden-   platform rules
                                  words line, before "Output ONLY"
  t2_block        T2              `batch_prompt.build_t2_system_prompt`: the end of the  platform rules, then the
                                  system prompt (before the em-dash fold `t2_dashless`)  tenant's
  t9_platform     T9 blog/social  `generate.write_content` / `rewrite_with_feedback`:    platform rules of the channel
                                  appended to SYSTEM_PROMPT (cached across tenants)
  t9_tenant       T9 blog/social  `prompts.build_user_prompt`: the lines right after the the tenant's rules of the
                                  brand's words line                                     channel
  repair hints    A1 flag-fix,    `flag_fix_node` after AUDIT ISSUES (only the flagged    `repair_hint` rules whose
                  T9 rewrite      codes), T9 `rewrite_with_feedback` feedback (only the  gate class failed
                                  failed gates)

**One builder** (`render_rules` / `_block` / `repair_hints`): every slot is rendered by the same code from the same
rule items, so A1, T2 and T9 never format a rule differently (lesson S218: one value, one function). Rules are
constraint-only (they cut, ban, rephrase, format); a rule line is one line (whitespace folded: a rule cannot open a
heading of its own), and a `repair_hint` rule never enters a writing slot, only a repair pass for its gate class.
Lexicon rules render as "Never write" / "Write X instead of Y" lines inside the rules block of a `rules` release; a
`lexicon_only` release is not rendered here (its path is L0, `l0.py`; `releases.wiring_problem`).

**The view carries the rule items** (`items`, loaded by `load_items`, the one loader of `resolve` and the R2 / shadow
stores): the prompt builders are sync and never read the DB. A part renders only when its items are complete and
their hash equals the release's `items_sha256` (the text approved is the text sent); otherwise it renders "" and the
stamp says `ignored: true` — the post-activation check (`post_activation.py`) reports such a write as not carrying
the release.

Release 0 (content class `empty`) and no view render "" in every slot, so the prompt is byte-identical to the prompt
without memory (WM-20, ADR 0010 decision 12); `tests/unit/test_aa919_release0_replay.py` (pins) and
`tests/unit/test_aa920_injection.py` prove it.

The resolver fails open (ADR 0010: memory never blocks a write): any error, a missing table, the switch off or no
active release gives None, and the writer runs exactly as without memory. It filters the tenant release with an
explicit `tenant_id = $2` (cis_app's policy on the release table is USING (true), ADR 0010 decision 10), and the rule
items of a part must belong to that part's writer and owner (defence in depth over the release CHECK / trigger).

The stamp is written by the caller of the writer that consumed the view (critic 29): A1 metadata.writing_release
(only the v2 writer has the hook), T2 shared.job.result.writing_release (every T2 write goes through
build_t2_system_prompt), T9 content_piece.writer_input.writing_release (every T9 write goes through write_content /
rewrite_with_feedback). It holds ids, numbers and the items hash (the release fingerprint), never rule text.
"""
from __future__ import annotations

from typing import Iterable, Optional

import structlog

logger = structlog.get_logger()

WRITERS = ("a1", "t2", "t9_blog", "t9_social")
SLOTS = ("a1_house_rules", "t2_block", "t9_platform", "t9_tenant")

_RESOLVE_SQL = """
    SELECT w.memory_enabled, r.release_id::text AS release_id, r.scope, r.tenant_id::text AS tenant_id, r.release_no,
           r.content_class, r.rule_ids::text[] AS rule_ids, r.items_sha256
      FROM shared.writing_memory_writer w
      LEFT JOIN shared.writing_memory_release r
             ON r.writer = w.writer AND r.status = 'active'
            AND (r.scope = 'platform' OR (r.scope = 'tenant' AND r.tenant_id = $2::uuid))
     WHERE w.writer = $1
"""

# The rule items of one release part (cis_app sees every row: the part's writer / owner are checked in Python).
_ITEMS_SQL = """
    SELECT rule_id::text AS rule_id, kind, writer, scope, tenant_id::text AS tenant_id, applies_to, gate_class,
           rule_text, replacement
      FROM shared.writing_rule
     WHERE rule_id = ANY($1::uuid[])
"""

# Writing slots: which kinds render where. `repair_hint` never renders in a writing slot (WM-23 scope 2 / WM-24
# scope 3: only in the repair pass of its gate).
WRITING_KINDS = ("writer_instruction", "lexicon", "preference_summary")
_KIND_ORDER = {"writer_instruction": 0, "lexicon": 1, "preference_summary": 2, "repair_hint": 3}
PLATFORM_HEADING = ("HOUSE RULES (approved writing rules; they only cut, ban, rephrase or format. Every rule above "
                    "and every fact in the source outrank them):")
TENANT_HEADING = ("THIS BRAND'S WRITING RULES (approved for this brand; they only cut, ban, rephrase or format, never "
                  "add a fact):")
REPAIR_HEADING = "REPAIR HINTS (approved; only for the problems listed above):"


def writer_for_channel(channel: Optional[str]) -> str:
    """The T9 writer of a channel: blog → t9_blog, every other channel → t9_social."""
    return "t9_blog" if channel == "blog" else "t9_social"


# ── the view ───────────────────────────────────────────────────────────────────────────────────────────────────────

def part_of(row: Optional[dict]) -> Optional[dict]:
    """A view part from a release row (the resolver's, the R2 / shadow stores'): ids, numbers, the items hash and, when
    loaded, the rule items. The one constructor of a part (r2.build_views uses it too)."""
    if not row or row.get("release_id") is None:
        return None
    part = {"release_id": row["release_id"], "release_no": int(row["release_no"]),
            "content_class": row["content_class"], "rule_ids": list(row.get("rule_ids") or [])}
    for k in ("scope", "tenant_id", "items_sha256"):
        if row.get(k) is not None:
            part[k] = row[k]
    if row.get("items") is not None:
        part["items"] = [dict(i) for i in row["items"]]
    if row.get("exemplars"):
        part["exemplars"] = [dict(e) for e in row["exemplars"]]
    return part


async def load_items(db, row: dict, writer: str, exemplars: Optional[list[dict]] = None) -> Optional[list[dict]]:
    """The rule items of one release row, checked: every rule exists, belongs to `writer` and to the row's owner (scope
    + tenant), and the items hash equals the row's `items_sha256` (AA-921: over the rules and the release's pinned
    `exemplars`, exemplars.release_sha256). None when any check fails (logged; the part then renders nothing). [] for
    a release without rules. `db` is a pool or a connection."""
    from services.writing_memory.exemplars import release_sha256

    ids = [str(i) for i in (row.get("rule_ids") or [])]
    if not ids:
        return []
    rows = [dict(r) for r in await db.fetch(_ITEMS_SQL, ids)]
    found = {r["rule_id"]: r for r in rows}
    problem = None
    if set(found) != set(ids):
        problem = "rule_missing"
    elif any(r["writer"] != writer or r["scope"] != row.get("scope")
             or (r.get("tenant_id") or None) != (row.get("tenant_id") or None) for r in rows):
        problem = "rule_isolation"
    elif not row.get("items_sha256") or release_sha256(rows, exemplars) != row["items_sha256"]:   # no hash, no render
        problem = "items_changed"
    if problem:
        logger.warning("wm_release_items_refused", writer=writer, release_id=row.get("release_id"), problem=problem)
        return None
    items = [{"rule_id": r["rule_id"], "kind": r["kind"], "applies_to": list(r.get("applies_to") or []),
              "gate_class": r.get("gate_class"), "rule_text": r["rule_text"], "replacement": r.get("replacement")}
             for r in rows]
    return sorted(items, key=lambda i: (_KIND_ORDER.get(i["kind"], 9), i["rule_id"]))


async def attach_items(db, row: Optional[dict], writer: str) -> Optional[dict]:
    """`row` with its checked `items` (when it has rules and they pass `load_items`) and, AA-921, its checked pinned
    `exemplars` (exemplars.load_release_exemplars; they are part of the items hash). Fails open: any error leaves the
    row without items (it renders nothing, the stamp says ignored)."""
    if not row or not row.get("rule_ids") or row.get("items") is not None:
        return row
    from services.writing_memory.exemplars import load_release_exemplars
    try:
        exemplars = await load_release_exemplars(db, row, writer)
        items = await load_items(db, row, writer, exemplars) if exemplars is not None else None
    except Exception as e:  # noqa: BLE001 — memory never blocks a write (fail open)
        logger.warning("wm_release_items_unavailable", writer=writer, error=f"{type(e).__name__}: {str(e)[:200]}")
        items = exemplars = None
    if items is None:
        return row
    return {**row, "items": items, "exemplars": exemplars or []}


async def resolve(db, writer: str, tenant_id: Optional[str] = None) -> Optional[dict]:
    """The view a writer call uses: {"writer", "platform": part | None, "tenant": part | None} (`part_of`). None when
    memory is off for the writer, no release is active, or anything fails (fail open). `db` is a pool or a
    connection. A1 never reads a tenant release."""
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
        view["platform" if r["scope"] == "platform" else "tenant"] = part_of(
            await attach_items(db, dict(r), writer))
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


def _renders(part: Optional[dict]) -> bool:
    """A part renders when it has content and its checked items are loaded (complete: one item per rule id)."""
    if not part or (part.get("content_class") == "empty" and not part.get("rule_ids")):
        return False
    items = part.get("items")
    return items is not None and {i["rule_id"] for i in items} == set(part.get("rule_ids") or [])


def ignored(view: Optional[dict]) -> bool:
    """True when the view holds a release with content that renders nothing (items not loaded or refused)."""
    return any(not _renders(p) for p in _parts(view) if p.get("content_class") != "empty" or p.get("rule_ids"))


# ── the one builder ────────────────────────────────────────────────────────────────────────────────────────────────

def _one_line(text: Optional[str]) -> str:
    return " ".join(str(text or "").split())


def _applies(item: dict, channel: Optional[str]) -> bool:
    chans = item.get("applies_to") or []
    return not chans or channel is None or channel in chans


def rule_line(item: dict) -> str:
    """One rule as one prompt line."""
    text = _one_line(item.get("rule_text"))
    if item.get("kind") == "lexicon":
        rep = _one_line(item.get("replacement"))
        return f'- Write "{rep}" instead of "{text}".' if rep else f'- Never write "{text}".'
    if item.get("kind") == "preference_summary":
        return f"- Voice: {text}"
    return f"- {text}"


def render_rules(items: Iterable[dict], *, kinds: Iterable[str] = WRITING_KINDS,
                 channel: Optional[str] = None) -> list[str]:
    """The rule lines of `items` of the given kinds that apply to `channel` (None = every channel), in item order."""
    ks = set(kinds)
    return [rule_line(i) for i in items
            if i.get("kind") in ks and _applies(i, channel) and _one_line(i.get("rule_text"))]


def _block(view: Optional[dict], scope: str, channel: Optional[str]) -> list[str]:
    """The heading + rule lines of one part of `view`, [] when it renders nothing."""
    part = (view or {}).get(scope)
    if not _renders(part):
        return []
    lines = render_rules(part["items"], channel=channel)
    if not lines:
        return []
    return [PLATFORM_HEADING if scope == "platform" else TENANT_HEADING, *lines]


EXAMPLES_HEADING = ("VOICE EXAMPLES (voice only, reuse no fact): they show this brand's tone, rhythm and sentence "
                    "shape. Every place, name, number, price, time and service in them belongs to another text and is "
                    "masked as [NAME], [N], [PRICE], [TIME] or [URL]; never copy a fact, a phrase or a masked token "
                    "from them:")


def voice_examples(view: Optional[dict], channel: Optional[str]) -> list[tuple[str, str]]:
    """AA-921 WM-27: (exemplar_id, masked_text) of the examples a write of `channel` shows: the tenant part's examples
    of the channel (by position), filled up to exemplars.PER_CHANNEL from the platform part's (never another
    tenant's). Only from a part that renders (its items and examples passed the hash check)."""
    from services.writing_memory.exemplars import PER_CHANNEL
    out: list[tuple[str, str]] = []
    for scope in ("tenant", "platform"):
        part = (view or {}).get(scope)
        if not _renders(part):
            continue
        for e in sorted(part.get("exemplars") or [], key=lambda x: int(x.get("position") or 0)):
            if len(out) >= PER_CHANNEL:
                break
            if channel is not None and e.get("channel") != channel:
                continue
            text = _one_line(e.get("masked_text"))
            if text and all(text != t for _, t in out):
                out.append((str(e["exemplar_id"]), text))
    return out


def example_lines(view: Optional[dict], channel: Optional[str]) -> list[str]:
    """The heading + one line per example ([] without one)."""
    ex = voice_examples(view, channel)
    if not ex:
        return []
    return [EXAMPLES_HEADING, *[f"Example {i}: {text}" for i, (_, text) in enumerate(ex, 1)]]


def a1_house_rules(view: Optional[dict]) -> str:
    """A1: the platform HOUSE RULES lines (one string, joined by the writer's own "\\n" join); "" without content."""
    return "\n".join(_block(view, "platform", None))


def t2_block(view: Optional[dict]) -> str:
    """T2: platform then tenant blocks, appended to the system prompt ("" without content)."""
    blocks = [b for b in (_block(view, "platform", None), _block(view, "tenant", None),
                          example_lines(view, "tour_page")) if b]
    return "".join("\n\n" + "\n".join(b) for b in blocks)


def t9_platform_block(view: Optional[dict], channel: Optional[str] = None) -> str:
    """T9: the platform block of the channel, appended to SYSTEM_PROMPT ("" without content)."""
    b = _block(view, "platform", channel)
    return "\n\n" + "\n".join(b) if b else ""


def t9_tenant_lines(view: Optional[dict], channel: Optional[str] = None) -> list[str]:
    """T9: the tenant block of the channel as user-prompt lines, then (AA-921) the channel's pinned voice examples
    (tenant first, platform fill; never in the cached SYSTEM_PROMPT: they are tenant text). [] without content."""
    return [*_block(view, "tenant", channel), *example_lines(view, channel)]


def _gate_class(code: str) -> Optional[str]:
    from services.writing_memory.mining import gate_class
    try:
        return gate_class(code)
    except Exception:  # noqa: BLE001 — a registry problem means no hint, never a failed repair
        return None


def repair_hints(view: Optional[dict], failed: Iterable[str], *, channel: Optional[str] = None) -> list[str]:
    """The `repair_hint` lines whose gate class is one of the failed gates' / codes' classes (platform first, then the
    tenant's), for the repair pass only. [] without content or without a matching hint."""
    parts = [p for p in _parts(view) if _renders(p)]
    if not parts:
        return []
    classes = {c for c in (_gate_class(f) for f in failed or () if f) if c}
    if not classes:
        return []
    out: list[str] = []
    for p in parts:
        hits = [i for i in p["items"] if i.get("kind") == "repair_hint" and i.get("gate_class") in classes]
        out += render_rules(hits, kinds=("repair_hint",), channel=channel)
    return out


def repair_hint_block(view: Optional[dict], failed: Iterable[str], *, channel: Optional[str] = None) -> str:
    """A1 flag-fix: the hints as a block appended to AUDIT ISSUES ("" without a matching hint)."""
    lines = repair_hints(view, failed, channel=channel)
    return "\n\n" + "\n".join([REPAIR_HEADING, *lines]) if lines else ""


def with_repair_hints(view: Optional[dict], feedback: list, failed: Iterable[str], *,
                      channel: Optional[str] = None) -> list:
    """T9 rewrite: the revision feedback plus the matching hints (the same list object when there is none)."""
    lines = repair_hints(view, failed, channel=channel)
    return [*feedback, *lines] if lines else feedback


def exemplar_texts(view: Optional[dict]) -> list[tuple[str, str]]:
    """(exemplar_id, masked_text) of every example the view's rendering parts pin (the carry-over check's input)."""
    return [(str(e["exemplar_id"]), e.get("masked_text") or "") for p in _parts(view) if _renders(p)
            for e in p.get("exemplars") or []]


def rule_texts(view: Optional[dict]) -> list[tuple[str, str]]:
    """(rule_id, text) of every rendered rule of the view: what the QA-artefact grep looks for in an output."""
    return [(i["rule_id"], i.get("rule_text") or "") for p in _parts(view) if _renders(p) for i in p["items"]]


# ── the stamp ──────────────────────────────────────────────────────────────────────────────────────────────────────

def stamp(view: Optional[dict], **extra) -> Optional[dict]:
    """What a written output records about the memory it was written with (ids, numbers and the items hash only): None
    without a view, so an output written with memory off keeps its shape (ADR 0010 decision 12)."""
    if not view:
        return None

    def _s(p):
        if p is None:
            return None
        out = {"release_id": p["release_id"], "release_no": p["release_no"]}
        if p.get("items_sha256"):
            out["items_sha256"] = p["items_sha256"]
        return out

    out = {"writer": view.get("writer"), "platform": _s(view.get("platform")), "tenant": _s(view.get("tenant")),
           "ignored": ignored(view)}
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


def generation_meta(view: Optional[dict], *, prompt_version: Optional[str] = None) -> Optional[dict]:
    """AA-920 WM-25: what a T2 version records in `tenant_tour_versions.generation_meta` (CIS 297).

    `writing_release` + `rule_ids` (only with a view): the stamp plus the rule ids the view carried (platform part
    first, then the tenant's; no duplicate). `writer_prompt_version` (when known): sha256[:8] of the exact system prompt
    of the kept draft (`batch_prompt.prompt_version_of`, as generated_content.metadata.prompt_version; WM-08 left T2
    out for want of this column). None when neither is there: the column stays NULL. Ids, numbers and hashes only,
    never rule text; the column is hidden from every tenant read (`_tenant_projection.TENANT_HIDDEN_VERSION_FIELDS`)."""
    out: dict = {}
    if prompt_version:
        out["writer_prompt_version"] = str(prompt_version)
    if view:
        rule_ids: list[str] = []
        for p in _parts(view):
            for rid in p.get("rule_ids") or []:
                if str(rid) not in rule_ids:
                    rule_ids.append(str(rid))
        out.update({"writing_release": stamp(view), "rule_ids": rule_ids})
    return out or None


__all__ = ["EXAMPLES_HEADING", "PLATFORM_HEADING", "REPAIR_HEADING", "SLOTS", "TENANT_HEADING", "WRITERS",
           "WRITING_KINDS", "a1_house_rules", "attach_items", "example_lines", "exemplar_texts", "generation_meta",
           "has_content", "ignored", "load_items", "part_of", "voice_examples",
           "render_rules", "repair_hint_block", "repair_hints", "resolve", "rule_line", "rule_texts", "stamp",
           "t2_block", "t9_platform_block", "t9_tenant_lines", "with_repair_hints", "writer_for_channel"]
