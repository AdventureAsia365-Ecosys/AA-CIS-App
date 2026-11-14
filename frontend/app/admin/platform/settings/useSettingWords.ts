"use client";
// app/admin/platform/settings/useSettingWords.ts — AA-1096 (S280 UX fix round): the page's words for values, bounds,
// areas and the form's problems in the page's language (lib/settingsRegistry stays free of React and of words: it
// returns problem ids and takes ValueWords).

import { useMemo } from "react";
import { useLang, useT } from "../../../_kit";
import type { Problem, ValueWords } from "@/lib/settingsRegistry";
import { SETTINGS, areaName, unitName } from "./messages";

export interface SettingWords {
  words: ValueWords;
  problem: (p: Problem | null | undefined) => string | null;
  area: (id: string) => string;
}

export function useSettingWords(): SettingWords {
  const t = useT(SETTINGS);
  const lang = useLang();
  return useMemo(() => ({
    words: { on: t("on"), off: t("off"), oneOf: (list: string) => t("oneOf", list), unit: (u: string) => unitName(u, lang) },
    problem: (p) => (p ? t(`problem_${p.id}`).replace("{0}", p.arg ?? "") : null),
    area: (id: string) => areaName(id, lang),
  }), [t, lang]);
}
