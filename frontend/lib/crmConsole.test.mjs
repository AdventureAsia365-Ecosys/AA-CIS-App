// frontend/lib/crmConsole.test.mjs
// AA-941 review fix: unit tests for the S-01 console helpers in crmConsole.ts.
// Run: npm run test:unit (node --test with --experimental-strip-types; crmConsole.ts holds only type-level TS).
//
// The partner CRM sends money as Pydantic Decimal strings (UsageMeters.ai_cost_usd_month = "0.1234"), so the
// "AI spend this month" tile must parse strings: a number-only formatter showed an em dash for every agency.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  crmErrorText,
  dataRequestStatus,
  formatUsd,
  isTicket,
  parseUsd,
  pct,
  usdPct,
} from "./crmConsole.ts";

// The exact usage answer shape of AA-Partner api/app/routers/ops.py UsageMeters (console._usd() strings).
const usage = {
  tenant_id: "00000000-0000-4000-8000-0000000000a1",
  period_start: "2026-10-01T00:00:00+00:00",
  period_end: "2026-10-24T09:00:00+00:00",
  seats_used: 2,
  seats_allowed: 10,
  active_members_30d: 1,
  storage_bytes: 1024,
  inbound_mail_30d: 0,
  ai_cost_usd_month: "0.1234",
  ai_budget_usd_month: "2.0000",
  failed_jobs_24h: 0,
  last_activity_at: null,
};

test("parseUsd reads the CRM's decimal strings (and plain numbers)", () => {
  assert.equal(parseUsd("0.1234"), 0.1234);
  assert.equal(parseUsd("2.0000"), 2);
  assert.equal(parseUsd("0.0000"), 0);
  assert.equal(parseUsd(1.5), 1.5);
  assert.equal(parseUsd(null), null);
  assert.equal(parseUsd(undefined), null);
  assert.equal(parseUsd(""), null);
  assert.equal(parseUsd("  "), null);
  assert.equal(parseUsd("abc"), null);
  assert.equal(parseUsd(Number.NaN), null);
});

test("the AI spend tile formats the usage strings instead of an em dash", () => {
  assert.equal(formatUsd(usage.ai_cost_usd_month), "$0.123");
  assert.equal(formatUsd(usage.ai_budget_usd_month), "$2.00");
  assert.equal(formatUsd("0.0000"), "$0.000");
  assert.equal(formatUsd("12.5000"), "$12.50");
  assert.equal(formatUsd(3), "$3.00");
  assert.equal(formatUsd(null), "—");
  assert.equal(formatUsd("not money"), "—");
});

test("the AI spend bar uses the parsed strings", () => {
  assert.equal(usdPct(usage.ai_cost_usd_month, usage.ai_budget_usd_month), 6);
  assert.equal(usdPct("1.6000", "2.0000"), 80);
  assert.equal(usdPct("5.0000", "2.0000"), 100);
  assert.equal(usdPct("1.0000", "0.0000"), 0);
  assert.equal(usdPct(null, "2.0000"), 0);
  assert.equal(pct(usage.seats_used, usage.seats_allowed), 20);
});

test("every DataRequest status the CRM sends has a label, export_downloaded included", () => {
  for (const s of [
    "awaiting_owner_confirmation",
    "confirmed",
    "export_downloaded",
    "rejected",
    "cancelled",
    "failed",
    "done",
  ]) {
    assert.notEqual(dataRequestStatus(s).label, s, s);
  }
  assert.equal(dataRequestStatus("export_downloaded").tone, "warning");
});

test("the create refusal tenant_exists reads in plain words with its code", () => {
  const text = crmErrorText(409, "tenant_exists");
  assert.match(text, /already exists/);
  assert.match(text, /\(tenant_exists\)$/);
});

test("ticket references follow the CRM's pattern", () => {
  assert.equal(isTicket("SUP-1234"), true);
  assert.equal(isTicket(" AA-941 "), true);
  assert.equal(isTicket("free text"), false);
  assert.equal(isTicket("AA941"), false);
});
