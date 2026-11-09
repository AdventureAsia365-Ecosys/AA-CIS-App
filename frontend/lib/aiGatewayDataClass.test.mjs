// frontend/lib/aiGatewayDataClass.test.mjs
// AA-1039 R6 slice 1 (S266) — AI providers › Routing, "Routing by data class": lib/aiGateway.ts stagesByDataClass
// (empty state while no stage carries `pii_class`, stages per class once one does) and the card's copy (EN + VI,
// no issue id, no claim that routes are already checked). Run: npm run test:unit.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { stagesByDataClass } from "./aiGateway.ts";

test("no stage with a class → null (the empty state)", () => {
  assert.equal(stagesByDataClass([]), null);
  assert.equal(stagesByDataClass([{ stage: "s1_generate" }, { stage: "t9_write", pii_class: null }]), null);
  assert.equal(stagesByDataClass([{ stage: "x", pii_class: "secret" }]), null, "an unknown value is not a class");
});

test("stages per class, order kept, the rest listed as not set", () => {
  const out = stagesByDataClass([
    { stage: "s1_generate", pii_class: "content" },
    { stage: "crm_draft_reply", pii_class: "pii" },
    { stage: "place_card", pii_class: "content" },
    { stage: "tp_compose" },
  ]);
  assert.deepEqual(out, {
    content: ["s1_generate", "place_card"], pii: ["crm_draft_reply"], unclassified: ["tp_compose"],
  });
});

test("the card copy: both languages, no issue id, the empty state says every stage counts as content", () => {
  const src = readFileSync(fileURLToPath(new URL("../app/admin/ai-gateway/models.messages.ts", import.meta.url)), "utf8");
  for (const key of ["dataClassTitle", "dataClassIntro", "dataClassEmptyTitle", "dataClassEmptyBody", "dataClassPii",
    "dataClassContent", "dataClassUnset", "dataClassNone"]) {
    assert.equal(src.split(`${key}:`).length - 1, 2, `${key} has an English and a Vietnamese row`);
  }
  // The card's rows (each block runs from dataClassTitle to the line after dataClassEmptyBody), in both languages.
  for (const start of [src.indexOf("dataClassTitle"), src.lastIndexOf("dataClassTitle")]) {
    const end = src.indexOf("\n", src.indexOf("\n", src.indexOf("dataClassEmptyBody", start)) + 1);
    assert.doesNotMatch(src.slice(start, end), /AA-\d+/);
  }
  assert.match(src, /dataClassEmptyBody:\s*\n\s*"The database does not hold a data class per stage yet \(its update is not applied\), so every stage is treated as content only/);
  // CIS 373: personal data stays on our own AWS accounts, and the class is not set on the page.
  assert.match(src, /dataClassIntro:\s*\n\s*"The rule: a stage that handles personal data runs only on our own AWS accounts, on routes kept in the United States/);
  const tab = readFileSync(fileURLToPath(new URL("../app/admin/ai-gateway/RoutingTab.tsx", import.meta.url)), "utf8");
  assert.match(tab, /<DataClassCard rows=\{rows\} \/>/);
  assert.match(tab, /<EmptyState title=\{t\("dataClassEmptyTitle"\)\}/);
});
