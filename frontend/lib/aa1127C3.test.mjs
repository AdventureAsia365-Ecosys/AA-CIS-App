// AA-1127 C3 (S283 UI audit): the read-only hint, readable copy and number formats on the CIS admin.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { alertMessage } from "./overviewAlerts.ts";
import { fmtCount, fmtUsd } from "./numberFormat.ts";
import { sessionReadOnly } from "./sessionAccess.ts";

const FRONTEND = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (p) => readFileSync(join(FRONTEND, p), "utf8");

test("the read-only hint: the API's answer wins, the role cookie decides while it is missing", () => {
  assert.equal(sessionReadOnly({ role: "admin", read_only: true, reason: "e2e" }, false), true);
  assert.equal(sessionReadOnly({ role: "admin", read_only: false, reason: null }, true), false);
  assert.equal(sessionReadOnly(undefined, true), true);
  assert.equal(sessionReadOnly(null, false), false);
});

test("the kit core carries the read-only context; the CIS shell provides it from GET /admin/session", () => {
  const core = read("app/_kit/core/ReadOnly.tsx");
  for (const name of ["ReadOnlyProvider", "useReadOnly", "WriteOnly", "ReadOnlyNote"]) {
    assert.match(core, new RegExp(`export function ${name}\\b`), name);
    assert.match(read("app/_kit/index.ts"), new RegExp(`\\b${name}\\b[^;]*from "\\./core/ReadOnly"`), `${name} exported`);
  }
  assert.ok("ReadOnly.tsx" in JSON.parse(read("app/_kit/core/MANIFEST.json")).files);
  assert.match(read("app/_kit/core/kit.messages.ts"), /readOnlySession:/);
  const shell = read("app/_kit/AdminShell.tsx");
  assert.match(shell, /<ReadOnlyProvider readOnly=\{readOnly\}>/);
  assert.match(read("app/_kit/useSessionAccess.ts"), /SESSION_API/);
  assert.match(read("lib/sessionAccess.ts"), /"\/api\/admin\/session"/);
});

test("run, save and paid buttons sit behind WriteOnly on Writing memory, Usage and Spend", () => {
  const wm = read("app/admin/lab/writing-memory/WritingMemory.tsx");
  const header = wm.indexOf("<WriteOnly fallback=");
  const gated = wm.slice(header, wm.indexOf("</WriteOnly>", header));
  for (const key of ["backfillDryRun", "backfillSignals", "runFactFilter", "r0Baseline", "mineRules"]) {
    assert.ok(gated.includes(`t("${key}")`), key);
  }
  // The tabs gate their run / write / paid controls on canAdmin; a read-only session must not get it from the role
  // cookie alone (the Dev E2E identity's role is admin, the API answers 403 e2e_read_only).
  const hook = read("app/admin/lab/writing-memory/useWmAdmin.ts");
  assert.match(hook, /useReadOnly\(\)/);
  assert.match(hook, /can\(role, "ai\.admin"\) && !readOnly/);
  for (const tab of ["MetricsTab", "ExemplarsTab", "ReleasesTab", "ClaimLabelsTab"]) {
    const src = read(`app/admin/lab/writing-memory/${tab}.tsx`);
    assert.match(src, /const canAdmin = useWmAdmin\(\);/, tab);
    assert.doesNotMatch(src, /can\(useAdminIdentity\(\)\.role/, `${tab}: no role-only canAdmin`);
  }
  // The Signals tab verdict buttons and the rule drawer's edit / reject / R0 / activate / roll back sit in WriteOnly.
  const between = (src, a, b) => src.slice(src.indexOf(a), src.indexOf(b));
  const signals = between(wm, "function SignalsTab(", "function LintBadges(");
  const gatedSignals = between(signals, "<WriteOnly>", "</WriteOnly>");
  for (const key of ["markPass", "markFail"]) assert.ok(gatedSignals.includes(`t("${key}")`), key);
  const drawer = between(wm, "function RuleDrawer(", "function CandidatesTab(");
  const gatedDrawer = [...drawer.matchAll(/<WriteOnly>([\s\S]*?)<\/WriteOnly>/g)].map((m) => m[1]).join("\n");
  for (const key of ["edit", "reject", "runR0", "activateL0", "rollBack"]) {
    assert.ok(gatedDrawer.includes(`t("${key}")`), key);
  }
  const usage = read("app/admin/usage/UsageExplorer.tsx");
  assert.ok(usage.slice(usage.indexOf("<WriteOnly>"), usage.indexOf("</WriteOnly>")).includes('t("saveView")'));
  for (const f of ["app/admin/spend/CostByApp.tsx", "app/admin/spend/page.tsx"]) {
    const src = read(f);
    assert.ok(src.slice(src.indexOf("<WriteOnly>"), src.indexOf("</WriteOnly>")).includes('t("refreshAws")'), f);
  }
});

test("an Overview alert reads its payload message (JSON string or object)", () => {
  const msg = "Invented provider credit is running low: about $2 left.";
  assert.equal(alertMessage(JSON.stringify({ message: msg, amount: 2 })), msg);
  assert.equal(alertMessage({ message: `  ${msg} ` }), msg);
  for (const p of [null, undefined, "", "not json", "[]", JSON.stringify({ message: "" }), JSON.stringify({ x: 1 }), 42]) {
    assert.equal(alertMessage(p), null, String(p));
  }
  assert.match(read("app/admin/overview/_components/AlertList.tsx"), /alertMessage\(a\.payload\)/);
});

test("counts and USD: separators in the UI language, 2 decimals from a cent up", () => {
  assert.equal(fmtCount(744707), "744,707");
  assert.equal(fmtCount(744707, "vi"), "744.707");
  assert.equal(fmtCount(null), "—");
  assert.equal(fmtUsd(139.4329), "$139.43");
  assert.equal(fmtUsd(0), "$0.00");
  assert.equal(fmtUsd(null), "$0.00");
  assert.equal(fmtUsd(0.0042), "$0.0042");
  assert.equal(fmtUsd(0.00001), "<$0.0001");
  assert.equal(fmtUsd(1234.5), "$1,234.50");
  assert.equal(fmtUsd(1234.5, "vi"), "$1.234,50");
  assert.equal(fmtUsd(-2.5), "-$2.50");
  assert.doesNotMatch(read("app/admin/jobs/page.tsx"), /toFixed\(4\)/);
  assert.doesNotMatch(read("app/admin/decisions/page.tsx"), /toLocaleString\(\)/);
  assert.match(read("app/admin/jobs/messages.ts"), /statusWindow:/);
});

test("copy: Overview AI note, Tenant 360 notes, Slate path, SEO title and deep links, review filter names", () => {
  const ov = read("app/admin/overview/messages.ts");
  assert.doesNotMatch(ov, /the same as the AI estimate/);
  assert.match(ov, /Partner CRM keeps its own AI log/);
  const ten = read("app/admin/tenants/messages.ts");
  assert.doesNotMatch(ten, /06\/07\/08|llm_call_log/);
  assert.doesNotMatch(read("app/admin/tenants/_components/tenant360.messages.ts"), /llm_call_log/);
  assert.match(read("app/admin/tenants/_components/TenantDetail.tsx"), /\/admin\/tenant-qa\/trace\?tenant=/);
  const slate = read("app/admin/tenant-qa/slate/SlateSection.tsx");
  assert.doesNotMatch(slate, /t7-planning/);
  assert.match(slate, /\/portal\/create\/social/);
  assert.match(slate, /aria-label=\{t\("channelAria"\)\}/);
  const seo = read("app/admin/seo-intelligence/messages.ts");
  assert.match(seo, /title: "Phân tích SEO"/);
  const seoPage = read("app/admin/seo-intelligence/page.tsx");
  assert.match(seoPage, /router\.replace/);
  assert.match(seoPage, /TAB_KEYS\.includes\(wanted\)/);
  const review = read("app/admin/review/page.tsx");
  for (const k of ["filterStatusAria", "filterCountryAria", "filterScoreAria"]) assert.match(review, new RegExp(k));
});

test("Overview health has a Partner CRM tile with no traffic numbers (health check and alarms)", () => {
  const strip = read("app/admin/overview/_components/AppHealthStrip.tsx");
  assert.match(strip, /app\.kind === "probe"/);
  assert.match(read("app/admin/overview/_components/opsTypes.ts"), /"cis" \| "tripplanner" \| "booking" \| "crm"/);
  assert.match(read("app/admin/platform/apps/AppsBoard.tsx"), /traffic\.kind === "probe"/);
});
