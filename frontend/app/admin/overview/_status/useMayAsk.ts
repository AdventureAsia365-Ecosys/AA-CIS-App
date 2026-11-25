"use client";
// app/admin/overview/_status/useMayAsk.ts — AA-1154 (A1): may this session ask the assistant's AI? Each ask (Analyse,
// the automatic analysis, Phrase with AI) is a POST the API refuses for a read-only session (a viewer, view-as, the
// Dev E2E identity: api/core/staff_authz.py and the BFF). The E2E identity's role cookie says "admin", so wait for the
// API's answer on the session (GET /admin/session through useSessionAccess) before the first ask.

import { useSessionAccess } from "../../../_kit/useSessionAccess";

export function useMayAsk(): boolean {
  const { readOnly, settled } = useSessionAccess();
  return settled && !readOnly;
}
