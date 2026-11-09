// tests/e2e/visual/summary.mjs — AA-1008 (root ADR 0016 decision 9): merges tests/e2e/results/visual/report/*.json
// into report.json and prints a Markdown table (page x 4 cells: 1440 light / 1440 dark / 390 light / 390 dark) for the
// job summary and the PR comment. A cell is "ok" or the reasons. Run from the repo root.
//   node tests/e2e/visual/summary.mjs                 the table
//   node tests/e2e/visual/summary.mjs --selftest      exit 0 only if every planted-overflow capture was caught
import { existsSync, readdirSync, readFileSync, writeFileSync } from "node:fs";

const OUT = "tests/e2e/results/visual";
const dir = `${OUT}/report`;
const rows = existsSync(dir)
  ? readdirSync(dir).filter((f) => f.endsWith(".json")).map((f) => JSON.parse(readFileSync(`${dir}/${f}`, "utf8")))
  : [];

export const docOverflow = (r) => r.overflow.doc > r.overflow.vw + 1;
export const frameOverflow = (r) => r.overflow.frames.length > 0;

if (process.argv.includes("--selftest")) {
  const planted = rows.filter((r) => r.selftest);
  const missed = planted.filter((r) => !(r.selftest === "document" ? docOverflow(r) : frameOverflow(r)));
  console.log(`Self-test: ${planted.length} planted-overflow captures, ${planted.length - missed.length} caught.`);
  for (const r of missed) console.log(`::error::planted ${r.selftest} overflow not caught: ${r.page} ${r.viewport} ${r.theme}`);
  // 2 pages x 2 viewports x 2 themes.
  process.exit(planted.length === 8 && missed.length === 0 ? 0 : 1);
}

writeFileSync(`${OUT}/report.json`, JSON.stringify(rows, null, 1));
const reasons = (r) => {
  const out = [];
  if (r.status !== (r.page === "not-found" ? 404 : 200)) out.push(`HTTP ${r.status}`);
  if (r.appliedTheme !== r.theme) out.push(`theme ${r.appliedTheme}`);
  if (docOverflow(r)) out.push(`overflow ${r.overflow.doc - r.overflow.vw}px`);
  if (frameOverflow(r)) out.push(`frame overflow ${r.overflow.frames[0].scrollWidth - r.overflow.frames[0].clientWidth}px`);
  if (r.hasFrame === false && r.frameExpected !== false) out.push("no kit frame");
  if (r.consoleErrors.length) out.push(`${r.consoleErrors.length} console error(s)`);
  if (r.failedRequests.length) out.push(`${r.failedRequests.length} failed request(s)`);
  return out.length ? `❌ ${out.join(", ")}` : "ok";
};
const cols = [["1440", "light"], ["1440", "dark"], ["390", "light"], ["390", "dark"]];
const pages = [...new Set(rows.map((r) => r.page))].sort();
const lines = [
  `| Page | ${cols.map(([v, t]) => `${v} ${t}`).join(" | ")} |`,
  `|---|${cols.map(() => "---").join("|")}|`,
  ...pages.map((p) => {
    const cells = cols.map(([v, t]) => {
      const r = rows.find((x) => x.page === p && x.viewport === v && x.theme === t);
      return r ? reasons(r) : "not run";
    });
    return `| ${p} | ${cells.join(" | ")} |`;
  }),
];
const bad = rows.filter((r) => reasons(r) !== "ok").length;
console.log(`${rows.length} captures, ${bad} with findings.\n\n${lines.join("\n")}`);
