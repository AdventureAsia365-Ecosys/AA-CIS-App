"use client";

// app/admin/_components/TourFactDraft.tsx — AA-1146 T1: the sheet draft of a tour (an Excel re-import merged field by
// field becomes a draft of the tour's facts, never a direct write). Shown on the Original tab of the tour drawer when
// one is waiting: per field the value on the tour now and the draft's value, the stale fields (the tour changed since
// the merge), Publish (POST /api/admin/tours/{id}/fact-draft/publish: the two-person rule, the price rule) and Discard.
// Nothing renders when the tour has no sheet draft or the API is not ready (503 before the database update).
// AA-1181: a sheet never changes a tour made by hand in the content workspace (``hand_made``): the note says so and
// Publish is off (the API refuses it too, 409 fact_draft_hand_made); Discard stays.

import { useCallback, useEffect, useState } from "react";
import {
  K, Badge, Button, DataTable, useAdminIdentity, useT, useToast, type ColumnDef, type Translator,
} from "../../_kit";
import { FACT_DRAFT_HAND_MADE, refusalText } from "@/lib/factDraftErrors";
import { can } from "@/lib/rbac";
import { D } from "./tourDraft.messages";

interface FactField { field: string; before: unknown; after: unknown; current: unknown; stale: boolean }
interface FactDraft { id: number; author: string; reason: string; stale: string[]; fields: FactField[] }
interface FactState { draft: FactDraft | null; two_person_publish: boolean; price_editable: boolean; hand_made?: boolean }

function show(v: unknown): string {
  if (v == null || v === "") return "—";
  const s = Array.isArray(v) ? v.join(" · ") : typeof v === "object" ? JSON.stringify(v) : String(v);
  return s.length > 240 ? s.slice(0, 240) + "…" : s;
}

const NOTE = { fontSize: 12, color: K.muted, margin: "0 0 8px" } as const;

function FactNotes({ state, draft, t }: { state: FactState; draft: FactDraft; t: Translator<typeof D.en> }) {
  const changesPrice = draft.fields.some(f => f.field === "price_raw");
  return (
    <>
      <p style={NOTE}>{t("factIntro", draft.author, draft.reason)}</p>
      {state.hand_made && <p style={{ ...NOTE, color: K.danger }}>{t("factHandMade")}</p>}
      {draft.stale.length > 0 && <p style={{ ...NOTE, color: K.danger }}>{t("factStale")}</p>}
      {state.two_person_publish && <p style={NOTE}>{t("twoPerson")}</p>}
      {changesPrice && !state.price_editable && <p style={NOTE}>{t("factPriceNeedsRole")}</p>}
    </>
  );
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

  async function act(method: "POST" | "DELETE", ok: string) {
    setBusy(true);
    try {
      const url = `/api/admin/tours/${tourId}/fact-draft${method === "POST" ? "/publish" : ""}`;
      const r = await fetch(url, { method });
      const body = await r.json().catch(() => ({}));
      if (r.ok) { toast.success(ok, 5000); reload(); }
      else toast.error(refusalText(body, t("failed"), { [FACT_DRAFT_HAND_MADE]: t("factHandMade") }), 6000);
    } finally { setBusy(false); }
  }

  const columns: ColumnDef<FactField, unknown>[] = [
    { id: "field", header: t("factField"), accessorFn: f => f.field,
      cell: ({ row: { original: f } }) => <Badge tone={f.stale ? "danger" : "warning"}>{f.field}</Badge> },
    { id: "current", header: t("factNow"), accessorFn: f => show(f.current) },
    { id: "after", header: t("factNew"), accessorFn: f => show(f.after) },
  ];
  const discard = () => { if (confirm(t("factDiscardConfirm"))) act("DELETE", t("factDiscarded")); };
  const publishOff = busy || draft.stale.length > 0 || state.hand_made === true;
  return (
    <div style={{ border: `1px solid ${K.line}`, borderRadius: 8, padding: "12px 16px", marginBottom: 16 }}>
      <div style={{ fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: K.muted,
        marginBottom: 6 }}>{t("factTitle")}</div>
      <FactNotes state={state} draft={draft} t={t} />
      <DataTable<FactField> tableId="tour-fact-draft" data={draft.fields} columns={columns} getRowId={f => f.field}
        searchable={false} paginate={false} compact />
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 10 }}>
        {can(role, "content.publish") || can(role, "review.act") ? (
          <Button variant="primary" disabled={publishOff} onClick={() => act("POST", t("factPublished"))}>
            {t("factPublish")}
          </Button>
        ) : null}
        {can(role, "content.write") ? (
          <Button variant="secondary" disabled={busy} onClick={() => discard()}>{t("factDiscard")}</Button>
        ) : null}
      </div>
    </div>
  );
}
