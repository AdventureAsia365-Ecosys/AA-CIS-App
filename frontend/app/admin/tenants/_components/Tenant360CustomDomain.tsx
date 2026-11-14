"use client";
// app/admin/tenants/_components/Tenant360CustomDomain.tsx — AA-1090 TripPlanner R8: Tenant 360 › Subscription ›
// TripPlanner own domains. Staff only; a tenant never sees it.
//
// GET /api/admin/tenants/{id}/tripplanner-site/domains: the agency's custom hosts (CIS 427) with their status and the
// two DNS records the agency creates (the CNAME of the host to the distribution, the ACM validation CNAME). "Add domain"
// (admin: tenant.write) opens a kit Drawer that POSTs the host; the API refuses a malformed, bare, platform or AWS host
// (422 with the field) and the limits, which are settings (409). "Remove" (ConfirmModal), "Retry" (a failed host) and
// "Retry removal" (a removal the job could not finish: the row stays removing with its reason)
// queue the tripplanner_custom_domain job. While the job works on a host the section refreshes itself. Kit only
// (Section, Drawer, Field, Input, Badge, Button, ConfirmModal, Toast).

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Badge, Button, ConfirmModal, DateText, Drawer, ErrorState, Field, Input, K, Section, mono, useT, useToast,
} from "../../../_kit";
import { PlanApiError, sendJson } from "@/lib/tenantPlans";
import {
  POLL_MS, canAdd, domainPath, domainsKey, domainsPath, hostProblem, normaliseHost, removalStuck, shouldPoll,
  statusTone,
  type CustomDomain, type DomainList,
} from "@/lib/tenantCustomDomain";
import { T360_DOMAIN } from "./customDomain.messages";

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45, margin: 0 } as const;
const code = { fontFamily: mono, fontSize: 12, color: K.ink, overflowWrap: "anywhere", minWidth: 0 } as const;
type Msg = keyof typeof T360_DOMAIN.en;

function errKey(c: string): Msg | null {
  const k = `err_${c}`;
  return k in T360_DOMAIN.en ? (k as Msg) : null;
}

function AddDomainDrawer({ tenantId, onClose }: { tenantId: string; onClose: () => void }) {
  const t = useT(T360_DOMAIN);
  const toast = useToast();
  const qc = useQueryClient();
  const [host, setHost] = useState("");
  const [tried, setTried] = useState(false);
  const [apiError, setApiError] = useState<{ field: boolean; text: string } | null>(null);
  const local = tried ? hostProblem(host) : null;
  const save = useMutation({
    mutationFn: () => sendJson<CustomDomain>(domainsPath(tenantId), "POST", { domain: normaliseHost(host) }),
    onSuccess: () => {
      toast.success(t("added"));
      void qc.invalidateQueries({ queryKey: domainsKey(tenantId) });
      onClose();
    },
    onError: (e) => {
      const c = e instanceof PlanApiError ? e.code : "";
      const key = errKey(c);
      const text = key ? (t(key) as string) : e instanceof Error ? e.message : String(e);
      if (e instanceof PlanApiError && e.status === 403) setApiError({ field: false, text: t("readOnly") });
      else setApiError({ field: c.startsWith("domain_"), text: key ? text : t("failed", text) });
    },
  });
  const fieldError = local ? (t(errKey(local) ?? "err_domain_invalid") as string)
    : apiError?.field ? apiError.text : undefined;

  return (
    <Drawer open onClose={onClose} width={520} title={t("addTitle")}
      footer={
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <Button variant="ghost" onClick={onClose} disabled={save.isPending}>{t("cancel")}</Button>
          <span data-testid="tp-domain-save" style={{ display: "inline-flex" }}>
            <Button variant="primary" disabled={!host.trim() || save.isPending}
              onClick={() => { setTried(true); if (!hostProblem(host)) save.mutate(); }}>
              {save.isPending ? t("saving") : t("save")}
            </Button>
          </span>
        </div>
      }>
      <div data-testid="tp-domain-drawer" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <p style={small}>{t("hint")}</p>
        <Field label={t("fieldDomain")} hint={t("fieldDomainHint")} required error={fieldError}>
          <Input value={host} placeholder="trip.agency-name.com" disabled={save.isPending} maxLength={253}
            inputMode="url" autoComplete="off" spellCheck={false} data-testid="tp-domain-input"
            onChange={(e) => { setHost(e.target.value); setApiError(null); }} />
        </Field>
        {apiError && !apiError.field && (
          <span role="alert" style={{ fontSize: 12, color: K.danger }}>{apiError.text}</span>
        )}
      </div>
    </Drawer>
  );
}

function DnsRecords({ d }: { d: CustomDomain }) {
  const t = useT(T360_DOMAIN);
  if (!d.dns_records.length) return <p style={small}>{t("recordsWaiting")}</p>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8, minWidth: 0 }} data-testid="tp-domain-records">
      <span style={{ ...small, fontWeight: 600 }}>{t("records")}</span>
      {d.dns_records.map((r) => (
        <div key={`${r.purpose}:${r.name}`} style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0,
          padding: "6px 8px", border: `1px solid ${K.line}`, borderRadius: 6 }}>
          <span style={small}>{t(`purpose_${r.purpose}`)}</span>
          <span style={{ display: "flex", gap: 8, flexWrap: "wrap", minWidth: 0 }}>
            <span style={small}>{t("colType")}</span><span style={code}>{r.type}</span>
          </span>
          <span style={{ display: "flex", gap: 8, flexWrap: "wrap", minWidth: 0 }}>
            <span style={small}>{t("colName")}</span><span style={code}>{r.name}</span>
          </span>
          <span style={{ display: "flex", gap: 8, flexWrap: "wrap", minWidth: 0 }}>
            <span style={small}>{t("colValue")}</span><span style={code}>{r.value}</span>
          </span>
        </div>
      ))}
    </div>
  );
}

function useDomainActions(tenantId: string, d: CustomDomain, onRemoved: () => void) {
  const t = useT(T360_DOMAIN);
  const toast = useToast();
  const qc = useQueryClient();
  const refresh = () => void qc.invalidateQueries({ queryKey: domainsKey(tenantId) });
  const failText = (e: unknown) => {
    const key = errKey(e instanceof PlanApiError ? e.code : "");
    return key ? (t(key) as string) : t("failed", e instanceof Error ? e.message : String(e));
  };
  const remove = useMutation({
    mutationFn: () => sendJson<CustomDomain>(domainPath(tenantId, d.domain_id), "DELETE"),
    onSuccess: () => { toast.success(t("removed")); onRemoved(); refresh(); },
    onError: (e) => { if (removalStuck(d)) toast.error(failText(e)); },
  });
  const retry = useMutation({
    mutationFn: () => sendJson<CustomDomain>(`${domainPath(tenantId, d.domain_id)}/retry`, "POST"),
    onSuccess: () => { toast.success(t("retried")); refresh(); },
    onError: (e) => toast.error(failText(e)),
  });
  return { remove, retry, failText };
}

function DomainHeader({ d }: { d: CustomDomain }) {
  const t = useT(T360_DOMAIN);
  const live = d.status === "active" && d.active_at;
  return (
    <>
      <span style={{ ...code, fontSize: 13, fontWeight: 600 }}>{d.domain}</span>
      <Badge tone={statusTone(d.status)}>{t(`status_${d.status}`)}</Badge>
      {live && <span style={small}>{t("liveSince")} <DateText value={d.active_at as string} relative /></span>}
    </>
  );
}

function DomainRow({ d, tenantId, canEdit }: { d: CustomDomain; tenantId: string; canEdit: boolean }) {
  const t = useT(T360_DOMAIN);
  const [confirm, setConfirm] = useState(false);
  const { remove, retry, failText } = useDomainActions(tenantId, d, () => setConfirm(false));
  const stuck = removalStuck(d);
  const showFailure = (d.status === "failed" || stuck) && !!d.failure_message;
  const showRecords = d.status !== "active" && d.status !== "removing";

  return (
    <div data-testid="tp-domain-row" style={{ display: "flex", flexDirection: "column", gap: 8, minWidth: 0,
      paddingBottom: 12, borderBottom: `1px solid ${K.line}` }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", minWidth: 0 }}>
        <DomainHeader d={d} />
        {canEdit && (
          <span style={{ display: "inline-flex", gap: 6, marginLeft: "auto" }}>
            {d.status === "failed" && (
              <Button size="sm" variant="secondary" disabled={retry.isPending} onClick={() => retry.mutate()}>
                {t("retry")}
              </Button>
            )}
            {d.status !== "removing" && (
              <Button size="sm" variant="ghost" onClick={() => setConfirm(true)}>{t("remove")}</Button>
            )}
            {stuck && (
              <Button size="sm" variant="secondary" disabled={remove.isPending} onClick={() => remove.mutate()}>
                {t("retryRemoval")}
              </Button>
            )}
          </span>
        )}
      </div>
      {showFailure && <p style={{ ...small, color: K.danger }} role="status">{d.failure_message}</p>}
      {showRecords && <DnsRecords d={d} />}
      {confirm && (
        <ConfirmModal open onClose={() => setConfirm(false)} onConfirm={() => remove.mutate()} destructive
          title={t("removeTitle")} body={t("removeBody", d.domain)} confirmLabel={t("remove")}
          cancelLabel={t("cancel")} busy={remove.isPending}
          error={remove.error ? failText(remove.error) : null} testId="tp-domain-remove" />
      )}
    </div>
  );
}

function DomainList({ data, tenantId, canEdit }: { data: DomainList; tenantId: string; canEdit: boolean }) {
  const t = useT(T360_DOMAIN);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12, minWidth: 0 }}>
      {!data.available && <p style={{ ...small, color: K.amber }}>{t("unavailable")}</p>}
      {!data.has_tripplanner && <p style={small}>{t("noTripPlanner")}</p>}
      <p style={small}>{t("limit", data.max_domains)}</p>
      {!data.domains.length && <p style={small}>{t("empty")}</p>}
      {data.domains.map((d) => <DomainRow key={d.domain_id} d={d} tenantId={tenantId} canEdit={canEdit} />)}
    </div>
  );
}

// Poll while the job works on a host; hide the section for a tenant without TripPlanner and without a host.
const refetchWhileWorking = (query: { state: { data?: DomainList } }) => (shouldPoll(query.state.data) ? POLL_MS : false);
const hiddenFor = (data: DomainList | undefined) => !!data && !data.has_tripplanner && !data.domains.length;

function AddDomainButton({ onClick }: { onClick: () => void }) {
  const t = useT(T360_DOMAIN);
  return (
    <span data-testid="tp-domain-add" style={{ display: "inline-flex" }}>
      <Button size="sm" variant="secondary" onClick={onClick}>{t("add")}</Button>
    </span>
  );
}

/** The TripPlanner own-domain section of the Subscription tab (mounted inside its ToastProvider). */
export function Tenant360CustomDomain({ tenantId, canEdit }: { tenantId: string; canEdit: boolean }) {
  const t = useT(T360_DOMAIN);
  const [adding, setAdding] = useState(false);
  const q = useQuery({
    queryKey: domainsKey(tenantId),
    queryFn: () => sendJson<DomainList>(domainsPath(tenantId), "GET"),
    retry: false,
    refetchInterval: refetchWhileWorking,
  });
  const data = q.data;
  if (hiddenFor(data)) return null;
  const addButton = canEdit && data && canAdd(data) ? <AddDomainButton onClick={() => setAdding(true)} /> : undefined;

  return (
    <Section title={t("title")} description={t("hint")} testId="tenant-tp-domains" actions={addButton}>
      {q.isPending && <p style={small}>{t("loading")}</p>}
      {q.error && <ErrorState message={`${t("loadError")}: ${(q.error as Error).message}`}
        onRetry={() => void q.refetch()} />}
      {data && <DomainList data={data} tenantId={tenantId} canEdit={canEdit} />}
      {adding && <AddDomainDrawer tenantId={tenantId} onClose={() => setAdding(false)} />}
    </Section>
  );
}
