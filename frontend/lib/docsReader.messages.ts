// lib/docsReader.messages.ts
// AA-994 — the docs reader's catalog labels (lib/docsReader.ts TYPE_LABEL / AUDIENCE_LABEL, same keys) and the
// names of the page languages, in English and Vietnamese. A type or audience the catalog adds later shows its raw
// value until it is added here (typeLabel() / audienceLabel() do the same in English).

import { defineMessages } from "../app/_kit/core/i18n";
import { AUDIENCE_LABEL, TYPE_LABEL } from "./docsReader";

export const DOC_TYPE_MESSAGES = defineMessages(TYPE_LABEL, {
  tutorial: "Hướng dẫn nhập môn",
  "how-to": "Cách làm",
  reference: "Tra cứu",
  explanation: "Giải thích",
  adr: "ADR",
  report: "Báo cáo",
  template: "Mẫu",
});

export const DOC_AUDIENCE_MESSAGES = defineMessages(AUDIENCE_LABEL, {
  owner: "Chủ doanh nghiệp (CEO)",
  staff: "Nhân viên",
  engineer: "Kỹ sư",
  devops: "DevOps",
  cto: "CTO",
  cio: "CIO",
  pm: "PM",
  agent: "Agent",
});

export const DOC_LANG_MESSAGES = defineMessages(
  { en: "English", vi: "Vietnamese" },
  { en: "Tiếng Anh", vi: "Tiếng Việt" },
);
