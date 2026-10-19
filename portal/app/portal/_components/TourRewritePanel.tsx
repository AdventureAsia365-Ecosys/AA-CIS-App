"use client";
// portal/app/portal/_components/TourRewritePanel.tsx — AA-668 part 2 (S239): the tour rewrite in
// the Writer panel.
//
// "Request Rewrite" in the My Catalog Tours drawer (CatalogDrawer.tsx) used to swap the drawer's
// content for an inline LiveWriter. It now opens the same right-side writer drawer as Social
// Content (`WriterDrawer`): context header (tour title, language, channel), the live steps + text
// of the `t2_rewrite` job (LiveWriter compact), "Writing didn't finish" + Retry on a failed job,
// and on success "Open in My Catalog" (the version drawer) + Export DOCX. Closing the panel never
// stops the job: the page's `useWriteWatcher` polls the version and toasts when it settles.
//
// Market is not in the header: T2 rewrites have no market field (the version carries a language),
// and the Slate API has no per-Subject market either — see docs/implementation-notes/AA-668.md.
//
// The panel does not poll on its own for the version status: CatalogTab's my-versions query
// already refetches every 5 s while any version is being written, and hands the row in (`version`).

import { CheckCircle2, Download, ExternalLink, RotateCcw } from "lucide-react";
import { Button, K } from "@/app/_kit";
import { AiContentNotice } from "./AiContentNotice";
import LiveWriter from "./LiveWriter";
import { WriterDrawer, type WatchItem, type WatchOutcome } from "./WriterPanel";
import { fetchVersionDetail, isAiWriting, isRewriteFailed, type Version } from "./catalogApi";

/** What My Catalog hands the panel when a rewrite starts (or a writing / failed row is opened). */
export interface TourRewriteTarget {
  versionId: string;
  publishedTourId: string;
  title: string;
  language: string;
}

/** The channel a T2 rewrite writes for: the tenant's own tour catalog. */
export const TOUR_REWRITE_CHANNEL = "Tour catalog";

export type RewriteState = "starting" | "writing" | "failed" | "ready";

export function rewriteState(version: Pick<Version, "status" | "edit_source" | "job_status"> | null): RewriteState {
  if (!version) return "starting"; // the list has not picked the new version up yet
  if (isRewriteFailed(version)) return "failed";
  if (isAiWriting(version)) return "writing";
  return "ready";
}

/** The watch item for a tour rewrite closed mid-write (useWriteWatcher in WriterPanel.tsx). */
export function watchTourRewrite(t: TourRewriteTarget): WatchItem {
  return {
    id: t.versionId, group: t.versionId, title: t.title,
    check: () => fetchVersionDetail(t.versionId).then((v): WatchOutcome => {
      const s = rewriteState(v);
      return s === "ready" ? "ready" : s === "failed" ? "failed" : "running";
    }),
    readyText: `"${t.title}" is ready — find it in My Catalog Tours.`,
    failedText: `Rewriting "${t.title}" didn't finish — open it in My Catalog Tours to retry.`,
    slowText: `"${t.title}" is still being written — it will show up in My Catalog Tours.`,
  };
}

export function TourRewritePanel({
  target, version, retries, retrying, exporting, onClose, onRetry, onOpenResult, onExportDocx,
}: {
  target: TourRewriteTarget | null;
  /** The version row from the my-versions list (null until the list has it). */
  version: Version | null;
  /** Bumped on every retry so the live view starts again from a fresh poll. */
  retries: number;
  retrying: boolean;
  exporting: boolean;
  /** `running` = the job was still writing when the tenant closed the panel. */
  onClose: (running: boolean) => void;
  onRetry: () => void;
  onOpenResult: () => void;
  onExportDocx: () => void;
}) {
  const state = rewriteState(version);
  const running = state === "starting" || state === "writing";
  const ready = state === "ready";

  return (
    <WriterDrawer
      open={target !== null}
      onClose={() => onClose(running)}
      title={target?.title ?? ""}
      context={target ? [
        { label: "Rewriting in", value: target.language },
        { label: "for", value: TOUR_REWRITE_CHANNEL },
      ] : []}
      footer={ready && target ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 10, width: "100%", minWidth: 0 }}>
          <AiContentNotice />
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", justifyContent: "flex-end" }}>
            <Button size="sm" variant="secondary" disabled={exporting} onClick={onExportDocx}>
              <Download size={12} /> {exporting ? "Exporting…" : "Export DOCX"}
            </Button>
            <Button size="sm" variant="primary" onClick={onOpenResult}>
              <ExternalLink size={12} /> Open in My Catalog
            </Button>
          </div>
        </div>
      ) : undefined}
    >
      {target && state === "failed" && (
        <div data-testid="tour-rewrite-failed" style={{ display: "flex", flexDirection: "column", gap: 10, alignItems: "flex-start" }}>
          <div style={{ fontSize: 14, fontWeight: 600, color: K.ink }}>Writing didn&apos;t finish</div>
          <div style={{ fontSize: 12.5, color: K.muted }}>
            Something went wrong while writing this tour. Retrying does not use another rewrite from your plan.
          </div>
          <Button size="sm" variant="primary" disabled={retrying} onClick={onRetry}>
            <RotateCcw size={12} /> {retrying ? "Retrying…" : "Retry writing"}
          </Button>
        </div>
      )}
      {target && ready && (
        <div data-testid="tour-rewrite-ready" style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12.5, color: K.body }}>
          <CheckCircle2 size={15} color={K.success} />
          <span>Your new version is ready and saved in My Catalog Tours, where you can edit and export it.</span>
        </div>
      )}
      {target && state !== "failed" && (
        <>
          {/* Polls until the version leaves "writing" (untilInactive), and is keyed per retry so a
              retried job starts from a fresh view. Once ready it keeps the last snapshot (the
              finished text). */}
          <LiveWriter key={`${target.versionId}:${retries}`} kind="tour" jobId={target.versionId}
            title="Writing your tour" compact active={running} untilInactive />
          {running && (
            <div style={{ fontSize: 12, color: K.muted }}>
              Usually a few minutes — longer when the checks ask for a revision. You can close this panel;
              the rewrite keeps going and you will get a notice when it is ready.
            </div>
          )}
        </>
      )}
    </WriterDrawer>
  );
}
