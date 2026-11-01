"use client";
// app/admin/platform/staff/DirectoryHealth.tsx
// AA-816 (S242 C7) — "Directory health" panel on Platform › Staff: the latest staff directory reconcile
// report (job staff_directory_reconcile, daily + on demand). Report only: it compares the WORKFORCE pool
// (Cognito sign-in) with the staff rows and shows drift and cutover readiness; nothing here changes
// Cognito or the directory.
//
// API: GET /api/admin/platform/staff-directory/health and POST …/reconcile (backend
// api/routers/admin_platform.py, staff.read = admin; BFF API_READ_RULES "platform").
// S243 — readiness is recomputed from the staff rows on every load (`readiness_now`), so it agrees with the
// page header right after a disable or invite; only the sign-in comparison (Cognito drift) is "as of" the last check.
// S253 — Dev only: the E2E test accounts root ADR 0006a allows (A10, A10a, A10b; A10c since AA-1031) come in
// `known_e2e_exceptions`, each with its ADR clause; they are listed in their own block and are not counted in "to
// check". An E2E address with any other group set stays a finding.
// AA-1031 — the CRM roles are compared with the crm-* sign-in groups (finding kind `crm_role`); a report made before
// the database update for CRM roles says `crm_compared: false`, shown as one muted line.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { Badge, Button, K, RADIUS, apiGet, apiSend, sans, useFormat, useT, type Tone } from "../../../_kit";
import type { Translator } from "../../../_kit/core/i18n";
import { HEALTH, HEALTH_KIND, HEALTH_REASON } from "./messages";

interface Finding {
  email?: string | null;
  username?: string | null;
  reason?: string;
  kind?: string;
  status?: string;
  directory?: string | null;
  cognito?: string | string[] | null;
  row_username?: string | null;
}

interface Report {
  checked_at?: string;
  trigger?: string;
  drift_total: number;
  counts: Record<string, number>;
  cognito_without_row: Finding[];
  rows_unknown_to_cognito: Finding[];
  group_drift: Finding[];
  status_findings: Finding[];
  // Created, no password set (FORCE_CHANGE_PASSWORD): normal with email-code sign-in, not drift.
  // Optional: reports stored before this field existed do not have it.
  pending_first_sign_in?: Finding[];
  readiness: {
    ready: boolean;
    active_rows: number;
    missing_email: string[];
    missing_cognito_sub: string[];
  };
  breakglass: { email: string; present: boolean; issues: string[] };
  groups_missing: string[];
  // Dev only, not counted in drift_total. Optional: reports stored before S253 do not have them.
  known_e2e_enabled?: boolean;
  known_e2e_exceptions?: KnownE2E[];
  // AA-1031. Optional: reports stored before it do not have it.
  crm_compared?: boolean;
}

interface KnownE2E {
  email: string | null;
  username: string;
  status: string;
  enabled: boolean;
  groups: string[];
  /** e.g. "ADR 0006a A10a" */
  clause: string;
  reason: string;
  row_username: string | null;
  /** the report categories this account is left out of (e.g. cognito_without_row); empty = checked like any account */
  exempt_from: string[];
}

interface JobRef {
  job_id: string;
  status: string;
  created_at: string | null;
  finished_at: string | null;
}

type Readiness = Report["readiness"];

interface HealthResp {
  /** readiness from the staff rows right now (null when it could not be read: fall back to the report's) */
  readiness_now?: (Readiness & { as_of: string }) | null;
  latest: (JobRef & { result: Report }) | null;
  active: JobRef | null;
  last_failed: (JobRef & { error: string | null }) | null;
}

// Under the "platform-staff" prefix: every staff write (StaffManage invalidates ["platform-staff"]) refetches this
// panel too, so the live readiness follows a disable or invite at once (S243).
const HEALTH_KEY = ["platform-staff", "directory-health"] as const;

/** The health card's words in the UI language: findings, reasons and finding kinds (an unknown code as sent). */
interface Words {
  t: Translator<typeof HEALTH.en>;
  reason: (code: string | undefined) => string;
  kind: (code: string | undefined) => string;
}

function useWords(): Words {
  const t = useT(HEALTH);
  const reasons = useT(HEALTH_REASON) as unknown as (k: string) => string;
  const kinds = useT(HEALTH_KIND) as unknown as (k: string) => string;
  return {
    t,
    reason: (code) => (code && Object.hasOwn(HEALTH_REASON.en, code) ? reasons(code) : code ?? ""),
    kind: (code) => (code && Object.hasOwn(HEALTH_KIND.en, code) ? kinds(code) : code ?? ""),
  };
}

function knownLine(k: KnownE2E, { t }: Words): string {
  const where = k.exempt_from.length
    ? k.exempt_from.map((c) => (c === "cognito_without_row" ? t("exemptNoRow") : c)).join(", ")
    : t("checkedLike", k.row_username ?? "?");
  const state = k.enabled ? "" : t("signInDisabled");
  return `${k.groups.join(", ") || t("noGroups")} (${where}${state})`;
}

function who(f: Finding, unknown = "unknown"): string {
  return f.email || f.username || f.row_username || unknown;
}

function show(v: Finding["cognito"], none: string): string {
  if (Array.isArray(v)) return v.join(", ");
  return v ?? none;
}

function findingLines(r: Report, w: Words): string[] {
  const { t } = w;
  const name = (f: Finding) => who(f, t("unknown"));
  const lines: string[] = [];
  for (const f of r.cognito_without_row) lines.push(`${name(f)}: ${w.reason(f.reason)}`);
  for (const f of r.rows_unknown_to_cognito) lines.push(`${name(f)}: ${w.reason(f.reason)}`);
  for (const f of r.group_drift) {
    const label = w.kind(f.kind);
    if (f.kind === "cognito_disabled") lines.push(t("activeAccount", name(f), label));
    else if (f.kind === "email") {
      lines.push(t("emailDrift", f.username ?? name(f), f.directory ?? t("none"), show(f.cognito, t("none"))));
    } else lines.push(t("groupDrift", name(f), label, f.directory ?? t("none"), show(f.cognito, t("none"))));
  }
  for (const f of r.status_findings) lines.push(t("statusFinding", name(f), f.status ?? ""));
  for (const i of r.breakglass.issues) lines.push(t("breakGlass", r.breakglass.email, i));
  for (const g of r.groups_missing) lines.push(t("groupMissing", g));
  return lines;
}

export default function DirectoryHealth() {
  const words = useWords();
  const { t } = words;
  const { formatDateTime } = useFormat();
  const qc = useQueryClient();
  const health = useQuery({
    queryKey: HEALTH_KEY,
    queryFn: () => apiGet<HealthResp>("/api/admin/platform/staff-directory/health"),
    staleTime: 60_000,
    // While a check is queued or running, look again every 5 s until it finishes.
    refetchInterval: (q) => (q.state.data?.active ? 5_000 : false),
  });
  const run = useMutation({
    mutationFn: () => apiSend<{ job_id: string }>("/api/admin/platform/staff-directory/reconcile", { method: "POST" }),
    onSettled: () => qc.invalidateQueries({ queryKey: HEALTH_KEY }),
  });

  const data = health.data;
  const report = data?.latest?.result ?? null;
  const running = !!data?.active || run.isPending;
  const lines = report ? findingLines(report, words) : [];
  const pending = report?.pending_first_sign_in ?? [];
  const known = report?.known_e2e_exceptions ?? [];
  const live = data?.readiness_now ?? null;
  const readiness: Readiness | null = live ?? report?.readiness ?? null;

  let tone: Tone = "neutral";
  let label = t("noReport");
  if (report) {
    tone = report.drift_total ? "warning" : "success";
    label = report.drift_total ? t("toCheck", report.drift_total) : t("inSync");
  }

  return (
    <section data-testid="directory-health" style={{
      background: K.card, border: `1px solid ${K.line}`, borderRadius: RADIUS.lg, padding: "12px 14px",
      marginBottom: 16, fontFamily: sans, fontSize: 13, color: K.ink,
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <span style={{ fontWeight: 600, fontSize: 14 }}>{t("title")}</span>
        <Badge tone={tone}>{label}</Badge>
        {readiness && (
          <Badge tone={readiness.ready ? "success" : "info"} dot={false}>
            {readiness.ready ? t("ssoReady") : t("ssoNotReady")}
          </Badge>
        )}
        <span style={{ flex: 1 }} />
        <Button variant="secondary" size="sm" onClick={() => run.mutate()} disabled={running}
          title={t("checkTitle")}>
          <RefreshCw size={13} /> {running ? t("checking") : t("checkNow")}
        </Button>
      </div>

      <div style={{ color: K.muted, fontSize: 12, marginTop: 6 }}>
        {data?.latest
          ? `${t("lastCompared", formatDateTime(data.latest.finished_at))}${report?.trigger === "schedule" ? t("daily") : ""}${t("asOfThen")}`
          : t("comparesDaily")}
      </div>

      {health.isError && (
        <div role="alert" style={{ color: K.danger, marginTop: 8 }}>
          {(health.error as Error)?.message || t("loadError")}
        </div>
      )}
      {run.isError && (
        <div role="alert" style={{ color: K.danger, marginTop: 8 }}>
          {(run.error as Error)?.message || t("startError")}
        </div>
      )}
      {data?.last_failed && (
        <div role="alert" style={{ color: K.danger, marginTop: 8, overflowWrap: "anywhere" }}>
          {t("lastFailed", formatDateTime(data.last_failed.finished_at), data.last_failed.error || t("unknownError"))}
        </div>
      )}

      {(report || readiness) && (
        <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 10 }}>
          {readiness && (
          <div data-testid="directory-readiness" style={{ lineHeight: 1.5 }}>
            <span style={{ fontWeight: 500 }}>{t("readiness")}</span>
            {t("signedInOf", readiness.active_rows - readiness.missing_cognito_sub.length, readiness.active_rows)}
            <span style={{ color: K.muted }}>
              {live ? t("asOfList", formatDateTime(live.as_of)) : t("asOfCheck")}
            </span>
            {readiness.missing_email.length > 0 && (
              <div style={{ color: K.muted, overflowWrap: "anywhere" }}>
                {t("noWorkEmail", readiness.missing_email.join(", "))}
              </div>
            )}
            {readiness.missing_cognito_sub.length > 0 && (
              <div style={{ color: K.muted, overflowWrap: "anywhere" }}>
                {t("notSignedIn", readiness.missing_cognito_sub.join(", "))}
              </div>
            )}
            {pending.length > 0 && (
              <div data-testid="directory-pending" style={{ color: K.muted, overflowWrap: "anywhere" }}>
                {t("noPassword", pending.map((f) => who(f, t("unknown"))).join(", "))}
              </div>
            )}
          </div>
          )}
          {report?.crm_compared === false && (
            <div data-testid="directory-crm-not-compared" style={{ color: K.muted }}>{t("crmNotCompared")}</div>
          )}
          {known.length > 0 && (
            <div data-testid="directory-e2e-exceptions" style={{ lineHeight: 1.5 }}>
              <span style={{ fontWeight: 500 }}>{t("e2eTitle")}</span>
              <span style={{ color: K.muted }}>{t("e2eNote")}</span>
              <ul style={{ margin: "4px 0 0", paddingLeft: 18, overflowWrap: "anywhere" }}>
                {known.map((k) => (
                  <li key={k.email ?? k.username}>
                    <Badge tone="info" dot={false}>{k.clause}</Badge>{" "}
                    {k.email ?? k.username}: {k.reason}{" "}
                    <span style={{ color: K.muted }}>{knownLine(k, words)}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {lines.length > 0 && (
            <details data-testid="directory-findings">
              <summary style={{ cursor: "pointer", fontWeight: 500 }}>
                {t("findings", lines.length)}
              </summary>
              <ul style={{ margin: "6px 0 0", paddingLeft: 18, lineHeight: 1.6, overflowWrap: "anywhere" }}>
                {lines.map((l, i) => <li key={i}>{l}</li>)}
              </ul>
            </details>
          )}
        </div>
      )}
    </section>
  );
}
