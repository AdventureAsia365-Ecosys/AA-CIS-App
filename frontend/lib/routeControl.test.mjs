// frontend/lib/routeControl.test.mjs
// AA-1042 (S268): the Route control tab of AI providers (backend /admin/ai-gateway/route-control). Pure helpers of the
// editor (moving an item, the draft against the config in force, the body with only what changed, problems per stage,
// grouping) plus text guards: the tab is wired into the page, its writes follow usePageAccess("route_control"), it
// uses the kit only, and its messages have the same keys in English and Vietnamese.
// Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { ACTION_CAPABILITY, PAGE_ACTIONS, TAB_KEYS, parseTab } from "./aiGateway.ts";
import {
  addableOptions, chainText, draftOf, groupStages, isEmptyProposal, moveItem, presetPath, problemsOf, proposalBody,
  skippedWhere, versionPath,
} from "./routeControl.ts";

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");

const STATE = {
  ready: true, version: 3, order_mode: "model_first", env_order_mode: "model_first", updated_at: null,
  updated_by: null, max_attempts: 6, models: {}, presets: [],
  stages: [
    { stage: "zz_writer", role: "writer", is_active: true, allowed_scopes: null, chain: ["m1", "m2"], product: null,
      options: [{ model_id: "m1", label: "M1", available: true }, { model_id: "m2", label: "M2", available: true },
        { model_id: "m3", label: "M3", available: true }, { model_id: "m4", label: "M4", available: false }] },
    { stage: "zz_judge", role: "judge", is_active: true, allowed_scopes: null, chain: ["m3"], product: null,
      options: [] },
  ],
  provider_order: [{ model_key: "m1", label: "M1", family: "f", deployments: [
    { connection_key: "acc3", label: "acc3", kind: "bedrock", data_scope: "aws_only", priority: 10, enabled: true,
      connection_enabled: true },
    { connection_key: "api", label: "api", kind: "anthropic", data_scope: "vendor_direct", priority: 20,
      enabled: true, connection_enabled: false }] }],
};

test("moveItem moves within range and returns a new array", () => {
  const list = ["a", "b", "c"];
  assert.deepEqual(moveItem(list, 0, 2), ["b", "c", "a"]);
  assert.deepEqual(moveItem(list, 2, 0), ["c", "a", "b"]);
  assert.deepEqual(moveItem(list, 1, 5), ["a", "b", "c"]);
  assert.deepEqual(list, ["a", "b", "c"]);
});

test("the draft of the config in force sends nothing", () => {
  const d = draftOf(STATE);
  assert.deepEqual(d.provider_order, { m1: ["acc3", "api"] });
  assert.ok(isEmptyProposal(proposalBody(STATE, d)));
});

test("the body names only what changed", () => {
  const d = draftOf(STATE);
  d.routes.zz_writer = ["m2", "m1"];
  d.provider_order.m1 = ["api", "acc3"];
  d.order_mode = "aws_first";
  assert.deepEqual(proposalBody(STATE, d), {
    order_mode: "aws_first", routes: { zz_writer: ["m2", "m1"] }, provider_order: { m1: ["api", "acc3"] },
  });
  d.routes.zz_writer = ["m1", "m2"];
  assert.equal(proposalBody(STATE, d).routes, undefined);
});

test("only available models not in the chain can be added", () => {
  assert.deepEqual(addableOptions(STATE.stages[0], ["m1", "m2"]).map((o) => o.model_id), ["m3"]);
});

test("problems are split per stage", () => {
  const preview = { errors: [{ stage: "zz_judge", rule: "x", message: "e" }, { stage: null, rule: "y", message: "g" }],
    warnings: [{ stage: "zz_judge", rule: "w", message: "w", existing: true }] };
  assert.equal(problemsOf(preview, "zz_judge").errors.length, 1);
  assert.equal(problemsOf(preview, "zz_judge").warnings.length, 1);
  assert.deepEqual(problemsOf(undefined, "zz_judge"), { errors: [], warnings: [] });
});

test("stages follow the group order, unknown ones last", () => {
  const out = groupStages([{ stage: "b" }, { stage: "x" }, { stage: "a" }], [{ key: "g1", stages: ["a", "b"] }]);
  assert.deepEqual(out.map((g) => [g.key, g.stages.map((s) => s.stage)]), [["g1", ["a", "b"]], ["other", ["x"]]]);
});

test("paths and chain text", () => {
  assert.equal(versionPath(4), "/api/admin/ai-gateway/route-control/versions/4");
  assert.equal(presetPath("prefer_aws"), "/api/admin/ai-gateway/route-control/presets/prefer_aws");
  assert.equal(chainText(["a", "b"]), "a → b");
  assert.equal(chainText([]), "—");
});

test("the tab is wired into the page and its writes follow the route_control access", () => {
  assert.ok(TAB_KEYS.includes("controls"));
  assert.equal(parseTab("controls"), "controls");
  assert.ok(PAGE_ACTIONS.includes("route_control"));
  assert.equal(ACTION_CAPABILITY.route_control, "ai.admin");
  const page = read("../app/admin/ai-gateway/AiGateway.tsx");
  assert.match(page, /tab === "controls" && <RouteControlTab \/>/);
  const tab = read("../app/admin/ai-gateway/RouteControlTab.tsx");
  assert.match(tab, /usePageAccess\(\)\.can\("route_control"\)/);
  assert.doesNotMatch(tab, /<(button|select|input|textarea)\b/, "kit controls only");
});

test("the messages have the same keys in English and Vietnamese and name no issue ids", () => {
  const src = read("../app/admin/ai-gateway/routeControl.messages.ts");
  assert.doesNotMatch(src.replace(/^\/\/.*$/gm, ""), /AA-\d+/);
  const at = src.indexOf("\n  {\n", src.indexOf("defineMessages("));
  const split = src.indexOf("\n  {\n", at + 1);
  const keys = (part) => [...part.matchAll(/^ {4}([A-Za-z_]+):/gm)].map((m) => m[1]).sort();
  const en = keys(src.slice(at, split));
  assert.ok(en.length > 50);
  assert.deepEqual(keys(src.slice(split)), en);
});

test("a skipped entry reads model@connection, or the model alone when nothing was planned", () => {
  assert.equal(skippedWhere({ model: "m1", connection: "acc3", reason: "refused (access_denied)" }), "m1@acc3");
  assert.equal(skippedWhere({ model: "m2", connection: null, reason: "disabled in the catalog" }), "m2");
});

test("the stage editor lists the skipped deployments with their reason", () => {
  const src = read("../app/admin/ai-gateway/RouteControlTab.tsx");
  assert.match(src, /route-control-skipped-\$\{stage\.stage\}/);
  assert.match(src, /t\("skippedItem", skippedWhere\(s\), s\.reason\)/);
});
