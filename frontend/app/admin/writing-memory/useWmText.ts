"use client";
// app/admin/writing-memory/useWmText.ts — AA-994: the lib/writingMemory.ts helpers that print text, bound to the UI
// language (lib/writingMemory.messages.ts). English outside an I18nProvider.

import { useMemo } from "react";
import { useLang } from "../../_kit";
import { checkSentence, owner, wmSend, type PostActivationReport, type Scope } from "@/lib/writingMemory";
import { CHECK_SENTENCE_MESSAGES, OWNER_MESSAGES, SEND_MESSAGES } from "@/lib/writingMemory.messages";

export function useWmText() {
  const lang = useLang();
  return useMemo(() => ({
    owner: (scope: Scope, tenantId: string | null, tenantName?: string) =>
      owner(scope, tenantId, tenantName, OWNER_MESSAGES[lang]),
    send: <T>(path: string, method: string, body?: unknown) => wmSend<T>(path, method, body, SEND_MESSAGES[lang]),
    checkSentence: (r: PostActivationReport) => checkSentence(r, CHECK_SENTENCE_MESSAGES[lang]),
  }), [lang]);
}
