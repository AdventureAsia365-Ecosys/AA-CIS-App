"use client";
// app/admin/review/RegenerateModal.tsx
// AA-662 PR 2 + AA-719 — non-blocking regenerate.
//
// Change from the original: it NO LONGER awaits pollJob. run-tour-async returns 202 + job_id
// immediately (the pipeline runs async on the backend), so we enqueue, hand the tour id back to
// the page (which marks the row "regenerating" and lets react-query poll the queue), and close the
// modal right away. The screen never freezes.
//
// Supports a single tour (the row's Regenerate button) or a bulk set (DataTable "Regenerate
// selected"). AA-1024 A2 (ADR 0009 decision 5): no model tier pick any more: every regenerate runs on the s1_generate
// route (AI providers › Routing), the same model the rest of the pipeline uses; the API refuses a per-request model.
// AA-916 WM-07: a reason chip row (preselected from the rows' failure codes) — every started job sends its review id
// and the chip, so the regenerate is a writing signal too.

import { RotateCcw } from "lucide-react";
import { useState } from "react";
import { K, Button, Modal, useT, Spinner } from "../../_kit";
import { M } from "./messages";
import { startRegenerate } from "./reviewApi";
import { WritingReasonChips } from "../_components/WritingReasonChips";
import { commonReason, type ReasonCode } from "@/lib/writingReasons";
import type { ReviewItem } from "./reviewModel";

export function RegenerateModal({
  items,
  onClose,
  onEnqueued,
}: {
  // One or more rows to regenerate. The modal is open iff items.length > 0.
  items: ReviewItem[];
  onClose: () => void;
  // Called after every tour has been enqueued, with the ids that started (so the page can mark
  // their rows "regenerating" and begin polling).
  onEnqueued: (tourIds: string[], reviewIds: string[]) => void;
}) {
  const t = useT(M);
  const bulk = items.length > 1;
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // AA-916 — null = the preselection from the rows' codes (derived, so a new set of rows gets its own)
  const [reasonPick, setReasonPick] = useState<ReasonCode | null>(null);
  const suggested = commonReason(items.map((it) => it.codes));
  const reason = reasonPick ?? suggested;

  const canRun = !running && items.length > 0;

  async function run() {
    setRunning(true);
    setError(null);
    const startedTours: string[] = [];
    const startedReviews: string[] = [];
    const errors: string[] = [];
    for (const it of items) {
      try {
        await startRegenerate({
          tourId: it.raw.tour_id, reviewId: it.id, reasonCode: reason,
          failMessage: (s) => t("regenStartFailed", s),
        });
        startedTours.push(String(it.raw.tour_id));
        startedReviews.push(it.id);
      } catch (err) {
        errors.push(`${it.name}: ${err instanceof Error ? err.message : t("failed")}`);
      }
    }
    setRunning(false);
    if (startedTours.length > 0) {
      onEnqueued(startedTours, startedReviews);
    }
    if (errors.length > 0 && startedTours.length === 0) {
      // Nothing started — keep the modal open and show why.
      setError(errors.join("; "));
    } else {
      onClose();
    }
  }

  const title = bulk ? t("regenMany", items.length) : t("regenOne");

  return (
    <Modal
      open={items.length > 0}
      onClose={() => {
        if (!running) onClose();
      }}
      title={title}
      footer={
        <>
          <Button variant="ghost" size="sm" onClick={onClose} disabled={running}>
            {t("cancel")}
          </Button>
          <Button variant="primary" size="sm" onClick={run} disabled={!canRun}>
            {running ? <Spinner size={12} color="currentColor" /> : <RotateCcw size={12} />} {t("regenerate")}
          </Button>
        </>
      }
    >
      <p style={{ fontSize: 13, color: K.body, lineHeight: 1.6, margin: "0 0 8px" }}>
        {t("costBefore")}<strong>{t("fullPipeline")}</strong>{t("costAfter")}
      </p>
      <p style={{ fontSize: 12, color: K.muted, lineHeight: 1.6, margin: "0 0 8px" }}>
        {t("regenExplain")}
      </p>
      <p data-testid="regen-route-note" style={{ fontSize: 12, color: K.muted, lineHeight: 1.6, margin: "0 0 14px" }}>
        {t("regenRouteNote")}
      </p>

      {bulk ? (
        <ul style={{ margin: "0 0 14px", paddingLeft: 18, fontSize: 12, color: K.body, maxHeight: 160, overflow: "auto" }}>
          {items.map((it) => (
            <li key={it.id} style={{ marginBottom: 2 }}>
              {it.name}
            </li>
          ))}
        </ul>
      ) : null}

      <div style={{ marginBottom: 14 }}>
        <WritingReasonChips value={reason} suggested={suggested} disabled={running} onChange={setReasonPick}
                            label={bulk ? t("whyAll") : t("why")} />
      </div>

      {error && (
        <div
          style={{
            fontSize: 12,
            padding: "8px 12px",
            borderRadius: 6,
            marginTop: 12,
            background: K.dangerSoft,
            color: K.danger,
          }}
        >
          {error}
        </div>
      )}
    </Modal>
  );
}
