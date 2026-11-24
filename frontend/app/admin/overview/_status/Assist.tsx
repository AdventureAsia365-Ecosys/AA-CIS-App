"use client";
// app/admin/overview/_status/Assist.tsx — AA-1154 (A0): the assistant panel of the Ecosystem status page. "Actions" on
// a Needs-attention row (and on a signal the assistant sees that the list does not show yet) opens a kit Drawer with
// the ranked action cards, their evidence and a plain-words Summary, read from GET /api/admin/ops-assist/... (backend
// api/services/ops_assist/). Read-only: no button runs an action in A0; each card names the API a person would call.
// The business owner gets the Summary only (the API answers audience "summary").

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import {
  Badge, Button, Drawer, ErrorState, K, LoadingScreen, Tabs, apiGet, useFormat, useT, type Tone,
} from "../../../_kit";
import {
  SIGNALS_PATH, callText, cardsPath, citedEvidence, confidenceTone, expired, extraSignals, ranked, riskTone,
  type ActionCard, type AssistAnswer, type Evidence, type SignalsAnswer,
} from "@/lib/opsAssist";
import { levelTone } from "@/lib/opsStatus";
import { ASSIST } from "../assist.messages";
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
        <Badge tone={riskTone(card.risk.level)}>{card.risk.level}</Badge>
        <span style={{ fontWeight: 700, color: K.ink, overflowWrap: "anywhere", minWidth: 0 }}>{card.title}</span>
      </div>
      <span style={small}>{card.risk.why.join(" ")}</span>
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
  return (
    <div data-testid="assist-summary" style={{ display: "grid", gap: 8 }}>
      <span style={{ fontWeight: 700, color: K.ink }}>{answer.summary.headline}</span>
      {answer.summary.lines.slice(1).map((l, i) => <p key={i} style={{ margin: 0, fontSize: 13 }}>{l}</p>)}
    </div>
  );
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

/** The notes above the tabs: no AI used, sources not read, cards past their time limit. */
function Notices({ answer, stale, onRefresh }: { answer: AssistAnswer; stale: boolean; onRefresh: () => void }) {
  const t = useAT();
  const missing = Object.keys(answer.unavailable ?? {});
  return (
    <>
      <span style={small}>{t("noLlm")}</span>
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

function Body({ answer, onRefresh }: { answer: AssistAnswer; onRefresh: () => void }) {
  const t = useAT();
  const [tab, setTab] = useState<string>("cards");
  const now = new Date();
  const cards = ranked(answer.cards);
  if (answer.audience === "summary") return <Summary answer={answer} />;
  return (
    <div style={{ display: "grid", gap: 12 }}>
      <Notices answer={answer} stale={cards.some((c) => expired(c, now))} onRefresh={onRefresh} />
      <Tabs ariaLabel={t("actions")} active={tab} onChange={setTab} testId="assist-tabs"
        tabs={[{ key: "cards", label: t("tabCards"), count: cards.length }, { key: "summary", label: t("tabSummary") },
          { key: "evidence", label: t("tabEvidence"), count: answer.evidence.length }]} variant="underline" />
      {tab === "summary" && <Summary answer={answer} />}
      {tab === "evidence" && <EvidenceTab answer={answer} />}
      {tab === "cards" && <CardsTab answer={answer} cards={cards} now={now} />}
    </div>
  );
}

/** The drawer for one item; fetches only while open. */
export function AssistDrawer({ itemId, title, range, open, onClose }: {
  itemId: string; title: string; range: string; open: boolean; onClose: () => void;
}) {
  const t = useAT();
  const res = useQuery({
    queryKey: ["ops-assist", itemId, range],
    queryFn: () => apiGet<AssistAnswer>(cardsPath(itemId, range), { admin: true }),
    enabled: open,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
    retry: false,
  });
  return (
    <Drawer open={open} onClose={onClose} title={t("actionsFor", title)} width={560} testId="assist-drawer">
      {res.isLoading && <LoadingScreen message={t("loading")} />}
      {res.isError && <ErrorState message={(res.error as Error)?.message || t("failed")} onRetry={() => res.refetch()} />}
      {res.data && <Body answer={res.data} onRefresh={() => res.refetch()} />}
    </Drawer>
  );
}

/** "Actions" on a Needs-attention row. */
export function AssistButton({ itemId, title, range }: { itemId: string; title: string; range: string }) {
  const t = useAT();
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button size="sm" variant="ghost" onClick={() => setOpen(true)} testId={`status-att-${itemId}-actions`}>
        {t("actions")}</Button>
      {open && <AssistDrawer itemId={itemId} title={title} range={range} open={open} onClose={() => setOpen(false)} />}
    </>
  );
}

/** What the assistant sees that the Needs-attention list does not show yet (an AI connection refusing, a spend jump). */
export function AssistSignals({ attentionIds, range }: { attentionIds: string[]; range: string }) {
  const t = useAT();
  const res = useQuery({
    queryKey: ["ops-assist-signals"],
    queryFn: () => apiGet<SignalsAnswer>(SIGNALS_PATH, { admin: true }),
    staleTime: 60_000,
    refetchInterval: 120_000,
    refetchOnWindowFocus: false,
    retry: false,
  });
  const extra = extraSignals(res.data?.signals ?? [], attentionIds);
  if (extra.length === 0) return null;
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
    </div>
  );
}
