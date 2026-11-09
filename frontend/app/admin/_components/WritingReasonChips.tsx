"use client";
// app/admin/_components/WritingReasonChips.tsx — AA-916 WM-07. The one-click reason chip row an admin sets when
// acting on writer output: the Content Quality sentence actions (T9 drawer, T2 TourVersionDrawer) and the A1 review
// Reject / Dismiss / Regenerate. One chip is always selected (preselected from the flagged gate or the review row's
// failure codes), so the action button stays one click; another chip is one more click. The value goes to the API
// as `reason_code` (shared.writing_signal, CIS 277). Codes and labels: lib/writingReasons.ts.
//
// The chips are the kit SegmentedControl (chips look): a radio group (role="radiogroup" / "radio"), the arrow keys
// move the choice (AA-1006).

import { K, SegmentedControl, useT } from "../../_kit";
import { REASON_CODES, type ReasonCode } from "@/lib/writingReasons";
import { REASON_HINT_MESSAGES, REASON_LABEL_MESSAGES } from "@/lib/writingReasons.messages";
import { M } from "./components.messages";

export function WritingReasonChips({ value, onChange, disabled = false, suggested, label: labelProp }: {
  value: ReasonCode;
  onChange: (code: ReasonCode) => void;
  disabled?: boolean;
  /** The preselected code (marked "suggested" in its tooltip). */
  suggested?: ReasonCode;
  label?: string;
}) {
  const t = useT(M);
  const reasonLabel = useT(REASON_LABEL_MESSAGES);
  const reasonHint = useT(REASON_HINT_MESSAGES);
  const label = labelProp ?? t("why");
  return (
    <div data-testid="wm-reason-chips" style={{ display: "flex", flexWrap: "wrap", gap: 5, alignItems: "center", minWidth: 0 }}>
      <span style={{ fontSize: 11, fontWeight: 600, color: K.muted, marginRight: 2 }}>{label}</span>
      <SegmentedControl<ReasonCode>
        label={t("reasonGroup", label)}
        appearance="chips"
        size="sm"
        value={value}
        onChange={onChange}
        style={{ gap: 5 }}
        options={REASON_CODES.map((code) => ({
          value: code,
          label: reasonLabel(code),
          disabled,
          title: `${reasonHint(code)}${code === suggested ? t("suggested") : ""}`,
        }))}
      />
    </div>
  );
}

/** The stored reason of an admin edit, read-only (history rows). */
export function ReasonTag({ code }: { code: string | null | undefined }) {
  const reasonLabel = useT(REASON_LABEL_MESSAGES);
  const reasonHint = useT(REASON_HINT_MESSAGES);
  if (!code) return null;
  const known = (REASON_CODES as readonly string[]).includes(code);
  return (
    <span data-testid="wm-reason-tag" title={known ? reasonHint(code as ReasonCode) : code}
          style={{ fontSize: 10.5, fontWeight: 600, padding: "1px 8px", borderRadius: 999, color: K.accentDeep,
                   border: `1px solid ${K.accentBorder}`, whiteSpace: "nowrap" }}>
      {known ? reasonLabel(code as ReasonCode) : code}
    </span>
  );
}
