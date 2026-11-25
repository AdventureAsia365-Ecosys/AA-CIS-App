// tests/e2e/admin-routes/summary.mjs
// S292 — the job summary of the admin route check (.github/workflows/admin-routes.yml): from the per-page JSON the spec
// writes (tests/e2e/results/admin-routes/*.json) and the stub's request log, a Markdown block: pages rendered, how many
// only the administrator can open, every page with a problem (new, tolerated or a stale tolerated entry), and the API
// reads the stub answered. Never fails (the Playwright step decides).
// Run: node tests/e2e/admin-routes/summary.mjs <results dir> [stub request log]

import { existsSync, readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

/** The Markdown summary of a list of per-page results and the stub's request log lines (pure, for the unit test). */
export function summarize(pages, logLines) {
  const out = [];
  const adminOnly = pages.filter((p) => p.adminOnly).length;
  const failing = pages.filter((p) => (p.problems ?? []).length || (p.stale ?? []).length);
  const known = pages.filter((p) => (p.known ?? []).length);
  out.push(`Pages rendered as the administrator: **${pages.length}** (${adminOnly} admin-only, ` +
    `${pages.length - adminOnly} shared with other roles); with a new problem: **${failing.length}**.`);
  if (failing.length) {
    out.push("", "| Page | Problem |", "| --- | --- |");
    for (const p of failing) {
      for (const msg of [...(p.problems ?? []), ...(p.stale ?? []).map((s) => `stale tolerated entry: ${s}`)]) {
        out.push(`| \`${p.path}\` | ${String(msg).replace(/\|/g, "\\|").slice(0, 300)} |`);
      }
    }
  }
  if (known.length) {
    out.push("", `Tolerated known problems (routes.ts KNOWN_PROBLEMS): ${known.map((p) => `\`${p.path}\``).join(", ")}.`);
  }
  const byStatus = {};
  for (const line of logLines) {
    const status = line.split(" ")[0];
    if (status) byStatus[status] = (byStatus[status] ?? 0) + 1;
  }
  const counts = Object.entries(byStatus).sort().map(([s, n]) => `${s}: ${n}`).join(", ");
  out.push("", `Stub API requests by status: ${counts || "none logged"}.`);
  return out.join("\n");
}

function main() {
  const [dir, log] = process.argv.slice(2);
  const pages = existsSync(dir ?? "")
    ? readdirSync(dir).filter((f) => f.endsWith(".json")).sort()
      .map((f) => { try { return JSON.parse(readFileSync(join(dir, f), "utf8")); } catch { return null; } })
      .filter(Boolean)
    : [];
  const lines = log && existsSync(log) ? readFileSync(log, "utf8").split("\n").filter(Boolean) : [];
  process.stdout.write(`${summarize(pages, lines)}\n`);
}

if (import.meta.url === `file://${process.argv[1]}`) main();
