// lib/gateSeverity.messages.ts
// AA-994 — the severity words of lib/gateSeverity.ts (SEVERITY_LABEL, countsLabel) in English and Vietnamese:
// `const t = useT(SEVERITY_MESSAGES); t(severity)`, and `countsLabel(c, SEVERITY_MESSAGES[lang])`.

import { defineMessages } from "../app/_kit/core/i18n";
import { COUNT_WORDS, SEVERITY_LABEL } from "./gateSeverity";

export const SEVERITY_MESSAGES = defineMessages(
  {
    block: SEVERITY_LABEL.block,
    warn: SEVERITY_LABEL.warn,
    countBlock: COUNT_WORDS.countBlock,
    countWarn: COUNT_WORDS.countWarn,
  },
  {
    block: "chặn",
    warn: "cảnh báo",
    countBlock: (n: number) => `${n} chặn`,
    countWarn: (n: number) => `${n} cảnh báo`,
  },
);
