"use client";
// app/admin/content-quality/PieceLabel.tsx — AA-791. The drawer's "Label" section: the caller's
// human verdict on one T9 piece (AA-783 criteria: as-is / light / heavy / reject, defects per class,
// minutes to fix, reason) and every other admin's label on it, for agreement.
//   GET  /admin/content-quality/pieces/{id}/label
//   POST /admin/content-quality/pieces/{id}/label  — one row per piece and labeller (re-save overwrites)
// Admin role only (the page, the BFF write rule and the backend all check). The backend answers 503
// until migration 231 (acp_shared.content_piece_label) is applied — shown as a plain note.
// Labels are admin data: they never reach the tenant.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { A } from "../_components/adminUi";
import { ApiError, Badge, Button, apiGet, apiSend, useAdminIdentity, useToast } from "../../_kit";
import {
  DEFECT_NAMES, LABEL_HINTS, LABEL_NAMES, fmtTime,
  type DefectLevel, type PieceDetail, type PieceLabel as Label, type PieceLabelBody, type PieceLabelValue, type PieceLabels,
} from "./shared";

const LABELS: PieceLabelValue[] = ["as_is", "light", "heavy", "reject"];
const DEFECTS = ["fact", "voice", "format", "ai_phrasing"];
const LEVELS: DefectLevel[] = ["none", "light", "heavy"];
const EDITABLE = ["approved", "held"];
const TONE: Record<PieceLabelValue, "success" | "info" | "warning" | "danger"> = {
  as_is: "success", light: "info", heavy: "warning", reject: "danger",
};

export function LabelBadge({ label }: { label: PieceLabelValue }) {
  return <Badge tone={TONE[label]} dot={false}>{LABEL_NAMES[label]}</Badge>;
}

const fieldStyle: React.CSSProperties = {
  fontSize: 12, padding: "6px 8px", border: `1px solid ${A.line}`, borderRadius: 6, background: A.card, color: A.ink,
};

function draftFrom(mine: Label | null): PieceLabelBody {
  return {
    label: mine?.label ?? "as_is", defects: { ...(mine?.defects ?? {}) },
    minutes_to_fix: mine?.minutes_to_fix ?? null, reason: mine?.reason ?? null,
  };
}

function LabelForm({ piece, mine }: { piece: PieceDetail; mine: Label | null }) {
  const toast = useToast();
  const qc = useQueryClient();
  const [d, setD] = useState<PieceLabelBody>(() => draftFrom(mine));
  const save = useMutation({
    mutationFn: (body: PieceLabelBody) =>
      apiSend<PieceLabels>(`/api/admin/content-quality/pieces/${piece.piece_id}/label`, { body, admin: true }),
    onSuccess: (r) => {
      qc.setQueryData(["cq-piece-label", piece.piece_id], (old: PieceLabels | undefined) => ({ ...old, ...r }));
      qc.invalidateQueries({ queryKey: ["cq-pieces"] });
      toast.success(`Labelled ${LABEL_NAMES[r.mine?.label ?? d.label]}`);
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : String(e), 8000),
  });
  const setDefect = (k: string, v: DefectLevel | "") => setD((x) => {
    const defects = { ...x.defects };
    if (v) defects[k] = v; else delete defects[k];
    return { ...x, defects };
  });

  return (
    <div data-testid="cq-label-form" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <div role="radiogroup" aria-label="Label" style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        {LABELS.map((l) => {
          const on = d.label === l;
          return (
            <button key={l} type="button" role="radio" aria-checked={on} title={LABEL_HINTS[l]}
                    data-testid={`cq-label-${l}`} onClick={() => setD((x) => ({ ...x, label: l }))}
                    style={{ ...fieldStyle, cursor: "pointer", fontWeight: on ? 700 : 400,
                             borderColor: on ? A.accent : A.line, background: on ? A.accentSoft : A.card }}>
              {LABEL_NAMES[l]}
              <span style={{ display: "block", fontSize: 10.5, fontWeight: 400, color: A.muted }}>{LABEL_HINTS[l]}</span>
            </button>
          );
        })}
      </div>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        {DEFECTS.map((k) => (
          <label key={k} style={{ display: "flex", gap: 4, alignItems: "center", fontSize: 12, color: A.body }}>
            {DEFECT_NAMES[k]}
            <select value={d.defects[k] ?? ""} onChange={(e) => setDefect(k, e.target.value as DefectLevel | "")}
                    aria-label={`${DEFECT_NAMES[k]} defects`} style={fieldStyle}>
              <option value="">—</option>
              {LEVELS.map((v) => <option key={v} value={v}>{v}</option>)}
            </select>
          </label>
        ))}
        <label style={{ display: "flex", gap: 4, alignItems: "center", fontSize: 12, color: A.body }}>
          Minutes to fix
          <input type="number" min={0} max={600} value={d.minutes_to_fix ?? ""} aria-label="Minutes to fix"
                 onChange={(e) => setD((x) => ({ ...x, minutes_to_fix: e.target.value === "" ? null : Math.max(0, Math.min(600, Number(e.target.value))) }))}
                 style={{ ...fieldStyle, width: 72 }} />
        </label>
      </div>
      <textarea value={d.reason ?? ""} maxLength={4000} rows={2} aria-label="Reason"
                placeholder="Reason — the worst sentences, or why"
                onChange={(e) => setD((x) => ({ ...x, reason: e.target.value }))}
                style={{ ...fieldStyle, width: "100%", resize: "vertical", boxSizing: "border-box", fontFamily: "inherit" }} />
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <span data-testid="cq-label-save">
          <Button size="sm" variant="primary" disabled={save.isPending}
                  onClick={() => save.mutate({ ...d, reason: (d.reason ?? "").trim() || null })}>
            {save.isPending ? "Saving…" : mine ? "Update my label" : "Save label"}
          </Button>
        </span>
        {mine && <span style={{ fontSize: 11.5, color: A.muted }}>Saved {fmtTime(mine.labelled_at)}</span>}
      </div>
    </div>
  );
}

function LabelList({ labels }: { labels: Label[] }) {
  if (!labels.length) return <div style={{ fontSize: 12, color: A.muted }}>No one has labelled this piece yet.</div>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      {labels.map((l) => (
        <div key={l.labelled_by} style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", fontSize: 12, color: A.body }}>
          <LabelBadge label={l.label} />
          <span style={{ color: A.muted }}>{l.labelled_by.replace(/^admin:/, "")} · {fmtTime(l.labelled_at)}</span>
          {Object.entries(l.defects || {}).filter(([, v]) => v !== "none").map(([k, v]) => (
            <span key={k} style={{ color: A.muted }}>{DEFECT_NAMES[k] ?? k}: {v}</span>
          ))}
          {l.minutes_to_fix != null && <span style={{ color: A.muted }}>{l.minutes_to_fix} min</span>}
          {l.text_changed_since_label && <Badge tone="warning" dot={false}>text edited since</Badge>}
          {l.reason && <span style={{ flexBasis: "100%", color: A.body }}>{l.reason}</span>}
        </div>
      ))}
    </div>
  );
}

export function PieceLabelSection({ piece }: { piece: PieceDetail }) {
  const { isAdmin } = useAdminIdentity();
  const { data, error, isLoading } = useQuery({
    queryKey: ["cq-piece-label", piece.piece_id],
    queryFn: () => apiGet<PieceLabels>(`/api/admin/content-quality/pieces/${piece.piece_id}/label`, { admin: true }),
    enabled: isAdmin,
    retry: false,
  });
  if (!isAdmin) return <div style={{ fontSize: 12, color: A.muted }}>Only an admin can label pieces.</div>;
  if (error) {
    const unavailable = error instanceof ApiError && error.status === 503;
    return (
      <div data-testid="cq-label-unavailable" style={{ fontSize: 12, color: unavailable ? A.muted : A.red }}>
        {error instanceof Error ? error.message : String(error)}
      </div>
    );
  }
  if (isLoading || !data) return <div style={{ fontSize: 12, color: A.muted }}>Loading…</div>;
  const others = data.labels.filter((l) => l.labelled_by !== data.mine?.labelled_by);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {EDITABLE.includes(piece.status)
        ? <LabelForm key={data.mine?.labelled_at ?? "new"} piece={piece} mine={data.mine} />
        : <div style={{ fontSize: 12, color: A.muted }}>A {piece.status} piece has no final text to label.</div>}
      {data.mine?.text_changed_since_label && (
        <div style={{ fontSize: 12, background: A.amberSoft, padding: "6px 8px", borderRadius: 6, color: A.ink }}>
          The text was edited after your label — check it still holds, then save again.
        </div>
      )}
      <div>
        <div style={{ fontSize: 11.5, fontWeight: 600, marginBottom: 4, color: A.ink }}>Other labellers · {others.length}</div>
        <LabelList labels={others} />
      </div>
    </div>
  );
}
