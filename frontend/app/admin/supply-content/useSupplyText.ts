// app/admin/supply-content/useSupplyText.ts — AA-994: the lib/supplyContent.ts labels and helper words in the UI
// language (lib/supplyContent.messages.ts), for the list and the detail page.

import { useMemo } from "react";
import { useLang } from "../../_kit";
import {
  PHOTO_STATE, type ContentStatus, type EntityType, type PhotoState, type SupplyWords, type Tone,
} from "@/lib/supplyContent";
import {
  PHOTO_STATE_MESSAGES, STATUS_HINT_MESSAGES, STATUS_LABEL_MESSAGES, SUPPLY_WORDS_MESSAGES, TYPE_LABEL_MESSAGES,
} from "@/lib/supplyContent.messages";

export interface SupplyText {
  words: SupplyWords;
  status: Record<ContentStatus, string>;
  hint: Record<ContentStatus, string>;
  type: Record<EntityType, string>;
  photo: (state: PhotoState) => { label: string; tone: Tone; hint: string };
}

export function useSupplyText(): SupplyText {
  const lang = useLang();
  return useMemo(() => {
    const photo = PHOTO_STATE_MESSAGES[lang];
    return {
      words: SUPPLY_WORDS_MESSAGES[lang],
      status: STATUS_LABEL_MESSAGES[lang],
      hint: STATUS_HINT_MESSAGES[lang],
      type: TYPE_LABEL_MESSAGES[lang],
      photo: (state: PhotoState) => ({
        label: photo[state],
        tone: PHOTO_STATE[state].tone,
        hint: photo[`${state}Hint`],
      }),
    };
  }, [lang]);
}
