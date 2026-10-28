"use client";
// app/admin/content-quality/useCqText.ts — AA-994: the Content Quality label helpers bound to the UI language. Gate
// names come from the quality registry (English or its generated Vietnamese twin), the rest from messages.ts.
// English outside an I18nProvider.

import { useCallback, useMemo } from "react";
import { useLang, useT } from "../../_kit";
import type { Catalog, Messages } from "../../_kit/core/i18n";
import { gateFilterGroups, gateLabelOf } from "@/lib/gateLabels";
import { GATE_LABEL_MESSAGES } from "@/lib/gateLabels.messages";
import { countsLabel, type Severity } from "@/lib/gateSeverity";
import { SEVERITY_MESSAGES } from "@/lib/gateSeverity.messages";
import { GATE_LABELS, RETIRED_GATES } from "@/lib/quality-registry.generated";
import { GATE_LABELS_VI } from "@/lib/quality-registry.generated.vi";
import { BINDING, TOUR_FIELD } from "./messages";

const has = <T extends object>(o: T, k: string): k is Extract<keyof T, string> => Object.prototype.hasOwnProperty.call(o, k);

/** The UI-language text of a code the API sends (a status, a source, a defect class); an unknown code as it is. */
export function useLookup<E extends Catalog>(messages: Messages<E>): (code: string | null | undefined) => string {
  const t = useT(messages) as unknown as (key: string) => string;
  return useCallback((code) => (code && has(messages.en, code) ? t(code) : code ?? ""), [t, messages]);
}

export function useCqText() {
  const lang = useLang();
  const g = useT(GATE_LABEL_MESSAGES);
  const binding = useT(BINDING);
  const field = useT(TOUR_FIELD);
  const severity = useT(SEVERITY_MESSAGES);
  return useMemo(() => {
    const labels: Record<string, string> = lang === "vi" ? GATE_LABELS_VI : GATE_LABELS;
    const suffix = g("retiredSuffix");
    const fieldLabel = (f: string) => (has(TOUR_FIELD.en, f) ? field(f) : f);
    return {
      /** "Cliché / banned phrase (retired)" in the UI language; the code itself for a gate the registry lacks. */
      gateLabel: (gate: string) => gateLabelOf(labels, RETIRED_GATES, gate, suffix),
      /** The gate filter's options: active gates, then the retired ones. */
      gateFilter: gateFilterGroups(labels, RETIRED_GATES, suffix),
      /** Which number set a T2 version's stored score (judge_breakdown.binding); "—" for none. */
      bindingLabel: (b: string | null | undefined) => (!b ? "—" : has(BINDING.en, b) ? binding(b) : b),
      /** A T2 section: "Highlight 3", "Summary". */
      sectionLabel: (f: string, index: number | null) => `${fieldLabel(f)}${index != null ? ` ${index + 1}` : ""}`,
      fieldLabel,
      severityLabel: (s: Severity) => severity(s),
      /** "2 blocking · 1 warning" in the UI language. */
      counts: (c: Record<Severity, number>) => countsLabel(c, SEVERITY_MESSAGES[lang]),
    };
  }, [lang, g, binding, field, severity]);
}
