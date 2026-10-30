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
import { K, ApiError, Badge, Button, apiGet, apiSend, useAdminIdentity, useT, useToast } from "../../_kit";
import {
  fmtTime,
  type DefectLevel, type PieceDetail, type PieceLabel as Label, type PieceLabelBody, type PieceLabelValue, type PieceLabels,
} from "./shared";
import { D } from "./drawer.messages";
import { DEFECT, DEFECT_LEVEL, LABEL_HINT, LABEL_NAME, STATUS } from "./messages";
import { useLookup } from "./useCqText";

const LABELS: PieceLabelValue[] = ["as_is", "light", "heavy", "reject"];
const DEFECTS = ["fact", "voice", "format", "ai_phrasing"] as const;
const LEVELS: DefectLevel[] = ["none", "light", "heavy"];
const EDITABLE = ["approved", "held"];
const TONE: Record<PieceLabelValue, "success" | "info" | "warning" | "danger"> = {
  as_is: "success", light: "info", heavy: "warning", reject: "danger",
};

export function LabelBadge({ label }: { label: PieceLabelValue }) {
  const name = useT(LABEL_NAME);
  return <Badge tone={TONE[label]} dot={false}>{name(label)}</Badge>;
}

const fieldStyle: React.CSSProperties = {
  fontSize: 12, padding: "6px 8px", border: `1px solid ${K.line}`, borderRadius: 6, background: K.card, color: K.ink,
};

// The form's working copy. `label` is null until the admin picks a verdict: an unlabelled piece
// starts with no verdict selected, so it cannot be saved as "As-is" by a stray click (S239).
type LabelDraft = Omit<PieceLabelBody, "label"> & { label: PieceLabelValue | null };

function draftFrom(mine: Label | null): LabelDraft {
  return {
    label: mine?.label ?? null, defects: { ...(mine?.defects ?? {}) },
    minutes_to_fix: mine?.minutes_to_fix ?? null, reason: mine?.reason ?? null,
  };
}

// The POST body, or null while no verdict is chosen (Save stays disabled).
function labelBodyFrom(d: LabelDraft): PieceLabelBody | null {
  if (!d.label) return null;
  return { ...d, label: d.label, reason: (d.reason ?? "").trim() || null };
}

function LabelForm({ piece, mine }: { piece: PieceDetail; mine: Label | null }) {
  const t = useT(D);
  const labelName = useT(LABEL_NAME);
  const labelHint = useT(LABEL_HINT);
  const defectName = useT(DEFECT);
  const level = useT(DEFECT_LEVEL);
  const toast = useToast();
  const qc = useQueryClient();
  const [d, setD] = useState<LabelDraft>(() => draftFrom(mine));
  const body = labelBodyFrom(d);
  const save = useMutation({
    mutationFn: (body: PieceLabelBody) =>
      apiSend<PieceLabels>(`/api/admin/content-quality/pieces/${piece.piece_id}/label`, { body, admin: true }),
    onSuccess: (r) => {
      qc.setQueryData(["cq-piece-label", piece.piece_id], (old: PieceLabels | undefined) => ({ ...old, ...r }));
      qc.invalidateQueries({ queryKey: ["cq-pieces"] });
      const saved = r.mine?.label ?? d.label;
      toast.success(saved ? t("labelledAs", labelName(saved)) : t("labelSaved"));
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
      <div role="radiogroup" aria-label={t("labelSection")} style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        {LABELS.map((l) => {
          const on = d.label === l;
          return (
            <button key={l} type="button" role="radio" aria-checked={on} title={labelHint(l)}
                    data-testid={`cq-label-${l}`} onClick={() => setD((x) => ({ ...x, label: l }))}
                    style={{ ...fieldStyle, cursor: "pointer", fontWeight: on ? 700 : 400,
                             borderColor: on ? K.accent : K.line, background: on ? K.accentSoft : K.card }}>
              {labelName(l)}
              <span style={{ display: "block", fontSize: 10.5, fontWeight: 400, color: K.muted }}>{labelHint(l)}</span>
            </button>
          );
        })}
      </div>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        {DEFECTS.map((k) => (
          <label key={k} style={{ display: "flex", gap: 4, alignItems: "center", fontSize: 12, color: K.body }}>
            {defectName(k)}
            <select value={d.defects[k] ?? ""} onChange={(e) => setDefect(k, e.target.value as DefectLevel | "")}
                    aria-label={t("defectsAria", defectName(k))} style={fieldStyle}>
              <option value="">—</option>
              {LEVELS.map((v) => <option key={v} value={v}>{level(v)}</option>)}
            </select>
          </label>
        ))}
        <label style={{ display: "flex", gap: 4, alignItems: "center", fontSize: 12, color: K.body }}>
          {t("minutesToFix")}
          <input type="number" min={0} max={600} value={d.minutes_to_fix ?? ""} aria-label={t("minutesToFix")}
                 onChange={(e) => setD((x) => ({ ...x, minutes_to_fix: e.target.value === "" ? null : Math.max(0, Math.min(600, Math.round(Number(e.target.value)))) }))}
                 style={{ ...fieldStyle, width: 72 }} />
        </label>
      </div>
      <textarea value={d.reason ?? ""} maxLength={4000} rows={2} aria-label={t("reasonAria")}
                placeholder={t("reasonPlaceholder")}
                onChange={(e) => setD((x) => ({ ...x, reason: e.target.value }))}
                style={{ ...fieldStyle, width: "100%", resize: "vertical", boxSizing: "border-box", fontFamily: "inherit" }} />
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <span data-testid="cq-label-save">
          <Button size="sm" variant="primary" disabled={save.isPending || !body}
                  onClick={() => { if (body) save.mutate(body); }}>
            {save.isPending ? t("saving") : mine ? t("updateLabel") : t("saveLabel")}
          </Button>
        </span>
        {!body && <span data-testid="cq-label-pick-hint" style={{ fontSize: 11.5, color: K.muted }}>{t("pickVerdict")}</span>}
        {mine && <span style={{ fontSize: 11.5, color: K.muted }}>{t("savedAt", fmtTime(mine.labelled_at))}</span>}
      </div>
    </div>
  );
}

function LabelList({ labels }: { labels: Label[] }) {
  const t = useT(D);
  const defectName = useLookup(DEFECT);
  const level = useLookup(DEFECT_LEVEL);
  if (!labels.length) return <div style={{ fontSize: 12, color: K.muted }}>{t("noLabels")}</div>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      {labels.map((l) => (
        <div key={l.labelled_by} style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", fontSize: 12, color: K.body }}>
          <LabelBadge label={l.label} />
          <span style={{ color: K.muted }}>{l.labelled_by.replace(/^admin:/, "")} · {fmtTime(l.labelled_at)}</span>
          {Object.entries(l.defects || {}).filter(([, v]) => v !== "none").map(([k, v]) => (
            <span key={k} style={{ color: K.muted }}>{defectName(k)}: {level(v)}</span>
          ))}
          {l.minutes_to_fix != null && <span style={{ color: K.muted }}>{t("minutes", l.minutes_to_fix)}</span>}
          {l.text_changed_since_label && <Badge tone="warning" dot={false}>{t("editedSince")}</Badge>}
          {l.reason && <span style={{ flexBasis: "100%", color: K.body }}>{l.reason}</span>}
        </div>
      ))}
    </div>
  );
}

export function PieceLabelSection({ piece }: { piece: PieceDetail }) {
  const t = useT(D);
  const status = useLookup(STATUS);
  const { isAdmin } = useAdminIdentity();
  const { data, error, isLoading } = useQuery({
    queryKey: ["cq-piece-label", piece.piece_id],
    queryFn: () => apiGet<PieceLabels>(`/api/admin/content-quality/pieces/${piece.piece_id}/label`, { admin: true }),
    enabled: isAdmin,
    retry: false,
  });
  if (!isAdmin) return <div style={{ fontSize: 12, color: K.muted }}>{t("onlyAdminLabel")}</div>;
  if (error) {
    const unavailable = error instanceof ApiError && error.status === 503;
    return (
      <div data-testid="cq-label-unavailable" style={{ fontSize: 12, color: unavailable ? K.muted : K.danger }}>
        {error instanceof Error ? error.message : String(error)}
      </div>
    );
  }
  if (isLoading || !data) return <div style={{ fontSize: 12, color: K.muted }}>{t("loading")}</div>;
  const others = data.labels.filter((l) => l.labelled_by !== data.mine?.labelled_by);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {EDITABLE.includes(piece.status)
        ? <LabelForm key={data.mine?.labelled_at ?? "new"} piece={piece} mine={data.mine} />
        : <div style={{ fontSize: 12, color: K.muted }}>{t("noFinalText", status(piece.status))}</div>}
      {data.mine?.text_changed_since_label && (
        <div style={{ fontSize: 12, background: K.amberSoft, padding: "6px 8px", borderRadius: 6, color: K.ink }}>
          {t("editedAfterLabel")}
        </div>
      )}
      <div>
        <div style={{ fontSize: 11.5, fontWeight: 600, marginBottom: 4, color: K.ink }}>{t("otherLabellers", others.length)}</div>
        <LabelList labels={others} />
      </div>
    </div>
  );
}
