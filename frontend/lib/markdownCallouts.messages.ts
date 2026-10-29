// lib/markdownCallouts.messages.ts
// AA-994 — the callout titles (lib/markdownCallouts.ts CALLOUT_LABEL, same kinds) in English and Vietnamese.

import { defineMessages } from "../app/_kit/core/i18n";
import { CALLOUT_LABEL } from "./markdownCallouts";

export const CALLOUT_MESSAGES = defineMessages(CALLOUT_LABEL, {
  note: "Ghi chú",
  tip: "Mẹo",
  important: "Quan trọng",
  warning: "Cảnh báo",
  caution: "Thận trọng",
});
