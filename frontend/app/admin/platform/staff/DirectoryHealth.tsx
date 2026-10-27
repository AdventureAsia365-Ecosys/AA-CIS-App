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
// S253 — Dev only: the E2E test accounts root ADR 0006a allows (A10, A10a, A10b) come in `known_e2e_exceptions`, each
// with its ADR clause; they are listed in their own block and are not counted in "to check". An E2E address with any
// other group set stays a finding.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { Badge, Button, K, RADIUS, apiGet, apiSend, formatDateTime, sans, type Tone } from "../../../_kit";

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

const REASON: Record<string, string> = {
  no_row: "no staff account",
  row_inactive: "account is inactive but sign-in is still enabled",
  row_bound_to_other_sub: "account is linked to an older sign-in identity",
  unknown_sub: "sign-in identity no longer exists",
  other_issuer: "linked to another sign-in pool",
};

const KIND: Record<string, string> = {
  cis_role: "CIS role",
  booking_role: "Booking role",
  cognito_disabled: "sign-in disabled",
  email: "email",
};

const EXEMPT: Record<string, string> = {
  cognito_without_row: "no staff account here, by design",
};

function knownLine(k: KnownE2E): string {
  const where = k.exempt_from.length
    ? k.exempt_from.map((c) => EXEMPT[c] ?? c).join(", ")
    : `staff account ${k.row_username ?? "?"}, checked like any account`;
  const state = k.enabled ? "" : ", sign-in disabled";
  return `${k.groups.join(", ") || "no groups"} (${where}${state})`;
}

function who(f: Finding): string {
  return f.email || f.username || f.row_username || "unknown";
}

function show(v: Finding["cognito"]): string {
  if (Array.isArray(v)) return v.join(", ");
  return v ?? "none";
}

function findingLines(r: Report): string[] {
  const lines: string[] = [];
  for (const f of r.cognito_without_row) lines.push(`${who(f)}: ${REASON[f.reason ?? ""] ?? f.reason}`);
  for (const f of r.rows_unknown_to_cognito) lines.push(`${who(f)}: ${REASON[f.reason ?? ""] ?? f.reason}`);
  for (const f of r.group_drift) {
    const label = KIND[f.kind ?? ""] ?? f.kind;
    if (f.kind === "cognito_disabled") lines.push(`${who(f)}: active account, ${label}`);
    else if (f.kind === "email") lines.push(`${f.username ?? who(f)}: email is ${f.directory ?? "none"} here, ${show(f.cognito)} in sign-in`);
    else lines.push(`${who(f)}: ${label} is ${f.directory ?? "none"} here, ${show(f.cognito)} in sign-in groups`);
  }
  for (const f of r.status_findings) lines.push(`${who(f)}: sign-in status ${f.status}`);
  for (const i of r.breakglass.issues) lines.push(`Break-glass (${r.breakglass.email}): ${i}`);
  for (const g of r.groups_missing) lines.push(`Sign-in group ${g} is missing`);
  return lines;
}

export default function DirectoryHealth() {
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
  const lines = report ? findingLines(report) : [];
  const pending = report?.pending_first_sign_in ?? [];
  const known = report?.known_e2e_exceptions ?? [];
  const live = data?.readiness_now ?? null;
  const readiness: Readiness | null = live ?? report?.readiness ?? null;

  let tone: Tone = "neutral";
  let label = "No report yet";
  if (report) {
    tone = report.drift_total ? "warning" : "success";
    label = report.drift_total ? `${report.drift_total} to check` : "In sync";
  }

  return (
    <section data-testid="directory-health" style={{
      background: K.card, border: `1px solid ${K.line}`, borderRadius: RADIUS.lg, padding: "12px 14px",
      marginBottom: 16, fontFamily: sans, fontSize: 13, color: K.ink,
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <span style={{ fontWeight: 600, fontSize: 14 }}>Directory health</span>
        <Badge tone={tone}>{label}</Badge>
        {readiness && (
          <Badge tone={readiness.ready ? "success" : "info"} dot={false}>
            {readiness.ready ? "Ready for single sign-on" : "Not ready for single sign-on"}
          </Badge>
        )}
        <span style={{ flex: 1 }} />
        <Button variant="secondary" size="sm" onClick={() => run.mutate()} disabled={running}
          title="Compare the sign-in accounts with the staff list now (read-only)">
          <RefreshCw size={13} /> {running ? "Checking…" : "Check now"}
        </Button>
      </div>

      <div style={{ color: K.muted, fontSize: 12, marginTop: 6 }}>
        {data?.latest
          ? `Sign-in accounts last compared ${formatDateTime(data.latest.finished_at)}${report?.trigger === "schedule" ? " (daily check)" : ""}; the findings below are as of then.`
          : "Compares sign-in accounts with this list once a day. Read-only."}
      </div>

      {health.isError && (
        <div role="alert" style={{ color: K.danger, marginTop: 8 }}>
          {(health.error as Error)?.message || "Could not load the directory health report"}
        </div>
      )}
      {run.isError && (
        <div role="alert" style={{ color: K.danger, marginTop: 8 }}>
          {(run.error as Error)?.message || "Could not start the check"}
        </div>
      )}
      {data?.last_failed && (
        <div role="alert" style={{ color: K.danger, marginTop: 8, overflowWrap: "anywhere" }}>
          Last check failed {formatDateTime(data.last_failed.finished_at)}: {data.last_failed.error || "unknown error"}
        </div>
      )}

      {(report || readiness) && (
        <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 10 }}>
          {readiness && (
          <div data-testid="directory-readiness" style={{ lineHeight: 1.5 }}>
            <span style={{ fontWeight: 500 }}>Single sign-on readiness: </span>
            {readiness.active_rows - readiness.missing_cognito_sub.length} of{" "}
            {readiness.active_rows} active accounts have signed in with their work email
            <span style={{ color: K.muted }}>
              {live ? ` (as of ${formatDateTime(live.as_of)}, from the staff list).` : " (as of the last check)."}
            </span>
            {readiness.missing_email.length > 0 && (
              <div style={{ color: K.muted, overflowWrap: "anywhere" }}>
                No work email: {readiness.missing_email.join(", ")}
              </div>
            )}
            {readiness.missing_cognito_sub.length > 0 && (
              <div style={{ color: K.muted, overflowWrap: "anywhere" }}>
                Not signed in yet: {readiness.missing_cognito_sub.join(", ")}
              </div>
            )}
            {pending.length > 0 && (
              <div data-testid="directory-pending" style={{ color: K.muted, overflowWrap: "anywhere" }}>
                No password set (fine with email-code sign-in): {pending.map(who).join(", ")}
              </div>
            )}
          </div>
          )}
          {known.length > 0 && (
            <div data-testid="directory-e2e-exceptions" style={{ lineHeight: 1.5 }}>
              <span style={{ fontWeight: 500 }}>Known E2E test accounts: </span>
              <span style={{ color: K.muted }}>
                allowed on Dev by the identity ADR, so not counted in &ldquo;to check&rdquo;. Any other sign-in group on
                these addresses is still a finding.
              </span>
              <ul style={{ margin: "4px 0 0", paddingLeft: 18, overflowWrap: "anywhere" }}>
                {known.map((k) => (
                  <li key={k.email ?? k.username}>
                    <Badge tone="info" dot={false}>{k.clause}</Badge>{" "}
                    {k.email ?? k.username}: {k.reason}{" "}
                    <span style={{ color: K.muted }}>{knownLine(k)}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {lines.length > 0 && (
            <details data-testid="directory-findings">
              <summary style={{ cursor: "pointer", fontWeight: 500 }}>
                {lines.length} finding{lines.length === 1 ? "" : "s"}
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
