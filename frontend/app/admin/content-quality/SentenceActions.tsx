"use client";
// app/admin/content-quality/SentenceActions.tsx — AA-769 (S234). The drawer's "Flagged sentences"
// list: one row per flagged sentence with Cut / Rewrite / Edit, and the "Admin edits" history.
//   Cut     — confirm, then POST …/sentences/apply mode=cut (no LLM).
//   Rewrite — POST …/sentences/suggest (one Haiku call, ~$0.001-0.006), preview Before / After
//             (editable), then Apply. Nothing is saved until Apply.
//   Edit    — the same preview, empty, no LLM call.
// Apply returns the refreshed piece, which replaces the drawer's cached copy. Admin role only, and
// only on approved / held pieces (the backend and the BFF enforce both again).
// AA-916 WM-07: each row carries a reason chip row (preselected from the API's `suggested_reason`); Cut and Apply
// send the chosen chip as `reason_code`, so the action stays one click. "Admin edits" shows each edit's stored
// reason and the "Use as example" / "Never use as example" toggles (PATCH …/signals/{signal_id}, WM-27 reads them).

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ReasonTag, WritingReasonChips } from "../_components/WritingReasonChips";
import { exemplarPath, preselect, toggledPref, type ExemplarPref, type ReasonCode } from "@/lib/writingReasons";
import {
  K, mono, AiNotice, ApiError, Badge, Button, ConfirmModal, Spinner, apiSend, useAdminIdentity, useT, useToast, Input, Textarea,
} from "../../_kit";
import {
  fmtTime, usd,
  type AdminEdit, type ApplyBody, type CheckFailure, type FlaggedSentence, type PieceDetail, type SuggestResponse,
  type TenantCopy,
} from "./shared";
import { D, EDIT_MODE } from "./drawer.messages";
import { STATUS } from "./messages";
import { useCqText, useLookup } from "./useCqText";
import { SeverityBadge, SourceNote } from "./Severity";
import { SEVERITY_TONE, markSeverity, worstOf } from "@/lib/gateSeverity";

const EDITABLE = ["approved", "held"];

export interface Editor {
  key: string;
  draft: string;
  note: string;
  suggestion: SuggestResponse | null; // null = manual edit, no model call
}

const problemText = (s: FlaggedSentence) => s.problems.map((p) => `${p.gate}: ${p.problem}`).join(" | ");

export function FlaggedSentences({ piece }: { piece: PieceDetail }) {
  const t = useT(D);
  const status = useLookup(STATUS);
  const { gateLabel } = useCqText();
  const toast = useToast();
  const qc = useQueryClient();
  const { isAdmin } = useAdminIdentity();
  const [cutting, setCutting] = useState<FlaggedSentence | null>(null);
  const [editor, setEditor] = useState<Editor | null>(null);
  // AA-916 — the chip per sentence the admin changed; untouched rows read the API's suggestion
  const [reasons, setReasons] = useState<Record<string, ReasonCode>>({});
  const reasonOf = (s: FlaggedSentence) => reasons[s.key] ?? preselect(s.suggested_reason);
  const base = `/api/admin/content-quality/pieces/${piece.piece_id}/sentences`;
  const canEdit = isAdmin && EDITABLE.includes(piece.status);
  const hasSource = !!piece.writer_input;
  // AA-802 — published to a channel or exported: the tenant holds a copy an edit does not reach
  const delivered = piece.published || piece.exported;

  const onFail = (e: unknown) => {
    toast.error(e instanceof Error ? e.message : String(e), 8000);
    if (e instanceof ApiError && e.status === 409) qc.invalidateQueries({ queryKey: ["cq-piece", piece.piece_id] });
  };

  const suggest = useMutation({
    mutationFn: ({ s, note }: { s: FlaggedSentence; note: string }) =>
      apiSend<SuggestResponse>(`${base}/suggest`, {
        body: { start: s.start, end: s.end, text: s.text, problem: problemText(s), note: note || undefined }, admin: true,
      }),
    onSuccess: (r, { s, note }) => setEditor({ key: s.key, draft: r.replacement, note, suggestion: r }),
    onError: onFail,
  });

  const apply = useMutation({
    mutationFn: (body: ApplyBody) =>
      apiSend<PieceDetail & { warnings?: CheckFailure[] }>(`${base}/apply`, { body, admin: true }),
    onSuccess: ({ warnings, ...detail }, body) => {
      qc.setQueryData(["cq-piece", piece.piece_id], detail);
      qc.invalidateQueries({ queryKey: ["cq-pieces"] });
      setCutting(null);
      setEditor(null);
      toast.success(body.mode === "cut" ? t("sentenceCut") : t("sentenceReplaced"));
      if (warnings && warnings.length > 0) {
        toast.info(t("savedWithWarnings", warnings.map((w) => `${gateLabel(w.gate)}: ${w.violations.join(" | ")}`).join("; ")), 8000);
      }
    },
    onError: onFail,
  });

  const busy = suggest.isPending || apply.isPending;

  const applyEditor = (s: FlaggedSentence, ed: Editor) => {
    const text = ed.draft.trim();
    const sug = ed.suggestion;
    apply.mutate({
      start: s.start, end: s.end, text: s.text, replacement: text,
      mode: sug && text === sug.replacement.trim() ? "rewrite" : "manual",
      gate: s.gates[0], problem: problemText(s).slice(0, 1000),
      suggestion: sug?.replacement, suggestion_model: sug?.model, suggestion_cost_usd: sug?.cost_usd,
      reason_code: reasonOf(s),
    });
  };

  if (piece.flagged_sentences.length === 0) {
    return <div style={{ fontSize: 12, color: K.muted }}>{t("noFlagged")}</div>;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {!canEdit && (
        <div style={{ fontSize: 11.5, color: K.muted }}>
          {isAdmin ? t("cannotEditStatus", status(piece.status)) : t("onlyAdminCut")}
        </div>
      )}
      {canEdit && delivered && (
        <div style={{ fontSize: 12, background: K.amberSoft, padding: "6px 8px", borderRadius: 6, color: K.ink }}>
          {piece.published ? t("deliveredPublished") : t("deliveredExported")}
        </div>
      )}
      {piece.flagged_sentences.map((s) => {
        const open = editor?.key === s.key ? editor : null;
        const loadingThis = suggest.isPending && suggest.variables?.s.key === s.key;
        return (
          <div key={s.key} id={`hl-${s.key}`} data-testid="cq-flag-row"
               style={{ border: `1px solid ${s.blocking ? K.dangerBorder : K.line}`, borderRadius: 8, padding: "8px 10px", background: K.card }}>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 6 }}>
              {/* AA-924 — a gate's chip is red only when one of ITS findings on this sentence blocks */}
              {s.gates.map((g) => (
                <Badge key={g} tone={SEVERITY_TONE[worstOf(s.problems.filter((p) => p.gate === g)) ?? "warn"]} dot={false}>{gateLabel(g)}</Badge>
              ))}
            </div>
            <ProblemList problems={s.problems} />
            <div style={{ fontSize: 12.5, lineHeight: 1.55, color: K.ink, background: K.bg, borderRadius: 6, padding: "6px 8px", wordBreak: "break-word" }}>{s.sentence}</div>
            {canEdit && (
              <div style={{ marginTop: 8 }}>
                <WritingReasonChips value={reasonOf(s)} suggested={preselect(s.suggested_reason)} disabled={busy}
                                    onChange={(code) => setReasons((r) => ({ ...r, [s.key]: code }))} />
              </div>
            )}
            {canEdit && !open && (
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 8, alignItems: "center" }}>
                <span data-testid="cq-cut"><Button size="sm" variant="danger" disabled={busy} onClick={() => setCutting(s)}>{t("cut")}</Button></span>
                <span data-testid="cq-rewrite">
                  <Button size="sm" variant="secondary" disabled={busy || !hasSource}
                          title={hasSource ? t("rewriteTitle") : t("noWriterInput")}
                          onClick={() => suggest.mutate({ s, note: "" })}>{t("rewrite")}</Button>
                </span>
                <Button size="sm" variant="ghost" disabled={busy} onClick={() => setEditor({ key: s.key, draft: s.sentence, note: "", suggestion: null })}>{t("edit")}</Button>
                {loadingThis && <Spinner size={14} />}
              </div>
            )}
            {canEdit && open && (
              <RewritePanel
                s={s} ed={open} busy={busy} regenerating={loadingThis}
                onChange={(patch) => setEditor({ ...open, ...patch })}
                onApply={() => applyEditor(s, open)}
                onRegenerate={hasSource ? () => suggest.mutate({ s, note: open.note }) : undefined}
                onDiscard={() => setEditor(null)}
              />
            )}
          </div>
        );
      })}
      <ConfirmModal
        open={!!cutting}
        onClose={() => !apply.isPending && setCutting(null)}
        onConfirm={() => cutting && apply.mutate({
          start: cutting.start, end: cutting.end, text: cutting.text, replacement: "", mode: "cut",
          gate: cutting.gates[0], problem: problemText(cutting).slice(0, 1000), reason_code: reasonOf(cutting),
        })}
        title={t("cutTitle")}
        destructive
        busy={apply.isPending}
        confirmLabel={t("cut")}
        body={
          <div style={{ display: "flex", flexDirection: "column", gap: 8, fontSize: 13 }}>
            <div style={{ background: K.dangerSoft, borderRadius: 6, padding: "6px 8px", color: K.ink }}>{cutting?.sentence}</div>
            {cutting && <div style={{ display: "flex", gap: 6, alignItems: "center", color: K.body }}>{t("reason")} <ReasonTag code={reasonOf(cutting)} /></div>}
            <div style={{ color: K.body }}>{t("cutBodyPiece")}</div>
            {delivered && <div style={{ color: K.amber }}>{piece.published ? t("cutPublished") : t("cutExported")}</div>}
          </div>
        }
      />
    </div>
  );
}

/** AA-924 — one line per problem of a flagged sentence: its own severity badge, the stored gate, the problem, and
 *  the legacy gate that found it inside a merged entry. Shared with the T2 drawer. */
export function ProblemList({ problems }: { problems: FlaggedSentence["problems"] }) {
  return (
    <div style={{ fontSize: 12, color: K.body, marginBottom: 6, display: "flex", flexDirection: "column", gap: 3 }}>
      {problems.map((p, i) => (
        <div key={i} data-testid="cq-problem" style={{ display: "flex", gap: 6, alignItems: "baseline", flexWrap: "wrap", minWidth: 0 }}>
          <SeverityBadge severity={markSeverity(p)} />
          <span style={{ fontFamily: mono, fontSize: 10.5, color: K.muted }}>{p.gate}</span>
          <span style={{ minWidth: 0, overflowWrap: "anywhere" }}>{p.problem}</span>
          <SourceNote source={p.source && p.source !== p.gate ? p.source : null} />
        </div>
      ))}
    </div>
  );
}

export function RewritePanel({ s, ed, busy, regenerating, onChange, onApply, onRegenerate, onDiscard }: {
  s: { sentence: string }; ed: Editor; busy: boolean; regenerating: boolean;
  onChange: (patch: Partial<Editor>) => void; onApply: () => void; onRegenerate?: () => void; onDiscard: () => void;
}) {
  const t = useT(D);
  const { gateLabel } = useCqText();
  const sug = ed.suggestion;
  const empty = !ed.draft.trim();
  return (
    <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 6 }}>
      <div style={{ fontSize: 11, fontWeight: 600, color: K.muted }}>{t("before")}</div>
      <div style={{ fontSize: 12.5, background: K.dangerSoft, color: K.ink, borderRadius: 6, padding: "6px 8px", textDecoration: "line-through", wordBreak: "break-word" }}>{s.sentence}</div>
      <div style={{ fontSize: 11, fontWeight: 600, color: K.muted }}>
        {t("after")}{sug ? t("suggestedBy", sug.model ?? t("theModel"), usd(sug.cost_usd)) : t("yourText")}
      </div>
      <Textarea
        aria-label={t("replacementAria")} value={ed.draft} rows={3} maxLength={3000}
        onChange={(e) => onChange({ draft: e.target.value })}
        style={{ width: "100%", boxSizing: "border-box", fontSize: 12.5, lineHeight: 1.5, background: K.successSoft, color: K.ink, border: `1px solid ${K.line}`, borderRadius: 6, padding: "6px 8px", fontFamily: "inherit", resize: "vertical" }}
      />
      {/* AA-1033: the replacement is a model's suggestion the admin applies to the tenant's copy (T2 and T9 drawers). */}
      {sug && <AiNotice testId="cq-rewrite-ai-notice" />}
      {sug && !sug.replacement && (
        <div style={{ fontSize: 12, color: K.body }}>{t("nothingLeft")}</div>
      )}
      {sug && sug.checks.length > 0 && ed.draft.trim() === sug.replacement.trim() && (
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          {sug.checks.map((c, i) => (
            <div key={`${c.gate}-${i}`} style={{ fontSize: 12, color: K.ink }}>
              <Badge tone={c.blocking ? "danger" : "warning"} dot={false}>{gateLabel(c.gate)}</Badge> {c.violations.join(" | ")}
            </div>
          ))}
          <div style={{ fontSize: 11.5, color: K.muted }}>
            {sug.checks.some((c) => c.blocking)
              ? t("wouldRefuse")
              : t("warningsOnly")}
          </div>
        </div>
      )}
      {onRegenerate && (
        <Input
          aria-label={t("noteAria")} placeholder={t("notePlaceholder")}
          value={ed.note} maxLength={200} onChange={(e) => onChange({ note: e.target.value })}
          style={{ width: "100%", boxSizing: "border-box", fontSize: 12, background: K.card, color: K.ink, border: `1px solid ${K.line}`, borderRadius: 6, padding: "5px 8px" }}
        />
      )}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center" }}>
        <Button size="sm" variant="primary" disabled={busy || empty} title={empty ? t("emptyUseCut") : undefined} onClick={onApply}>{t("apply")}</Button>
        {onRegenerate && <Button size="sm" variant="secondary" disabled={busy} onClick={onRegenerate}>{sug ? t("regenerate") : t("suggest")}</Button>}
        <Button size="sm" variant="ghost" disabled={busy} onClick={onDiscard}>{t("discard")}</Button>
        {(regenerating || busy) && <Spinner size={14} />}
      </div>
    </div>
  );
}

/** Which object the edits belong to: the exemplar PATCH goes through it (the backend checks the signal is its). */
export interface EditOwner { kind: "pieces" | "tour-versions"; id: string; queryKey: readonly unknown[] }

export function AdminEdits({ edits, owner }: { edits: AdminEdit[]; owner?: EditOwner }) {
  const t = useT(D);
  const mode = useLookup(EDIT_MODE);
  const { sectionLabel } = useCqText();
  const { isAdmin } = useAdminIdentity();
  if (edits.length === 0) return <div style={{ fontSize: 12, color: K.muted }}>{t("noAdminEdits")}</div>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {edits.map((e, i) => (
        <div key={`${e.created_at}-${i}`} data-testid="cq-admin-edit" style={{ fontSize: 12, borderBottom: `1px solid ${K.line}`, paddingBottom: 6 }}>
          <div style={{ color: K.muted, display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center" }}>
            <span>
              {fmtTime(e.created_at)} · {e.actor} · {e.details.mode ? mode(e.details.mode) : e.action}{e.details.gate ? ` · ${e.details.gate}` : ""}
              {e.details.field ? ` · ${sectionLabel(e.details.field, e.details.index ?? null)}` : ""}
            </span>
            <ReasonTag code={e.details.reason_code} />
          </div>
          <div style={{ textDecoration: "line-through", color: K.body, wordBreak: "break-word" }}>{e.details.before}</div>
          {e.details.after ? <div style={{ color: K.ink, wordBreak: "break-word" }}>{e.details.after}</div> : <div style={{ color: K.muted }}>{t("removed")}</div>}
          {owner && e.details.signal_id && (
            <ExemplarToggles owner={owner} signalId={e.details.signal_id} value={e.details.exemplar_pref ?? null} canEdit={isAdmin} />
          )}
        </div>
      ))}
    </div>
  );
}

/** AA-916 WM-07 — the example preference of one admin edit (its writing signal). A click on the active toggle
 *  clears it. The drawer query is refetched after the save, so the value shown is the stored one. */
function ExemplarToggles({ owner, signalId, value, canEdit }: {
  owner: EditOwner; signalId: string; value: ExemplarPref; canEdit: boolean;
}) {
  const t = useT(D);
  const exemplar = (p: "use" | "never") => (p === "use" ? t("useAsExample") : t("neverAsExample"));
  const toast = useToast();
  const qc = useQueryClient();
  const save = useMutation({
    mutationFn: (pref: ExemplarPref) =>
      apiSend<{ signal_id: string; exemplar_pref: ExemplarPref }>(exemplarPath(owner.kind, owner.id, signalId), {
        method: "PATCH", body: { exemplar_pref: pref }, admin: true,
      }),
    onSuccess: (r) => {
      toast.success(r.exemplar_pref ? t("savedPref", exemplar(r.exemplar_pref).toLowerCase()) : t("prefCleared"));
      qc.invalidateQueries({ queryKey: owner.queryKey });
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : String(e), 8000),
  });
  if (!canEdit) {
    return value ? <div style={{ marginTop: 4, fontSize: 11.5, color: K.muted }}>{exemplar(value)}</div> : null;
  }
  return (
    <div data-testid="cq-exemplar" data-value={value ?? ""} style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 6, alignItems: "center" }}>
      {(["use", "never"] as const).map((pref) => (
        <Button key={pref} size="sm" variant={value === pref ? "primary" : "ghost"} disabled={save.isPending}
                title={value === pref ? t("clickToClear") : pref === "use" ? t("useTitle") : t("neverTitle")}
                onClick={() => save.mutate(toggledPref(value, pref))}>
          {exemplar(pref)}
        </Button>
      ))}
      {save.isPending && <Spinner size={14} />}
    </div>
  );
}

/** AA-802 — the tenant took a copy away (published / exported) BEFORE an admin edit: that copy still
 *  has the old text and nothing re-syncs it. Shown at the top of the T9 and T2 drawers. */
export function TenantCopyNotice({ copy, what }: { copy?: TenantCopy | null; what: "piece" | "version" }) {
  const t = useT(D);
  // optional: the admin UI can ship before the API that returns `tenant_copy` (Vercel vs ECS deploy)
  if (!copy?.older_copy) return null;
  const how = copy.published_at && copy.delivered_at === copy.published_at ? t("howPublished") : t("howDownloaded");
  const rest = t("olderCopyBody", how, fmtTime(copy.delivered_at), copy.edits_after_delivery,
    what === "piece" ? t("whatPiece") : t("whatVersion"), copy.published_at ? t("untilRepublish") : t("untilExport"));
  return (
    <div data-testid="cq-older-copy" style={{ fontSize: 12.5, background: K.amberSoft, borderRadius: 8, padding: "8px 10px", color: K.ink, marginBottom: 10 }}>
      <b>{t("olderCopy")}</b>{rest}
    </div>
  );
}
