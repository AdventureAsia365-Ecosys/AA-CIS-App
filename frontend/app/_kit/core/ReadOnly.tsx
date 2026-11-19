// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
"use client";
// app/_kit/core/ReadOnly.tsx
// AA-1127 C3 — the read-only session hint, one for the whole app. The app shell knows whether the signed-in session
// may change data at all (CIS: GET /admin/session through app/_kit/useSessionAccess.ts; a read-only role or a Dev-only
// E2E identity) and hands it down with <ReadOnlyProvider readOnly>; a page reads it with useReadOnly() or wraps its
// run / save / paid buttons in <WriteOnly>, and says why they are missing with <ReadOnlyNote>. Before this, only the
// AI gateway page knew, and every other page showed buttons the server then refused.
//
// It is a display hint, never an access decision: the server still refuses every write it must. Outside a provider
// the hint is "may write" (false), so an app that has no such signal yet behaves as before.

import { createContext, useContext, type ReactNode } from "react";
import { KIT_MESSAGES } from "./kit.messages";
import { K } from "./tokens";
import { useT } from "./useI18n";

const ReadOnlyContext = createContext(false);

/** The app shell's provider: `readOnly` true when the session may not change data. */
export function ReadOnlyProvider({ readOnly, children }: { readOnly: boolean; children: ReactNode }) {
  return <ReadOnlyContext.Provider value={readOnly}>{children}</ReadOnlyContext.Provider>;
}

/** True when the signed-in session may not change data (false outside a provider). */
export function useReadOnly(): boolean {
  return useContext(ReadOnlyContext);
}

/** Renders `children` (write controls) only when the session may change data; else `fallback` (default nothing). */
export function WriteOnly({ children, fallback = null }: { children: ReactNode; fallback?: ReactNode }) {
  return <>{useReadOnly() ? fallback : children}</>;
}

/** One short line saying the page's actions are hidden because the session is read-only; nothing otherwise. */
export function ReadOnlyNote({ testId = "read-only-note" }: { testId?: string }) {
  const t = useT(KIT_MESSAGES);
  if (!useReadOnly()) return null;
  return (
    <span data-testid={testId} role="note" style={{ fontSize: 12, color: K.muted }}>
      {t("readOnlySession")}
    </span>
  );
}
