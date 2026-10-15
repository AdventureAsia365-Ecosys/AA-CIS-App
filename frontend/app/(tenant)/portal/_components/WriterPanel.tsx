"use client";
// app/(tenant)/portal/_components/WriterPanel.tsx — AA-668, the Writer panel (Social Content).
//
// Picking a Subject on the Slate opens this right-side kit Drawer (full width on a phone) instead
// of expanding AngleGateWizard inline under the row (AA-564 4.2), so the tenant never scrolls the
// page to find the goal/angle picker or the writing output. Flow inside the panel:
//   context header (Subject + Channel) → Goal cards → Angle cards ("Write with this angle" starts
//   the write at once, CTA from the brand default) → LiveWriter steps + text → the finished piece
//   with its actions (export, publish, open in My Content, change angle) and the AA-819 AI notice.
//
// Everything slow is a background job already: T8 angles (`t8_angles`, AA-764) and T9 write
// (`t9_write`, AA-652). The panel only polls existing endpoints (request status, piece status,
// /v1/progress/piece live view) — no LLM call runs inside an HTTP request. Closing the panel
// therefore never stops anything: `useWriteWatcher` keeps polling a piece that was still being
// written and raises a toast when it finishes.
//
// Tenant sees content only: the finished view shows the content and nothing about gates, holds
// or scores (same tenant-safe piece shape as My Content). Copy is locked like My Content's drawer
// (export is the way out, and every export is audit-logged — AA-613).

import { CheckCircle2, Download, ExternalLink, Send } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { ConfirmModal, Drawer, useToast } from "../../../_kit";
import { T, sans, Btn } from "./ui";
import AngleGateWizard, { type ContentPiece } from "./AngleGateWizard";
import { AiContentNotice } from "./AiContentNotice";
import { PUBLISHABLE_CHANNELS, exportPiece, publishPiece } from "./reviewListApi";

/** What the Slate hands the panel when a Subject is picked (or reopened this session). */
export interface WriterTarget {
  subjectId: string;
  requestId: string;
  title: string;
  channel: string;
  kind: "Segment" | "Route";
}

const CHANNEL_LABEL: Record<string, string> = {
  blog: "Blog", linkedin: "LinkedIn", facebook: "Facebook", instagram: "Instagram",
  tiktok: "TikTok", email: "Email", landing_page: "Landing Page", ads: "Ads",
};
export function channelName(channel: string): string {
  return CHANNEL_LABEL[channel] ?? channel;
}

// Desktop width of the panel. The kit Drawer caps it at 100% of the viewport, which makes it a
// full-screen sheet on a phone.
const PANEL_WIDTH = 720;

export function WriterPanel({ target, onClose, onBackgroundPiece }: {
  target: WriterTarget | null;
  /** Called with the last piece the panel saw, so the Slate can keep watching a running write. */
  onClose: (lastPiece: ContentPiece | null) => void;
  /** A write whose first answer arrived after the panel closed (see AngleGateWizard onDetachedPiece). */
  onBackgroundPiece?: (piece: ContentPiece, target: WriterTarget) => void;
}) {
  // The wizard reports its current piece through this (stable) setter. The Slate remounts the
  // panel per request (key), so a previous request's piece never shows here.
  const [piece, setPiece] = useState<ContentPiece | null>(null);
  // The publish confirm is lifted here so Escape (which both kit Drawer and Modal listen for on
  // window) closes only the confirm, not the whole panel behind it.
  const [confirmPublish, setConfirmPublish] = useState(false);
  const ready = piece?.ready_state === "ready" && piece.content_text != null;
  const confirmOpen = confirmPublish && ready; // the confirm only renders with the result actions
  const close = useCallback(() => { if (!confirmOpen) onClose(piece); }, [onClose, piece, confirmOpen]);
  const detached = useCallback((p: ContentPiece) => {
    if (target) onBackgroundPiece?.(p, target);
  }, [target, onBackgroundPiece]);

  return (
    <Drawer
      open={target !== null}
      onClose={close}
      width={PANEL_WIDTH}
      title={target && (
        <div style={{ minWidth: 0 }}>
          <div data-testid="writer-panel-title" style={{ overflowWrap: "anywhere" }}>{target.title}</div>
          <div style={{ fontFamily: sans, fontSize: 12, fontWeight: 500, color: T.muted, marginTop: 2 }}>
            Writing for <strong style={{ color: T.ink }}>{channelName(target.channel)}</strong>
            <span style={{ color: T.muted2 }}> · </span>{target.kind}
          </div>
        </div>
      )}
      footer={ready && target
        ? <ResultActions piece={piece} target={target} confirmPublish={confirmPublish} setConfirmPublish={setConfirmPublish} />
        : undefined}
    >
      {target && (
        <div data-testid="writer-panel" style={{ display: "flex", flexDirection: "column", gap: 18 }}>
          <AngleGateWizard key={target.requestId} requestId={target.requestId} embedded
            onPieceChange={setPiece} onDetachedPiece={detached} />
          {ready && <FinishedPiece piece={piece} />}
        </div>
      )}
    </Drawer>
  );
}

// The finished piece, copy-locked like My Content (ReviewDrawer.tsx): export is the way out.
function FinishedPiece({ piece }: { piece: ContentPiece }) {
  const toast = useToast();
  const hint = () => toast.info("Use Export to download this content.");
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10, fontFamily: sans }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12.5, color: T.body }}>
        <CheckCircle2 size={15} color={T.green} />
        <span>Your post is ready and saved in <Link href={`/portal/t10-review?piece=${piece.piece_id}`} style={{ color: T.gold, fontWeight: 600 }}>My Content</Link>, where you can edit it.</span>
      </div>
      <div
        data-testid="writer-panel-result"
        onCopy={(e) => { e.preventDefault(); hint(); }}
        onCut={(e) => { e.preventDefault(); hint(); }}
        onContextMenu={(e) => { e.preventDefault(); hint(); }}
        style={{
          padding: "14px 16px", borderRadius: 10, border: `1px solid ${T.line}`, background: T.card,
          whiteSpace: "pre-wrap", overflowWrap: "anywhere", fontSize: 13.5, lineHeight: 1.65, color: T.body,
          userSelect: "none", WebkitUserSelect: "none",
        }}
      >
        {piece.content_text}
      </div>
    </div>
  );
}

function ResultActions({ piece, target, confirmPublish, setConfirmPublish }: {
  piece: ContentPiece; target: WriterTarget;
  confirmPublish: boolean; setConfirmPublish: (open: boolean) => void;
}) {
  const toast = useToast();
  const router = useRouter();
  const [publishing, setPublishing] = useState(false);
  const channel = piece.channel ?? target.channel;
  const exportable = { piece_id: piece.piece_id, channel, angle: { name: target.title } };

  async function doExport(format: "text" | "html") {
    try {
      await exportPiece(exportable, format);
    } catch {
      toast.error("Export failed — try again.");
    }
  }

  async function doPublish() {
    setPublishing(true);
    try {
      const res = await publishPiece(piece.piece_id);
      toast.success(res.replayed ? "Already published." : `Published to ${channelName(channel)}.`);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Couldn't publish — try again.");
    } finally {
      setPublishing(false);
      setConfirmPublish(false);
    }
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10, width: "100%", minWidth: 0 }}>
      {/* AA-819 — the neutral AI notice beside every publish/export action. */}
      <AiContentNotice />
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", justifyContent: "flex-end" }}>
        <Btn size="sm" variant="secondary" onClick={() => doExport("text")}>
          <Download size={12} /> Export text
        </Btn>
        {channel === "blog" && (
          <Btn size="sm" variant="secondary" onClick={() => doExport("html")}>
            <Download size={12} /> Export HTML
          </Btn>
        )}
        <Btn size="sm" variant="secondary" onClick={() => router.push(`/portal/t10-review?piece=${piece.piece_id}`)}>
          <ExternalLink size={12} /> Open in My Content
        </Btn>
        {PUBLISHABLE_CHANNELS.has(channel) && (
          <Btn size="sm" variant="primary" disabled={publishing} onClick={() => setConfirmPublish(true)}>
            <Send size={12} /> Publish
          </Btn>
        )}
      </div>
      <ConfirmModal
        open={confirmPublish}
        onClose={() => !publishing && setConfirmPublish(false)}
        onConfirm={doPublish}
        busy={publishing}
        title={`Publish to ${channelName(channel)}?`}
        body={<>This posts the content to your connected {channelName(channel)} account now. <AiContentNotice style={{ marginTop: 10 }} /></>}
        confirmLabel="Publish now"
      />
    </div>
  );
}

// ── Background watch ────────────────────────────────────────────────────────────────────────
// A write keeps running when the panel closes (it is a t9_write job). The Slate keeps a list of
// pieces it closed mid-write and polls each one until it settles, then raises a toast.

interface Watched { pieceId: string; requestId: string; title: string; since: number }

const WATCH_POLL_MS = 4000;
const WATCH_CEILING_MS = 10 * 60_000;

export function useWriteWatcher() {
  const toast = useToast();
  const [watched, setWatched] = useState<Watched[]>([]);
  // Settled ids, so a slow response from an older interval can't toast the same piece twice.
  const settled = useRef<Set<string>>(new Set());

  const watch = useCallback((w: Omit<Watched, "since">) => {
    settled.current.delete(w.pieceId);
    setWatched(prev => (prev.some(x => x.pieceId === w.pieceId) ? prev : [...prev, { ...w, since: Date.now() }]));
  }, []);

  // Reopening a request hands it back to the panel (the wizard resumes it itself).
  const unwatch = useCallback((requestId: string) => {
    setWatched(prev => prev.filter(x => x.requestId !== requestId));
  }, []);

  useEffect(() => {
    if (watched.length === 0) return;
    const drop = (pieceId: string) => {
      settled.current.add(pieceId);
      setWatched(prev => prev.filter(x => x.pieceId !== pieceId));
    };
    const id = setInterval(() => {
      for (const w of watched) {
        if (settled.current.has(w.pieceId)) continue;
        if (Date.now() - w.since > WATCH_CEILING_MS) {
          drop(w.pieceId);
          toast.info(`"${w.title}" is still being written — it will show up in My Content.`, 8000);
          continue;
        }
        fetch(`/api/tenant/v1/content-writing/pieces/${w.pieceId}`, { cache: "no-store" })
          .then(r => (r.ok ? r.json() : null))
          .then((p: ContentPiece | null) => {
            if (!p || p.ready_state === "in_progress" || settled.current.has(w.pieceId)) return;
            drop(w.pieceId);
            if (p.ready_state === "ready") toast.success(`"${w.title}" is ready — find it in My Content.`, 8000);
            else toast.error(`Writing "${w.title}" didn't finish — open it again to retry.`, 8000);
          })
          .catch(() => { /* transient — next tick */ });
      }
    }, WATCH_POLL_MS);
    return () => clearInterval(id);
  }, [watched, toast]);

  return { watch, unwatch, watching: watched.length };
}
