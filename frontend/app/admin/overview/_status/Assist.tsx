"use client";
// app/admin/overview/_status/Assist.tsx — AA-1154: the assistant panel of the Ecosystem status page. "Analyse" on a
// Needs-attention row (and on a signal the assistant sees that the list does not show yet) opens a kit Drawer: the
// rule-made action cards at once (GET .../cards, no AI call, A0), then the same cards ranked and explained by the AI
// gateway (POST .../analyse, stage ops_assist, its own month cap, A1); "Deep analysis" asks Sonnet, "Analyse again"
// skips the answer saved for the card lifetime. The model has no tools and no button here runs an action: each card
// names the API a person would call. The business owner gets the Summary only (the API answers audience "summary").
// While the page is open it asks the server to analyse each open incident of an automatic severity once (POST
// .../auto; cis.ops.assist_auto_severities). A read-only session (a viewer, view-as, the Dev E2E identity) sends no AI
// request at all (the API refuses every POST of it): the rule-made cards still show, the AI buttons are hidden.

import Link from "next/link";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
  Badge, Button, Drawer, ErrorState, K, LoadingScreen, Tabs, WriteOnly, apiGet, apiSend, useFormat, useT, type Tone,
} from "../../../_kit";
import {
  SIGNALS_PATH, analysePath, autoPath, callText, cardsPath, citedEvidence, confidenceTone, costText, expired,
  explanationEvidence, extraSignals, llmNotice, ranked, riskTone,
  type ActionCard, type AssistAnswer, type Depth, type Evidence, type LlmMeta, type LlmNoticeKey, type SignalsAnswer,
} from "@/lib/opsAssist";
import { levelTone } from "@/lib/opsStatus";
import { ASSIST } from "../assist.messages";
import { AssistLookup } from "./AssistLookup";
import { useMayAsk } from "./useMayAsk";
import { rowWrap, small } from "./ui";

function useAT() {
  return useT(ASSIST);
}

const CONF_KEY = {
  code: "confcode", measured: "confmeasured", history: "confhistory", not_enough_data: "confnot_enough_data",
} as const;

const label = { fontSize: 11, fontWeight: 700, color: K.muted, textTransform: "uppercase", letterSpacing: 0.4 } as const;
const code = { fontFamily: "monospace", fontSize: 12, overflowWrap: "anywhere" } as const;

function Field({ name, children }: { name: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "grid", gap: 2, minWidth: 0 }}>
      <span style={label}>{name}</span>
      <div style={{ fontSize: 13, color: K.ink, overflowWrap: "anywhere" }}>{children}</div>
    </div>
  );
}

function EvidenceLinks({ items }: { items: Evidence[] }) {
  return (
    <ul style={{ margin: 0, paddingLeft: 18, display: "grid", gap: 2 }}>
      {items.map((e) => (
        <li key={e.id} style={small}>
          <Link href={e.href} style={{ color: K.accentDeep, fontWeight: 600 }}>{e.label}</Link>: {e.value}
        </li>
      ))}
    </ul>
  );
}

function Predictions({ card }: { card: ActionCard }) {
  const t = useAT();
  return (
    <ul style={{ margin: 0, paddingLeft: 18, display: "grid", gap: 4 }}>
      {card.predictions.map((p, i) => (
        <li key={i}>
          <Badge tone={confidenceTone(p.confidence)} dot={false}>{t(CONF_KEY[p.confidence])}</Badge>{" "}
          {p.text} <span style={small}>({p.basis})</span>
        </li>
      ))}
    </ul>
  );
}

function Approval({ card }: { card: ActionCard }) {
  const t = useAT();
  const min = card.approval.needs_reason ? card.approval.reason_min_chars : null;
  return (
    <Field name={t("approves")}>
      {card.approval.who}
      {min !== null && <div style={small}>{t("needsReason", min)}</div>}
    </Field>
  );
}

function Precondition({ card }: { card: ActionCard }) {
  const t = useAT();
  if (!card.precondition) return null;
  return (
    <Field name={t("precondition")}>
      {!card.precondition_met && <Badge tone="warning" dot={false}>{t("preconditionOpen")}</Badge>}{" "}
      {card.precondition}
    </Field>
  );
}

/** The API the card names (never called from here in A0), or the runbook and text of a copy-only step. */
function CallInfo({ card }: { card: ActionCard }) {
  const t = useAT();
  if (card.copy_only) {
    return (
      <Field name={t("copyOnly")}>
        {card.runbook && <Link href={card.runbook} style={{ color: K.accentDeep, fontWeight: 600 }}>{t("runbook")}</Link>}
        {card.copy_text && <div style={{ ...code, marginTop: 4 }} data-testid={`assist-copy-${card.action}`}>
          {card.copy_text}</div>}
      </Field>
    );
  }
  if (!card.call) return null;
  return (
    <Field name={t("call")}>
      <div style={code}>{callText(card.call)}</div>
      {card.dry_run && <div style={small}>{t("dryRun")}: <span style={code}>{callText(card.dry_run)}</span></div>}
    </Field>
  );
}

function Card({ card, evidence, now }: { card: ActionCard; evidence: Evidence[]; now: Date }) {
  const t = useAT();
  const { formatDateTime } = useFormat();
  const cost = card.cost.usd === null ? t("costUnknown") : `${card.cost.usd} USD`;
  return (
    <div data-testid={`assist-card-${card.action}`} role="listitem"
      style={{ display: "grid", gap: 8, padding: 12, border: `1px solid ${K.line}`, borderRadius: 8, background: K.card,
        opacity: card.precondition_met ? 1 : 0.85, minWidth: 0 }}>
      <div style={rowWrap}>
        <Badge tone="neutral" dot={false}>{t("rank", card.rank)}</Badge>
        {card.rule_rank !== undefined && card.rule_rank !== card.rank && (
          <span style={small}>{t("ruleRank", card.rule_rank)}</span>)}
        <Badge tone={riskTone(card.risk.level)}>{card.risk.level}</Badge>
        <span style={{ fontWeight: 700, color: K.ink, overflowWrap: "anywhere", minWidth: 0 }}>{card.title}</span>
      </div>
      <span style={small}>{card.risk.why.join(" ")}</span>
      {card.explanation && (
        <div data-testid={`assist-why-${card.action}`}
          style={{ display: "grid", gap: 4, padding: 8, borderRadius: 6, background: K.accentSoft, minWidth: 0 }}>
          <Field name={t("whyNow")}>{card.explanation.text}</Field>
          <EvidenceLinks items={explanationEvidence(card, evidence)} />
        </div>
      )}
      <Field name={t("reason")}>{card.reason}</Field>
      <Field name={t("evidence")}><EvidenceLinks items={citedEvidence(card, evidence)} /></Field>
      <Field name={t("predicted")}><Predictions card={card} /></Field>
      <Field name={t("ifNothing")}>{card.if_nothing}</Field>
      <Field name={t("wayBack")}>
        {card.way_back.text}
        {card.way_back.call && <div style={code}>{callText(card.way_back.call)}</div>}
      </Field>
      <Field name={t("cost")}>{cost} <span style={small}>({card.cost.basis})</span></Field>
      <Approval card={card} />
      <Precondition card={card} />
      <CallInfo card={card} />
      <span style={{ ...small, color: expired(card, now) ? K.danger : K.muted }}>
        {t("expires", formatDateTime(card.expires_at))}</span>
    </div>
  );
}

function Summary({ answer }: { answer: AssistAnswer }) {
  const t = useAT();
  const ai = answer.ai_summary;
  return (
    <div data-testid="assist-summary" style={{ display: "grid", gap: 8 }}>
      <span style={{ fontWeight: 700, color: K.ink }}>{answer.summary.headline}</span>
      {ai?.owner_text && (
        <Field name={t("ownerAi")}><span data-testid="assist-owner-ai">{ai.owner_text}</span></Field>)}
      {answer.summary.lines.slice(1).map((l, i) => <p key={i} style={{ margin: 0, fontSize: 13 }}>{l}</p>)}
    </div>
  );
}

type AT = ReturnType<typeof useAT>;

/** The words of each notice (one small function per case keeps each one simple). */
const LLM_TEXT: Record<LlmNoticeKey, (t: AT, llm: LlmMeta, when: string) => string> = {
  llmUsed: (t, l, w) => t("llmUsed", l.model ?? "AI", costText(l.cost_usd), w),
  llmCached: (t, l, w) => t("llmCached", l.model ?? "AI", w),
  llmFailed: (t, l) => t("llmFailed", l.error ?? "error"),
  llmOff: (t) => t("llmOff"),
  llmBudget: (t) => t("llmBudget"),
  llmNotSet: (t) => t("llmNotSet"),
  llmNone: (t) => t("noLlm"),
};

/** What the model did: its model, cost and time, a saved answer, or why the cards keep the rules' order. */
function LlmLine({ answer, running }: { answer: AssistAnswer; running: boolean }) {
  const t = useAT();
  const { formatDateTime } = useFormat();
  const llm = answer.llm ?? { used: false, calls: 0 };
  const when = llm.analysed_at ? formatDateTime(llm.analysed_at) : "";
  const text = running ? t("aiRunning") : LLM_TEXT[llmNotice(llm)](t, llm, when);
  const fallback = !running && llm.deep_fallback ? ` ${t("deepFallback", llm.deep_model ?? "")}` : "";
  return <span style={small} data-testid="assist-llm">{text}{fallback}</span>;
}

function EvidenceTab({ answer }: { answer: AssistAnswer }) {
  const t = useAT();
  return (
    <div style={{ display: "grid", gap: 6 }} data-testid="assist-evidence">
      <EvidenceLinks items={answer.evidence} />
      {answer.evidence.filter((e) => e.untrusted_detail).map((e) => (
        <div key={e.id} style={small}><b>{t("untrusted")}:</b> {e.untrusted_detail}</div>
      ))}
    </div>
  );
}

function CardsTab({ answer, cards, now }: { answer: AssistAnswer; cards: ActionCard[]; now: Date }) {
  const t = useAT();
  return (
    <div role="list" style={{ display: "grid", gap: 10 }} data-testid="assist-cards">
      {cards.map((c) => <Card key={c.id} card={c} evidence={answer.evidence} now={now} />)}
      {cards.length === 0 && <span style={small}>{t("noCards")}</span>}
      {answer.not_proposed.length > 0 && (
        <details data-testid="assist-not-proposed">
          <summary style={{ ...small, cursor: "pointer" }}>{t("notProposed")}</summary>
          <ul style={{ margin: "6px 0 0", paddingLeft: 18, ...small }}>
            {answer.not_proposed.map((n) => <li key={n.action}><b>{n.title}</b>: {n.why}</li>)}
          </ul>
        </details>
      )}
    </div>
  );
}

/** The notes above the tabs: what the AI did, sources not read, cards past their time limit. */
function Notices({ answer, stale, running, onRefresh }: {
  answer: AssistAnswer; stale: boolean; running: boolean; onRefresh: () => void;
}) {
  const t = useAT();
  const missing = Object.keys(answer.unavailable ?? {});
  return (
    <>
      <LlmLine answer={answer} running={running} />
      {missing.length > 0 && <span style={small}>{t("unavailable", missing.join(", "))}</span>}
      {stale && !answer.replay && (
        <div style={rowWrap}>
          <Badge tone="warning">{t("expired")}</Badge>
          <Button size="sm" variant="ghost" onClick={onRefresh}>{t("refresh")}</Button>
        </div>
      )}
    </>
  );
}

/** The tech lead's AI summary (the business owner reads the plain-words paragraph on the Summary tab). */
function AiSummary({ answer }: { answer: AssistAnswer }) {
  const t = useAT();
  const text = answer.ai_summary?.text;
  if (!text) return null;
  return <Field name={t("aiSummary")}><span data-testid="assist-ai-summary">{text}</span></Field>;
}

/** "Deep analysis" and "Analyse again" (each one AI call within the stage's month cap). */
function AiButtons({ depth, busy, onAsk }: { depth: Depth; busy: boolean; onAsk: (d: Depth, fresh: boolean) => void }) {
  const t = useAT();
  return (
    <div style={rowWrap}>
      {depth === "quick" && (
        <Button size="sm" variant="ghost" disabled={busy} onClick={() => onAsk("deep", false)}
          testId="assist-deep">{t("deep")}</Button>)}
      <Button size="sm" variant="ghost" disabled={busy} onClick={() => onAsk(depth, true)}
        testId="assist-again">{t("again")}</Button>
    </div>
  );
}

function Body({ answer, running, depth, onAsk, onRefresh }: {
  answer: AssistAnswer; running: boolean; depth: Depth; onAsk: (d: Depth, fresh: boolean) => void;
  onRefresh: () => void;
}) {
  const t = useAT();
  const [tab, setTab] = useState<string>("cards");
  const now = new Date();
  const cards = ranked(answer.cards);
  if (answer.audience === "summary") return <Summary answer={answer} />;
  return (
    <div style={{ display: "grid", gap: 12 }}>
      <Notices answer={answer} stale={cards.some((c) => expired(c, now))} running={running} onRefresh={onRefresh} />
      {!answer.replay && <WriteOnly><AiButtons depth={depth} busy={running} onAsk={onAsk} /></WriteOnly>}
      <AiSummary answer={answer} />
      <Tabs ariaLabel={t("actions")} active={tab} onChange={setTab} testId="assist-tabs"
        tabs={[{ key: "cards", label: t("tabCards"), count: cards.length }, { key: "summary", label: t("tabSummary") },
          { key: "evidence", label: t("tabEvidence"), count: answer.evidence.length }]} variant="underline" />
      {tab === "summary" && <Summary answer={answer} />}
      {tab === "evidence" && <EvidenceTab answer={answer} />}
      {tab === "cards" && <CardsTab answer={answer} cards={cards} now={now} />}
    </div>
  );
}

/** The rule-made cards at once (GET, no AI call) and the AI's ranking of them (POST) for one open drawer. */
function useAnalysis(itemId: string, range: string, open: boolean) {
  const [ask, setAsk] = useState<{ depth: Depth; fresh: boolean; n: number }>({ depth: "quick", fresh: false, n: 0 });
  const mayAsk = useMayAsk();
  const rules = useQuery({
    queryKey: ["ops-assist", itemId, range],
    queryFn: () => apiGet<AssistAnswer>(cardsPath(itemId, range), { admin: true }),
    enabled: open,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
    retry: false,
  });
  const ai = useQuery({
    queryKey: ["ops-assist-ai", itemId, range, ask.depth, ask.fresh, ask.n],
    queryFn: () => apiSend<AssistAnswer>(analysePath(itemId, range, ask.depth, ask.fresh), { method: "POST",
      admin: true }),
    enabled: open && mayAsk,
    placeholderData: (prev) => prev,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    retry: false,
  });
  const onAsk = (depth: Depth, fresh: boolean) => setAsk((a) => ({ depth, fresh, n: a.n + 1 }));
  const answer = ai.data ?? rules.data;
  return {
    answer, depth: ask.depth, running: ai.isFetching, onAsk,
    loading: !answer && (rules.isLoading || ai.isLoading),
    failed: !answer && rules.isError && (ai.isError || !mayAsk) ? (rules.error as Error) : null,
    refresh: () => { rules.refetch(); if (mayAsk) onAsk(ask.depth, true); },
    retry: () => rules.refetch(),
  };
}

/** The drawer for one item; fetches only while open: the rule-made cards at once, the AI's ranking when it answers. */
export function AssistDrawer({ itemId, title, range, open, onClose }: {
  itemId: string; title: string; range: string; open: boolean; onClose: () => void;
}) {
  const t = useAT();
  const a = useAnalysis(itemId, range, open);
  return (
    <Drawer open={open} onClose={onClose} title={t("analyseFor", title)} width={560} testId="assist-drawer">
      {a.loading && <LoadingScreen message={t("loading")} />}
      {a.failed && <ErrorState message={a.failed.message || t("failed")} onRetry={a.retry} />}
      {a.answer && <Body answer={a.answer} running={a.running} depth={a.depth} onAsk={a.onAsk}
        onRefresh={a.refresh} />}
    </Drawer>
  );
}

/** "Analyse" on a Needs-attention row. */
export function AssistButton({ itemId, title, range }: { itemId: string; title: string; range: string }) {
  const t = useAT();
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button size="sm" variant="ghost" onClick={() => setOpen(true)} testId={`status-att-${itemId}-actions`}>
        {t("analyse")}</Button>
      {open && <AssistDrawer itemId={itemId} title={title} range={range} open={open} onClose={() => setOpen(false)} />}
    </>
  );
}

/** While the page is open, the server analyses each open incident of an automatic severity once (it keeps the
 * "once"); never for a read-only session. */
function useAutoAnalysis(range: string) {
  const qc = useQueryClient();
  const mayAsk = useMayAsk();
  useQuery({
    queryKey: ["ops-assist-auto", range],
    queryFn: async () => {
      const r = await apiSend<{ analysed: string[] }>(autoPath(range), { method: "POST", admin: true });
      if (r.analysed?.length) qc.invalidateQueries({ queryKey: ["ops-assist-ai"] });
      return r;
    },
    enabled: mayAsk,
    staleTime: 240_000,
    refetchInterval: 300_000,
    refetchOnWindowFocus: false,
    retry: false,
  });
}

/** What the assistant sees that the Needs-attention list does not show yet (an AI connection refusing, a spend jump). */
export function AssistSignals({ attentionIds, range }: { attentionIds: string[]; range: string }) {
  const t = useAT();
  useAutoAnalysis(range);
  const res = useQuery({
    queryKey: ["ops-assist-signals"],
    queryFn: () => apiGet<SignalsAnswer>(SIGNALS_PATH, { admin: true }),
    staleTime: 60_000,
    refetchInterval: 120_000,
    refetchOnWindowFocus: false,
    retry: false,
  });
  const extra = extraSignals(res.data?.signals ?? [], attentionIds);
  if (extra.length === 0) return <AssistLookup />;
  return (
    <div data-testid="assist-signals" style={{ display: "grid", gap: 6 }}>
      <span style={label}>{t("signalsTitle")}</span>
      {extra.map((s) => (
        <div key={s.id} style={{ ...rowWrap, padding: "8px 12px", border: `1px dashed ${K.line}`, borderRadius: 8 }}
          data-testid={`assist-signal-${s.id}`}>
          <Badge tone={levelTone(s.severity) as Tone}>{s.severity.toUpperCase()}</Badge>
          <span style={{ fontWeight: 600, color: K.ink, overflowWrap: "anywhere", minWidth: 0 }}>{s.title}</span>
          <span style={small}>{s.detail}</span>
          <span style={{ marginLeft: "auto" }}><AssistButton itemId={s.id} title={s.title} range={range} /></span>
        </div>
      ))}
      <AssistLookup />
    </div>
  );
}
