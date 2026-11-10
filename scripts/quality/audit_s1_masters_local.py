"""AA-1049 — the local half of the S1 Master audit (``audit_s1_masters.py``): decode its ecsx output and write the
public-safe README. No DB, no network, no model; standard library only.

    python3 apps/AA-CIS-App/scripts/quality/audit_s1_masters_local.py <ecsx output file> \\
        [--work <dir>] [--readme docs/audits/2026-11-11-S277-s1-master-audit/README.md]

1. decodes the two gzip+base64 blocks (``s1_master_audit.jsonl``: one line per Master, ``s1_master_audit_summary.json``)
   into ``--work`` (default: the gitignored ``<root>/.tmp-session/s1-master-audit-<time>``); the per-Master lines stay
   there (they are never committed);
2. writes the README from the summary only: ids, codes and counts (no page text, no source text, no brand words).

Exit 2 when a block is missing or the summary schema is not the audit's.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import gzip
import json
import re
import sys
from pathlib import Path

SCHEMA = "aa1049-s1-master-audit/1"
LINES_BLOCK = "s1_master_audit.jsonl"
SUMMARY_BLOCK = "s1_master_audit_summary.json"
GROUPS = ("all", "v2", "older")


def tmp_session(start: Path) -> Path | None:
    """The workspace's gitignored ``.tmp-session`` dir: the nearest one at or above ``start``."""
    for d in (start, *start.parents):
        if (d / ".tmp-session").is_dir() and d.name != ".tmp-session":
            return d / ".tmp-session"
    return None


def decode_block(text: str, name: str) -> bytes:
    m = re.search(rf"=== BEGIN {re.escape(name)}\n(.*?)\n=== END {re.escape(name)}", text, re.S)
    if not m:
        raise ValueError(f"block {name} missing")
    return gzip.decompress(base64.b64decode("".join(m.group(1).split())))


def _table(title: str, data: dict, *, key: str = "code", top: int = 0) -> list[str]:
    """One Markdown table: a row per key, a column per writer group (all / v2 / older)."""
    keys = sorted({k for g in GROUPS for k in (data.get(g) or {})},
                  key=lambda k: (-(data.get("all") or {}).get(k, 0), str(k)))
    if top:
        keys = keys[:top]
    lines = [f"### {title}", ""]
    if not keys:
        return lines + ["none", ""]
    lines += [f"| {key} | all | v2 | older |", "|---|---:|---:|---:|"]
    lines += [f"| `{k}` | " + " | ".join(str((data.get(g) or {}).get(k, 0)) for g in GROUPS) + " |" for k in keys]
    return lines + [""]


def _counts(title: str, data: dict, key: str) -> list[str]:
    if not data:
        return [f"### {title}", "", "none", ""]
    rows = sorted(data.items(), key=lambda kv: (-kv[1], str(kv[0])))
    return [f"### {title}", "", f"| {key} | tours |", "|---|---:|", *(f"| `{k}` | {v} |" for k, v in rows), ""]


def readme(summary: dict) -> str:
    """The audit README from the summary: ids, codes and counts only."""
    m = summary.get("masters") or {}
    pop = summary.get("population") or {}
    act = pop.get("active_masters") or {}
    out = [
        "# S1 Master audit (S277)", "",
        f"Generated {summary.get('generated_at')} by `apps/AA-CIS-App/scripts/quality/audit_s1_masters.py` "
        f"(schema `{summary.get('schema')}`, contract `{summary.get('contract_version')}`), read-only, no model call.",
        "Every active Master of the AA master tenant, judged on its published version and its raw source with the "
        "code that is on CIS main today. Ids, codes and counts only.", "",
        "## How many Masters", "",
        f"- Active Masters (master tenant): **{act.get('master_tenant')}**; other tenants: {act.get('other_tenants')}.",
        f"- Audited: **{m.get('all', 0)}** = S1 writer v2 **{m.get('v2', 0)}** + older writers **{m.get('older', 0)}**"
        + (f" (LIMIT {summary.get('limit')})" if summary.get("limit") else "") + ".",
        f"- Brand rule: active {((summary.get('brand_rule') or {}).get('active'))}, "
        f"{(summary.get('brand_rule') or {}).get('forbidden_words')} forbidden words.", "",
    ]
    out += _table("Writer of the published version", summary.get("writers") or {}, key="writer")
    out += ["## What happened to the other tours", "",
            f"The master tenant has **{pop.get('raw_tours')}** source rows (`raw_tours`, every version and state).", ""]
    out += _counts("Source state (deleted, or source status / lifecycle stage)", pop.get("source") or {}, "state")
    out += _counts("Master state (master status / deleted / reason; none = never published)",
                   pop.get("master") or {}, "state")
    unm = pop.get("unmastered_live_sources") or []
    out += [f"### Live sources without an active Master: {len(unm)}", ""]
    out += _counts("Their pipeline status", pop.get("pipeline_unmastered") or {}, "pipeline_status")
    out += _counts("Their latest version status", pop.get("latest_unmastered") or {}, "status")
    if unm:
        out += ["| tour_id | pipeline | lifecycle | master | reason | latest version |", "|---|---|---|---|---|---|"]
        out += [f"| `{u['tour_id']}` | {u['pipeline_status']} | {u['lifecycle_stage']} | {u['master_status']} | "
                f"{u['master_status_reason'] or ''} | {u['latest_version_status'] or 'none'} |" for u in unm]
        out += [""]
    out += ["## Final contract verdict (recomputed today, brand words included)", ""]
    out += _table("Verdict", summary.get("verdict") or {}, key="verdict")
    out += _table("Stored verdict -> today", summary.get("verdict_changed") or {}, key="change")
    out += _table("Blocking findings (Masters per code)", summary.get("block_codes") or {})
    out += _table("Warnings (Masters per code)", summary.get("warn_codes") or {})
    out += ["## S1 writing rules on the stored text (deterministic, today's code)", ""]
    out += _table("validate codes", summary.get("validate_codes") or {})
    out += _table("validate codes not in the stored failure codes (new since the version was written)",
                  summary.get("validate_new") or {})
    out += _table("Brand pre-audit codes", summary.get("pre_audit_codes") or {})
    out += _table("Pre-audit codes that need Jev to confirm (not run)", summary.get("pre_audit_needs_model") or {})
    out += _table("S1 code checks (Masters per code)", summary.get("s1_check_codes") or {})
    out += _table("Numeric grounding (sentences with a figure the source lacks)",
                  summary.get("grounding_numeric") or {}, key="count")
    out += _table("Forbidden words, brand list (Masters per field)", summary.get("brand_word_fields") or {},
                  key="field")
    out += _table("Forbidden words, AA core list (Masters per field)", summary.get("core_word_fields") or {},
                  key="field")
    out += ["## S1 train compose diff (what code composition would change today)", "",
            "Fields counted per Master that would differ: `day_headings`, `fact_lines` (verbatim meals / overnight "
            "lines), `day_prose`, `day_count`, `seo_title` (word-boundary fit), `seo_meta` (code template). `slug` is "
            "not applicable on S1 (no slug column). The train composes v2 pages only; older versions are measured "
            "for comparison.", ""]
    out += _table("Masters with any composed field changed", summary.get("compose_any") or {}, key="count")
    out += _table("Masters per changed field", summary.get("compose_fields") or {}, key="field")
    out += ["## Not run (need a model) and errors", ""]
    out += [f"- `{k}`: {v}" for k, v in (summary.get("not_run_why") or {}).items()] + [""]
    out += _table("Not run (Masters)", summary.get("not_run") or {}, key="check")
    out += _table("Section errors (Masters)", summary.get("errors") or {}, key="section")
    out += _table("Source state of the active Masters", summary.get("source_state") or {}, key="state")
    np_ = summary.get("not_publish") or []
    out += [f"## Masters whose verdict is not publish: {len(np_)}", ""]
    if np_:
        out += ["| tour_id | writer | verdict | blocking codes |", "|---|---|---|---|"]
        out += [f"| `{r['tour_id']}` | {r['writer_group']} | {r['verdict']} | {', '.join(r['block_codes'])} |"
                for r in np_]
    return "\n".join(out).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("ecsx_output")
    ap.add_argument("--work", help="where the decoded files go (default: <root>/.tmp-session/s1-master-audit-<time>)")
    ap.add_argument("--readme", help="write the public-safe README here")
    a = ap.parse_args(argv)
    text = Path(a.ecsx_output).read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    try:
        lines = decode_block(text, LINES_BLOCK)
        summary_raw = decode_block(text, SUMMARY_BLOCK)
    except (ValueError, OSError) as e:
        print(f"decode failed: {e}", file=sys.stderr)
        return 2
    summary = json.loads(summary_raw)
    if summary.get("schema") != SCHEMA:
        print(f"summary schema must be {SCHEMA}", file=sys.stderr)
        return 2
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    if a.work:
        work = Path(a.work)
    else:
        base = tmp_session(Path.cwd().resolve())
        if base is None:
            print("no .tmp-session above the current directory: run from the workspace root or pass --work",
                  file=sys.stderr)
            return 2
        work = base / f"s1-master-audit-{stamp}"
    work.mkdir(parents=True, exist_ok=True)
    (work / LINES_BLOCK).write_bytes(lines)
    (work / SUMMARY_BLOCK).write_bytes(summary_raw)
    md = readme(summary)
    (work / "README.md").write_text(md, encoding="utf-8")
    if a.readme:
        Path(a.readme).parent.mkdir(parents=True, exist_ok=True)
        Path(a.readme).write_text(md, encoding="utf-8")
    n = lines.count(b"\n")
    print(f"decoded {n} Master line(s) -> {work / LINES_BLOCK}")
    print(f"summary -> {work / SUMMARY_BLOCK}; README -> {a.readme or work / 'README.md'}")
    print("verdict " + json.dumps(summary.get("verdict", {}).get("all", {}), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
