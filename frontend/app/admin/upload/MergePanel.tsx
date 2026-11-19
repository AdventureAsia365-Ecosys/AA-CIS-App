"use client";

// app/admin/upload/MergePanel.tsx — AA-1146 T1: the "Merge field by field" decision of a re-imported tour. Reads
// GET /api/admin/upload-staging/{staging_id}/merge-preview: fields nobody edited by hand take the sheet's value; each
// hand-edited field that differs is a choice (keep the hand edit, the default, or take the sheet's value); a reason is
// required. The parent sends {decision: "merge", resolutions, reason} with the other decisions.

import { useEffect, useState } from "react";
import { K, Badge, DataTable, Input, Select, useT, type ColumnDef } from "../../_kit";
import { D } from "../_components/tourDraft.messages";

interface MergeItem { field: string; current: unknown; incoming: unknown; hand_edited: boolean }
interface MergePreview { auto: MergeItem[]; conflicts: MergeItem[]; unchanged: string[] }
export interface MergeChoice { resolutions: Record<string, "incoming" | "current">; reason: string; ready: boolean }

function show(v: unknown): string {
  if (v == null) return "—";
  const s = Array.isArray(v) ? v.join(" · ") : typeof v === "object" ? JSON.stringify(v) : String(v);
  return s.length > 240 ? s.slice(0, 240) + "…" : s;
}

export default function MergePanel({ stagingId, onChange }: {
  stagingId: string;
  onChange: (choice: MergeChoice) => void;
}) {
  const t = useT(D);
  const [preview, setPreview] = useState<MergePreview | null>(null);
  const [error, setError] = useState("");
  const [choices, setChoices] = useState<Record<string, "incoming" | "current">>({});
  const [reason, setReason] = useState("");

  useEffect(() => {
    let alive = true;
    fetch(`/api/admin/upload-staging/${stagingId}/merge-preview`)
      .then(async r => {
        const body = await r.json().catch(() => ({}));
        if (!alive) return;
        if (!r.ok) {
          const d = body?.detail;
          setError(typeof d === "string" ? d : d?.message || t("loadFailed"));
          return;
        }
        setPreview(body);
        const initial: Record<string, "incoming" | "current"> = {};
        for (const c of body.conflicts || []) initial[c.field] = "current";
        setChoices(initial);
        onChange({ resolutions: initial, reason: "", ready: false });
      })
      .catch(() => { if (alive) setError(t("loadFailed")); });
    return () => { alive = false; };
    // the parent's callback is read once per load (it changes on every parent render)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stagingId, t]);

  // Tell the parent on each change, from the event (no effect: an effect on the callback would loop).
  function emit(nextChoices: Record<string, "incoming" | "current">, nextReason: string) {
    const r = nextReason.trim();
    onChange({ resolutions: nextChoices, reason: r, ready: !!preview && r.length >= 3 && r.length <= 500 });
  }

  if (error) return <div style={{ padding: "8px 16px", fontSize: 12, color: K.danger }}>{error}</div>;
  if (!preview) return <div style={{ padding: "8px 16px", fontSize: 12, color: K.muted }}>{t("mergeLoading")}</div>;

  const columns: ColumnDef<MergeItem, unknown>[] = [
    { id: "field", header: "", accessorFn: (c) => c.field,
      cell: ({ row: { original: c } }) => <Badge tone="warning">{c.field}</Badge> },
    { id: "incoming", header: t("incoming"), accessorFn: (c) => show(c.incoming) },
    { id: "current", header: t("current"), accessorFn: (c) => show(c.current) },
    { id: "choice", header: "", accessorFn: (c) => choices[c.field] || "current",
      cell: ({ row: { original: c } }) => (
        <Select value={choices[c.field] || "current"}
          onChange={e => {
            const next = { ...choices, [c.field]: e.target.value as "incoming" | "current" };
            setChoices(next);
            emit(next, reason);
          }}>
          <option value="current">{t("keepCurrent")}</option>
          <option value="incoming">{t("takeIncoming")}</option>
        </Select>
      ) },
  ];
  return (
    <div style={{ padding: "10px 16px", borderTop: `1px solid ${K.line}` }}>
      <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.12em", color: K.muted,
        marginBottom: 6 }}>{t("mergeTitle")}</div>
      {preview.auto.length + preview.conflicts.length === 0 && (
        <div style={{ fontSize: 12, color: K.muted }}>{t("mergeNone")}</div>
      )}
      {preview.auto.length > 0 && (
        <div style={{ fontSize: 12, color: K.muted, marginBottom: 6 }}>
          {t("mergeAuto", preview.auto.length)}: {preview.auto.map(a => a.field).join(", ")}
        </div>
      )}
      {preview.conflicts.length > 0 && (
        <>
          <div style={{ fontSize: 12, color: K.muted, marginBottom: 6 }}>{t("mergeConflicts", preview.conflicts.length)}</div>
          <DataTable<MergeItem>
            tableId="upload-merge-conflicts"
            data={preview.conflicts}
            columns={columns}
            getRowId={(c) => c.field}
            searchable={false}
            paginate={false}
            compact
          />
        </>
      )}
      <div style={{ marginTop: 8 }}>
        <div style={{ fontSize: 11, fontWeight: 600, color: K.muted, marginBottom: 4 }}>{t("mergeReason")}</div>
        <Input value={reason} onChange={e => { setReason(e.target.value); emit(choices, e.target.value); }}
          maxLength={500} style={{ width: "100%" }}
          placeholder={t("reasonHint")} />
      </div>
    </div>
  );
}
