"use client";

// app/admin/_components/TourFactDraft.tsx — AA-1146 T1: the sheet draft of a tour (an Excel re-import merged field by
// field becomes a draft of the tour's facts, never a direct write). Shown on the Original tab of the tour drawer when
// one is waiting: per field the value on the tour now and the draft's value, the stale fields (the tour changed since
// the merge), Publish (POST /api/admin/tours/{id}/fact-draft/publish: the two-person rule, the price rule) and Discard.
// Nothing renders when the tour has no sheet draft or the API is not ready (503 before the database update).

import { useCallback, useEffect, useState } from "react";
import { K, Badge, Button, DataTable, useAdminIdentity, useT, useToast, type ColumnDef } from "../../_kit";
import { can } from "@/lib/rbac";
import { D } from "./tourDraft.messages";

interface FactField { field: string; before: unknown; after: unknown; current: unknown; stale: boolean }
interface FactDraft { id: number; author: string; reason: string; stale: string[]; fields: FactField[] }
interface FactState { draft: FactDraft | null; two_person_publish: boolean; price_editable: boolean }

function show(v: unknown): string {
  if (v == null || v === "") return "—";
  const s = Array.isArray(v) ? v.join(" · ") : typeof v === "object" ? JSON.stringify(v) : String(v);
  return s.length > 240 ? s.slice(0, 240) + "…" : s;
}

function errText(body: unknown, fallback: string): string {
  const d = (body as { detail?: unknown } | null)?.detail;
  if (typeof d === "string") return d;
  const m = (d as { message?: unknown } | undefined)?.message;
  return typeof m === "string" ? m : fallback;
}

function useFactDraft(tourId: string) {
  const [state, setState] = useState<FactState | null>(null);
  const [round, setRound] = useState(0);
  const reload = useCallback(() => setRound(n => n + 1), []);
  useEffect(() => {
    let alive = true;
    fetch(`/api/admin/tours/${tourId}/fact-draft`)
      .then(async r => { if (alive) setState(r.ok ? await r.json() : null); })
      .catch(() => { if (alive) setState(null); });
    return () => { alive = false; };
  }, [tourId, round]);
  return { state, reload };
}

export default function TourFactDraft({ tourId }: { tourId: string }) {
  const t = useT(D);
  const toast = useToast();
  const { role } = useAdminIdentity();
  const { state, reload } = useFactDraft(tourId);
  const [busy, setBusy] = useState(false);
  const draft = state?.draft;
  if (!state || !draft) return null;
  const changesPrice = draft.fields.some(f => f.field === "price_raw");

  async function act(method: "POST" | "DELETE", ok: string) {
    setBusy(true);
    try {
      const url = `/api/admin/tours/${tourId}/fact-draft${method === "POST" ? "/publish" : ""}`;
      const r = await fetch(url, { method });
      const body = await r.json().catch(() => ({}));
      if (r.ok) { toast.success(ok, 5000); reload(); }
      else toast.error(errText(body, t("failed")), 6000);
    } finally { setBusy(false); }
  }

  const columns: ColumnDef<FactField, unknown>[] = [
    { id: "field", header: t("factField"), accessorFn: f => f.field,
      cell: ({ row: { original: f } }) => <Badge tone={f.stale ? "danger" : "warning"}>{f.field}</Badge> },
    { id: "current", header: t("factNow"), accessorFn: f => show(f.current) },
    { id: "after", header: t("factNew"), accessorFn: f => show(f.after) },
  ];
  const canPublish = can(role, "content.publish") || can(role, "review.act");
  const canDiscard = can(role, "content.write");
  return (
    <div style={{ border: `1px solid ${K.line}`, borderRadius: 8, padding: "12px 16px", marginBottom: 16 }}>
      <div style={{ fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: K.muted,
        marginBottom: 6 }}>{t("factTitle")}</div>
      <p style={{ fontSize: 12, color: K.muted, margin: "0 0 8px" }}>{t("factIntro", draft.author, draft.reason)}</p>
      {draft.stale.length > 0 && <p style={{ fontSize: 12, color: K.danger, margin: "0 0 8px" }}>{t("factStale")}</p>}
      {state.two_person_publish && <p style={{ fontSize: 12, color: K.muted, margin: "0 0 8px" }}>{t("twoPerson")}</p>}
      {changesPrice && !state.price_editable && (
        <p style={{ fontSize: 12, color: K.muted, margin: "0 0 8px" }}>{t("factPriceNeedsRole")}</p>
      )}
      <DataTable<FactField> tableId="tour-fact-draft" data={draft.fields} columns={columns} getRowId={f => f.field}
        searchable={false} paginate={false} compact />
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 10 }}>
        {canPublish && (
          <Button variant="primary" disabled={busy || draft.stale.length > 0} onClick={() => act("POST", t("factPublished"))}>
            {t("factPublish")}
          </Button>
        )}
        {canDiscard && (
          <Button variant="secondary" disabled={busy}
            onClick={() => { if (confirm(t("factDiscardConfirm"))) act("DELETE", t("factDiscarded")); }}>
            {t("factDiscard")}
          </Button>
        )}
      </div>
    </div>
  );
}
