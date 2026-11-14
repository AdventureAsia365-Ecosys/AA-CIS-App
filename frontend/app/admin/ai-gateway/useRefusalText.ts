"use client";
// app/admin/ai-gateway/useRefusalText.ts — S280 (AA-1096): the text of a refused AI providers write in the UI language.
// The read-only session (403 e2e_read_only) and a tighten-only role (S279: a 403 whose message starts with the
// backend's settings_registry.TIGHTEN_ONLY sentence) read as the page's own sentences; any other error as its
// message (the kit apiSend already turns a pydantic 422 into "<field>: <msg>").

import { useCallback } from "react";
import { useT } from "../../_kit";
import { isReadOnlyRefusal } from "@/lib/aiGateway";
import { isTightenOnlyRefusal } from "@/lib/capTighten";
import { REFUSAL } from "./refusal.messages";

export function useRefusalText(): (e: unknown) => string {
  const t = useT(REFUSAL);
  return useCallback((e: unknown) => (isReadOnlyRefusal(e) ? t("readOnly")
    : isTightenOnlyRefusal(e) ? t("tightenOnly") : e instanceof Error ? e.message : String(e)), [t]);
}
