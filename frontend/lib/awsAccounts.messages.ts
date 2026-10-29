// lib/awsAccounts.messages.ts
// AA-994 — the words accountName() / accountLabel() / withAccountNames() use (lib/awsAccounts.ts AccountText), in
// English and Vietnamese. A page passes the set of its UI language as the last argument:
//   const lang = useLang();  accountLabel(key, dir, AWS_ACCOUNT_MESSAGES[lang]);

import { defineMessages } from "../app/_kit/core/i18n";

export const AWS_ACCOUNT_MESSAGES = defineMessages(
  { retired: "Retired account", unresolved: "AWS account", main: "main infrastructure", llm: "LLM" },
  { retired: "Tài khoản đã ngừng dùng", unresolved: "Tài khoản AWS", main: "hạ tầng chính", llm: "LLM" },
);
