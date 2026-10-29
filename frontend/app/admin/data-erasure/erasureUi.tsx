"use client";
// app/admin/data-erasure/erasureUi.tsx — AA-839: small layout pieces of the Data erasure page (kit tokens only).

import { AlertTriangle, Copy } from "lucide-react";
import { useState } from "react";
import { Badge, Button, K, RADIUS, mono, sans, useLang, useT } from "../../_kit";
import { ERASURE_WORDS_MESSAGES, ACTION_HELP_MESSAGES } from "@/lib/dataErasure.messages";
import { actionTone, orderedTotals, type ErasureWords } from "@/lib/dataErasure";
import { ER } from "./messages";

/** The lib/dataErasure.ts helper words in the UI language (AA-994). */
export function useErasureWords(): ErasureWords {
  return ERASURE_WORDS_MESSAGES[useLang()];
}

function useActionHelp(): Record<string, string> {
  return ACTION_HELP_MESSAGES[useLang()];
}

export function Section({ title, children, right, testId }: {
  title: React.ReactNode;
  children: React.ReactNode;
  right?: React.ReactNode;
  testId?: string;
}) {
  return (
    <section
      data-testid={testId}
      style={{
        background: K.card, border: `1px solid ${K.line}`, borderRadius: RADIUS.lg, padding: "16px 18px",
        marginBottom: 16, minWidth: 0, fontFamily: sans,
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8, flexWrap: "wrap", marginBottom: 12 }}>
        <h2 style={{ margin: 0, fontSize: 14, fontWeight: 700, color: K.ink }}>{title}</h2>
        {right}
      </div>
      {children}
    </section>
  );
}

export function Notice({ tone, children, testId }: {
  tone: "warning" | "info" | "danger" | "success";
  children: React.ReactNode;
  testId?: string;
}) {
  const color = tone === "danger" ? K.danger : tone === "warning" ? K.amber : tone === "success" ? K.success : K.info;
  const bg = tone === "danger" ? K.dangerSoft : tone === "warning" ? K.amberSoft : tone === "success" ? K.successSoft : K.infoSoft;
  return (
    <div
      data-testid={testId}
      role={tone === "danger" || tone === "warning" ? "alert" : "status"}
      style={{
        display: "flex", gap: 10, alignItems: "flex-start", padding: "10px 14px", borderRadius: RADIUS.md,
        background: bg, color, fontSize: 13, lineHeight: 1.5, fontFamily: sans, marginBottom: 12,
      }}
    >
      <AlertTriangle size={16} style={{ flex: "none", marginTop: 2 }} />
      <div style={{ color: K.ink, minWidth: 0, overflowWrap: "anywhere" }}>{children}</div>
    </div>
  );
}

/** A grid of small label / value tiles that wraps to one column on a phone. */
export function Facts({ items }: { items: { label: string; value: React.ReactNode }[] }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 10 }}>
      {items.map((it) => (
        <div key={it.label} style={{ border: `1px solid ${K.line}`, borderRadius: RADIUS.md, padding: "10px 12px", minWidth: 0 }}>
          <div style={{ fontSize: 11.5, color: K.muted, textTransform: "uppercase", letterSpacing: "0.04em", fontWeight: 600 }}>
            {it.label}
          </div>
          <div style={{ fontSize: 15, color: K.ink, marginTop: 4, fontWeight: 600, overflowWrap: "anywhere" }}>{it.value}</div>
        </div>
      ))}
    </div>
  );
}

export function ActionBadge({ action }: { action: string }) {
  const help = useActionHelp();
  const base = action.split(":", 1)[0];
  return (
    <span title={help[base] ?? action}>
      <Badge tone={actionTone(action)} dot={false}>{action}</Badge>
    </span>
  );
}

/** Totals per action as badges with counts. */
export function TotalsRow({ totals, testId }: { totals: Record<string, number> | undefined; testId?: string }) {
  const t = useT(ER);
  const help = useActionHelp();
  const entries = orderedTotals(totals);
  if (!entries.length) return <span style={{ fontSize: 13, color: K.muted }}>{t("noRows")}</span>;
  return (
    <div data-testid={testId} style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
      {entries.map(([k, n]) => (
        <span key={k} style={{ display: "inline-flex", alignItems: "center", gap: 6 }} title={help[k] ?? k}>
          <Badge tone={actionTone(k)} dot={false}>{k}</Badge>
          <span style={{ fontFamily: mono, fontSize: 13, color: K.ink }}>{n.toLocaleString("en-US")}</span>
        </span>
      ))}
    </div>
  );
}

export function Fingerprint({ value }: { value: string }) {
  const t = useT(ER);
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard refused: the value stays selectable */
    }
  };
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", minWidth: 0 }}>
      <code
        data-testid="erasure-fingerprint"
        style={{ fontFamily: mono, fontSize: 12.5, color: K.ink, overflowWrap: "anywhere", wordBreak: "break-all", minWidth: 0 }}
      >
        <strong>{value.slice(0, 8)}</strong>{value.slice(8)}
      </code>
      <Button size="sm" variant="ghost" onClick={copy} ariaLabel={t("copyFingerprint")}>
        <Copy size={12} /> {copied ? t("copied") : t("copy")}
      </Button>
    </div>
  );
}

export const inputStyle: React.CSSProperties = {
  padding: "8px 10px", borderRadius: RADIUS.md, border: `1px solid ${K.line}`, background: K.card, color: K.ink,
  fontSize: 13, fontFamily: sans, fontWeight: 400, minWidth: 0, width: "100%", boxSizing: "border-box",
};

export const labelStyle: React.CSSProperties = {
  display: "flex", flexDirection: "column", gap: 5, fontSize: 12.5, fontWeight: 600, color: K.ink3, minWidth: 0,
};

export const hintStyle: React.CSSProperties = { fontSize: 12, fontWeight: 400, color: K.muted, lineHeight: 1.45 };
