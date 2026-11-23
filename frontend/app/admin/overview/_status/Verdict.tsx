"use client";
// app/admin/overview/_status/Verdict.tsx — AA-1140: the one verdict at the top ("All systems normal" / "N things need
// you" / unknown when the main sources did not answer) and the Needs attention list with its severity / status
// filters, sort and the resolved items of the range. The verdict and its rows come from the API (ops_status/verdict.py).

import Link from "next/link";
import { Badge, K, Section, TONE, useFormat, type Tone } from "../../../_kit";
import {
  APP_LABEL, ITEM_STATES, SEVERITIES, countBy, filterAttention, levelTone, parseSort, severityRank, sortRows,
  withSort, type AttentionItem, type Verdict as V, type View,
} from "@/lib/opsStatus";
import { ChipFilter, SortSelect, rowWrap, small, sortOptions, useST, type ST } from "./ui";

const SORT_DEFAULT = "severity:desc";

function headline(v: V, t: ST): { text: string; tone: Tone } {
  if (v.status === "attention") return { text: t("needYou", v.open), tone: "danger" };
  if (v.status === "unknown") return { text: t("verdictUnknown"), tone: "neutral" };
  return { text: t("allNormal"), tone: "success" };
}

function gateText(gate: V["chips"]["deploy_gate"], t: ST): string {
  if (gate === "open") return t("gateOpen");
  return gate === "closed" ? t("gateClosed") : t("gateUnknown");
}

function sortValue(col: string) {
  return (i: AttentionItem): string | number | null => {
    if (col === "since") return i.since;
    if (col === "app") return APP_LABEL[i.app] ?? i.app;
    return severityRank(i.severity) * 10 + (i.status === "open" ? 1 : 0);
  };
}

function ItemRow({ item }: { item: AttentionItem }) {
  const t = useST();
  const { formatDateTime } = useFormat();
  return (
    <div role="listitem" data-testid={`status-att-${item.id}`}
      style={{ display: "grid", gap: 4, padding: "10px 12px", border: `1px solid ${K.line}`, borderRadius: 8,
        background: K.card, minWidth: 0 }}>
      <div style={rowWrap}>
        <Badge tone={levelTone(item.severity) as Tone}>{item.severity.toUpperCase()}</Badge>
        <Badge tone="neutral" dot={false}>{APP_LABEL[item.app] ?? item.app}</Badge>
        <span style={{ fontWeight: 600, color: K.ink, overflowWrap: "anywhere", minWidth: 0 }}>{item.title}</span>
        {item.status !== "open" && <Badge tone="info" dot={false}>{t("stAcked")}</Badge>}
      </div>
      {item.detail && <div style={{ ...small, overflowWrap: "anywhere" }}>{item.detail}</div>}
      <div style={{ ...rowWrap, ...small }}>
        {item.since && <span>{t("since", formatDateTime(item.since))}</span>}
        {item.count > 1 && <span>{t("times", item.count)}</span>}
        {item.source && <span>· {item.source}</span>}
        {item.links.map((l) => (
          <Link key={l.href} href={l.href} style={{ color: K.accentDeep, fontWeight: 600 }}>{l.label}</Link>
        ))}
      </div>
    </div>
  );
}

function Resolved({ items }: { items: V["resolved"] }) {
  const t = useST();
  const { formatDateTime } = useFormat();
  if (items.length === 0) return null;
  return (
    <details data-testid="status-resolved">
      <summary style={{ ...small, cursor: "pointer" }}>{t("resolved", items.length)}</summary>
      <ul style={{ margin: "6px 0 0", paddingLeft: 18, ...small }}>
        {items.map((r) => (
          <li key={r.id}>{r.text} · {formatDateTime(r.t)} – {formatDateTime(r.t2)}</li>
        ))}
      </ul>
    </details>
  );
}

function Chips({ v, t }: { v: V; t: ST }) {
  const gateTone: Tone = v.chips.deploy_gate === "open" ? "success" : "neutral";
  return (
    <div style={rowWrap} data-testid="status-chips">
      <Badge tone={v.chips.alarms_firing ? "danger" : "neutral"}>{t("alarmsFiring", v.chips.alarms_firing)}</Badge>
      <Badge tone={v.chips.open_alerts ? "warning" : "neutral"}>{t("openAlerts", v.chips.open_alerts)}</Badge>
      <Badge tone={v.chips.deploy_gate === "closed" ? "warning" : gateTone}>{gateText(v.chips.deploy_gate, t)}</Badge>
      {v.unknown.length > 0 && <span style={small}>{t("unknownSources", v.unknown.join(", "))}</span>}
    </div>
  );
}

export function VerdictCard({ verdict, view, setView }: {
  verdict: V; view: View; setView: (v: Partial<View>) => void;
}) {
  const t = useST();
  const h = headline(verdict, t);
  const scoped = verdict.attention.filter((i) => filterAttention([i], { apps: view.apps, sev: [], st: [] }).length);
  const shown = filterAttention(scoped, view);
  const sort = parseSort(view.sort.att, SORT_DEFAULT);
  const rows = sortRows(shown, sortValue(sort.col), sort.dir);
  const sev = countBy(scoped, (i) => i.severity);
  const st = countBy(scoped, (i) => i.status);
  return (
    <Section testId="status-verdict" bodyStyle={{ display: "grid", gap: 12 }}>
      <div style={{ ...rowWrap, justifyContent: "space-between" }}>
        <span data-testid="status-verdict-title"
          style={{ fontSize: 20, fontWeight: 700, color: TONE[h.tone].fg }}>{h.text}</span>
        <Chips v={verdict} t={t} />
      </div>
      {verdict.attention.length > 0 && (
        <>
          <div style={rowWrap}>
            <ChipFilter label={t("severity")} testId="status-sev" value={view.sev} onChange={(s) => setView({ sev: s })}
              options={SEVERITIES.map((s) => ({ value: s, label: s.toUpperCase(), count: sev[s] ?? 0 }))} />
            <ChipFilter label={t("status")} testId="status-st" value={view.st} onChange={(s) => setView({ st: s })}
              options={ITEM_STATES.map((s) => ({ value: s, label: s === "open" ? t("stOpen") : t("stAcked"),
                count: st[s] ?? 0 }))} />
            <span style={{ marginLeft: "auto" }}>
              <SortSelect label={t("sortBy")} value={`${sort.col}:${sort.dir}`}
                onChange={(s) => setView({ sort: withSort(view.sort, "att", s === SORT_DEFAULT ? "" : s) })}
                options={sortOptions([{ col: "severity", label: t("sortSeverity") }, { col: "since",
                  label: t("sortSince") }, { col: "app", label: t("sortApp") }])} />
            </span>
          </div>
          <div role="list" style={{ display: "grid", gap: 8 }} data-testid="status-attention">
            {rows.map((i) => <ItemRow key={i.id} item={i} />)}
            {rows.length === 0 && <span style={small}>{t("noAttention")}</span>}
          </div>
        </>
      )}
      <Resolved items={verdict.resolved} />
    </Section>
  );
}
