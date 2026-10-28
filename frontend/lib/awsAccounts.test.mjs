// AA-998 — accounts are shown by their AWS account ID, never by the internal key.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  LLM_ACCOUNT, MAIN_ACCOUNT, RETIRED_ACCOUNT, accountLabel, accountName, withAccountNames,
} from "./awsAccounts.ts";

// AWS documentation example accounts (the resource-ID check allows them).
const DIR = {
  [MAIN_ACCOUNT]: { account_id: "111122223333", name: "111122223333", label: "111122223333 (main infrastructure)", role: "main infrastructure" },
  [LLM_ACCOUNT]: { account_id: "444455556666", name: "444455556666", label: "444455556666 (LLM)", role: "LLM" },
  [RETIRED_ACCOUNT]: { account_id: null, name: "Retired account", label: "Retired account", role: "history" },
};
const KEY = /acc[0-9]/;

test("live keys show the account ID", () => {
  assert.equal(accountName(LLM_ACCOUNT, DIR), "444455556666");
  assert.equal(accountLabel(MAIN_ACCOUNT, DIR), "111122223333 (main infrastructure)");
});

test("before the directory loads a live key shows a neutral name, never the key", () => {
  assert.equal(accountName(LLM_ACCOUNT), "AWS account");
  assert.doesNotMatch(accountLabel(LLM_ACCOUNT), KEY);
});

test("any other stored key is the retired account, with no ID", () => {
  for (const k of [RETIRED_ACCOUNT, "acc9", "something-old"]) {
    assert.equal(accountName(k, DIR), "Retired account");
    assert.equal(accountLabel(k, DIR), "Retired account");
  }
});

test("no account shows nothing", () => {
  for (const k of [null, undefined, "", "unknown", "none"]) assert.equal(accountName(k, DIR), "");
});

test("JSON views get names for account fields only", () => {
  const out = withAccountNames({ account: LLM_ACCOUNT, nested: [{ account_route: MAIN_ACCOUNT }], note: LLM_ACCOUNT,
                                 satellite_account: "x" }, DIR);
  assert.deepEqual(out, { account: "444455556666", nested: [{ account_route: "111122223333" }], note: LLM_ACCOUNT,
                          satellite_account: "x" });
});
