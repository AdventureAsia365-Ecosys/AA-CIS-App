// frontend/lib/bookingBilling.test.mjs
// AA-1037 (root ADR 0019 d9 "Screens now") — Tenant 360 › Billing › Booking invoices: the chips, the overdue amounts
// and the read path of lib/bookingBilling.ts; the section sits in the Billing tab above the meters and links to
// Booking only through lib/apps.ts. Run: npm run test:unit.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  accountStatusTone, bookingBillingKey, bookingBillingPath, bookingStatusTone, overdueEntries,
} from "./bookingBilling.ts";

const FRONTEND = fileURLToPath(new URL("..", import.meta.url));
const read = (rel) => readFileSync(join(FRONTEND, rel), "utf8");

test("status and account chips", () => {
  assert.equal(bookingStatusTone("ok"), "success");
  assert.equal(bookingStatusTone("warn"), "warning");
  assert.equal(bookingStatusTone("past_due"), "danger");
  assert.equal(bookingStatusTone("something_new"), "neutral");
  assert.equal(bookingStatusTone(null), "neutral");
  assert.deepEqual(["active", "trial", "past_due", "suspended", "offboarded", null].map(accountStatusTone),
    ["success", "info", "warning", "danger", "neutral", "neutral"]);
});

test("overdue amounts: ISO codes only, zero dropped, sorted", () => {
  assert.deepEqual(overdueEntries({ USD: "240.50", EUR: "80", VND: "0", usd: "5", XX: "1", GBP: "abc" }),
    [["EUR", "80"], ["USD", "240.50"]]);
  assert.deepEqual(overdueEntries({}), []);
  assert.deepEqual(overdueEntries(null), []);
});

test("read path and query key", () => {
  const t = "11111111-2222-3333-4444-555555555555";
  assert.equal(bookingBillingPath(t), `/api/admin/tenants/${t}/booking-billing`);
  assert.equal(bookingBillingPath("a/b"), "/api/admin/tenants/a%2Fb/booking-billing");
  assert.deepEqual(bookingBillingKey(t), ["tenant360", t, "booking-billing"]);
});

test("the messages carry no issue id (the Vietnamese rows are checked by tsc: defineMessages)", () => {
  const src = read("app/admin/tenants/_components/bookingBilling.messages.ts");
  const strings = src.split("\n").filter((l) => !/^\s*\/\//.test(l)).join("\n");
  assert.doesNotMatch(strings, /\bAA-\d+/);
  assert.match(src, /defineMessages\(/);
});

test("the section is in the Billing tab above the meters, read only, linking through lib/apps.ts", () => {
  const detail = read("app/admin/tenants/_components/TenantDetail.tsx");
  const at = detail.indexOf("<Tenant360BookingBilling");
  assert.ok(at > 0 && at < detail.indexOf("<BillingTabContent  tenantId"), "above the AA-666 meters");
  const src = read("app/admin/tenants/_components/Tenant360BookingBilling.tsx");
  assert.match(src, /bookingAgencyInvoicesUrl\(tenantId\)/);
  assert.doesNotMatch(src, /booking\.lumiguides|BOOKING_ADMIN_URL|agency-billing/);
  assert.doesNotMatch(src, /useMutation|method: "(POST|PUT|PATCH|DELETE)"/, "read only");
});
