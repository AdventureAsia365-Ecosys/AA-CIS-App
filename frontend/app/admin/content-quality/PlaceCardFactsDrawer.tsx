"use client";
// app/admin/content-quality/PlaceCardFactsDrawer.tsx — AA-774 (S235). Correct one place card's facts:
// per fact a kind select + text, or Drop; one Save with a required reason →
// PATCH /admin/content-quality/place-cards/{id}/facts (admin role only, audited with before/after).
// A fact whose kind is visiting / practical stays on the card but is not shown to the T9 writer.
// Each edit sends the text the admin loaded (expected_text): a card changed meanwhile answers 409.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { A } from "../_components/adminUi";
import { ApiError, Badge, Button, Drawer, apiGet, apiSend, useAdminIdentity, useToast } from "../../_kit";
import type { FactEdit, PlaceCardDetail, PlaceCardFactRow } from "./shared";

const fieldStyle: React.CSSProperties = {
  fontSize: 12.5, padding: "6px 8px", border: `1px solid ${A.line}`, borderRadius: 6, background: A.card,
  color: A.ink3, width: "100%", boxSizing: "border-box", fontFamily: "inherit",
};

interface Draft { kind: string; text: string; drop: boolean }

function editsFrom(rows: PlaceCardFactRow[], drafts: Record<number, Draft>): FactEdit[] {
  const out: FactEdit[] = [];
  for (const r of rows) {
    const d = drafts[r.index];
    if (!d) continue;
    if (d.drop) { out.push({ index: r.index, drop: true, expected_text: r.text }); continue; }
    const e: FactEdit = { index: r.index, expected_text: r.text };
    if (d.kind !== r.kind) e.kind = d.kind;
    if (d.text.trim() !== r.text) e.text = d.text.trim();
    if (e.kind !== undefined || e.text !== undefined) out.push(e);
  }
  return out;
}

function FactsForm({ card, onSaved }: { card: PlaceCardDetail; onSaved: (c: PlaceCardDetail) => void }) {
  const toast = useToast();
  const qc = useQueryClient();
  const { isAdmin } = useAdminIdentity();
  const [drafts, setDrafts] = useState<Record<number, Draft>>({});
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const draftOf = (r: PlaceCardFactRow): Draft => drafts[r.index] ?? { kind: r.kind, text: r.text, drop: false };
  const set = (r: PlaceCardFactRow, patch: Partial<Draft>) =>
    setDrafts((d) => ({ ...d, [r.index]: { ...draftOf(r), ...patch } }));
  const edits = editsFrom(card.fact_rows, drafts);
  const emptyText = card.fact_rows.some((r) => { const d = draftOf(r); return !d.drop && !d.text.trim(); });
  const save = useMutation({
    mutationFn: () => apiSend<PlaceCardDetail>(`/api/admin/content-quality/place-cards/${card.card_id}/facts`,
      { method: "PATCH", body: { edits, reason: reason.trim() }, admin: true }),
    onSuccess: (c) => {
      toast.success(`Saved ${edits.length} fact change(s)`);
      setDrafts({}); setReason(""); setError(null);
      onSaved(c);
    },
    onError: (e) => {
      setError(e instanceof Error ? e.message : String(e));
      if (e instanceof ApiError && e.status === 409) qc.invalidateQueries({ queryKey: ["cq-card", card.card_id] });
    },
  });
  const canSave = isAdmin && edits.length > 0 && !emptyText && reason.trim().length >= 3 && !save.isPending;

  if (!card.fact_rows.length) return <div style={{ fontSize: 13, color: A.muted }}>This card has no facts.</div>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{ fontSize: 12, color: A.muted }}>
        Only what_it_is, history, setting and naming facts reach the T9 writer. Re-kind an access, permit or
        duration line to visiting / practical, fix its wording, or drop it.
      </div>
      {card.fact_rows.map((r) => {
        const d = draftOf(r);
        const changed = edits.some((e) => e.index === r.index);
        return (
          <div key={r.index} style={{ border: `1px solid ${changed ? A.accent : A.line}`, borderRadius: 8, padding: 10,
                                      display: "flex", flexDirection: "column", gap: 6, opacity: d.drop ? 0.55 : 1 }}>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <span style={{ fontSize: 11.5, color: A.muted }}>#{r.index + 1}</span>
              <select value={d.kind} disabled={d.drop || !isAdmin} onChange={(e) => set(r, { kind: e.target.value })}
                      aria-label={`Kind of fact ${r.index + 1}`} style={{ ...fieldStyle, width: "auto" }}>
                {card.fact_kinds.map((k) => <option key={k} value={k}>{k}</option>)}
              </select>
              <Badge tone={r.in_writer_seed ? "success" : "neutral"} dot={false}>
                {r.in_writer_seed ? "in writer seed" : "admin only"}
              </Badge>
              <span style={{ flex: 1 }} />
              <Button size="sm" variant={d.drop ? "secondary" : "danger"} disabled={!isAdmin}
                      onClick={() => set(r, { drop: !d.drop })}>{d.drop ? "Keep" : "Drop"}</Button>
            </div>
            <textarea value={d.text} disabled={d.drop || !isAdmin} rows={Math.min(8, Math.max(3, Math.ceil(d.text.length / 60)))} maxLength={600}
                      onChange={(e) => set(r, { text: e.target.value })} aria-label={`Text of fact ${r.index + 1}`}
                      style={{ ...fieldStyle, resize: "vertical", textDecoration: d.drop ? "line-through" : "none" }} />
          </div>
        );
      })}
      <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 12, color: A.ink3 }}>
        Reason (required, kept in the audit log)
        <input value={reason} onChange={(e) => setReason(e.target.value)} maxLength={500} disabled={!isAdmin}
               placeholder="e.g. access line is a visiting fact, not setting" style={fieldStyle} />
      </label>
      {emptyText && <div role="alert" style={{ fontSize: 12, color: A.red }}>A fact cannot be empty — drop it instead.</div>}
      {error && <div role="alert" style={{ fontSize: 12, color: A.red }}>Could not save: {error}</div>}
      {!isAdmin && <div style={{ fontSize: 12, color: A.muted }}>Only an admin can edit facts.</div>}
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <Button variant="primary" size="sm" disabled={!canSave} onClick={() => { setError(null); save.mutate(); }}>
          {save.isPending ? "Saving…" : edits.length ? `Save ${edits.length} change${edits.length > 1 ? "s" : ""}` : "Save"}
        </Button>
        <Button size="sm" variant="ghost" disabled={!edits.length || save.isPending}
                onClick={() => { setDrafts({}); setError(null); }}>Reset</Button>
      </div>
    </div>
  );
}

export default function PlaceCardFactsDrawer({ cardId, onClose }: { cardId: string; onClose: () => void }) {
  const qc = useQueryClient();
  const key = ["cq-card", cardId];
  const { data, isLoading, error } = useQuery({
    queryKey: key,
    queryFn: () => apiGet<PlaceCardDetail>(`/api/admin/content-quality/place-cards/${cardId}`, { admin: true }),
  });
  const title = data ? `${data.display_name} — ${[data.locality, data.region, data.country].filter(Boolean).join(", ")}` : "Place card";
  return (
    <Drawer open onClose={onClose} title={title} width={640}>
      {isLoading && <div style={{ fontSize: 13, color: A.muted }}>Loading…</div>}
      {error && <div role="alert" style={{ fontSize: 13, color: A.red }}>Could not load the card: {String(error)}</div>}
      {data && (
        <FactsForm key={data.updated_at} card={data} onSaved={(c) => {
          qc.setQueryData(key, c);
          qc.invalidateQueries({ queryKey: ["cq-cards"] });
        }} />
      )}
    </Drawer>
  );
}
