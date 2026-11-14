"use client";
// app/admin/ai-gateway/capEditorParts.tsx — S280 (AA-1096): the shared parts of the two cap editors of the Budgets tab
// (a provider / job cap and a central LLM cap, BudgetsTab.tsx).
//
// A tighten-only role (no settings.write, S279: the API refuses any loosening with a 403) sees the current value as its
// limit beside each field, the "At the cap" select locked on "Stops at the cap" when it may not soften it, and what it
// may not loosen before Save; the API's refusal reads in the UI language. The cap's target (a stage, an app, a job
// kind) is offered from the call sites the read already returns, and a name that matches none is flagged: a typo
// would create a cap that guards nothing. Kit only (Field, Input, Select).

import { useMemo } from "react";
import { Field, Input, K, Select, useT } from "../../_kit";
import { CALL_APPS, isReadOnlyRefusal, usd, type CallSiteRow } from "@/lib/aiGateway";
import { SPEND_CAP_KINDS, isTightenOnlyRefusal, loosenedFields } from "@/lib/capTighten";
export { anyOf } from "@/lib/capTighten";
import { BUDGETS } from "./budgets.messages";

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;
const alertStyle = { fontSize: 12, color: K.danger } as const;

/** The providers whose job caps guard LLM calls: a job kind there should be one an LLM call site lists. */
export const LLM_PROVIDERS = new Set(["bedrock", "openai", "anthropic", "azure_openai", "llm"]);

/** The names a cap's target can take, from the read's call sites: apps, stages and the job kinds that call them. */
export function knownTargets(sites: readonly CallSiteRow[]): { app: string[]; stage: string[]; job: string[] } {
  const jobs = new Set<string>();
  for (const s of sites) for (const k of s.job_kinds) jobs.add(k);
  return { app: [...CALL_APPS], stage: sites.map((s) => s.stage).sort(), job: [...jobs].sort() };
}

/** What a tighten-only role may not do with this cap: the fields the draft loosens, and whether the hard stop is locked
 *  on (a new cap, or a cap that stops now). Nothing for a role that may loosen. */
export function tightenState(tightenOnly: boolean, row: { hard_stop: boolean } | null, body: object | null | undefined): {
  worse: string[]; stopLocked: boolean;
} {
  if (!tightenOnly) return { worse: [], stopLocked: false };
  return { worse: body ? loosenedFields(row, body, SPEND_CAP_KINDS) : [], stopLocked: row ? row.hard_stop : true };
}

/** The name a scope target is checked against: the known names of its kind ([] when the kind has no list). */
export function targetOptions(kind: string, known: { app: string[]; stage: string[]; job: string[] }): string[] {
  if (kind === "app" || kind === "stage" || kind === "job") return known[kind];
  return [];
}

/** True when a typed target is not one of the known names (only when there is a list to compare with). */
export function unknownName(target: string, options: readonly string[]): boolean {
  return !!target && options.length > 0 && !options.includes(target);
}

/** The provider cap's scope check: the known job kinds and whether a typed "job:<kind>" matches none of them. */
export function useJobKinds(sites: readonly CallSiteRow[], provider: string, scope: string): {
  jobs: string[]; unknownKind: boolean;
} {
  const jobs = useMemo(() => knownTargets(sites).job, [sites]);
  const s = scope.trim();
  const kind = s.startsWith("job:") ? s.slice(4) : "";
  return { jobs, unknownKind: LLM_PROVIDERS.has(provider) && unknownName(kind, jobs) };
}

/** The editors' words for a tighten-only role: the fields it may not loosen, and a refused write in the UI language. */
export function useTighten() {
  const t = useT(BUDGETS);
  const names: Record<string, string> = {
    per_run_usd: t("fieldPerRunName"), per_day_usd: t("fieldPerDayName"), per_month_usd: t("fieldPerMonthName"),
    hard_stop: t("fieldStopName"), alert_pct: t("fieldAlertName"),
  };
  const worseText = (fields: string[]) => t("tightenWorse", fields.map((f) => names[f] ?? f).join(", "));
  const refusal = (e: unknown) => {
    if (isReadOnlyRefusal(e)) return t("readOnlySession");
    if (isTightenOnlyRefusal(e)) return t("tightenOnlyRefused");
    return e instanceof Error ? e.message : String(e);
  };
  return { worseText, refusal };
}

/** A money cap field: empty = no cap. A tighten-only role reads the current value as its limit. */
export function CapField({ label, hint, now, tightenOnly, error, value, onChange, disabled, testId }: {
  label: string; hint: string; now?: number | null; tightenOnly: boolean; error?: string; value: string;
  onChange: (v: string) => void; disabled: boolean; testId?: string;
}) {
  const t = useT(BUDGETS);
  const shown = tightenOnly && now != null ? t("tightenCapHint", usd(now, 2)) : hint;
  return (
    <Field label={label} hint={shown} error={error}>
      <Input inputMode="decimal" value={value} placeholder={t("noCap")} disabled={disabled}
        onChange={(e) => onChange(e.target.value)} data-testid={testId} />
    </Field>
  );
}

/** "At the cap": stops or alerts only. Locked on "stops" for a tighten-only role that may not soften it. */
export function AtCapField({ stop, locked, hint, disabled, onChange }: {
  stop: boolean; locked: boolean; hint?: string; disabled: boolean; onChange: (stop: boolean) => void;
}) {
  const t = useT(BUDGETS);
  return (
    <Field label={t("fieldAtCap")} hint={hint}>
      <Select value={stop ? "stop" : "alert"} disabled={disabled || locked}
        onChange={(e) => onChange(e.target.value === "stop")}
        options={[{ value: "stop", label: t("stopsAtCap") }, { value: "alert", label: t("alertOnly") }]} />
    </Field>
  );
}

/** "Alert at %": a tighten-only role reads the current value (it may lower it only). */
export function AlertPctField({ value, now, tightenOnly, error, disabled, onChange }: {
  value: string; now?: number | null; tightenOnly: boolean; error?: string; disabled: boolean;
  onChange: (v: string) => void;
}) {
  const t = useT(BUDGETS);
  const hint = tightenOnly && now != null ? t("tightenAlertHint", String(now)) : undefined;
  return (
    <Field label={t("fieldAlertPct")} error={error} hint={hint}>
      <Input inputMode="numeric" value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)} />
    </Field>
  );
}

/** The tighten-only note at the top of an editor (nothing for other roles). */
export function TightenNote({ show, testId }: { show: boolean; testId?: string }) {
  const t = useT(BUDGETS);
  if (!show) return null;
  return <p style={{ ...small, margin: 0 }} data-testid={testId}>{t("tightenOnlyNote")}</p>;
}

/** The problems under an editor's fields, each as an alert line (empty ones are skipped). */
export function AlertLines({ lines }: { lines: (string | null | undefined | false)[] }) {
  return <>{lines.filter(Boolean).map((l) => <span key={String(l)} role="alert" style={alertStyle}>{l}</span>)}</>;
}

/** The suggestions of a name field (a datalist for an Input's `list`). */
export function NameOptions({ id, options }: { id: string; options: readonly string[] }) {
  return <datalist id={id}>{options.map((o) => <option key={o} value={o} />)}</datalist>;
}
