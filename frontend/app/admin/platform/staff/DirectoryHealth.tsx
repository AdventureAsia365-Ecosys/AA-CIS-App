"use client";
// app/admin/platform/staff/DirectoryHealth.tsx
// AA-816 (S242 C7) — "Directory health" panel on Platform › Staff: the latest staff directory reconcile
// report (job staff_directory_reconcile, daily + on demand). Report only: it compares the WORKFORCE pool
// (Cognito sign-in) with the staff rows and shows drift and cutover readiness; nothing here changes
// Cognito or the directory.
//
// API: GET /api/admin/platform/staff-directory/health and POST …/reconcile (backend
// api/routers/admin_platform.py, staff.read = admin; BFF API_READ_RULES "platform").

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
  readiness: {
    ready: boolean;
    active_rows: number;
    missing_email: string[];
    missing_cognito_sub: string[];
  };
  breakglass: { email: string; present: boolean; issues: string[] };
  groups_missing: string[];
}

interface JobRef {
  job_id: string;
  status: string;
  created_at: string | null;
  finished_at: string | null;
}

interface HealthResp {
  latest: (JobRef & { result: Report }) | null;
  active: JobRef | null;
  last_failed: (JobRef & { error: string | null }) | null;
}

const HEALTH_KEY = ["platform-staff-directory-health"] as const;

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
};

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
    lines.push(f.kind === "cognito_disabled"
      ? `${who(f)}: active account, ${label}`
      : `${who(f)}: ${label} is ${f.directory ?? "none"} here, ${show(f.cognito)} in sign-in groups`);
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
        {report && (
          <Badge tone={report.readiness.ready ? "success" : "info"} dot={false}>
            {report.readiness.ready ? "Ready for single sign-on" : "Not ready for single sign-on"}
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
          ? `Last checked ${formatDateTime(data.latest.finished_at)}${report?.trigger === "schedule" ? " (daily check)" : ""}.`
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

      {report && (
        <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 10 }}>
          <div data-testid="directory-readiness" style={{ lineHeight: 1.5 }}>
            <span style={{ fontWeight: 500 }}>Single sign-on readiness: </span>
            {report.readiness.active_rows - report.readiness.missing_cognito_sub.length} of{" "}
            {report.readiness.active_rows} active accounts have signed in with their work email.
            {report.readiness.missing_email.length > 0 && (
              <div style={{ color: K.muted, overflowWrap: "anywhere" }}>
                No work email: {report.readiness.missing_email.join(", ")}
              </div>
            )}
            {report.readiness.missing_cognito_sub.length > 0 && (
              <div style={{ color: K.muted, overflowWrap: "anywhere" }}>
                Not signed in yet: {report.readiness.missing_cognito_sub.join(", ")}
              </div>
            )}
          </div>
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
