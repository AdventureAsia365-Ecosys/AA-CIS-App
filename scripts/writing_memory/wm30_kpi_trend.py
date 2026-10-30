"""AA-922 WM-30 — the writing-memory KPI trend as Markdown, from the stored weekly reports (read-only, $0).

Reads shared.writing_memory_kpi_report (newest first) and prints one table per writer: per week, writes, re-flag,
repeat errors, light-edit share, minutes to fix, tenant edits per write, reasons captured and cost per write, then
the soft stops and actions of each week. Numbers and ids only (the reports hold no text). The output is what the
WM-30 Done-when "trend written to docs/audits/ once" commits to the root repo (the private one: it names tenant ids).

Usage (S3-mediated ECS exec on aa-cis-dev-api; the container's DATABASE_URL = cis_app; stdout only):
  python3 /tmp/s.py [--weeks 8]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

WRITERS = ("a1", "t2", "t9_blog", "t9_social")
COLS = (("writes", "Writes", None), ("reflag_rate", "Re-flag", "pct"), ("repeat_error_rate", "Repeat", "pct"),
        ("light_edit_share", "Light edits", "pct"), ("minutes_to_fix", "Min to fix", None),
        ("tenant_edit_rate", "Tenant edits/write", "pct"), ("capture_complete", "Reasons", "pct"),
        ("cost_per_write", "USD/write", "usd"))


def _fmt(v, kind):
    if v is None:
        return "—"
    if kind == "pct":
        return f"{v * 100:.1f}%"
    if kind == "usd":
        return f"${v:.4f}"
    return str(v)


def render(reports: list[dict]) -> str:
    """Markdown of the reports (newest first in, oldest first out)."""
    rows = sorted(reports, key=lambda r: str(r["week_start"]))
    out = ["# Writing memory: weekly KPI trend", "",
           f"{len(rows)} stored weekly report(s). A dash = not measured (no data), never zero.", ""]
    for w in WRITERS:
        out += [f"## {w}", "", "| Week | " + " | ".join(c[1] for c in COLS) + " |",
                "|---|" + "---|" * len(COLS)]
        for r in rows:
            k = r["kpis"] if isinstance(r["kpis"], dict) else json.loads(r["kpis"])
            o = ((k.get("writers") or {}).get(w) or {}).get("overall") or {}
            out.append(f"| {r['week_start']} | " + " | ".join(_fmt(o.get(c[0]), c[2]) for c in COLS) + " |")
        out.append("")
    out += ["## Soft stops and actions", ""]
    for r in rows:
        acts = r["actions"] if isinstance(r["actions"], list) else json.loads(r["actions"])
        fired = [a for a in acts if a.get("codes") or a.get("kept_codes")]
        out.append(f"- {r['week_start']}: " + ("; ".join(
            f"{a['writer']} #{a['release_no']} {','.join(a.get('codes') or a.get('kept_codes'))} -> {a['action']}"
            for a in fired) if fired else "none"))
    return "\n".join(out) + "\n"


async def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--weeks", type=int, default=8)
    args = ap.parse_args(argv)
    import asyncpg
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], ssl="require")
    try:
        rows = [dict(r) for r in await conn.fetch(
            "SELECT week_start, kpis, actions FROM shared.writing_memory_kpi_report "
            "ORDER BY week_start DESC, created_at DESC LIMIT $1", args.weeks)]
    finally:
        await conn.close()
    sys.stdout.write(render(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
