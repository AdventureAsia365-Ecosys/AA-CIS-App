"use client";
// app/admin/content-quality/SentenceActions.tsx — AA-769 (S234). The drawer's "Flagged sentences"
// list: one row per flagged sentence with Cut / Rewrite / Edit, and the "Admin edits" history.
//   Cut     — confirm, then POST …/sentences/apply mode=cut (no LLM).
//   Rewrite — POST …/sentences/suggest (one Haiku call, ~$0.001-0.006), preview Before / After
//             (editable), then Apply. Nothing is saved until Apply.
//   Edit    — the same preview, empty, no LLM call.
// Apply returns the refreshed piece, which replaces the drawer's cached copy. Admin role only, and
// only on approved / held pieces (the backend and the BFF enforce both again).

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { A, mono } from "../_components/adminUi";
import { ApiError, Badge, Button, ConfirmModal, Spinner, apiSend, useAdminIdentity, useToast } from "../../_kit";
import {
  fmtTime, gateLabel, tourSectionLabel, usd,
  type AdminEdit, type ApplyBody, type CheckFailure, type FlaggedSentence, type PieceDetail, type SuggestResponse,
  type TenantCopy,
} from "./shared";

const EDITABLE = ["approved", "held"];

export interface Editor {
  key: string;
  draft: string;
  note: string;
  suggestion: SuggestResponse | null; // null = manual edit, no model call
}

const problemText = (s: FlaggedSentence) => s.problems.map((p) => `${p.gate}: ${p.problem}`).join(" | ");

export function FlaggedSentences({ piece }: { piece: PieceDetail }) {
  const toast = useToast();
  const qc = useQueryClient();
  const { isAdmin } = useAdminIdentity();
  const [cutting, setCutting] = useState<FlaggedSentence | null>(null);
  const [editor, setEditor] = useState<Editor | null>(null);
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
      toast.success(body.mode === "cut" ? "Sentence cut" : "Sentence replaced");
      if (warnings && warnings.length > 0) {
        toast.info(`Saved with warnings — ${warnings.map((w) => `${gateLabel(w.gate)}: ${w.violations.join(" | ")}`).join("; ")}`, 8000);
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
    });
  };

  if (piece.flagged_sentences.length === 0) {
    return <div style={{ fontSize: 12, color: A.muted }}>No flagged sentence found in the final text.</div>;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {!canEdit && (
        <div style={{ fontSize: 11.5, color: A.muted }}>
          {isAdmin ? `A ${piece.status} piece cannot be edited.` : "Only an admin can cut or rewrite sentences."}
        </div>
      )}
      {canEdit && delivered && (
        <div style={{ fontSize: 12, background: A.amberSoft, padding: "6px 8px", borderRadius: 6, color: A.ink }}>
          The tenant already {piece.published ? "published" : "exported"} this piece. An edit changes our copy only —
          the {piece.published ? "published post (WordPress is not re-synced)" : "downloaded file"} keeps the old text.
        </div>
      )}
      {piece.flagged_sentences.map((s) => {
        const open = editor?.key === s.key ? editor : null;
        const loadingThis = suggest.isPending && suggest.variables?.s.key === s.key;
        return (
          <div key={s.key} id={`hl-${s.key}`} data-testid="cq-flag-row"
               style={{ border: `1px solid ${s.blocking ? A.redBorder : A.line}`, borderRadius: 8, padding: "8px 10px", background: A.card }}>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 6 }}>
              {s.gates.map((g) => (
                <Badge key={g} tone={s.problems.some((p) => p.gate === g && p.blocking) ? "danger" : "warning"} dot={false}>{gateLabel(g)}</Badge>
              ))}
            </div>
            <div style={{ fontSize: 12, color: A.body, marginBottom: 6 }}>
              {s.problems.map((p, i) => <div key={i}><span style={{ fontFamily: mono, fontSize: 10.5, color: A.muted }}>{p.gate}</span> {p.problem}</div>)}
            </div>
            <div style={{ fontSize: 12.5, lineHeight: 1.55, color: A.ink, background: A.bg, borderRadius: 6, padding: "6px 8px", wordBreak: "break-word" }}>{s.sentence}</div>
            {canEdit && !open && (
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 8, alignItems: "center" }}>
                <span data-testid="cq-cut"><Button size="sm" variant="danger" disabled={busy} onClick={() => setCutting(s)}>Cut</Button></span>
                <span data-testid="cq-rewrite">
                  <Button size="sm" variant="secondary" disabled={busy || !hasSource}
                          title={hasSource ? "Ask the model for a rewrite (about $0.001-0.006)" : "No writer input stored for this piece — use Edit"}
                          onClick={() => suggest.mutate({ s, note: "" })}>Rewrite</Button>
                </span>
                <Button size="sm" variant="ghost" disabled={busy} onClick={() => setEditor({ key: s.key, draft: s.sentence, note: "", suggestion: null })}>Edit</Button>
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
          gate: cutting.gates[0], problem: problemText(cutting).slice(0, 1000),
        })}
        title="Cut this sentence?"
        destructive
        busy={apply.isPending}
        confirmLabel="Cut"
        body={
          <div style={{ display: "flex", flexDirection: "column", gap: 8, fontSize: 13 }}>
            <div style={{ background: A.redSoft, borderRadius: 6, padding: "6px 8px", color: A.ink }}>{cutting?.sentence}</div>
            <div style={{ color: A.body }}>The tenant&rsquo;s copy of this piece changes. The edit is audit-logged with the removed text.</div>
            {delivered && <div style={{ color: A.amber }}>The tenant already {piece.published ? "published" : "exported"} this piece; their copy is not updated.</div>}
          </div>
        }
      />
    </div>
  );
}

export function RewritePanel({ s, ed, busy, regenerating, onChange, onApply, onRegenerate, onDiscard }: {
  s: { sentence: string }; ed: Editor; busy: boolean; regenerating: boolean;
  onChange: (patch: Partial<Editor>) => void; onApply: () => void; onRegenerate?: () => void; onDiscard: () => void;
}) {
  const sug = ed.suggestion;
  const empty = !ed.draft.trim();
  return (
    <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 6 }}>
      <div style={{ fontSize: 11, fontWeight: 600, color: A.muted }}>BEFORE</div>
      <div style={{ fontSize: 12.5, background: A.redSoft, color: A.ink, borderRadius: 6, padding: "6px 8px", textDecoration: "line-through", wordBreak: "break-word" }}>{s.sentence}</div>
      <div style={{ fontSize: 11, fontWeight: 600, color: A.muted }}>
        AFTER {sug ? `· suggested by ${sug.model ?? "the model"} · ${usd(sug.cost_usd)}` : "· your text"}
      </div>
      <textarea
        aria-label="Replacement sentence" value={ed.draft} rows={3} maxLength={3000}
        onChange={(e) => onChange({ draft: e.target.value })}
        style={{ width: "100%", boxSizing: "border-box", fontSize: 12.5, lineHeight: 1.5, background: A.greenSoft, color: A.ink, border: `1px solid ${A.line}`, borderRadius: 6, padding: "6px 8px", fontFamily: "inherit", resize: "vertical" }}
      />
      {sug && !sug.replacement && (
        <div style={{ fontSize: 12, color: A.body }}>The model found nothing true left to say and suggests cutting this sentence — use Cut, or write your own text.</div>
      )}
      {sug && sug.checks.length > 0 && ed.draft.trim() === sug.replacement.trim() && (
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          {sug.checks.map((c, i) => (
            <div key={`${c.gate}-${i}`} style={{ fontSize: 12, color: A.ink }}>
              <Badge tone={c.blocking ? "danger" : "warning"} dot={false}>{gateLabel(c.gate)}</Badge> {c.violations.join(" | ")}
            </div>
          ))}
          <div style={{ fontSize: 11.5, color: A.muted }}>
            {sug.checks.some((c) => c.blocking)
              ? "This text would be refused on Apply — edit it first."
              : "Warnings only — Apply still saves this text."}
          </div>
        </div>
      )}
      {onRegenerate && (
        <input
          aria-label="Note for the model" placeholder="Optional note for Regenerate (e.g. keep the place name)"
          value={ed.note} maxLength={200} onChange={(e) => onChange({ note: e.target.value })}
          style={{ width: "100%", boxSizing: "border-box", fontSize: 12, background: A.card, color: A.ink, border: `1px solid ${A.line}`, borderRadius: 6, padding: "5px 8px" }}
        />
      )}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center" }}>
        <Button size="sm" variant="primary" disabled={busy || empty} title={empty ? "Empty text — use Cut instead" : undefined} onClick={onApply}>Apply</Button>
        {onRegenerate && <Button size="sm" variant="secondary" disabled={busy} onClick={onRegenerate}>{sug ? "Regenerate" : "Suggest"}</Button>}
        <Button size="sm" variant="ghost" disabled={busy} onClick={onDiscard}>Discard</Button>
        {(regenerating || busy) && <Spinner size={14} />}
      </div>
    </div>
  );
}

const MODE_LABEL: Record<string, string> = { cut: "cut", rewrite: "rewrite (suggestion)", manual: "edit" };

export function AdminEdits({ edits }: { edits: AdminEdit[] }) {
  if (edits.length === 0) return <div style={{ fontSize: 12, color: A.muted }}>No admin edits.</div>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {edits.map((e, i) => (
        <div key={`${e.created_at}-${i}`} style={{ fontSize: 12, borderBottom: `1px solid ${A.line}`, paddingBottom: 6 }}>
          <div style={{ color: A.muted }}>
            {fmtTime(e.created_at)} · {e.actor} · {MODE_LABEL[e.details.mode ?? ""] ?? e.action}{e.details.gate ? ` · ${e.details.gate}` : ""}
            {e.details.field ? ` · ${tourSectionLabel(e.details.field, e.details.index ?? null)}` : ""}
          </div>
          <div style={{ textDecoration: "line-through", color: A.body, wordBreak: "break-word" }}>{e.details.before}</div>
          {e.details.after ? <div style={{ color: A.ink, wordBreak: "break-word" }}>{e.details.after}</div> : <div style={{ color: A.muted }}>(removed)</div>}
        </div>
      ))}
    </div>
  );
}

/** AA-802 — the tenant took a copy away (published / exported) BEFORE an admin edit: that copy still
 *  has the old text and nothing re-syncs it. Shown at the top of the T9 and T2 drawers. */
export function TenantCopyNotice({ copy, what }: { copy: TenantCopy; what: "piece" | "version" }) {
  if (!copy.older_copy) return null;
  const how = copy.published_at && copy.delivered_at === copy.published_at
    ? "published it (WordPress / the channel is not re-synced)"
    : "downloaded it";
  return (
    <div data-testid="cq-older-copy" style={{ fontSize: 12.5, background: A.amberSoft, borderRadius: 8, padding: "8px 10px", color: A.ink, marginBottom: 10 }}>
      <b>The tenant has an older copy.</b> They {how} on {fmtTime(copy.delivered_at)}, before {copy.edits_after_delivery} admin
      edit{copy.edits_after_delivery === 1 ? "" : "s"} to this {what}. Their copy keeps the old text until they {copy.published_at ? "re-publish" : "export again"}.
    </div>
  );
}
