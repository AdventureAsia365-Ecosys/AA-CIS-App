// app/admin/aa-content/useAaText.ts — AA-994: the lib/aaContent.ts labels and helper words in the UI language
// (lib/aaContent.messages.ts), for the list, the create dialog and the editor drawer.

import { useMemo } from "react";
import { useLang, type Lang } from "../../_kit";
import { type AaKind, type AaLocale, type AaState, type AaWords } from "@/lib/aaContent";
import {
  AA_WORDS_MESSAGES, KIND_LABEL_MESSAGES, LOCALE_LABEL_MESSAGES, STATUS_FILTER_LABEL_MESSAGES,
} from "@/lib/aaContent.messages";

export interface AaText {
  lang: Lang;
  words: AaWords;
  kind: Record<AaKind, string>;
  locale: Record<AaLocale, string>;
  status: Record<AaState, string>;
}

export function useAaText(): AaText {
  const lang = useLang();
  return useMemo(() => ({
    lang,
    words: AA_WORDS_MESSAGES[lang],
    kind: KIND_LABEL_MESSAGES[lang],
    locale: LOCALE_LABEL_MESSAGES[lang],
    status: STATUS_FILTER_LABEL_MESSAGES[lang],
  }), [lang]);
}
