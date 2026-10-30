"use client";
// app/admin/content-quality/TourVersionDrawer.tsx — AA-802. One T2 tenant tour version as the admin
// sees it (score, QA state, escalations, cost: never shown to the tenant, AA-770) plus the silent
// correction: an admin cuts / rewrites / edits a sentence of a version the tenant has not decided on
// yet, and the tenant's copy changes in place (no new version, no flag, no notice). Every edit is
// kept in "Admin edits" with the text before and after.
//   Flagged sentences — what T3 would flag on the current text (a number the source tour lacks, a
//                       forbidden word); the same Cut / Rewrite / Edit actions as the T9 drawer.
//   Text              — every editable section; click any sentence to edit it.
// Admin role only; the backend re-checks the role, the version status and the span.
// AA-916 WM-07: every sentence action carries a reason chip row (a flagged sentence preselects the API's
// `suggested_reason`, a sentence picked in the text `style_only`); Cut / Apply send it as `reason_code`.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import { K, mono, ApiError, Badge, Button, ConfirmModal, Drawer, Spinner, apiGet, apiSend, useAdminIdentity, useT, useToast } from "../../_kit";
import { AdminEdits, ProblemList, RewritePanel, TenantCopyNotice, type Editor } from "./SentenceActions";
import { JudgeBreakdownPanel } from "./JudgeBreakdownPanel";
import { SEVERITY_TONE, worstOf } from "@/lib/gateSeverity";
import { ReasonTag, WritingReasonChips } from "../_components/WritingReasonChips";
import { preselect, type ReasonCode } from "@/lib/writingReasons";
import { D } from "./drawer.messages";
import { useCqText } from "./useCqText";
import {
  codePointIndex, usd,
  type CheckFailure, type TourApplyBody, type TourProblem, type TourSection, type TourSentence,
  type TourSuggestResponse, type TourVersionDetail,
} from "./shared";

/** A sentence the admin acts on, with the section it sits in. */
interface Target { field: string; index: number | null; s: TourSentence; problems: TourProblem[]; suggested?: string }

const targetKey = (t: { field: string; index: number | null; s: TourSentence }) => `${t.field}:${t.index ?? "-"}:${t.s.start}`;
const problemText = (t: Target) => t.problems.map((p) => `${p.gate}: ${p.problem}`).join(" | ");

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginTop: 18 }}>
      <div style={{ fontSize: 10.5, fontWeight: 700, letterSpacing: "0.1em", textTransform: "uppercase", color: K.muted, marginBottom: 6 }}>{title}</div>
      {children}
    </div>
  );
}

const pre: React.CSSProperties = {
  whiteSpace: "pre-wrap", wordBreak: "break-word", fontSize: 12, background: K.bg, border: `1px solid ${K.line}`,
  borderRadius: 8, padding: 10, maxHeight: 320, overflow: "auto", margin: 0, color: K.body,
};

/** A section's text with each sentence clickable (flagged ones highlighted). Offsets are Python code points. */
function SectionText({ sec, canEdit, activeKey, onPick }: {
  sec: TourSection; canEdit: boolean; activeKey: string | null; onPick: (s: TourSentence) => void;
}) {
  const t = useT(D);
  const { gateLabel } = useCqText();
  const at = codePointIndex(sec.text);
  const runs: React.ReactNode[] = [];
  let pos = 0;
  sec.sentences.forEach((s, i) => {
    const start = at(s.start), end = at(s.end);
    if (start < pos) return;
    if (start > pos) runs.push(<span key={`t${i}`}>{sec.text.slice(pos, start)}</span>);
    const flagged = s.problems.length > 0;
    const active = activeKey === targetKey({ field: sec.field, index: sec.index, s });
    runs.push(
      <span key={`s${i}`} role={canEdit ? "button" : undefined} tabIndex={canEdit ? 0 : undefined}
            title={flagged ? s.problems.map((p) => `${gateLabel(p.gate)}: ${p.problem}`).join("\n") : canEdit ? t("editThisSentence") : undefined}
            onClick={canEdit ? () => onPick(s) : undefined}
            onKeyDown={canEdit ? (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onPick(s); } } : undefined}
            style={{
              background: active ? K.accentSoft : flagged ? K.dangerSoft : "transparent",
              borderBottom: flagged ? `2px solid ${K.danger}` : undefined,
              cursor: canEdit ? "pointer" : "default", borderRadius: 3, color: K.ink,
            }}>
        {sec.text.slice(start, end)}
      </span>,
    );
    pos = end;
  });
  if (pos < sec.text.length) runs.push(<span key="end">{sec.text.slice(pos)}</span>);
  return <div style={{ whiteSpace: "pre-wrap", wordBreak: "break-word", fontSize: 13, lineHeight: 1.65, color: K.body }}>{runs}</div>;
}

/** The admin UI (Vercel) can go live before the API (ECS) that adds the AA-802 fields: an older
 *  payload renders read-only instead of crashing the drawer. */
function withDefaults(v: TourVersionDetail, needsNewerApi = "Sentence edits need the newer API — reload after the deploy."): TourVersionDetail {
  return {
    ...v,
    reviews: v.reviews ?? [], cost_by_stage: v.cost_by_stage ?? [], sections: v.sections ?? [],
    flagged_sentences: v.flagged_sentences ?? [], admin_edits: v.admin_edits ?? [],
    editable: v.editable ?? false,
    edit_block_reason: v.edit_block_reason ?? (v.editable === undefined ? needsNewerApi : null),
    tenant_copy: v.tenant_copy ?? { published_at: null, exported_at: null, delivered_at: null, older_copy: false, edits_after_delivery: 0 },
  };
}

export default function TourVersionDrawer({ id, onClose }: { id: string; onClose: () => void }) {
  const t = useT(D);
  const { gateLabel, sectionLabel } = useCqText();
  const toast = useToast();
  const qc = useQueryClient();
  const { isAdmin } = useAdminIdentity();
  const { data: v, isError, error } = useQuery({
    queryKey: ["cq-version", id],
    queryFn: () => apiGet<TourVersionDetail>(`/api/admin/content-quality/tour-versions/${id}`, { admin: true }).then((d) => withDefaults(d, t("needsNewerApi"))),
  });
  const [target, setTarget] = useState<Target | null>(null);
  const [editor, setEditor] = useState<Editor | null>(null);
  const [cutting, setCutting] = useState<Target | null>(null);
  // AA-916 — the chip per sentence the admin changed; untouched ones read the API's suggestion
  const [reasons, setReasons] = useState<Record<string, ReasonCode>>({});
  const reasonOf = (t: Target) => reasons[targetKey(t)] ?? preselect(t.suggested);
  const base = `/api/admin/content-quality/tour-versions/${id}/sentences`;
  const canEdit = isAdmin && !!v?.editable;

  const onFail = (e: unknown) => {
    toast.error(e instanceof Error ? e.message : String(e), 8000);
    if (e instanceof ApiError && e.status === 409) qc.invalidateQueries({ queryKey: ["cq-version", id] });
  };

  const suggest = useMutation({
    mutationFn: ({ t, note }: { t: Target; note: string }) =>
      apiSend<TourSuggestResponse>(`${base}/suggest`, {
        body: { field: t.field, index: t.index, start: t.s.start, end: t.s.end, text: t.s.text,
                problem: problemText(t), note: note || undefined },
        admin: true,
      }),
    onSuccess: (r, { t, note }) => { setTarget(t); setEditor({ key: targetKey(t), draft: r.replacement, note, suggestion: r }); },
    onError: onFail,
  });

  const apply = useMutation({
    mutationFn: (body: TourApplyBody) =>
      apiSend<TourVersionDetail & { warnings?: CheckFailure[] }>(`${base}/apply`, { body, admin: true }),
    onSuccess: ({ warnings, ...detail }, body) => {
      qc.setQueryData(["cq-version", id], withDefaults(detail, t("needsNewerApi")));
      qc.invalidateQueries({ queryKey: ["cq-versions"] });
      setCutting(null);
      setEditor(null);
      setTarget(null);
      toast.success(body.mode === "cut" ? t("tenantCopyUpdatedCut") : t("tenantCopyUpdatedReplaced"));
      if (warnings && warnings.length > 0) {
        toast.info(t("savedWithWarnings", warnings.map((w) => `${gateLabel(w.gate)}: ${w.violations.join(" | ")}`).join("; ")), 8000);
      }
    },
    onError: onFail,
  });

  const busy = suggest.isPending || apply.isPending;

  const bodyFor = (t: Target, mode: TourApplyBody["mode"], replacement: string, ed?: Editor): TourApplyBody => ({
    field: t.field, index: t.index, start: t.s.start, end: t.s.end, text: t.s.text, replacement, mode,
    gate: t.problems[0]?.gate, problem: problemText(t).slice(0, 1000) || undefined,
    suggestion: ed?.suggestion?.replacement, suggestion_model: ed?.suggestion?.model, suggestion_cost_usd: ed?.suggestion?.cost_usd,
    reason_code: reasonOf(t),
  });

  const applyEditor = (t: Target, ed: Editor) => {
    const text = ed.draft.trim();
    const sug = ed.suggestion;
    apply.mutate(bodyFor(t, sug && text === sug.replacement.trim() ? "rewrite" : "manual", text, ed));
  };

  /** The actions + editor for one sentence (a flagged row, or a sentence picked in the text). */
  const actions = (tg: Target) => {
    const key = targetKey(tg);
    const chips = (
      <div style={{ marginTop: 8 }}>
        <WritingReasonChips value={reasonOf(tg)} suggested={preselect(tg.suggested)} disabled={busy}
                            onChange={(code) => setReasons((r) => ({ ...r, [key]: code }))} />
      </div>
    );
    return <>{chips}{sentenceButtons(tg)}</>;
  };

  const sentenceButtons = (tg: Target) => {
    const key = targetKey(tg);
    const open = editor?.key === key ? editor : null;
    const loadingThis = suggest.isPending && suggest.variables && targetKey(suggest.variables.t) === key;
    if (open) {
      return (
        <RewritePanel
          s={tg.s} ed={open} busy={busy} regenerating={!!loadingThis}
          onChange={(patch) => setEditor({ ...open, ...patch })}
          onApply={() => applyEditor(tg, open)}
          onRegenerate={() => suggest.mutate({ t: tg, note: open.note })}
          onDiscard={() => { setEditor(null); setTarget(null); }}
        />
      );
    }
    return (
      <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 8, alignItems: "center" }}>
        <span data-testid="cq-tv-cut"><Button size="sm" variant="danger" disabled={busy} onClick={() => setCutting(tg)}>{t("cut")}</Button></span>
        <span data-testid="cq-tv-rewrite">
          <Button size="sm" variant="secondary" disabled={busy} title={t("rewriteFromSourceTitle")}
                  onClick={() => suggest.mutate({ t: tg, note: "" })}>{t("rewrite")}</Button>
        </span>
        <Button size="sm" variant="ghost" disabled={busy} onClick={() => { setTarget(tg); setEditor({ key, draft: tg.s.sentence, note: "", suggestion: null }); }}>{t("edit")}</Button>
        {loadingThis && <Spinner size={14} />}
      </div>
    );
  };

  const title = v ? `${v.tour_name} — ${v.tenant_name} v${v.version_number}` : t("loading");
  const picked = target && !v?.flagged_sentences.some((f) => targetKey({ field: f.field, index: f.index, s: f }) === targetKey(target)) ? target : null;

  return (
    <Drawer open onClose={onClose} width={860} title={title}>
      {isError && <div style={{ color: K.danger, fontSize: 12.5 }}>{t("couldNotLoadVersion", error instanceof Error ? error.message : String(error))}</div>}
      {!v ? <div style={{ color: K.muted, fontSize: 12.5 }}>{t("loading")}</div> : (
        <>
          <TenantCopyNotice copy={v.tenant_copy} what="version" />
          <div style={{ fontSize: 12.5, color: K.body }}>
            {t("versionStatus")} <b>{v.status}</b> · {t("score")} <b>{v.quality_score ?? "—"}</b> · {t("qa")} <b>{v.qa_status ?? "—"}</b>
            {v.qa_auto_passed ? t("autoPassed") : ""} · {t("repairs", v.qa_repair_count ?? 0)} · {v.edit_source}
            {v.latest_version_number && v.latest_version_number > v.version_number ? t("tenantHas", v.latest_version_number) : ""}
          </div>
          <div data-testid="cq-tv-edit-note" style={{ fontSize: 12, marginTop: 8, padding: "6px 8px", borderRadius: 6, background: v.editable ? K.bg : K.amberSoft, color: K.ink }}>
            {!v.editable ? v.edit_block_reason
              : !isAdmin ? t("onlyAdminCorrect")
              : t("editsInPlace")}
          </div>

          <Section title={t("judgeBreakdown")}>
            <JudgeBreakdownPanel b={v.judge_breakdown} />
          </Section>

          <Section title={t("flaggedSentences", v.flagged_sentences.length)}>
            {v.flagged_sentences.length === 0 ? (
              <div style={{ fontSize: 12, color: K.muted }}>{t("noFlaggedVersion")}</div>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {v.flagged_sentences.map((f) => {
                  const tg: Target = { field: f.field, index: f.index, s: f, problems: f.problems, suggested: f.suggested_reason };
                  return (
                    <div key={targetKey(tg)} data-testid="cq-tv-flag-row" style={{ border: `1px solid ${K.dangerBorder}`, borderRadius: 8, padding: "8px 10px", background: K.card }}>
                      <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 6, alignItems: "center" }}>
                        <span style={{ fontSize: 11, color: K.muted }}>{sectionLabel(f.field, f.index)}</span>
                        {f.gates.map((g) => (
                          <Badge key={g} tone={SEVERITY_TONE[worstOf(f.problems.filter((p) => p.gate === g)) ?? "block"]} dot={false}>{gateLabel(g)}</Badge>
                        ))}
                      </div>
                      <ProblemList problems={f.problems} />
                      <div style={{ fontSize: 12.5, lineHeight: 1.55, color: K.ink, background: K.bg, borderRadius: 6, padding: "6px 8px", wordBreak: "break-word" }}>{f.sentence}</div>
                      {canEdit && actions(tg)}
                    </div>
                  );
                })}
              </div>
            )}
          </Section>

          <Section title={canEdit ? t("textClick") : t("text")}>
            {v.sections.length === 0 ? <div style={{ fontSize: 12, color: K.muted }}>{t("noEditableText")}</div> : (
              <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                {v.sections.map((sec) => (
                  <div key={`${sec.field}:${sec.index ?? "-"}`} style={{ border: `1px solid ${K.line}`, borderRadius: 8, padding: "8px 10px", background: K.card }}>
                    <div style={{ fontSize: 11, fontWeight: 600, color: K.muted, marginBottom: 4 }}>{sectionLabel(sec.field, sec.index)}</div>
                    <SectionText sec={sec} canEdit={canEdit} activeKey={target ? targetKey(target) : null}
                                 onPick={(s) => { setEditor(null); setTarget({ field: sec.field, index: sec.index, s, problems: s.problems }); }} />
                    {canEdit && picked && picked.field === sec.field && picked.index === sec.index && (
                      <div style={{ marginTop: 8, borderTop: `1px dashed ${K.line}`, paddingTop: 8 }}>
                        <div style={{ fontSize: 12.5, color: K.ink, background: K.bg, borderRadius: 6, padding: "6px 8px", wordBreak: "break-word" }}>{picked.s.sentence}</div>
                        {actions(picked)}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </Section>

          {v.admin_edits.length > 0 && (
            <Section title={t("adminEdits", v.admin_edits.length)}>
              <AdminEdits edits={v.admin_edits} owner={{ kind: "tour-versions", id: String(v.id), queryKey: ["cq-version", id] }} />
            </Section>
          )}

          <Section title={t("escalations", v.reviews.length)}>
            {v.reviews.length === 0 ? <div style={{ fontSize: 12, color: K.muted }}>{t("noneDot")}</div> : <pre style={pre}>{JSON.stringify(v.reviews, null, 2)}</pre>}
          </Section>

          <Section title={t("costByStage")}>
            {v.cost_by_stage.length === 0 ? <div style={{ fontSize: 12, color: K.muted }}>{t("noCallsVersion")}</div> : (
              <div style={{ display: "grid", gridTemplateColumns: "1fr auto auto", gap: "4px 16px", fontSize: 12 }}>
                {v.cost_by_stage.map((c) => (
                  <Fragment key={`${c.stage}-${c.role}`}><span style={{ fontFamily: mono }}>{c.stage} · {c.role}</span><span>{t("calls", c.calls)}</span><span style={{ fontFamily: mono }}>{usd(c.cost_usd)}</span></Fragment>
                ))}
              </div>
            )}
          </Section>

          <Section title={t("storedContent")}>
            <pre style={pre}>{typeof v.rewritten_content === "string" ? v.rewritten_content : JSON.stringify(v.rewritten_content, null, 2)}</pre>
          </Section>

          <ConfirmModal
            open={!!cutting}
            onClose={() => !apply.isPending && setCutting(null)}
            onConfirm={() => cutting && apply.mutate(bodyFor(cutting, "cut", ""))}
            title={t("cutTitle")}
            destructive
            busy={apply.isPending}
            confirmLabel={t("cut")}
            body={
              <div style={{ display: "flex", flexDirection: "column", gap: 8, fontSize: 13 }}>
                <div style={{ background: K.dangerSoft, borderRadius: 6, padding: "6px 8px", color: K.ink }}>{cutting?.s.sentence}</div>
                {cutting && <div style={{ display: "flex", gap: 6, alignItems: "center", color: K.body }}>{t("reason")} <ReasonTag code={reasonOf(cutting)} /></div>}
                <div style={{ color: K.body }}>{t("cutBodyVersion")}</div>
                {v.tenant_copy.exported_at && <div style={{ color: K.amber }}>{t("cutDownloaded")}</div>}
              </div>
            }
          />
        </>
      )}
    </Drawer>
  );
}
