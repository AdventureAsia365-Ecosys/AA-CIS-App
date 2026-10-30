"use client";
// app/admin/review/ReasonActionModal.tsx — AA-916 WM-07. Reject / Dismiss of one review row with the reason chip:
// the chip is preselected from the row's failure codes (lib/writingReasons.ts reviewReason), so the action is one
// click on the confirm button; another chip is one more click. The code goes to the API as `reason_code` (a
// reason-only writing signal, CIS 277).

import { Ban, X } from "lucide-react";
import { useState } from "react";
import { K, Button, Modal, useT } from "../../_kit";
import { M } from "./messages";
import { WritingReasonChips } from "../_components/WritingReasonChips";
import { reviewReason, type ReasonCode } from "@/lib/writingReasons";
import type { ReviewItem } from "./reviewModel";

export type ReasonAction = "reject" | "dismiss";

const COPY = {
  reject: { title: "rejectTitle", body: "rejectBody", confirm: "reject" },
  dismiss: { title: "dismissModalTitle", body: "dismissBody", confirm: "dismiss" },
} as const satisfies Record<ReasonAction, { title: keyof typeof M.en; body: keyof typeof M.en; confirm: keyof typeof M.en }>;

export function ReasonActionModal({ action, item, busy, onClose, onConfirm }: {
  action: ReasonAction;
  item: ReviewItem;
  busy: boolean;
  onClose: () => void;
  onConfirm: (reason: ReasonCode) => void;
}) {
  const suggested = reviewReason(item.codes);
  const [reason, setReason] = useState<ReasonCode>(suggested);
  const t = useT(M);
  const keys = COPY[action];
  const copy = { title: t(keys.title), body: t(keys.body), confirm: t(keys.confirm) };
  return (
    <Modal
      open
      onClose={() => { if (!busy) onClose(); }}
      title={copy.title}
      width={520}
      footer={
        <>
          <Button variant="ghost" size="sm" onClick={onClose} disabled={busy}>{t("cancel")}</Button>
          <span data-testid={`review-${action}-confirm`}>
            <Button variant={action === "reject" ? "danger" : "primary"} size="sm" disabled={busy} onClick={() => onConfirm(reason)}>
              {action === "reject" ? <X size={12} /> : <Ban size={12} />} {copy.confirm}
            </Button>
          </span>
        </>
      }
    >
      <div style={{ fontSize: 13, fontWeight: 600, color: K.ink, marginBottom: 4, overflowWrap: "anywhere" }}>{item.name}</div>
      <p style={{ fontSize: 12.5, color: K.body, lineHeight: 1.6, margin: "0 0 12px" }}>{copy.body}</p>
      <WritingReasonChips value={reason} suggested={suggested} disabled={busy} onChange={setReason} />
    </Modal>
  );
}
