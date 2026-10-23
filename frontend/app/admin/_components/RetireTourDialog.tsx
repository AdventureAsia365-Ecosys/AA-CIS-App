"use client";
// AA-986 L2 (ADR 0015, Q8) — confirm dialog for retiring a published tour.
//
// Before the staff member confirms, the dialog shows a dry run of what the retire does: the backend runs the very
// same lifecycle action in a transaction it rolls back (GET /admin/tours/{id}/lifecycle-preview) and returns the
// counts: tenants that will be warned, their T2 copies / Slate subjects / angle requests / pieces / live posts, the
// queued tenant jobs it cancels and the pending reviews it dismisses. Bookings are not counted in CIS (AA-Booking
// keeps confirmed bookings and flags them for staff, slice L5); the dialog says so.
//
// Two actions use it: "Retire tour" (the source at S0/A0: PATCH /admin/tours/{id}/trash) and the Master "Trash"
// (PATCH /admin/master/{id}/trash). Both retire the tour; restore brings it back.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { ConfirmModal, K, apiGet, ApiError } from "../../_kit";
import { staffErrorMessage } from "@/lib/rbac";

export type RetireAction = "retire_source" | "trash_master";

interface PreviewCounts {
  tenants: number;
  t2_copies: number;
  subjects: number;
  angle_requests: number;
  pieces: number;
  live_posts: number;
}

interface LifecyclePreview {
  action: RetireAction;
  allowed: boolean;
  refused: string | null;
  state_before?: string | null;
  state_after?: string | null;
  reason_after?: string | null;
  derivatives: PreviewCounts;
  queued_jobs: number;
  jobs_cancelled?: number;
  reviews_dismissed?: number;
  notifies_tenants?: number;
  bookings: number | null;
  bookings_note: string;
}

const ENDPOINT: Record<RetireAction, (id: string) => string> = {
  retire_source: (id) => `/api/admin/tours/${id}/trash`,
  trash_master: (id) => `/api/admin/master/${id}/trash`,
};

const TITLE: Record<RetireAction, string> = {
  retire_source: "Retire this tour",
  trash_master: "Trash this Master",
};

function Row({ label, value, note }: { label: string; value: React.ReactNode; note?: string }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", gap: 12, padding: "4px 0",
                  borderBottom: `1px solid ${K.line}` }}>
      <span style={{ color: K.body }}>
        {label}
        {note && <span style={{ display: "block", fontSize: 11, color: K.muted }}>{note}</span>}
      </span>
      <span style={{ fontWeight: 600, color: K.ink, whiteSpace: "nowrap" }}>{value}</span>
    </div>
  );
}

export function RetireTourDialog({
  tourId,
  tourName,
  action,
  onClose,
  onDone,
}: {
  tourId: string;
  tourName: string;
  action: RetireAction;
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const preview = useQuery({
    queryKey: ["lifecycle-preview", tourId, action],
    queryFn: () => apiGet<LifecyclePreview>(
      `/api/admin/tours/${tourId}/lifecycle-preview?action=${encodeURIComponent(action)}`),
    staleTime: 0,
    gcTime: 0,
  });
  const p = preview.data;
  const blocked = !p || !p.allowed;

  async function confirm() {
    setBusy(true);
    setError("");
    try {
      const r = await fetch(ENDPOINT[action](tourId), { method: "PATCH" });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        setError(staffErrorMessage(r.status, body.detail, "Failed to retire the tour"));
        return;
      }
      onDone(action === "retire_source"
        ? `"${tourName}" retired — tenants with a copy were warned`
        : `"${tourName}" moved to trash — tenants with a copy were warned`);
    } finally {
      setBusy(false);
    }
  }

  let body: React.ReactNode;
  if (preview.isLoading) {
    body = <div style={{ color: K.muted }}>Counting what this affects…</div>;
  } else if (preview.error) {
    const e = preview.error;
    body = <div style={{ color: K.danger }}>
      {e instanceof ApiError ? staffErrorMessage(e.status, e.message, "Could not count the effects") : String(e)}
    </div>;
  } else if (p && !p.allowed) {
    body = <div style={{ color: K.danger }}>{p.refused}</div>;
  } else if (p) {
    const d = p.derivatives;
    body = (
      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        <div>
          <strong>{tourName}</strong> stops being served by AA everywhere: Master Content, A3 (atoms, ranking,
          routes), the tenant pool and the catalog that TripPlanner and Booking read. Tenants keep their own copies
          and are warned. You can restore it later.
        </div>
        <div>
          <Row label="Tenants warned" value={p.notifies_tenants ?? d.tenants} />
          <Row label="Their T2 copies (kept)" value={d.t2_copies} />
          <Row label="Slate subjects" value={d.subjects} />
          <Row label="Angle requests / pieces (kept)" value={`${d.angle_requests} / ${d.pieces}`} />
          <Row label="Live posts on tenant sites" value={d.live_posts}
               note="Stay online (the tenant's site); the warning tells the tenant." />
          <Row label="Queued tenant jobs cancelled" value={p.jobs_cancelled ?? p.queued_jobs} />
          <Row label="Pending reviews dismissed" value={p.reviews_dismissed ?? 0} />
          <Row label="Bookings" value="—" note={p.bookings_note} />
        </div>
      </div>
    );
  }

  return (
    <ConfirmModal
      open
      onClose={onClose}
      onConfirm={() => { if (!blocked) void confirm(); }}
      title={TITLE[action]}
      destructive
      busy={busy || blocked}
      confirmLabel={action === "retire_source" ? "Retire tour" : "Move to trash"}
      body={
        <>
          {body}
          {error && <div style={{ color: K.danger, marginTop: 10, fontSize: 13 }}>{error}</div>}
        </>
      }
    />
  );
}
