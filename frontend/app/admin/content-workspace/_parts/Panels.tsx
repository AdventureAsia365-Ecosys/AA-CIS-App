"use client";
// app/admin/content-workspace/_parts/Panels.tsx — AA-1148: the side panels of the hand-made tour editor: the required
// list ("Before you publish", from the backend's checklist; the publish is refused while a required item is missing),
// the read-only price from Booking, the customer preview (facts + the saved wording draft) and the history of the
// tour's fact changes (raw_tour_revision).

import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, Check, X } from "lucide-react";
import { useState } from "react";
import {
  K, Section, SegmentedControl, apiGet, useFormat, useT, type Translator,
} from "../../../_kit";
import { MEALS, type CheckItem, type FactsForm, type TourView } from "@/lib/contentWorkspace";
import { CW } from "../messages";
import { stack } from "./stack";

type T = Translator<typeof CW.en>;

type ReqKey = "req_name" | "req_country" | "req_supplier" | "req_nights" | "req_nights_match" | "req_day_titles"
  | "req_day_places" | "req_day_descriptions" | "req_overnight" | "req_no_overlap" | "req_wording"
  | "req_wording_days";

/** The label of a checklist item in the UI language (the backend's English message for an unknown key). */
export function itemLabel(item: CheckItem, t: T): string {
  const p = item.params ?? {};
  switch (item.key) {
    case "days": return t("req_days", Number(p.max_days ?? 0));
    case "inclusions": return t("req_inclusions", Number(p.minimum ?? 0));
    case "exclusions": return t("req_exclusions", Number(p.minimum ?? 0));
    default: {
      const key = `req_${item.key}`;
      return key in CW.en ? t(key as ReqKey) : item.message;
    }
  }
}

export function Checklist({ items, completeness }: { items: CheckItem[]; completeness: number }) {
  const t = useT(CW);
  const required = items.filter((i) => i.required);
  const hints = items.filter((i) => !i.required && !i.ok);
  const done = required.every((i) => i.ok);
  return (
    <Section title={t("required")} testId="cw-required"
      actions={<span style={{ fontSize: 12, fontWeight: 600, color: done ? K.success : K.amberDeep }}>
        {t("completeness", completeness)}</span>}>
      <div role="progressbar" aria-valuenow={completeness} aria-valuemin={0} aria-valuemax={100}
        aria-label={t("completeness", completeness)}
        style={{ height: 6, borderRadius: 3, background: K.line, overflow: "hidden", marginBottom: 10 }}>
        <div style={{ width: `${completeness}%`, height: "100%", background: done ? K.success : K.accent }} />
      </div>
      <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 6 }}>
        {required.map((i) => <Row key={i.key} item={i} t={t} />)}
        {hints.map((i) => <Row key={i.key} item={i} t={t} hint />)}
      </ul>
      {done && <p style={{ fontSize: 12, color: K.muted, margin: "10px 0 0" }}>{t("allDone")}</p>}
    </Section>
  );
}

function RowIcon({ ok, hint }: { ok: boolean; hint: boolean }) {
  if (ok) return <Check size={14} color={K.success} />;
  return hint ? <AlertTriangle size={14} color={K.amberDeep} /> : <X size={14} color={K.danger} />;
}

function missingDays(item: CheckItem): number[] {
  return !item.ok && Array.isArray(item.params?.missing) ? (item.params.missing as number[]) : [];
}

function Row({ item, t, hint = false }: { item: CheckItem; t: T; hint?: boolean }) {
  const missing = missingDays(item);
  return (
    <li data-testid={`cw-req-${item.key}`} data-ok={item.ok ? "1" : "0"}
      style={{ display: "flex", gap: 8, alignItems: "flex-start", fontSize: 13, color: item.ok ? K.ink3 : K.ink }}>
      <span style={{ flex: "none", marginTop: 2 }}><RowIcon ok={item.ok} hint={hint} /></span>
      <span>
        {hint && <span style={{ color: K.muted }}>{t("hint")}: </span>}
        {itemLabel(item, t)}
        {missing.length > 0 && (
          <span style={{ display: "block", fontSize: 12, color: K.muted }}>{t("req_days_missing", missing.join(", "))}</span>
        )}
      </span>
    </li>
  );
}

export function PricePanel({ price }: { price: TourView["from_price"] }) {
  const t = useT(CW);
  return (
    <Section title={t("price")} testId="cw-price">
      <p style={{ margin: 0, fontSize: 13, color: price ? K.ink : K.muted }}>
        {price ? t("priceFrom", price.amount, price.currency, price.pax) : t("priceNone")}
      </p>
    </Section>
  );
}

/** "Day N — Title\nbody" blocks of a written itinerary (the canonical shape of services/content_generation/day_parse). */
export function wordingBlocks(text: string | null | undefined): { title: string; body: string }[] {
  if (!text) return [];
  const out: { title: string; body: string }[] = [];
  for (const chunk of text.split(/\n(?=Days?\s+\d+(?:\s*[-–]\s*\d+)?\s+—)/)) {
    const [head, ...rest] = chunk.trim().split("\n");
    if (!head) continue;
    out.push({ title: head.replace(/^Days?\s+[\d\s–-]+—\s*/, "").trim(), body: rest.join("\n").trim() });
  }
  return out;
}

interface DraftFields { aa_name?: string | null; aa_summary?: string | null; aa_itineraries?: string | null }
type Device = "desktop" | "phone";

function useWording(tourId: string | null): DraftFields {
  const draft = useQuery({
    queryKey: ["content-workspace-draft", tourId],
    enabled: !!tourId,
    queryFn: () => apiGet<{ draft: { fields: DraftFields } | null; live: { fields: DraftFields } | null }>(
      `/api/admin/tours/${encodeURIComponent(tourId ?? "")}/draft`),
    retry: false,
  });
  return draft.data?.draft?.fields ?? draft.data?.live?.fields ?? {};
}

/** "3 days, 2 nights · 2–12 travellers" under the preview title. */
function metaLine(form: FactsForm, t: T): string {
  const parts: string[] = [];
  if (form.days.length) parts.push(t("pvDuration", form.days.length, form.nights.trim()));
  if (form.group_min || form.group_max) parts.push(t("pvGroup", form.group_min, form.group_max));
  return parts.join(" · ");
}

export function Preview({ tourId, form }: { tourId: string | null; form: FactsForm }) {
  const t = useT(CW);
  const [device, setDevice] = useState<Device>("desktop");
  const w = useWording(tourId);
  const blocks = wordingBlocks(w.aa_itineraries);
  const phone = device === "phone";
  const summary = w.aa_summary || form.summary;
  return (
    <div style={stack(12)}>
      <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{t("previewIntro")}</p>
      <SegmentedControl label={t("previewDevice")} value={device} onChange={setDevice} size="sm"
        options={[{ value: "desktop", label: t("desktop") }, { value: "phone", label: t("phone") }]} />
      <article data-testid="cw-preview" style={{ maxWidth: phone ? 390 : 760, width: "100%",
        border: `1px solid ${K.line}`, borderRadius: 12, padding: 16, background: K.card, boxSizing: "border-box" }}>
        <h2 style={{ margin: "0 0 6px", fontSize: phone ? 20 : 26, color: K.ink, overflowWrap: "anywhere" }}>
          {w.aa_name || form.name || t("untitled")}
        </h2>
        <p style={{ margin: "0 0 10px", fontSize: 13, color: K.muted }}>{metaLine(form, t)}</p>
        {summary && <p style={{ margin: "0 0 14px", color: K.ink }}>{summary}</p>}
        <h3 style={{ fontSize: 15, margin: "0 0 8px", color: K.ink }}>{t("pvItinerary")}</h3>
        <ol style={{ margin: "0 0 14px", paddingLeft: 18, display: "grid", gap: 10 }}>
          {form.days.map((d, i) => <PreviewDay key={i} n={i + 1} day={d} block={blocks[i]} t={t} />)}
        </ol>
        <div style={{ display: "grid", gap: 12, gridTemplateColumns: phone ? "minmax(0,1fr)" : "repeat(2, minmax(0, 1fr))" }}>
          <ListBlock title={t("pvIncluded")} items={form.inclusions} />
          <ListBlock title={t("pvExcluded")} items={form.exclusions} />
        </div>
      </article>
    </div>
  );
}

/** "Meals: Lunch, Dinner · Overnight: Lodge" under a preview day. */
function dayFacts(day: FactsForm["days"][number], t: T): string {
  const parts: string[] = [];
  if (day.meals.length) {
    parts.push(`${t("pvMeals")}: ${MEALS.filter((m) => day.meals.includes(m)).map((m) => t(`meal_${m}` as const)).join(", ")}`);
  }
  if (day.overnight) parts.push(`${t("pvOvernight")}: ${day.overnight}`);
  return parts.join(" · ");
}

function PreviewDay({ n, day, block, t }: {
  n: number; day: FactsForm["days"][number]; block: { title: string; body: string } | undefined; t: T;
}) {
  const facts = dayFacts(day, t);
  return (
    <li style={{ color: K.ink }}>
      <strong>{t("day", n)}: {block?.title || day.title}</strong>
      <div style={{ fontSize: 13, whiteSpace: "pre-wrap" }}>{block?.body || day.description}</div>
      {facts && <div style={{ fontSize: 12, color: K.muted }}>{facts}</div>}
    </li>
  );
}

function ListBlock({ title, items }: { title: string; items: string[] }) {
  return (
    <div>
      <h3 style={{ fontSize: 14, margin: "0 0 6px", color: K.ink }}>{title}</h3>
      <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13, color: K.ink }}>
        {items.map((x) => <li key={x}>{x}</li>)}
      </ul>
    </div>
  );
}

interface Revision {
  revision_num: number; source: string; fields: string[]; actor: string; reason: string | null; created_at: string;
}

export function History({ tourId }: { tourId: string }) {
  const t = useT(CW);
  const { formatDateTime } = useFormat();
  const q = useQuery({
    queryKey: ["content-workspace-history", tourId],
    queryFn: () => apiGet<{ items: Revision[] }>(`/api/admin/tours/${encodeURIComponent(tourId)}/raw-revisions`),
    retry: false,
  });
  const items = q.data?.items ?? [];
  if (!q.isLoading && items.length === 0) return <p style={{ color: K.muted, fontSize: 13 }}>{t("historyEmpty")}</p>;
  return (
    <ul data-testid="cw-history" style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 10 }}>
      {items.map((r) => (
        <li key={r.revision_num} style={{ borderBottom: `1px solid ${K.line}`, paddingBottom: 8, fontSize: 13 }}>
          <div style={{ fontWeight: 600, color: K.ink }}>{t("historyRow", r.revision_num, r.actor)}</div>
          <div style={{ color: K.muted }}>{formatDateTime(r.created_at)}</div>
          <div style={{ color: K.ink3 }}>{t("fieldsChanged")}: {r.fields.join(", ")}</div>
          {r.reason && <div style={{ color: K.ink3 }}>{r.reason}</div>}
        </li>
      ))}
    </ul>
  );
}
