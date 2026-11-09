"use client";
// app/admin/ai-gateway/useErrorClassText.ts — C4 polish (N4): an LLM call's error class (ledger v2, migration 319:
// access_denied, quota_exhausted, throttled, credential, other, deadline, budget, truncated; disabled from
// migration 326; role_not_served from migration 338) in words, in the page's
// language (messages.ts ERROR_CLASS). A class this build does not know reads "other error"; the code is never shown.

import { useMemo } from "react";
import { useT } from "../../_kit";
import { errorClassKey } from "@/lib/aiGateway";
import { ERROR_CLASS } from "./messages";

/** `(cls) => words | null`: null when there is no class. */
export function useErrorClassText(): (cls: string | null | undefined) => string | null {
  const te = useT(ERROR_CLASS);
  return useMemo(() => (cls: string | null | undefined) => {
    const key = errorClassKey(cls);
    return key ? te(key) : null;
  }, [te]);
}
