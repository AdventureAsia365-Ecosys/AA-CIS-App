"use client";
// app/admin/review/ReasonActionModal.tsx — AA-916 WM-07. Reject / Dismiss of one review row with the reason chip:
// the chip is preselected from the row's failure codes (lib/writingReasons.ts reviewReason), so the action is one
// click on the confirm button; another chip is one more click. The code goes to the API as `reason_code` (a
// reason-only writing signal, CIS 277).

import { Ban, X } from "lucide-react";
import { useState } from "react";
import { A, Btn } from "../_components/adminUi";
import { Modal } from "../../_kit";
import { WritingReasonChips } from "../_components/WritingReasonChips";
import { reviewReason, type ReasonCode } from "@/lib/writingReasons";
import type { ReviewItem } from "./reviewModel";

export type ReasonAction = "reject" | "dismiss";

const COPY: Record<ReasonAction, { title: string; body: string; confirm: string }> = {
  reject: {
    title: "Reject this version?",
    body: "The tour is marked rejected and this version is not published.",
    confirm: "Reject",
  },
  dismiss: {
    title: "Dismiss this version?",
    body: "Drops this stale failed version from the queue: no edit, no publish. A later failing rerun can still add a new row.",
    confirm: "Dismiss",
  },
};

export function ReasonActionModal({ action, item, busy, onClose, onConfirm }: {
  action: ReasonAction;
  item: ReviewItem;
  busy: boolean;
  onClose: () => void;
  onConfirm: (reason: ReasonCode) => void;
}) {
  const suggested = reviewReason(item.codes);
  const [reason, setReason] = useState<ReasonCode>(suggested);
  const copy = COPY[action];
  return (
    <Modal
      open
      onClose={() => { if (!busy) onClose(); }}
      title={copy.title}
      width={520}
      footer={
        <>
          <Btn variant="ghost" size="sm" onClick={onClose} disabled={busy}>Cancel</Btn>
          <span data-testid={`review-${action}-confirm`}>
            <Btn variant={action === "reject" ? "danger" : "primary"} size="sm" disabled={busy} onClick={() => onConfirm(reason)}>
              {action === "reject" ? <X size={12} /> : <Ban size={12} />} {copy.confirm}
            </Btn>
          </span>
        </>
      }
    >
      <div style={{ fontSize: 13, fontWeight: 600, color: A.ink, marginBottom: 4, overflowWrap: "anywhere" }}>{item.name}</div>
      <p style={{ fontSize: 12.5, color: A.body, lineHeight: 1.6, margin: "0 0 12px" }}>{copy.body}</p>
      <WritingReasonChips value={reason} suggested={suggested} disabled={busy} onChange={setReason} />
    </Modal>
  );
}
