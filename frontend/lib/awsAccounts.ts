// lib/awsAccounts.ts — how the admin names an AWS account (AA-998).
//
// The API keys accounts internally ("acc2" = main infrastructure, "acc3" = the LLM account): llm_call_log.account,
// llm_role_config.account_route and the cost rows carry these keys. No page prints a key: it shows the 12-digit AWS
// account ID instead, from GET /api/admin/aws-accounts (backend shared/aws_client/accounts.py). This repo is public,
// so no ID is written here. Any other stored account (the history of the retired fallback account) is "Retired
// account", with no ID.
//
// This is the only frontend file that may spell the keys: the guard tests/unit/test_aa998_no_account_keys_in_ui.py
// fails on any other occurrence. Pages use the constants and accountName() below.

export const MAIN_ACCOUNT = "acc2";
export const LLM_ACCOUNT = "acc3";
export const RETIRED_ACCOUNT = "retired";
/** The account keys that still run, in serving order. */
export const LIVE_ACCOUNTS: readonly string[] = [LLM_ACCOUNT, MAIN_ACCOUNT];

export const RETIRED_ACCOUNT_LABEL = "Retired account";
export const UNRESOLVED_ACCOUNT_LABEL = "AWS account";

export interface AwsAccountInfo {
  account_id: string | null;
  /** the account ID, "Retired account", or "AWS account (ID unavailable)" */
  name: string | null;
  /** name + role, e.g. "111122223333 (LLM)" */
  label: string | null;
  role: string;
}
export type AwsAccounts = Record<string, AwsAccountInfo>;
export interface AwsAccountsResp {
  accounts: AwsAccounts;
}

const ROLE: Record<string, string> = {
  [MAIN_ACCOUNT]: "main infrastructure",
  [LLM_ACCOUNT]: "LLM",
};

export const isLiveAccount = (key: string | null | undefined): boolean => !!key && LIVE_ACCOUNTS.includes(key);

/** What a page shows for an account key: the account ID of a live account ("AWS account" until the directory has
 *  loaded or when the API could not read the ID), "Retired account" for any other stored key, "" for no account. */
export function accountName(key: string | null | undefined, dir?: AwsAccounts | null): string {
  if (!key || key === "unknown" || key === "none") return "";
  if (!isLiveAccount(key)) return RETIRED_ACCOUNT_LABEL;
  return dir?.[key]?.account_id ?? UNRESOLVED_ACCOUNT_LABEL;
}

/** accountName() with the account's role, e.g. "111122223333 (LLM)"; "Retired account" alone. */
export function accountLabel(key: string | null | undefined, dir?: AwsAccounts | null): string {
  const name = accountName(key, dir);
  if (!name || !key || !isLiveAccount(key)) return name;
  return `${name} (${ROLE[key]})`;
}

const ACCOUNT_FIELDS = new Set(["account", "account_route", "satellite_account"]);
const ACCOUNT_KEY_RE = /^acc[0-9]+$/;

/** A copy of a JSON value (a job payload or result) with every account-key value of an account field replaced by
 *  accountName(), so a raw JSON view never prints a key either. Any other value is kept as it is. */
export function withAccountNames(value: unknown, dir?: AwsAccounts | null): unknown {
  if (Array.isArray(value)) return value.map((v) => withAccountNames(v, dir));
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value as Record<string, unknown>).map(([k, v]) => [
      k,
      ACCOUNT_FIELDS.has(k) && typeof v === "string" && ACCOUNT_KEY_RE.test(v)
        ? accountName(v, dir)
        : withAccountNames(v, dir),
    ]));
  }
  return value;
}
