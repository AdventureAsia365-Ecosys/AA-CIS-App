"use client";
// app/admin/data-erasure/SubjectForm.tsx — AA-839: pick the subject and start a DRY RUN.
//
// Traveller: the address is hashed in the browser (sha256 of the trimmed, lowercased address, Web Crypto) and the
// input is cleared at once; only the hash is sent. The address is never put in the URL, a query key or storage.
// Tenant: the shared admin TenantSelect (AA-919 S254) offering only offboarded tenants (GET
// /admin/tenants?include_inactive=true), or a pasted tenant id ("Another tenant id…").

import { Play } from "lucide-react";
import { useCallback, useMemo, useState } from "react";
import { Button, K, Tabs, mono, useFormat, useT } from "../../_kit";
import {
  emailLooksValid, emailSha256, hoursSince, tenantIdProblem, type Subject, type TenantOption,
} from "@/lib/dataErasure";
import { tenantOptionText, type TenantRef } from "@/lib/tenants";
import { TenantSelect } from "../_components/TenantSelect";
import type { SendResult } from "./ApplyPanel";
import { Notice, Section, hintStyle, inputStyle, labelStyle, useErasureWords } from "./erasureUi";
import { ER } from "./messages";

const OTHER = "__other__";
const isOffboarded = (t: TenantRef) => t.status === "offboarded";

export default function SubjectForm({ tenants, tenantsError, retentionDays, now, send, onStarted }: {
  tenants: TenantOption[] | undefined;
  tenantsError: string | null;
  retentionDays: number | undefined;
  now: number;
  send: (body: Record<string, unknown>) => Promise<SendResult>;
  /** jobId of the new DRY RUN; hash only for a traveller (kept in memory by the page for the verification run). */
  onStarted: (jobId: string, travellerHash: string | null) => void;
}) {
  const t = useT(ER);
  const w = useErasureWords();
  const { formatDateTime } = useFormat();
  const otherOption = useMemo(() => [{ value: OTHER, label: t("anotherTenant") }] as const, [t]);
  const [subject, setSubject] = useState<Subject>("tenant");
  const [picked, setPicked] = useState<string>("");
  const [pasted, setPasted] = useState("");
  const [email, setEmail] = useState("");
  const [lastHash, setLastHash] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const offboarded = (tenants ?? []).filter((t) => t.status === "offboarded");
  const tenantId = picked === OTHER || offboarded.length === 0 ? pasted.trim().toLowerCase() : picked;
  const tenantProblem = subject === "tenant" ? tenantIdProblem(tenantId, w) : null;
  const pickedRow = offboarded.find((t) => t.tenant_id === tenantId);
  const days = pickedRow ? hoursSince(pickedRow.cancelled_at, now) : null;
  const optionText = useCallback(
    (r: TenantRef) => `${tenantOptionText(r)}${r.cancelled_at ? ` ${formatDateTime(r.cancelled_at)}` : ""}`, [formatDateTime],
  );
  const emailOk = emailLooksValid(email);
  const canRun = !busy && (subject === "tenant" ? !tenantProblem : emailOk);

  const run = async () => {
    if (!canRun) return;
    setBusy(true);
    setError(null);
    let body: Record<string, unknown>;
    let hash: string | null = null;
    if (subject === "tenant") {
      body = { subject: "tenant", tenant_id: tenantId };
    } else {
      try {
        hash = await emailSha256(email);
      } catch {
        setBusy(false);
        setError(t("noWebCrypto"));
        return;
      }
      setEmail(""); // the address leaves the page state as soon as it is hashed
      setLastHash(hash);
      body = { subject: "traveller", email_sha256: hash };
    }
    const out = await send(body);
    setBusy(false);
    if (!out.ok) {
      setError(out.message);
      return;
    }
    onStarted(String(out.data.job_id), hash);
  };

  return (
    <Section title={t("subjectTitle")} testId="erasure-subject">
      <div style={{ marginBottom: 12 }}>
        <Tabs
          tabs={[{ key: "tenant", label: t("tabTenant") }, { key: "traveller", label: t("tabTraveller") }]}
          active={subject}
          onChange={(k) => { setSubject(k as Subject); setError(null); }}
        />
      </div>
      {subject === "tenant" ? (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))", gap: 12 }}>
          {offboarded.length > 0 && (
            <TenantSelect label={t("offboardedTenant")} includeInactive offer={isOffboarded} optionText={optionText}
              extraOptions={otherOption} value={picked} onChange={setPicked} testId="erasure-tenant-select" />
          )}
          {(picked === OTHER || offboarded.length === 0) && (
            <label style={labelStyle}>
              {t("tenantId")}
              <input
                data-testid="erasure-tenant-id"
                value={pasted}
                onChange={(e) => setPasted(e.target.value)}
                placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
                autoComplete="off"
                spellCheck={false}
                style={{ ...inputStyle, fontFamily: mono }}
              />
              <span style={hintStyle}>
                {offboarded.length === 0 && !tenantsError ? t("noneOffboarded") : ""}
                {t("notOffboardedHint")}
              </span>
            </label>
          )}
          {pickedRow && days !== null && retentionDays !== undefined && (
            <div style={{ fontSize: 12.5, color: K.body, alignSelf: "end" }}>
              {t("offboardedAgo", Math.floor(days / 24), retentionDays)}
              {days / 24 < retentionDays ? t("retentionWaits") : t("retentionPassed")}
            </div>
          )}
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <label style={{ ...labelStyle, maxWidth: 420 }}>
            {t("travellerEmail")}
            <input
              data-testid="erasure-email"
              type="email"
              name="erasure-subject-address"
              autoComplete="off"
              spellCheck={false}
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") void run(); }}
              style={inputStyle}
            />
            <span style={hintStyle}>
              {t("emailHint")}
            </span>
          </label>
          {lastHash && (
            <div style={{ fontSize: 12.5, color: K.muted }}>
              {t("lastHash")} <span style={{ fontFamily: mono }}>{lastHash.slice(0, 12)}…</span>
            </div>
          )}
        </div>
      )}
      {tenantsError && subject === "tenant" && (
        <div style={{ marginTop: 10 }}><Notice tone="warning">{t("tenantsNotLoaded", tenantsError)}</Notice></div>
      )}
      {error && <div style={{ marginTop: 12 }}><Notice tone="danger" testId="erasure-run-error">{error}</Notice></div>}
      <div style={{ marginTop: 14, display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        <Button variant="primary" onClick={run} disabled={!canRun}>
          <Play size={13} /> {busy ? t("starting") : t("runDry")}
        </Button>
        <span style={{ fontSize: 12.5, color: K.muted }}>{t("readOnly")}</span>
      </div>
    </Section>
  );
}
