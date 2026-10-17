"use client";
// app/admin/content-quality/PieceDrawer.tsx — AA-769. One T9 piece as the admin sees it: the final
// text with every flagged sentence highlighted (red = blocking gate, amber = warning), the gate
// ledger, what the writer was given (seed, place background, facts, brand pack, angle), the attempt
// history and the LLM cost by stage. None of this reaches the tenant (AA-770).
// S234: each flagged sentence can be cut or rewritten by an admin (SentenceActions.tsx).
// AA-791: a "Label" section (PieceLabel.tsx) and, per failed attempt, the gates that failed in it
// (repair_log[].gate_ledger, kept for pieces written after AA-791).
// AA-863: a "Slate seed" line — the frozen seed atom's place · country and a mismatch badge.

import { useQuery } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import { A, mono } from "../_components/adminUi";
import { Badge, Drawer, StatusBadge, apiGet } from "../../_kit";
import { SeedLine, seedSkipText } from "../_components/SeedLine";
import { PieceLabelSection } from "./PieceLabel";
import { AdminEdits, FlaggedSentences, TenantCopyNotice } from "./SentenceActions";
import { codePointIndex, fmtTime, gateLabel, usd, type Gate, type Highlight, type PieceDetail } from "./shared";

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginTop: 18 }}>
      <div style={{ fontSize: 10.5, fontWeight: 700, letterSpacing: "0.1em", textTransform: "uppercase", color: A.muted, marginBottom: 6 }}>{title}</div>
      {children}
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
  const runs: React.ReactNode[] = [];
  const at = codePointIndex(text);
  let pos = 0;
  highlights.forEach((h, i) => {
    const start = at(h.start), end = at(h.end);
    if (start < pos) return;
    if (start > pos) runs.push(<span key={`t${i}`}>{text.slice(pos, start)}</span>);
    runs.push(
      <mark key={`h${i}`} title={`${gateLabel(h.gate)} (${h.gate}): ${h.problem}`}
            onClick={() => document.getElementById(`hl-${h.key}`)?.scrollIntoView({ behavior: "smooth", block: "center" })}
            style={{ background: h.blocking ? A.redSoft : A.amberSoft, color: A.ink, borderBottom: `2px solid ${h.blocking ? A.red : A.amber}`, padding: "0 1px", cursor: "pointer" }}>
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
  const rows = log.filter((e) => Array.isArray(e.gate_ledger));
  if (!rows.length) return null;
  return (
    <div data-testid="cq-attempt-ledgers" style={{ display: "flex", flexDirection: "column", gap: 4, marginBottom: 8 }}>
      {rows.map((e, i) => {
        const failed = (e.gate_ledger as Gate[]).filter((g) => !g.passed);
        return (
          <div key={i} style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center", fontSize: 12 }}>
            <span style={{ fontWeight: 600, color: A.ink, minWidth: 74 }}>Attempt {String(e.attempt ?? i + 1)}</span>
            {failed.length === 0 ? <span style={{ color: A.muted }}>no gate failed</span> : failed.map((g) => (
              <span key={g.gate} title={(g.violations || []).join(" | ")}>
                <Badge tone={g.blocking === false ? "warning" : "danger"} dot={false}>{gateLabel(g.gate)}</Badge>
              </span>
            ))}
            <span style={{ color: A.muted }}>· {(e.gate_ledger as Gate[]).length} gates ran</span>
          </div>
        );
      })}
    </div>
  );
}

function GateTable({ gates }: { gates: Gate[] }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      {gates.map((g, i) => (
        <div key={`${g.gate}-${i}`} style={{ display: "grid", gridTemplateColumns: "190px 1fr", gap: 10, fontSize: 12, padding: "6px 8px", borderRadius: 6, background: g.passed ? "transparent" : (g.blocking === false ? A.amberSoft : A.redSoft) }}>
          <div>
            <div style={{ fontWeight: 600, color: A.ink }}>{gateLabel(g.gate)}</div>
            <div style={{ fontFamily: mono, fontSize: 10.5, color: A.muted }}>{g.gate}{g.blocking === false ? " · warn" : ""}</div>
          </div>
          <div style={{ color: g.passed ? A.muted : A.body }}>
            {g.passed ? "passed" : (g.violations || []).map((v, j) => <div key={j} style={{ marginBottom: 3 }}>{v}</div>)}
          </div>
        </div>
      ))}
    </div>
  );
}

export default function PieceDrawer({ pieceId, onClose }: { pieceId: string; onClose: () => void }) {
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
      <span>{p.seo_title || p.angle_name || "Piece"}</span>
      <span style={{ display: "flex", gap: 6, alignItems: "center", fontSize: 12, fontWeight: 400, color: A.muted }}>
        <StatusBadge status={p.status === "held" ? "failed" : p.status === "approved" ? "succeeded" : p.status} label={p.status} />
        {p.tenant_name} · {p.channel} · attempt {p.attempt_number} · {usd(p.cost_usd)} · {fmtTime(p.created_at)}
      </span>
    </div>
  ) : "Loading…";

  return (
    <Drawer open onClose={onClose} title={header} width={860}>
      {isError && <div style={{ color: A.red, fontSize: 12.5 }}>Could not load the piece ({error instanceof Error ? error.message : String(error)})</div>}
      {!p ? <div style={{ fontSize: 12.5, color: A.muted }}>Loading…</div> : (
        <>
          <TenantCopyNotice copy={p.tenant_copy} what="piece" />
          {p.held_reason && (
            <div style={{ fontSize: 12.5, background: A.redSoft, borderRadius: 8, padding: "8px 10px", color: A.ink }}>
              <b>Held (admin label only — the tenant received this piece):</b> {p.held_reason}
            </div>
          )}
          {p.seed && (
            <Section title="Slate seed — the atom this piece was written from">
              <SeedLine seed={p.seed} showSegment />
              {!p.seed.atom_id && seedSkipText(p.seed.skip_reason) && (
                <div style={{ fontSize: 12, color: A.muted, marginTop: 4 }}>No seed: {seedSkipText(p.seed.skip_reason)}.</div>
              )}
            </Section>
          )}

          <Section title={`Text · ${p.highlights.length} flagged span${p.highlights.length === 1 ? "" : "s"}`}>
            {/* S241 UI verify — alignItems flex-start: with the default `stretch` the two pills grew
                to the height of the 3-line helper text at 390 (~50px tall). */}
            <div style={{ display: "flex", alignItems: "flex-start", gap: 8, marginBottom: 6, fontSize: 11.5, color: A.muted }}>
              <span style={{ flexShrink: 0, display: "inline-flex" }}><Badge tone="danger" dot={false}>blocking</Badge></span>
              <span style={{ flexShrink: 0, display: "inline-flex" }}><Badge tone="warning" dot={false}>warning</Badge></span>
              <span style={{ minWidth: 0 }}>hover a highlight for the gate and reason, click it to jump to its actions</span>
            </div>
            <HighlightedText text={p.content_text || ""} highlights={p.highlights} />
            {p.channel === "blog" && (
              <div style={{ fontSize: 12, marginTop: 8, color: A.body }}>
                <div><b>SEO title</b> ({(p.seo_title || "").length}): {p.seo_title || "—"}</div>
                <div><b>Meta</b> ({(p.meta_description || "").length}): {p.meta_description || "—"}</div>
              </div>
            )}
          </Section>

          <Section title={`Flagged sentences · ${p.flagged_sentences.length}`}>
            <FlaggedSentences piece={p} />
          </Section>

          {p.admin_edits.length > 0 && (
            <Section title={`Admin edits · ${p.admin_edits.length}`}><AdminEdits edits={p.admin_edits} /></Section>
          )}

          <Section title="Label"><PieceLabelSection piece={p} /></Section>

          <Section title="Gates (final attempt) — as the writer produced it"><GateTable gates={p.gate_ledger || []} /></Section>
          {(p.flags || []).some((f) => !p.gate_ledger.find((g) => g.gate === f.gate)) && (
            <Section title="Other flags">
              <GateTable gates={(p.flags || []).filter((f) => !p.gate_ledger.find((g) => g.gate === f.gate))} />
            </Section>
          )}

          <Section title="What the writer was given">
            {!wi ? <div style={{ fontSize: 12, color: A.muted }}>Not recorded (piece written before AA-769).</div> : (
              <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                {thinBrand && <div style={{ fontSize: 12, background: A.amberSoft, padding: "6px 8px", borderRadius: 6 }}>This tenant&rsquo;s brand pack has no core idea or style guide — the writer had no brand to follow.</div>}
                <div style={{ fontSize: 12, color: A.body }}>
                  <b>Angle:</b> {wi.angle?.name} — {wi.angle?.best_final_style}
                  <br /><b>Goal:</b> {wi.goal} · <b>Keyword:</b> {wi.keyword || "—"} · <b>CTA:</b> {wi.cta}
                  {wi.scope && <><br /><b>Scope:</b> {wi.scope}</>}
                </div>
                <div><div style={{ fontSize: 11.5, fontWeight: 600, marginBottom: 4 }}>Seed ({(wi.seed || "").length} chars)</div><Pre text={wi.seed || ""} /></div>
                {wi.facts && <div><div style={{ fontSize: 11.5, fontWeight: 600, marginBottom: 4 }}>Facts</div><Pre text={wi.facts} /></div>}
                <div><div style={{ fontSize: 11.5, fontWeight: 600, marginBottom: 4 }}>Brand pack</div><Pre text={JSON.stringify(wi.brand_pack, null, 2)} /></div>
              </div>
            )}
          </Section>

          <Section title="Attempts">
            {(p.repair_log || []).length === 0 ? <div style={{ fontSize: 12, color: A.muted }}>Passed on the first attempt.</div> : (
              <>
                <AttemptLedgers log={p.repair_log} />
                <Pre text={JSON.stringify(p.repair_log, null, 2)} />
              </>
            )}
            {(p.discarded_attempts || []).length > 0 && (
              <div style={{ marginTop: 8 }}>
                <button onClick={() => setShowDiscarded((v) => !v)} style={{ fontSize: 12, background: "none", border: "none", color: A.accent, cursor: "pointer", padding: 0 }}>
                  {showDiscarded ? "Hide" : "Show"} the discarded attempt
                </button>
                {showDiscarded && p.discarded_attempts.map((d) => (
                  <div key={d.attempt_number} style={{ marginTop: 6 }}>
                    <div style={{ fontSize: 11.5, fontWeight: 600 }}>Attempt {d.attempt_number}</div>
                    <Pre text={d.content_text} />
                  </div>
                ))}
              </div>
            )}
          </Section>

          <Section title="Cost by stage">
            {p.cost_by_stage.length === 0 ? <div style={{ fontSize: 12, color: A.muted }}>No calls linked to this piece.</div> : (
              <div style={{ display: "grid", gridTemplateColumns: "1fr auto auto", gap: "4px 16px", fontSize: 12 }}>
                {p.cost_by_stage.map((c) => (
                  <Fragment key={`${c.stage}-${c.role}`}><span style={{ fontFamily: mono }}>{c.stage} · {c.role}</span><span>{c.calls} calls</span><span style={{ fontFamily: mono }}>{usd(c.cost_usd)}</span></Fragment>
                ))}
              </div>
            )}
          </Section>
        </>
      )}
    </Drawer>
  );
}
