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
// selected"). For bulk, one tier choice applies to every selected tour that has no recorded tier.

import { Loader2, RotateCcw } from "lucide-react";
import { useState } from "react";
import { A, mono, sans, Btn } from "../_components/adminUi";
import { Modal } from "../../_kit";
import { startRegenerate } from "./reviewApi";
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
  const bulk = items.length > 1;
  // A tour's recorded tier, if any. For bulk we only need a chosen tier when some tour lacks one.
  const anyMissingTier = items.some((it) => !it.raw?.requested_tier);
  const [tier, setTier] = useState<string>("");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Each tour uses its own recorded tier if present, else the chosen fallback tier.
  const canRun = !running && items.length > 0 && (!anyMissingTier || !!tier);

  async function run() {
    setRunning(true);
    setError(null);
    const startedTours: string[] = [];
    const startedReviews: string[] = [];
    const errors: string[] = [];
    for (const it of items) {
      const effectiveTier = (it.raw?.requested_tier as string) || tier;
      if (!effectiveTier) {
        errors.push(`${it.name}: no model tier`);
        continue;
      }
      try {
        await startRegenerate({ tourId: it.raw.tour_id, modelTier: effectiveTier });
        startedTours.push(String(it.raw.tour_id));
        startedReviews.push(it.id);
      } catch (err) {
        errors.push(`${it.name}: ${err instanceof Error ? err.message : "failed"}`);
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

  const title = bulk ? `Regenerate ${items.length} tours?` : "Regenerate this version?";

  return (
    <Modal
      open={items.length > 0}
      onClose={() => {
        if (!running) onClose();
      }}
      title={title}
      footer={
        <>
          <Btn variant="ghost" size="sm" onClick={onClose} disabled={running}>
            Cancel
          </Btn>
          <Btn variant="primary" size="sm" onClick={run} disabled={!canRun}>
            {running ? <Loader2 size={12} className="spin" /> : <RotateCcw size={12} />} Regenerate
          </Btn>
        </>
      }
    >
      <p style={{ fontSize: 13, color: A.body, lineHeight: 1.6, margin: "0 0 8px" }}>
        This re-runs the <strong>full pipeline</strong> on real models — it costs actual Bedrock
        spend.
      </p>
      <p style={{ fontSize: 12, color: A.muted, lineHeight: 1.6, margin: "0 0 14px" }}>
        Each job is queued and runs in the background; the queue updates on its own as jobs finish. A
        version that passes publishes to Master Content and leaves this queue; one that fails
        replaces the current row.
      </p>

      {bulk ? (
        <ul style={{ margin: "0 0 14px", paddingLeft: 18, fontSize: 12, color: A.body, maxHeight: 160, overflow: "auto" }}>
          {items.map((it) => (
            <li key={it.id} style={{ marginBottom: 2 }}>
              {it.name}
              {it.raw?.requested_tier ? (
                <span style={{ color: A.muted, fontFamily: mono }}> · {String(it.raw.requested_tier)}</span>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}

      {anyMissingTier ? (
        <div style={{ marginBottom: 6 }}>
          <div style={{ fontSize: 12, color: A.body, marginBottom: 6 }}>
            {bulk
              ? "Some tours have no recorded model tier — pick one for them:"
              : "No model tier was recorded for this tour — choose one:"}
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            {(["haiku", "sonnet"] as const).map((t) => (
              <button
                key={t}
                onClick={() => setTier(t)}
                disabled={running}
                style={{
                  flex: 1,
                  padding: "8px 10px",
                  borderRadius: 8,
                  cursor: running ? "not-allowed" : "pointer",
                  fontSize: 13,
                  fontFamily: sans,
                  textTransform: "capitalize",
                  border: `1px solid ${tier === t ? A.gold : A.line}`,
                  background: tier === t ? A.gold : A.card,
                  color: tier === t ? "var(--aa-on-solid)" : A.body,
                }}
              >
                {t}
              </button>
            ))}
          </div>
        </div>
      ) : null}

      {error && (
        <div
          style={{
            fontSize: 12,
            padding: "8px 12px",
            borderRadius: 6,
            marginTop: 12,
            background: A.redSoft,
            color: A.red,
          }}
        >
          {error}
        </div>
      )}
    </Modal>
  );
}
