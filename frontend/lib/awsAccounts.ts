// lib/awsAccounts.ts — how the admin names an AWS account (AA-998).
//
// The API keys accounts internally ("acc2" = main infrastructure, "acc3" = the LLM account): llm_call_log.account,
// llm_role_config.account_route and the cost rows carry these keys. No page prints a key: it shows the 12-digit AWS
// account ID instead, from GET /api/admin/aws-accounts (backend shared/aws_client/accounts.py). This repo is public,
// so no ID is written here. A vendor-API call stores its connection key as the account (gateway S6: "openai-direct",
// "anthropic-direct"); pages name it by its vendor ("OpenAI API"). Any other stored account (the history of the
// retired fallback account) is "Retired account", with no ID.
//
// This is the only frontend file that may spell the keys: the guard tests/unit/test_aa998_no_account_keys_in_ui.py
// fails on any other occurrence. Pages use the constants and accountName() below.

export const MAIN_ACCOUNT = "acc2";
export const LLM_ACCOUNT = "acc3";
export const RETIRED_ACCOUNT = "retired";
/** The account keys that still run, in serving order. */
export const LIVE_ACCOUNTS: readonly string[] = [LLM_ACCOUNT, MAIN_ACCOUNT];

export const RETIRED_ACCOUNT_LABEL = "Retired account";
/** Vendor-API connections whose key a call-log row stores as its account (no AWS account behind them). Product
 *  names, the same in every UI language; the backend map is shared/aws_client/accounts.py VENDOR_CONNECTION_LABELS. */
export const VENDOR_CONNECTION_LABELS: Readonly<Record<string, string>> = {
  "openai-direct": "OpenAI API",
  "anthropic-direct": "Anthropic API",
};
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

/** The words accountName() / accountLabel() use. English by default; AA-994: a page passes the UI language's set
 *  (lib/awsAccounts.messages.ts AWS_ACCOUNT_MESSAGES[lang], same keys). */
export interface AccountText {
  retired: string;
  unresolved: string;
  main: string;
  llm: string;
}

export const ACCOUNT_TEXT: AccountText = {
  retired: RETIRED_ACCOUNT_LABEL,
  unresolved: UNRESOLVED_ACCOUNT_LABEL,
  main: "main infrastructure",
  llm: "LLM",
};

function roleOf(key: string, text: AccountText): string {
  return key === MAIN_ACCOUNT ? text.main : key === LLM_ACCOUNT ? text.llm : "";
}

export const isLiveAccount = (key: string | null | undefined): boolean => !!key && LIVE_ACCOUNTS.includes(key);

/** What a page shows for an account key: the account ID of a live account ("AWS account" until the directory has
 *  loaded or when the API could not read the ID), the vendor of a vendor-API connection, "Retired account" for any
 *  other stored key, "" for no account. */
export function accountName(
  key: string | null | undefined, dir?: AwsAccounts | null, text: AccountText = ACCOUNT_TEXT,
): string {
  if (!key || key === "unknown" || key === "none") return "";
  if (Object.prototype.hasOwnProperty.call(VENDOR_CONNECTION_LABELS, key)) return VENDOR_CONNECTION_LABELS[key];
  if (!isLiveAccount(key)) return text.retired;
  return dir?.[key]?.account_id ?? text.unresolved;
}

/** accountName() with the account's role, e.g. "111122223333 (LLM)"; "Retired account" alone. */
export function accountLabel(
  key: string | null | undefined, dir?: AwsAccounts | null, text: AccountText = ACCOUNT_TEXT,
): string {
  const name = accountName(key, dir, text);
  if (!name || !key || !isLiveAccount(key)) return name;
  return `${name} (${roleOf(key, text)})`;
}

const ACCOUNT_FIELDS = new Set(["account", "account_route", "satellite_account"]);
const ACCOUNT_KEY_RE = /^acc[0-9]+$/;

/** A copy of a JSON value (a job payload or result) with every account-key value of an account field replaced by
 *  accountName(), so a raw JSON view never prints a key either. Any other value is kept as it is. */
export function withAccountNames(value: unknown, dir?: AwsAccounts | null, text: AccountText = ACCOUNT_TEXT): unknown {
  if (Array.isArray(value)) return value.map((v) => withAccountNames(v, dir, text));
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value as Record<string, unknown>).map(([k, v]) => [
      k,
      ACCOUNT_FIELDS.has(k) && typeof v === "string" && ACCOUNT_KEY_RE.test(v)
        ? accountName(v, dir, text)
        : withAccountNames(v, dir, text),
    ]));
  }
  return value;
}
