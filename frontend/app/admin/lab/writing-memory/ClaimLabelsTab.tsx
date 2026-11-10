"use client";
// app/admin/lab/writing-memory/ClaimLabelsTab.tsx — AA-1063: Writing memory › Claim labels (root ADR 0020 W2).
//
// Two views:
//   Review queue  the golden_claim items of the disagreement queue as plain-language cards: the sentence with its
//                 context and the unsupported part highlighted, the matching source lines, the full source (on
//                 demand), the three lenses with their reasons and the agent's proposal; Correct / Wrong / Unsure saves
//                 a tech-lead label at once (POST .../review/{id}/answer). "Accept every remaining proposal" answers
//                 every open item under the filters with its proposal after an in-page second click (no
//                 window.confirm). Filters: label set version, writer, proposal, state.
//   Label sets    every set version with who decided its labels (tech lead / agreed panel / none), corrections, audit
//                 flips and splits; a set opens every labelled sentence (who, when, why, history), and a tech lead can
//                 change a final label (append only: the old one stays in the history).
// Every call goes through the BFF (/api/admin/writing-memory/claim-labels/*, admin only; writes ai.admin). The texts
// are tour or agency copy: admin only, like the queue; ai_ops reads are logged by the API.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import {
  Badge, Button, ButtonGroup, DataTable, Drawer, ErrorState, Field, Input, K, SegmentedControl, Select, TableSkeleton,
  Textarea, apiGet, mono, useAdminIdentity, useFormat, useLang, useT, useToast, type ColumnDef, type Translator,
} from "../../../_kit";
import { can } from "@/lib/rbac";
import {
  ANSWERS, ANSWER_OF_LABEL, CLAIM_API, CLAIM_WRITERS, CLAIMS_PAGE, LABELS, REVIEW_PAGE, acceptable, acceptableCount,
  chunks, claimsUrl, deciderLabel, facetCounts, labelTone, note, proposalReason, reviewPageOffset, reviewUrl, setKey,
  splitMark,
  type AcceptResult, type Answer, type AnswerResult, type ClaimEntry, type ClaimFilters, type ClaimLabel,
  type ClaimsPage, type ItemContext, type ReviewFilters, type ReviewItem, type ReviewPage, type SetSummary,
} from "@/lib/claimLabels";
import { CL } from "./claimLabels.messages";
import { useWmText } from "./useWmText";

type T = Translator<typeof CL.en>;
const LABEL_KEY = { ok: "label_ok", violation: "label_violation", unsure: "label_unsure" } as const;
const SOURCE_KEY = { tech_lead: "src_tech_lead", prefilter: "src_prefilter", panel_agreed: "src_panel_agreed",
  none: "src_none" } as const;

const box: React.CSSProperties = { background: K.bg, borderRadius: 8, padding: "10px 12px", display: "grid", gap: 4,
  fontSize: 13, minWidth: 0, overflowWrap: "anywhere" };
const eyebrow: React.CSSProperties = { fontSize: 11, fontWeight: 600, color: K.muted, textTransform: "uppercase",
  letterSpacing: "0.04em" };

function labelName(t: T, label: string): string {
  return label in LABEL_KEY ? t(LABEL_KEY[label as ClaimLabel]) : label;
}

function Sentence({ item, lang }: { item: ReviewItem; lang: "en" | "vi" }) {
  const t = useT(CL);
  const ctx = item.context;
  const vi = lang === "vi" ? note(item.proposal?.notes, "claim", "vi") : "";
  return (
    <div style={{ display: "grid", gap: 4 }}>
      <span style={eyebrow}>{t("sentence")}</span>
      <p style={{ margin: 0, fontSize: 14, lineHeight: 1.55, overflowWrap: "anywhere" }}>
        {ctx?.before && <span style={{ color: K.muted }}>…{ctx.before.slice(-220)} </span>}
        <strong style={{ fontWeight: 600 }}>
          {splitMark(item.claim_text, item.proposal?.bad_part).map((p, i) => p.mark
            ? <mark key={i} style={{ background: K.amberSoft, color: K.ink, borderRadius: 3, padding: "0 2px" }}>
              {p.text}</mark>
            : <span key={i}>{p.text}</span>)}
        </strong>
        {ctx?.after && <span style={{ color: K.muted }}> {ctx.after.slice(0, 220)}…</span>}
      </p>
      {vi && <p style={{ margin: 0, fontSize: 13, color: K.ink3 }}>{t("sentenceVi")}: {vi}</p>}
    </div>
  );
}

function FullSource({ itemId }: { itemId: string }) {
  const t = useT(CL);
  const [open, setOpen] = useState(false);
  const q = useQuery({ queryKey: ["wm", "claims", "context", itemId], enabled: open,
    queryFn: () => apiGet<ItemContext>(`${CLAIM_API}/review/${itemId}/context`) });
  return (
    <details onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
      <summary style={{ cursor: "pointer", color: K.muted, fontSize: 13 }}>{t("fullSource")}</summary>
      {q.isLoading ? <div style={{ fontSize: 12.5, color: K.muted, marginTop: 6 }}>{t("loadingSource")}</div>
        : q.error ? <ErrorState message={(q.error as Error).message} onRetry={() => q.refetch()} />
        : q.data && (q.data.found ? (
          <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", fontFamily: "inherit", fontSize: 12.5,
            background: K.bg, padding: "10px 12px", borderRadius: 8, maxHeight: 420, overflow: "auto",
            margin: "8px 0 0" }}>{q.data.source_text}</pre>
        ) : <div style={{ fontSize: 12.5, color: K.muted, marginTop: 6 }}>{t("noContext")}</div>)}
    </details>
  );
}

function ItemCard({ item, canAdmin, lang, onAnswered }: {
  item: ReviewItem; canAdmin: boolean; lang: "en" | "vi"; onAnswered: () => void;
}) {
  const t = useT(CL);
  const toast = useToast();
  const wm = useWmText();
  const { formatDateTime } = useFormat();
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState<ClaimLabel | null>(null);
  const p = item.proposal;
  const proposed: Answer | null = p ? ANSWER_OF_LABEL[p.label] : null;
  const decision = item.decision;
  const open = item.status === "open" && !saved;

  async function answer(a: Answer) {
    setBusy(true);
    const r = await wm.send<AnswerResult>(`/claim-labels/review/${item.item_id}/answer`, "POST",
      { answer: a, proposal_id: p?.proposal_id ?? null });
    setBusy(false);
    if (r.ok) {
      setSaved(r.data.label);
      toast.success(t("saved", labelName(t, r.data.label)));
      onAnswered();
    } else toast.error(r.message);
  }

  const ctx = item.context;
  return (
    <section data-testid="claim-review-card" style={{ background: K.card, border: `1px solid ${saved || decision
      ? K.successBorder : K.line}`, borderRadius: 12, padding: "14px 16px", display: "grid", gap: 10, minWidth: 0 }}>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
        <Badge dot={false}>{setKey(item.set_id, item.set_version)}</Badge>
        <Badge tone="info" dot={false}>{item.writer}</Badge>
        <Badge tone={item.human_label === "audit" ? "purple" : "warning"} dot={false}>
          {item.human_label === "audit" ? t("why_audit") : t("why_disputed")}</Badge>
        <Badge dot={false}>{item.split === "held_out" ? t("heldOut") : t("train")}</Badge>
        <span style={{ fontSize: 12, color: K.muted }}>
          {t("panelSaid", item.machine_verdict === "split" ? item.machine_verdict : labelName(t, item.machine_verdict))}
        </span>
        <span style={{ fontSize: 11.5, color: K.muted2, fontFamily: mono }}>{item.item_id.slice(0, 8)}</span>
      </div>
      <Sentence item={item} lang={lang} />
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 300px), 1fr))", gap: 10 }}>
        <div style={box}>
          <span style={eyebrow}>{t("evidence")}</span>
          {ctx && !ctx.found ? <span style={{ color: K.muted }}>{t("noContext")}</span>
            : ctx?.evidence.length ? ctx.evidence.map((e, i) => <span key={i}>{e}</span>)
            : <span style={{ color: K.muted }}>{t("noEvidence")}</span>}
        </div>
        <div style={{ ...box, background: p ? (p.label === "ok" ? K.successSoft : p.label === "violation"
          ? K.dangerSoft : K.amberSoft) : K.bg }}>
          <span style={eyebrow}>{t("proposal")}{p?.confidence ? ` · ${t("confidence", p.confidence)}` : ""}</span>
          {p ? (
            <>
              <strong style={{ fontSize: 14 }}>{labelName(t, p.label)}</strong>
              <span>{proposalReason(p, lang)}</span>
              {note(p.notes, "source_says", lang) && <span><em>{t("sourceSays")}:</em>{" "}
                {note(p.notes, "source_says", lang)}</span>}
              {note(p.notes, "doubt", lang) && <span><em>{t("doubt")}:</em> {note(p.notes, "doubt", lang)}</span>}
              <span style={{ fontSize: 11.5, color: K.muted }}>{t("proposalBy", p.run_id)}</span>
            </>
          ) : <span style={{ color: K.muted }}>{t("noProposal")}</span>}
        </div>
      </div>
      <div style={{ display: "grid", gap: 4 }}>
        <span style={eyebrow}>{t("lenses")}</span>
        {item.panel.map((l) => (
          <div key={l.lens ?? "x"} style={{ display: "flex", gap: 8, flexWrap: "wrap", fontSize: 12.5,
            alignItems: "baseline", minWidth: 0 }}>
            <span style={{ fontFamily: mono, minWidth: 64 }}>{l.lens}</span>
            <Badge tone={labelTone(l.label)} dot={false}>{labelName(t, l.label)}</Badge>
            <span style={{ fontFamily: mono, color: K.muted, fontSize: 11.5 }}>{l.claim_class}</span>
            <span style={{ flex: "1 1 260px", minWidth: 0, overflowWrap: "anywhere", color: K.ink3 }}>{l.reason}</span>
          </div>
        ))}
      </div>
      <FullSource itemId={item.item_id} />
      {open && decision && (
        <div role="note" style={{ fontSize: 12.5, padding: "6px 10px", borderRadius: 8, background: K.amberSoft }}>
          {t("alreadyDecided", labelName(t, decision.label), deciderLabel(decision.by))}</div>
      )}
      {open ? (canAdmin ? (
        <ButtonGroup label={t("answerAria")}>
          {ANSWERS.map((a) => (
            <Button key={a} size="sm" variant={a === proposed ? "primary" : "secondary"} disabled={busy}
              onClick={() => answer(a)}>
              {t(a)}{a === proposed ? ` ${t("proposed")}` : ""}</Button>
          ))}
        </ButtonGroup>
      ) : <span style={{ fontSize: 12.5, color: K.muted }}>{t("readOnly")}</span>)
        : (
          <div style={{ fontSize: 12.5, color: K.success }}>
            {saved ? t("saved", labelName(t, saved))
              : decision ? t("decided", labelName(t, decision.label), deciderLabel(decision.by),
                decision.at ? formatDateTime(decision.at) : "—") : ""}
            {item.resolution && <span style={{ color: K.muted }}> · {t("resolution", item.resolution)}</span>}
          </div>
        )}
    </section>
  );
}

function ReviewView({ canAdmin }: { canAdmin: boolean }) {
  const t = useT(CL);
  const lang = useLang();
  const toast = useToast();
  const qc = useQueryClient();
  const wm = useWmText();
  const [f, setF] = useState<ReviewFilters>({ set: "", writer: "", proposal: "", state: "open" });
  const [offset, setOffset] = useState(0);
  const [answered, setAnswered] = useState(0);
  const [armedAt, setArmedAt] = useState<number | null>(null);
  const [bulk, setBulk] = useState<string | null>(null);
  const q = useQuery({ queryKey: ["wm", "claims", "review", f, offset],
    queryFn: () => apiGet<ReviewPage>(reviewUrl(f, offset)) });
  // the whole queue's counts (progress, accept count), refreshed after each answer while the page's cards stay put
  const fq = useQuery({ queryKey: ["wm", "claims", "review", "facets"],
    queryFn: () => apiGet<ReviewPage>(reviewUrl({ set: "", writer: "", proposal: "", state: "open" }, 0, 1, false)) });
  const facets = useMemo(() => fq.data?.facets ?? q.data?.facets ?? [], [fq.data, q.data]);
  const counts = useMemo(() => facetCounts(facets, f.set), [facets, f.set]);
  const toAccept = acceptableCount(facets, f);
  const total = q.data?.total ?? 0;
  const done = counts.state.resolved ?? 0;
  const all = done + (counts.state.open ?? 0);

  function set<K extends keyof ReviewFilters>(k: K, v: ReviewFilters[K]) {
    setF((x) => ({ ...x, [k]: v }));
    setOffset(0);
    setAnswered(0);
    setArmedAt(null);
  }

  // answered open items have left the open list on the server: refetch and step back by their number (the next page
  // can be this same offset, so the cached page must not be reused)
  function go(dir: 1 | -1) {
    const next = reviewPageOffset(f.state, offset, answered, dir);
    setAnswered(0);
    qc.invalidateQueries({ queryKey: ["wm", "claims", "review"] });
    setOffset(next);
  }

  function onAnswered() {
    setAnswered((n) => n + 1);
    qc.invalidateQueries({ queryKey: ["wm", "claims", "review", "facets"] });
    qc.invalidateQueries({ queryKey: ["wm", "claims", "sets"] });
  }

  // the second click must come soon after the first one; the armed bar closes by itself
  useEffect(() => {
    if (armedAt === null) return;
    const id = setTimeout(() => setArmedAt(null), 8000);
    return () => clearTimeout(id);
  }, [armedAt]);

  async function acceptAll() {
    setArmedAt(null);
    setBulk(t("acceptGathering"));
    const ids: string[] = [];
    try {
      for (let off = 0; ; off += 100) {
        const page = await apiGet<ReviewPage>(reviewUrl({ ...f, state: "open" }, off, 100, false));
        ids.push(...acceptable(page.items));
        if (off + 100 >= page.total) break;
      }
    } catch (e) {
      setBulk(null);
      toast.error((e as Error).message);
      return;
    }
    if (!ids.length) {
      setBulk(null);
      toast.info(t("acceptNone"));
      return;
    }
    let saved = 0;
    let refused = 0;
    for (const part of chunks(ids)) {
      setBulk(t("acceptSaving", saved, ids.length));
      const r = await wm.send<AcceptResult>("/claim-labels/review/accept-proposals", "POST", { item_ids: part });
      if (!r.ok) {
        toast.error(r.message);
        break;
      }
      saved += r.data.accepted;
      refused += r.data.refused.length;
    }
    setBulk(null);
    toast.success(t("acceptDone", saved, refused));
    setOffset(0);
    setAnswered(0);
    qc.invalidateQueries({ queryKey: ["wm", "claims"] });
  }

  const proposalOptions = [
    { value: "", label: t("pAll") }, { value: "ok", label: t("pOk"), count: counts.proposal.ok ?? 0 },
    { value: "violation", label: t("pViolation"), count: counts.proposal.violation ?? 0 },
    { value: "unsure", label: t("pUnsure"), count: counts.proposal.unsure ?? 0 },
    { value: "none", label: t("pNone"), count: counts.proposal.none ?? 0 },
  ] as const;
  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 12 }}>
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end" }}>
        <Field label={t("fSet")}>
          <Select value={f.set} onChange={(e) => set("set", e.target.value)} size="sm">
            <option value="">{t("fAllSets")}</option>
            {counts.sets.map((s) => <option key={s.key} value={s.key}>{s.key} ({s.n})</option>)}
          </Select>
        </Field>
        <Field label={t("fWriter")}>
          <Select value={f.writer} onChange={(e) => set("writer", e.target.value as ReviewFilters["writer"])} size="sm">
            <option value="">{t("fAllWriters")}</option>
            {CLAIM_WRITERS.map((w) => <option key={w} value={w}>{w}</option>)}
          </Select>
        </Field>
        <SegmentedControl label={t("fState")} size="sm" value={f.state} onChange={(v) => set("state", v)}
          options={[{ value: "open", label: t("sOpen"), count: counts.state.open ?? 0 },
            { value: "resolved", label: t("sResolved"), count: counts.state.resolved ?? 0 },
            { value: "all", label: t("sAll") }]} />
      </div>
      <SegmentedControl label={t("fProposal")} size="sm" appearance="chips" value={f.proposal}
        onChange={(v) => set("proposal", v)} options={[...proposalOptions]} />
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center",
        borderBottom: `1px solid ${K.line}`, paddingBottom: 10 }}>
        <span style={{ fontSize: 12.5, color: K.muted, fontFamily: mono }}>{t("progress", done, all)}</span>
        {canAdmin && f.state !== "resolved" && (armedAt !== null ? (
          <span role="alert" style={{ display: "inline-flex", gap: 8, flexWrap: "wrap", alignItems: "center",
            fontSize: 13, padding: "6px 10px", borderRadius: 8, background: K.amberSoft }}>
            {t("acceptConfirm", toAccept)}
            <Button size="sm" variant="primary" onClick={acceptAll}>{t("acceptYes")}</Button>
            <Button size="sm" variant="ghost" onClick={() => setArmedAt(null)}>{t("acceptNo")}</Button>
          </span>
        ) : (
          <Button size="sm" variant="primary" disabled={!!bulk || toAccept === 0}
            onClick={() => setArmedAt(Date.now())}>{t("acceptAll", toAccept)}</Button>
        ))}
        {bulk && <span style={{ fontSize: 12.5, color: K.muted }}>{bulk}</span>}
      </div>
      {q.error ? <ErrorState message={(q.error as Error).message} onRetry={() => q.refetch()} />
        : q.isLoading ? <TableSkeleton rows={4} cols={3} />
        : !q.data?.items.length ? <div style={{ fontSize: 13, color: K.muted }}>{t("empty")}</div>
        : (
          <div style={{ display: "grid", gap: 12 }}>
            {q.data.items.map((it) => (
              <ItemCard key={it.item_id} item={it} canAdmin={canAdmin} lang={lang} onAnswered={onAnswered} />
            ))}
          </div>
        )}
      {total > REVIEW_PAGE && (
        <div style={{ display: "flex", gap: 8, alignItems: "center", justifyContent: "center" }}>
          <Button size="sm" variant="ghost" disabled={offset === 0} onClick={() => go(-1)}>{t("prev")}</Button>
          <span style={{ fontSize: 12.5, color: K.muted }}>
            {t("page", offset + 1, Math.min(offset + REVIEW_PAGE, total), total)}</span>
          <Button size="sm" variant="ghost" disabled={offset + REVIEW_PAGE >= total} onClick={() => go(1)}>
            {t("next")}</Button>
        </div>
      )}
    </div>
  );
}

// ── label sets ───────────────────────────────────────────────────────────────────────────────────────────────────

function ClaimDrawer({ entry, set, classes, canAdmin, onClose }: {
  entry: ClaimEntry | null; set: SetSummary; classes: string[]; canAdmin: boolean; onClose: () => void;
}) {
  const t = useT(CL);
  const toast = useToast();
  const qc = useQueryClient();
  const wm = useWmText();
  const { formatDateTime } = useFormat();
  // the parent remounts the drawer per sentence (key), so the form starts from that sentence's label
  const [label, setLabel] = useState<ClaimLabel>(entry?.label === "ok" ? "violation" : "ok");
  const [cls, setCls] = useState(entry?.claim_class ?? "");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);

  async function save() {
    if (!entry) return;
    setBusy(true);
    const r = await wm.send(`/claim-labels/${encodeURIComponent(set.set_id)}/${encodeURIComponent(set.set_version)}`
      + `/claims/${entry.claim_hash}/relabel`, "POST",
    { label, claim_class: cls || null, reason: reason.trim(), supersedes: entry.head_relabel_id });
    setBusy(false);
    if (r.ok) {
      toast.success(t("relabelSaved"));
      qc.invalidateQueries({ queryKey: ["wm", "claims"] });
      onClose();
    } else {
      toast.error(r.message);
      qc.invalidateQueries({ queryKey: ["wm", "claims", "set"] });
    }
  }

  const sourceName = (s: string) => (s in SOURCE_KEY ? t(SOURCE_KEY[s as keyof typeof SOURCE_KEY]) : s);
  return (
    <Drawer open={!!entry} onClose={onClose} title={t("drawerTitle")} width={560}
      footer={entry && canAdmin && entry.source !== "none" ? (
        <Button variant="primary" onClick={save} disabled={busy || reason.trim().length < 3}>
          {t("saveRelabel")}</Button>) : undefined}>
      {entry && (
        <div style={{ display: "grid", gap: 14, fontSize: 13 }}>
          <p style={{ margin: 0, fontSize: 14, lineHeight: 1.55, overflowWrap: "anywhere" }}>{entry.claim_text}</p>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
            <span style={eyebrow}>{t("finalLabel")}</span>
            <Badge tone={labelTone(entry.label)} dot={false}>{labelName(t, entry.label)}</Badge>
            <span style={{ fontFamily: mono, fontSize: 11.5, color: K.muted }}>{entry.claim_class}</span>
            <Badge dot={false}>{sourceName(entry.source)}</Badge>
            <Badge tone="info" dot={false}>{entry.writer}</Badge>
          </div>
          <div style={box}>
            <span style={eyebrow}>{t("whoWhenWhy")}</span>
            <span>{deciderLabel(entry.decided_by)} · {entry.decided_at ? formatDateTime(entry.decided_at) : "—"}</span>
            {entry.why && <span style={{ color: K.ink3 }}>{entry.why}</span>}
          </div>
          <div style={{ display: "grid", gap: 4 }}>
            <span style={eyebrow}>{t("lenses")}</span>
            {entry.panel.map((l) => (
              <div key={l.lens ?? "x"} style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "baseline" }}>
                <span style={{ fontFamily: mono, minWidth: 64 }}>{l.lens}</span>
                <Badge tone={labelTone(l.label)} dot={false}>{labelName(t, l.label)}</Badge>
                <span style={{ flex: "1 1 240px", minWidth: 0, overflowWrap: "anywhere", color: K.ink3 }}>
                  {l.reason}</span>
              </div>
            ))}
          </div>
          <div style={{ display: "grid", gap: 6 }}>
            <span style={eyebrow}>{t("history")}</span>
            {entry.history.length === 0 && <span style={{ color: K.muted }}>{sourceName(entry.source)}</span>}
            {entry.history.map((h) => (
              <div key={h.id} style={{ display: "grid", gap: 2, borderLeft: `3px solid ${K.line}`, paddingLeft: 8 }}>
                <span>
                  <strong>{h.kind === "relabel" ? t("histRelabel") : t("histTechLead")}</strong>{" "}
                  <Badge tone={labelTone(h.label)} dot={false}>{labelName(t, h.label)}</Badge>{" "}
                  <span style={{ color: K.muted, fontSize: 12 }}>
                    {deciderLabel(h.by)} · {h.at ? formatDateTime(h.at) : "—"}</span>
                </span>
                {h.reason && <span style={{ color: K.ink3, overflowWrap: "anywhere" }}>{h.reason}</span>}
              </div>
            ))}
          </div>
          {canAdmin && (entry.source === "none" ? (
            <div style={{ color: K.muted }}>{t("relabelNone")}</div>
          ) : (
            <div style={{ display: "grid", gap: 10, borderTop: `1px solid ${K.line}`, paddingTop: 12 }}>
              <strong>{t("relabel")}</strong>
              <span style={{ color: K.muted, fontSize: 12.5 }}>{t("relabelHelp")}</span>
              <Field label={t("newLabel")}>
                <Select value={label} onChange={(e) => setLabel(e.target.value as ClaimLabel)}>
                  {LABELS.map((l) => <option key={l} value={l}>{labelName(t, l)}</option>)}
                </Select>
              </Field>
              <Field label={t("newClass")}>
                <Select value={cls} onChange={(e) => setCls(e.target.value)}>
                  {[...new Set([entry.claim_class, ...classes])].filter(Boolean).map((c) => (
                    <option key={c} value={c}>{c}</option>))}
                </Select>
              </Field>
              <Field label={t("reason")} required>
                <Textarea value={reason} onChange={(e) => setReason(e.target.value)} maxLength={600} rows={3} />
              </Field>
            </div>
          ))}
        </div>
      )}
    </Drawer>
  );
}

function ClaimsBrowser({ set, canAdmin }: { set: SetSummary; canAdmin: boolean }) {
  const t = useT(CL);
  const { formatDateTime } = useFormat();
  const [f, setF] = useState<ClaimFilters>({ source: "", label: "", writer: "", split: "", relabelled: "", q: "" });
  const [search, setSearch] = useState("");
  const [page, setPage] = useState({ index: 0, size: CLAIMS_PAGE });
  const [open, setOpen] = useState<string | null>(null);
  const q = useQuery({ queryKey: ["wm", "claims", "set", set.set_id, set.set_version, f, page],
    queryFn: () => apiGet<ClaimsPage>(claimsUrl(set.set_id, set.set_version, f, page.index * page.size, page.size)) });

  useEffect(() => {
    const id = setTimeout(() => {
      setF((x) => (x.q === search ? x : { ...x, q: search }));
      setPage((p) => (p.index === 0 ? p : { ...p, index: 0 }));
    }, 350);
    return () => clearTimeout(id);
  }, [search]);

  function put<K extends keyof ClaimFilters>(k: K, v: ClaimFilters[K]) {
    setF((x) => ({ ...x, [k]: v }));
    setPage((p) => ({ ...p, index: 0 }));
  }

  const sourceName = (s: string) => (s in SOURCE_KEY ? t(SOURCE_KEY[s as keyof typeof SOURCE_KEY]) : s);
  const cols = useMemo<ColumnDef<ClaimEntry, unknown>[]>(() => [
    { accessorKey: "claim_text", header: t("colSentence"),
      cell: (c) => <span style={{ display: "block", minWidth: 260, overflowWrap: "anywhere" }}>
        {c.row.original.claim_text}</span> },
    { accessorKey: "label", header: t("colLabel"),
      cell: (c) => <Badge tone={labelTone(c.row.original.label)} dot={false}>
        {labelName(t, c.row.original.label)}{c.row.original.relabelled ? " *" : ""}</Badge> },
    { accessorKey: "source", header: t("colSource"),
      cell: (c) => <span>{sourceName(c.row.original.source)}
        {c.row.original.decided_by && c.row.original.source === "tech_lead" && (
          <span style={{ color: K.muted, fontSize: 11.5 }}> · {deciderLabel(c.row.original.decided_by)}</span>)}
      </span> },
    { accessorKey: "decided_at", header: t("colWhen"),
      cell: (c) => (c.row.original.decided_at ? formatDateTime(c.row.original.decided_at) : "—") },
    { accessorKey: "claim_class", header: t("colClass"),
      cell: (c) => <span style={{ fontFamily: mono, fontSize: 12 }}>{c.row.original.claim_class}</span> },
    { accessorKey: "writer", header: t("colWriter"),
      cell: (c) => <span style={{ fontFamily: mono, fontSize: 12 }}>{c.row.original.writer}</span> },
  // eslint-disable-next-line react-hooks/exhaustive-deps
  ], [t]);

  const entries = q.data?.claims ?? [];
  const current = entries.find((e) => e.claim_hash === open) ?? null;
  return (
    <section style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 10 }}>
      <h2 style={{ fontSize: 14, margin: "6px 0 0" }}>{t("claimsTitle", setKey(set.set_id, set.set_version))}</h2>
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end" }}>
        <Field label={t("fSource")}>
          <Select size="sm" value={f.source} onChange={(e) => put("source", e.target.value as ClaimFilters["source"])}>
            <option value="">{t("fAll")}</option>
            {(["tech_lead", "prefilter", "panel_agreed", "none"] as const).map((s) => (
              <option key={s} value={s}>{sourceName(s)}</option>))}
          </Select>
        </Field>
        <Field label={t("fLabel")}>
          <Select size="sm" value={f.label} onChange={(e) => put("label", e.target.value as ClaimFilters["label"])}>
            <option value="">{t("fAll")}</option>
            {LABELS.map((l) => <option key={l} value={l}>{labelName(t, l)}</option>)}
          </Select>
        </Field>
        <Field label={t("fWriter")}>
          <Select size="sm" value={f.writer} onChange={(e) => put("writer", e.target.value as ClaimFilters["writer"])}>
            <option value="">{t("fAll")}</option>
            {CLAIM_WRITERS.map((w) => <option key={w} value={w}>{w}</option>)}
          </Select>
        </Field>
        <Field label={t("fSplit")}>
          <Select size="sm" value={f.split} onChange={(e) => put("split", e.target.value as ClaimFilters["split"])}>
            <option value="">{t("fAll")}</option>
            <option value="held_out">{t("heldOut")}</option>
            <option value="train">{t("train")}</option>
          </Select>
        </Field>
        <Field label={t("fRelabelled")}>
          <Select size="sm" value={f.relabelled}
            onChange={(e) => put("relabelled", e.target.value as ClaimFilters["relabelled"])}>
            <option value="">{t("fAll")}</option>
            <option value="yes">{t("colRelabels")}</option>
          </Select>
        </Field>
        <Field label={t("fSearch")}>
          <Input size="sm" value={search} onChange={(e) => setSearch(e.target.value)} maxLength={200} />
        </Field>
      </div>
      {q.error ? <ErrorState message={(q.error as Error).message} onRetry={() => q.refetch()} /> : (
        <DataTable tableId="wm-claim-labels" data={entries} columns={cols} getRowId={(e) => e.claim_hash}
          loading={q.isLoading} compact tableMinWidth={900} emptyTitle={t("claimsEmpty")}
          onRowClick={(e) => setOpen(e.claim_hash)}
          serverPagination={{ pageIndex: page.index, pageSize: page.size, total: q.data?.total ?? 0,
            onChange: (index, size) => setPage({ index, size }) }} />
      )}
      <ClaimDrawer key={current?.claim_hash ?? "none"} entry={current} set={set} classes={q.data?.classes ?? []} canAdmin={canAdmin}
        onClose={() => setOpen(null)} />
    </section>
  );
}

function SetsView({ canAdmin }: { canAdmin: boolean }) {
  const t = useT(CL);
  const { formatDateTime } = useFormat();
  const q = useQuery({ queryKey: ["wm", "claims", "sets"],
    queryFn: () => apiGet<{ sets: SetSummary[] }>(`${CLAIM_API}/sets`) });
  const [sel, setSel] = useState<string | null>(null);
  const sets = q.data?.sets ?? [];
  const chosen = sets.find((s) => setKey(s.set_id, s.set_version) === sel) ?? null;
  const cols = useMemo<ColumnDef<SetSummary, unknown>[]>(() => [
    { id: "set", accessorFn: (s) => setKey(s.set_id, s.set_version), header: t("colSet"),
      cell: (c) => <span style={{ fontFamily: mono }}>{setKey(c.row.original.set_id, c.row.original.set_version)}</span> },
    { accessorKey: "claims", header: t("colClaims") },
    { id: "lead", accessorFn: (s) => s.evidence_sources.tech_lead, header: t("colTechLead") },
    { id: "prefilter", accessorFn: (s) => s.evidence_sources.prefilter ?? 0, header: t("colPrefilter") },
    { id: "panel", accessorFn: (s) => s.evidence_sources.panel_agreed, header: t("colPanel") },
    { id: "none", accessorFn: (s) => s.evidence_sources.none, header: t("colNone") },
    { accessorKey: "relabels", header: t("colRelabels") },
    { id: "audit", accessorFn: (s) => s.audit.flipped, header: t("colAudit"),
      cell: (c) => `${c.row.original.audit.flipped} / ${c.row.original.audit.audited}` },
    { id: "disputed", accessorFn: (s) => s.panel_states.disputed ?? 0, header: t("colDisputed") },
    { id: "split", header: t("colSplit"), enableSorting: false,
      cell: (c) => `${c.row.original.splits.held_out ?? 0} / ${c.row.original.splits.train ?? 0}` },
    { id: "queue", header: t("colQueue"), enableSorting: false,
      cell: (c) => `${c.row.original.queue?.open ?? 0} / ${c.row.original.queue?.resolved ?? 0}` },
    { accessorKey: "last_change", header: t("colLast"),
      cell: (c) => (c.row.original.last_change ? formatDateTime(c.row.original.last_change) : "—") },
  // eslint-disable-next-line react-hooks/exhaustive-deps
  ], [t]);
  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 12 }}>
      <div style={{ fontSize: 12.5, color: K.muted, maxWidth: 760 }}>{t("setsHelp")}</div>
      {q.error ? <ErrorState message={(q.error as Error).message} onRetry={() => q.refetch()} /> : (
        <DataTable tableId="wm-claim-sets" data={sets} columns={cols} getRowId={(s) => setKey(s.set_id, s.set_version)}
          loading={q.isLoading} paginate={false} compact tableMinWidth={1000} emptyTitle={t("setsEmpty")}
          onRowClick={(s) => setSel(setKey(s.set_id, s.set_version))} />
      )}
      {chosen && <ClaimsBrowser key={sel} set={chosen} canAdmin={canAdmin} />}
    </div>
  );
}

export default function ClaimLabelsTab() {
  const t = useT(CL);
  const canAdmin = can(useAdminIdentity().role, "ai.admin");
  const [view, setView] = useState<"review" | "sets">("review");
  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 14 }} data-testid="wm-claim-labels">
      <div style={{ fontSize: 12.5, color: K.muted, maxWidth: 760 }}>{t("help")}</div>
      <SegmentedControl label={t("viewAria")} value={view} onChange={setView}
        options={[{ value: "review", label: t("viewReview") }, { value: "sets", label: t("viewSets") }]} />
      {view === "review" ? <ReviewView canAdmin={canAdmin} /> : <SetsView canAdmin={canAdmin} />}
    </div>
  );
}
