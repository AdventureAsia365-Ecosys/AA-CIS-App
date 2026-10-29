"use client";
// app/admin/content-quality/PieceDrawer.tsx — AA-769. One T9 piece as the admin sees it: the final
// text with every flagged sentence highlighted (red = blocking gate, amber = warning), the gate
// ledger, what the writer was given (seed, place background, facts, brand pack, angle), the attempt
// history and the LLM cost by stage. None of this reaches the tenant (AA-770).
// S234: each flagged sentence can be cut or rewritten by an admin (SentenceActions.tsx).
// AA-791: a "Label" section (PieceLabel.tsx) and, per failed attempt, the gates that failed in it
// (repair_log[].gate_ledger, kept for pieces written after AA-791).
// AA-863: a "Slate seed" line — the frozen seed atom's place · country and a mismatch badge.
// AA-779 (S245): "Search questions" — the real questions the blog writer got and where each final FAQ
// question came from (Segment match / place PAA / tour PAA, or `writer` = made up). Admin only.
// AA-924 PR2: per-violation severity. Each failed gate lists its findings with their own blocking / warning badge
// (a merged F2_lexicon / F15_voice entry also names the legacy gate that found each one); highlights and the
// attempt history read the finding's severity, not the entry's.

import { useQuery } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import { A, mono } from "../_components/adminUi";
import { Badge, Drawer, StatusBadge, apiGet, useLang, useT } from "../../_kit";
import { SeedLine, seedSkipText } from "../_components/SeedLine";
import { PieceLabelSection } from "./PieceLabel";
import { AdminEdits, FlaggedSentences, TenantCopyNotice } from "./SentenceActions";
import { codePointIndex, fmtTime, usd, type Gate, type Highlight, type PieceDetail, type QuestionWithSource, type WriterInput } from "./shared";
import { FindingList } from "./Severity";
import { SEVERITY_TONE, entrySeverity, findingsOf, markSeverity, severityCounts } from "@/lib/gateSeverity";
import { D, QUESTION_SOURCE } from "./drawer.messages";
import { STATUS } from "./messages";
import { useCqText, useLookup } from "./useCqText";

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginTop: 18 }}>
      <div style={{ fontSize: 10.5, fontWeight: 700, letterSpacing: "0.1em", textTransform: "uppercase", color: A.muted, marginBottom: 6 }}>{title}</div>
      {children}
    </div>
  );
}

function counts(c: Record<string, number> | undefined, source: (k: string) => string, none: string) {
  const parts = Object.entries(c || {}).map(([k, n]) => `${source(k)} ${n}`);
  return parts.length ? parts.join(" · ") : none;
}

function QuestionList({ title, rows }: { title: string; rows: QuestionWithSource[] }) {
  const source = useLookup(QUESTION_SOURCE);
  if (rows.length === 0) return null;
  return (
    <div style={{ marginBottom: 6 }}>
      <div style={{ fontSize: 11, color: A.muted, marginBottom: 2 }}>{title}</div>
      {rows.map((r, i) => (
        <div key={i} style={{ display: "flex", gap: 6, alignItems: "baseline", fontSize: 12, color: A.body }}>
          <span style={{ flexShrink: 0, display: "inline-flex" }}>
            <Badge tone={r.source === "writer" ? "warning" : "neutral"} dot={false}>{source(r.source)}</Badge>
          </span>
          <span>{r.question}</span>
        </div>
      ))}
    </div>
  );
}

function SearchQuestions({ wi }: { wi: WriterInput }) {
  const t = useT(D);
  const source = useLookup(QUESTION_SOURCE);
  if (!wi.question_sources) return null;
  const faq = wi.faq_questions || [];
  const real = faq.filter((f) => f.source !== "writer").length;
  return (
    <div>
      <div style={{ fontSize: 11.5, fontWeight: 600, marginBottom: 4 }}>{t("searchQuestions")}</div>
      <div style={{ fontSize: 12, color: A.body, marginBottom: 6 }}>
        <b>{t("candidates")}</b> {counts(wi.question_sources.candidates, source, t("none"))}<br />
        <b>{t("inScope")}</b> {counts(wi.question_sources.kept, source, t("none"))}<br />
        <b>{t("faqFromReal")}</b> {t("ofN", real, faq.length)}
      </div>
      <QuestionList title={t("givenToWriter")} rows={wi.search_questions || []} />
      <QuestionList title={t("finalFaq")} rows={faq} />
    </div>
  );
}

function Pre({ text }: { text: string }) {
  return (
    <pre style={{ whiteSpace: "pre-wrap", wordBreak: "break-word", fontSize: 12, lineHeight: 1.55, background: A.bg, border: `1px solid ${A.line}`, borderRadius: 8, padding: "10px 12px", margin: 0, color: A.body, maxHeight: 320, overflow: "auto" }}>{text}</pre>
  );
}

/** The text split into plain and highlighted runs (overlaps keep the first highlight). Backend
 *  offsets are code points; `at` maps them to JS string (UTF-16) indices. A click on a highlight
 *  scrolls to its row in the flagged-sentence list. */
function HighlightedText({ text, highlights }: { text: string; highlights: Highlight[] }) {
  const { gateLabel, severityLabel } = useCqText();
  const runs: React.ReactNode[] = [];
  const at = codePointIndex(text);
  let pos = 0;
  highlights.forEach((h, i) => {
    const start = at(h.start), end = at(h.end);
    if (start < pos) return;
    if (start > pos) runs.push(<span key={`t${i}`}>{text.slice(pos, start)}</span>);
    // AA-924 — the finding's own severity: a warn stock phrase inside a blocking F2_lexicon entry is amber
    const block = markSeverity(h) === "block";
    runs.push(
      <mark key={`h${i}`} data-severity={block ? "block" : "warn"}
            title={`${severityLabel(block ? "block" : "warn")} · ${gateLabel(h.gate)} (${h.source ?? h.gate}): ${h.problem}`}
            onClick={() => document.getElementById(`hl-${h.key}`)?.scrollIntoView({ behavior: "smooth", block: "center" })}
            style={{ background: block ? A.redSoft : A.amberSoft, color: A.ink, borderBottom: `2px solid ${block ? A.red : A.amber}`, padding: "0 1px", cursor: "pointer" }}>
        {text.slice(start, end)}
      </mark>,
    );
    pos = end;
  });
  if (pos < text.length) runs.push(<span key="end">{text.slice(pos)}</span>);
  return (
    <div style={{ whiteSpace: "pre-wrap", fontSize: 13, lineHeight: 1.65, color: A.body, background: A.card, border: `1px solid ${A.line}`, borderRadius: 10, padding: "12px 14px", maxHeight: 460, overflow: "auto" }}>
      {runs}
    </div>
  );
}

/** AA-791 — one line per failed attempt: the blocking / warning gates that failed in that attempt,
 *  from the trimmed ledger the T9 loop keeps in each repair_log entry (absent on older pieces). */
function AttemptLedgers({ log }: { log: Record<string, unknown>[] }) {
  const t = useT(D);
  const { gateLabel, severityLabel, counts: countsLabel } = useCqText();
  const rows = log.filter((e) => Array.isArray(e.gate_ledger));
  if (!rows.length) return null;
  return (
    <div data-testid="cq-attempt-ledgers" style={{ display: "flex", flexDirection: "column", gap: 4, marginBottom: 8 }}>
      {rows.map((e, i) => {
        const failed = (e.gate_ledger as Gate[]).filter((g) => !g.passed);
        return (
          <div key={i} style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center", fontSize: 12 }}>
            <span style={{ fontWeight: 600, color: A.ink, minWidth: 74 }}>{t("attempt", String(e.attempt ?? i + 1))}</span>
            {failed.length === 0 ? <span style={{ color: A.muted }}>{t("noGateFailed")}</span> : failed.map((g) => (
              <span key={g.gate} data-testid="cq-attempt-gate"
                    title={findingsOf(g).map((f) => `[${severityLabel(f.severity)}] ${f.text}`).join(" | ")}>
                <Badge tone={SEVERITY_TONE[entrySeverity(g) ?? "block"]} dot={false}>
                  {gateLabel(g.gate)}{g.findings ? ` · ${countsLabel(severityCounts(g))}` : ""}
                </Badge>
              </span>
            ))}
            <span style={{ color: A.muted }}>{t("gatesRan", (e.gate_ledger as Gate[]).length)}</span>
          </div>
        );
      })}
    </div>
  );
}

/** AA-924 — one row per gate; a failed gate lists every finding with its own severity badge. The row tint follows
 *  the entry's worst severity. */
function GateTable({ gates }: { gates: Gate[] }) {
  const t = useT(D);
  const { gateLabel, counts: countsLabel } = useCqText();
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      {gates.map((g, i) => {
        const sev = entrySeverity(g);
        const counts = countsLabel(severityCounts(g));
        return (
          <div key={`${g.gate}-${i}`} data-testid="cq-gate-row" data-severity={sev ?? "passed"}
               style={{ display: "grid", gridTemplateColumns: "minmax(0, 190px) minmax(0, 1fr)", gap: 10, fontSize: 12, padding: "6px 8px", borderRadius: 6, background: sev === null ? "transparent" : (sev === "warn" ? A.amberSoft : A.redSoft) }}>
            <div style={{ minWidth: 0 }}>
              <div style={{ fontWeight: 600, color: A.ink }}>{gateLabel(g.gate)}</div>
              <div style={{ fontFamily: mono, fontSize: 10.5, color: A.muted, overflowWrap: "anywhere" }}>{g.gate}{sev === "warn" ? t("warnSuffix") : ""}</div>
              {sev !== null && counts && <div style={{ fontSize: 10.5, color: A.muted }}>{counts}</div>}
            </div>
            <div style={{ color: sev === null ? A.muted : A.body, minWidth: 0 }}>
              {sev === null ? t("passed") : <FindingList entry={g} />}
            </div>
          </div>
        );
      })}
    </div>
  );
}

export default function PieceDrawer({ pieceId, onClose }: { pieceId: string; onClose: () => void }) {
  const t = useT(D);
  const lang = useLang();
  const status = useLookup(STATUS);
  const { severityLabel } = useCqText();
  const { data: p, isError, error } = useQuery({
    queryKey: ["cq-piece", pieceId],
    queryFn: () => apiGet<PieceDetail>(`/api/admin/content-quality/pieces/${pieceId}`, { admin: true }),
  });
  const [showDiscarded, setShowDiscarded] = useState(false);
  const wi = p?.writer_input;
  const pack = (wi?.brand_pack ?? {}) as Record<string, unknown>;
  const thinBrand = wi && !pack.core_idea && !pack.style_guide;

  const header = p ? (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <span>{p.seo_title || p.angle_name || t("piece")}</span>
      <span style={{ display: "flex", gap: 6, alignItems: "center", fontSize: 12, fontWeight: 400, color: A.muted }}>
        <StatusBadge status={p.status === "held" ? "failed" : p.status === "approved" ? "succeeded" : p.status} label={status(p.status)} />
        {p.tenant_name}{t("pieceMeta", p.channel ?? "", p.attempt_number)} · {usd(p.cost_usd)} · {fmtTime(p.created_at)}
      </span>
    </div>
  ) : t("loading");

  return (
    <Drawer open onClose={onClose} title={header} width={860}>
      {isError && <div style={{ color: A.red, fontSize: 12.5 }}>{t("couldNotLoadPiece", error instanceof Error ? error.message : String(error))}</div>}
      {!p ? <div style={{ fontSize: 12.5, color: A.muted }}>{t("loading")}</div> : (
        <>
          <TenantCopyNotice copy={p.tenant_copy} what="piece" />
          {p.held_reason && (
            <div style={{ fontSize: 12.5, background: A.redSoft, borderRadius: 8, padding: "8px 10px", color: A.ink }}>
              <b>{t("heldLead")}</b> {p.held_reason}
            </div>
          )}
          {p.seed && (
            <Section title={t("seedSection")}>
              <SeedLine seed={p.seed} showSegment />
              {!p.seed.atom_id && seedSkipText(p.seed.skip_reason, lang) && (
                <div style={{ fontSize: 12, color: A.muted, marginTop: 4 }}>{t("noSeed", seedSkipText(p.seed.skip_reason, lang) ?? "")}</div>
              )}
            </Section>
          )}

          <Section title={t("textSection", p.highlights.length)}>
            {/* S241 UI verify — alignItems flex-start: with the default `stretch` the two pills grew
                to the height of the 3-line helper text at 390 (~50px tall). */}
            <div style={{ display: "flex", alignItems: "flex-start", gap: 8, marginBottom: 6, fontSize: 11.5, color: A.muted }}>
              <span style={{ flexShrink: 0, display: "inline-flex" }}><Badge tone="danger" dot={false}>{severityLabel("block")}</Badge></span>
              <span style={{ flexShrink: 0, display: "inline-flex" }}><Badge tone="warning" dot={false}>{severityLabel("warn")}</Badge></span>
              <span style={{ minWidth: 0 }}>{t("severityHelp")}</span>
            </div>
            <HighlightedText text={p.content_text || ""} highlights={p.highlights} />
            {p.channel === "blog" && (
              <div style={{ fontSize: 12, marginTop: 8, color: A.body }}>
                <div><b>{t("seoTitle")}</b> ({(p.seo_title || "").length}): {p.seo_title || "—"}</div>
                <div><b>{t("meta")}</b> ({(p.meta_description || "").length}): {p.meta_description || "—"}</div>
              </div>
            )}
          </Section>

          <Section title={t("flaggedSentences", p.flagged_sentences.length)}>
            <FlaggedSentences piece={p} />
          </Section>

          {p.admin_edits.length > 0 && (
            <Section title={t("adminEdits", p.admin_edits.length)}>
              <AdminEdits edits={p.admin_edits} owner={{ kind: "pieces", id: p.piece_id, queryKey: ["cq-piece", pieceId] }} />
            </Section>
          )}

          <Section title={t("labelSection")}><PieceLabelSection piece={p} /></Section>

          <Section title={t("gatesSection")}><GateTable gates={p.gate_ledger || []} /></Section>
          {(p.flags || []).some((f) => !p.gate_ledger.find((g) => g.gate === f.gate)) && (
            <Section title={t("otherFlags")}>
              <GateTable gates={(p.flags || []).filter((f) => !p.gate_ledger.find((g) => g.gate === f.gate))} />
            </Section>
          )}

          <Section title={t("writerGiven")}>
            {!wi ? <div style={{ fontSize: 12, color: A.muted }}>{t("notRecorded")}</div> : (
              <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                {thinBrand && <div style={{ fontSize: 12, background: A.amberSoft, padding: "6px 8px", borderRadius: 6 }}>{t("thinBrand")}</div>}
                <div style={{ fontSize: 12, color: A.body }}>
                  <b>{t("angle")}</b> {wi.angle?.name} — {wi.angle?.best_final_style}
                  <br /><b>{t("goal")}</b> {wi.goal} · <b>{t("keyword")}</b> {wi.keyword || "—"} · <b>{t("cta")}</b> {wi.cta}
                  {wi.scope && <><br /><b>{t("scope")}</b> {wi.scope}</>}
                </div>
                <div><div style={{ fontSize: 11.5, fontWeight: 600, marginBottom: 4 }}>{t("seedChars", (wi.seed || "").length)}</div><Pre text={wi.seed || ""} /></div>
                {wi.facts && <div><div style={{ fontSize: 11.5, fontWeight: 600, marginBottom: 4 }}>{t("factsHeading")}</div><Pre text={wi.facts} /></div>}
                <SearchQuestions wi={wi} />
                <div><div style={{ fontSize: 11.5, fontWeight: 600, marginBottom: 4 }}>{t("brandPack")}</div><Pre text={JSON.stringify(wi.brand_pack, null, 2)} /></div>
              </div>
            )}
          </Section>

          <Section title={t("attempts")}>
            {(p.repair_log || []).length === 0 ? <div style={{ fontSize: 12, color: A.muted }}>{t("firstAttempt")}</div> : (
              <>
                <AttemptLedgers log={p.repair_log} />
                <Pre text={JSON.stringify(p.repair_log, null, 2)} />
              </>
            )}
            {(p.discarded_attempts || []).length > 0 && (
              <div style={{ marginTop: 8 }}>
                <button onClick={() => setShowDiscarded((v) => !v)} style={{ fontSize: 12, background: "none", border: "none", color: A.accent, cursor: "pointer", padding: 0 }}>
                  {showDiscarded ? t("hideDiscarded") : t("showDiscarded")}
                </button>
                {showDiscarded && p.discarded_attempts.map((d) => (
                  <div key={d.attempt_number} style={{ marginTop: 6 }}>
                    <div style={{ fontSize: 11.5, fontWeight: 600 }}>{t("attempt", String(d.attempt_number))}</div>
                    <Pre text={d.content_text} />
                  </div>
                ))}
              </div>
            )}
          </Section>

          <Section title={t("costByStage")}>
            {p.cost_by_stage.length === 0 ? <div style={{ fontSize: 12, color: A.muted }}>{t("noCallsPiece")}</div> : (
              <div style={{ display: "grid", gridTemplateColumns: "1fr auto auto", gap: "4px 16px", fontSize: 12 }}>
                {p.cost_by_stage.map((c) => (
                  <Fragment key={`${c.stage}-${c.role}`}><span style={{ fontFamily: mono }}>{c.stage} · {c.role}</span><span>{t("calls", c.calls)}</span><span style={{ fontFamily: mono }}>{usd(c.cost_usd)}</span></Fragment>
                ))}
              </div>
            )}
          </Section>
        </>
      )}
    </Drawer>
  );
}
