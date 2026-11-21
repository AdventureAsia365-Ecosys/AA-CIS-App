"use client";
// app/admin/platform/staff/EditorParts.tsx — AA-1155 (spec tasks T13 / T14): the pieces the Staff page's CIS role
// editor (RoleEditor.tsx) and preset editor (PresetEditor.tsx) share: one write through the BFF with its outcome
// banner, and the status line (loading, error, not available yet, empty). Kit components only.

import { useState } from "react";
import { K, RADIUS, apiSend } from "../../../_kit";
import { OutcomeBanner, type Outcome } from "./StaffManage";

/** One editor write (POST / PATCH through the BFF): busy while it runs, its outcome for the banner. `onSaved` runs
 *  after a write that succeeded (refresh the lists). Never throws. */
export function useEditorWrite(onSaved: () => Promise<unknown>) {
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [busy, setBusy] = useState(false);
  const run = async (path: string, method: "POST" | "PATCH", body: unknown, ok: string): Promise<boolean> => {
    setBusy(true);
    let next: Outcome = { kind: "ok", message: ok };
    try {
      await apiSend(path, { method, body });
      await onSaved();
    } catch (e) {
      next = { kind: "error", message: e instanceof Error ? e.message : String(e) };
    }
    setBusy(false);
    setOutcome(next);
    return next.kind === "ok";
  };
  return { outcome, setOutcome, busy, run };
}

/** The editor's banner (the last write's outcome) and its status line. */
export function EditorStatus({ outcome, onOutcome, loading, error, unavailable, empty, words }: {
  outcome: Outcome | null;
  onOutcome: (o: Outcome | null) => void;
  loading: boolean;
  error: unknown;
  unavailable: boolean;
  empty: boolean;
  words: { loading: string; unavailable: string; none: string };
}) {
  const line = loading ? words.loading : unavailable ? words.unavailable : empty ? words.none : null;
  return (
    <>
      {outcome && <OutcomeBanner outcome={outcome} onRetried={onOutcome} />}
      {error instanceof Error && <OutcomeBanner outcome={{ kind: "error", message: error.message }} onRetried={() => {}} />}
      {line && <span style={{ fontSize: 13, color: K.muted }}>{line}</span>}
    </>
  );
}

/** One row of an editor list: name, badges and a details line at the left, the actions at the right. */
export function EditorRow({ testId, dim, title, details, actions }: {
  testId: string;
  dim: boolean;
  title: React.ReactNode;
  details: React.ReactNode;
  actions?: React.ReactNode;
}) {
  return (
    <div data-testid={testId} style={{
      display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", padding: "10px 12px", background: K.line2,
      borderRadius: RADIUS.sm, opacity: dim ? 0.75 : 1,
    }}>
      <span style={{ flex: "1 1 260px", minWidth: 0, display: "flex", flexDirection: "column", gap: 3 }}>
        <span style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>{title}</span>
        <span style={{ fontSize: 12, color: K.muted, lineHeight: 1.45, overflowWrap: "anywhere" }}>{details}</span>
      </span>
      {actions && <span style={{ display: "flex", gap: 6 }}>{actions}</span>}
    </div>
  );
}

/** A group of kit checkboxes laid out in a wrapping row. */
export const CHOICE_ROW: React.CSSProperties = { display: "flex", gap: "4px 16px", flexWrap: "wrap" };
