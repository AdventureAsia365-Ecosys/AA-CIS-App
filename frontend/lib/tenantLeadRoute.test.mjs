// frontend/lib/tenantLeadRoute.test.mjs
// AA-1038 R5 slice 3 follow-up (S272) — Tenant 360 › Subscription › TripPlanner lead route: the draft, the checks the
// form makes alone and the field an API refusal lands on (the API refuses a non-public webhook URL). Run: npm run
// test:unit.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  fieldOfCode, leadRouteBody, leadRouteDraftOf, leadRouteKey, leadRoutePath, sameLeadRoute,
} from "./tenantLeadRoute.ts";

const FRONTEND = fileURLToPath(new URL("..", import.meta.url));
const read = (rel) => readFileSync(join(FRONTEND, rel), "utf8");

test("path and query key", () => {
  const t = "11111111-2222-3333-4444-555555555555";
  assert.equal(leadRoutePath(t), `/api/admin/tenants/${t}/tripplanner-site/lead-route`);
  assert.equal(leadRoutePath("a/b"), "/api/admin/tenants/a%2Fb/tripplanner-site/lead-route");
  assert.deepEqual(leadRouteKey(t), ["tenant360", t, "lead-route"]);
});

test("draft of a stored route and of none", () => {
  assert.deepEqual(leadRouteDraftOf(null), { channel: "none", url: "", email: "" });
  assert.deepEqual(leadRouteDraftOf({ lead_channel: "webhook", lead_webhook_url: "https://hooks.example.com/l",
                                      lead_email: null }),
    { channel: "webhook", url: "https://hooks.example.com/l", email: "" });
});

test("body: trimmed, empty targets are null, other channel's target kept", () => {
  assert.deepEqual(leadRouteBody({ channel: "webhook", url: " https://hooks.example.com/l ", email: "" }).body,
    { lead_channel: "webhook", lead_webhook_url: "https://hooks.example.com/l", lead_email: null });
  assert.deepEqual(leadRouteBody({ channel: "email", url: "https://hooks.example.com/l", email: "desk@example.com" })
    .body, { lead_channel: "email", lead_webhook_url: "https://hooks.example.com/l", lead_email: "desk@example.com" });
  assert.deepEqual(leadRouteBody({ channel: "none", url: "", email: "" }).body,
    { lead_channel: "none", lead_webhook_url: null, lead_email: null });
});

test("problems the form sees alone", () => {
  const p = (d) => leadRouteBody(d).problem;
  assert.deepEqual(p({ channel: "webhook", url: "", email: "" }),
    { field: "lead_webhook_url", code: "lead_webhook_url_required" });
  assert.deepEqual(p({ channel: "webhook", url: "http://hooks.example.com/l", email: "" }),
    { field: "lead_webhook_url", code: "lead_webhook_url_not_https" });
  assert.deepEqual(p({ channel: "webhook", url: `https://hooks.example.com/${"a".repeat(480)}`, email: "" }),
    { field: "lead_webhook_url", code: "lead_webhook_url_too_long" });
  assert.deepEqual(p({ channel: "email", url: "", email: "" }), { field: "lead_email", code: "lead_email_required" });
  assert.deepEqual(p({ channel: "email", url: "", email: "not-an-address" }),
    { field: "lead_email", code: "lead_email_invalid" });
  // a private host is the API's call (no rule copy here): the form sends it and shows the 422 under the field
  assert.equal(p({ channel: "webhook", url: "https://10.0.3.7:8443/leads", email: "" }), null);
});

test("an API refusal lands on its field", () => {
  assert.equal(fieldOfCode("lead_webhook_url_not_public"), "lead_webhook_url");
  assert.equal(fieldOfCode("lead_webhook_url_invalid"), "lead_webhook_url");
  assert.equal(fieldOfCode("lead_email_invalid"), "lead_email");
  assert.equal(fieldOfCode("lead_channel_invalid"), "lead_channel");
  assert.equal(fieldOfCode("tenant_offboarded"), null);
  assert.equal(fieldOfCode(undefined), null);
});

test("unchanged draft", () => {
  assert.equal(sameLeadRoute({ channel: "webhook", url: "https://a.example.com ", email: "" },
                             { channel: "webhook", url: "https://a.example.com", email: "" }), true);
  assert.equal(sameLeadRoute({ channel: "none", url: "", email: "" }, { channel: "email", url: "", email: "" }), false);
});

test("the section uses kit components and has a message for every refusal code the API sends", () => {
  const src = read("app/admin/tenants/_components/Tenant360LeadRoute.tsx");
  assert.match(src, /from "\.\.\/\.\.\/\.\.\/_kit"/);
  const code = src.split("\n").filter((l) => !l.trim().startsWith("//")).join("\n");
  assert.doesNotMatch(code, /AA-\d+/);  // no issue ids in UI text
  const msgs = read("app/admin/tenants/_components/leadRoute.messages.ts");
  const api = read("../services/tenant_account/tripplanner_site.py");
  const codes = [...api.matchAll(/= "(lead_(?:webhook_url|email)_[a-z_]+)"/g)].map((m) => m[1]);
  assert.ok(codes.length >= 7);
  for (const code of codes) assert.match(msgs, new RegExp(`err_${code}:`), code);
});
