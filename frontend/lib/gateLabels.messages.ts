// lib/gateLabels.messages.ts
// AA-994 — the "(retired)" suffix of a gate label (lib/gateLabels.ts RETIRED_SUFFIX) and the retired group of the gate
// filter, in English and Vietnamese. The gate names themselves come from the quality registry
// (quality-registry.generated.ts / quality-registry.generated.vi.ts).

import { defineMessages } from "../app/_kit/core/i18n";
import { RETIRED_SUFFIX } from "./gateLabels";

export const GATE_LABEL_MESSAGES = defineMessages(
  {
    retiredSuffix: RETIRED_SUFFIX,
    retiredGroup: "Retired (older pieces only)",
    anyGate: "Any gate",
  },
  {
    retiredSuffix: " (đã ngừng)",
    retiredGroup: "Đã ngừng (chỉ bài cũ)",
    anyGate: "Mọi cổng kiểm",
  },
);
