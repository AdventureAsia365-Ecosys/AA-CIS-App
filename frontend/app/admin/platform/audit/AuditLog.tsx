"use client";
// app/admin/platform/audit/AuditLog.tsx
// AA-884 (S241 admin console GĐ8) — Platform › People & access › Audit log: who did what on CIS, from
// acp_shared.audit_log (append-only and hash-chained since CIS 248).
//
// API (backend api/routers/admin_platform_ops.py → services/platform_audit.py; audit.read = admin; BFF
// API_READ_RULES "platform"):
//   GET /api/admin/platform/audit?scope&actor&action&route&since&until&cursor&limit — newest first, keyset cursor.
//     Filters run on the server; "Load older rows" follows next_cursor (no offset paging, no client filter).
//   GET /api/admin/platform/audit/verify — recomputes the hash chain (cached 60 s on the backend): the badge.
// Scope "staff" (default) = staff and machine actions (actor_type staff/machine or an admin:<id> actor); "all"
// adds tenant and system rows.
//
// Rows a legacy password session wrote name the actor from the unverified x-admin-user-id header
// (details.auth = legacy_unverified); they carry a "Legacy" badge until the AA-816 cutover.
// Booking keeps its own trail; the header links to it (CIS never reads the booking schema).
// S243 — one paging mechanism: the keyset "Load older" button with a count (the table itself does not page,
// kit DataTable paginate={false}); a smaller page and compact two-line cards on a phone; the chain badge says the
// chain covers every row while the list is filtered; short filter placeholders; no internal codes in the copy.

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { ExternalLink, RefreshCw, ShieldAlert, ShieldCheck } from "lucide-react";
import { useMemo, useState } from "react";
import {
  Badge,
  Button,
  DataTable,
  Drawer,
  ErrorState,
  K,
  PageHeader,
  RADIUS,
  apiGet,
  PHONE_QUERY,
  mono,
  sans,
  useFormat,
  useIsPhone,
  useT,
  type ColumnDef,
  type Tone, PageFrame,
  Input, Select,
  LinkButton,
} from "../../../_kit";
import { BOOKING_AUDIT_LOG_URL } from "@/lib/apps";
import { TenantName } from "../../_components/TenantSelect";
import {
  EMPTY_AUDIT_FILTERS,
  auditQuery,
  isLegacyUnverified,
  type AuditFilters,
  type AuditListResp,
  type AuditRow,
  type AuditVerifyResp,
} from "@/lib/platform";
import { AUDIT } from "./messages";

const PAGE_LIMIT = 50;
const PHONE_PAGE_LIMIT = 25;

// Read when a page is fetched, not from the query key: crossing the 768 px breakpoint (rotation, or the first
// render before useIsPhone settles after hydration) must not drop the older pages already loaded.
function pageLimitNow(): number {
  return typeof window !== "undefined" && window.matchMedia?.(PHONE_QUERY).matches ? PHONE_PAGE_LIMIT : PAGE_LIMIT;
}

function statusTone(code: number | null): Tone {
  if (code == null) return "neutral";
  if (code < 300) return "success";
  if (code < 400) return "info";
  if (code < 500) return "warning";
  return "danger";
}

/** "admin:<uuid>" → "admin:9d8c7b6a…" (the full value is in the cell's tooltip and the drawer). */
function shortActor(actor: string | null): string {
  if (!actor) return "—";
  const m = /^(admin:)([0-9a-f]{8})-[0-9a-f-]{27}$/i.exec(actor);
  return m ? `${m[1]}${m[2]}…` : actor;
}

function actorLabel(r: AuditRow): string {
  return r.actor_name || r.actor_email || shortActor(r.actor);
}

export default function AuditLog() {
  const t = useT(AUDIT);
  const { formatDateTime } = useFormat();
  // Draft (the form) vs applied (the query): a query per keystroke would hit the DB for nothing.
  const [draft, setDraft] = useState<AuditFilters>(EMPTY_AUDIT_FILTERS);
  const [filters, setFilters] = useState<AuditFilters>(EMPTY_AUDIT_FILTERS);
  const [openId, setOpenId] = useState<string | null>(null);
  const phone = useIsPhone();
  const limit = phone ? PHONE_PAGE_LIMIT : PAGE_LIMIT;

  const list = useInfiniteQuery({
    queryKey: ["platform-audit", filters],
    queryFn: ({ pageParam }) =>
      apiGet<AuditListResp>(`/api/admin/platform/audit?${auditQuery(filters, pageParam, pageLimitNow())}`),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
    staleTime: 60_000,
  });
  const verify = useQuery({
    queryKey: ["platform-audit-verify"],
    queryFn: () => apiGet<AuditVerifyResp>("/api/admin/platform/audit/verify"),
    staleTime: 60_000,
  });

  const rows = useMemo(() => list.data?.pages.flatMap((p) => p.rows) ?? [], [list.data]);
  const open = rows.find((r) => r.id === openId) ?? null;

  // Phone: two compact columns (the kit renders each row as a card: title + one line) instead of six label rows.
  const phoneColumns = useMemo<ColumnDef<AuditRow, unknown>[]>(() => [
    {
      id: "summary",
      accessorFn: (r) => `${r.action ?? ""} ${actorLabel(r)}`,
      header: t("colAction"),
      cell: (c) => <PhoneSummary r={c.row.original} />,
    },
    {
      id: "request",
      accessorFn: (r) => `${r.http_method ?? ""} ${r.route ?? ""}`,
      header: t("colRequest"),
      cell: (c) => <RequestCell r={c.row.original} />,
    },
  ], [t]);

  const columns = useMemo<ColumnDef<AuditRow, unknown>[]>(() => [
    {
      accessorKey: "created_at",
      header: t("colWhen"),
      cell: (c) => <span style={{ whiteSpace: "nowrap" }}>{formatDateTime(c.row.original.created_at)}</span>,
    },
    {
      id: "actor",
      accessorFn: (r) => `${actorLabel(r)} ${r.actor ?? ""}`,
      header: t("colWho"),
      cell: (c) => <ActorCell r={c.row.original} />,
    },
    {
      accessorKey: "action",
      header: t("colAction"),
      cell: (c) => <span style={{ fontFamily: mono, fontSize: 12 }}>{c.row.original.action ?? "—"}</span>,
    },
    {
      id: "request",
      accessorFn: (r) => `${r.http_method ?? ""} ${r.route ?? ""}`,
      header: t("colRequest"),
      cell: (c) => <RequestCell r={c.row.original} />,
    },
    {
      id: "resource",
      accessorFn: (r) => `${r.resource_type ?? ""} ${r.resource_id ?? ""}`,
      header: t("colResource"),
      cell: (c) => {
        const r = c.row.original;
        if (!r.resource_type && !r.resource_id) return <span style={{ color: K.muted }}>—</span>;
        return (
          <span title={r.resource_id ?? ""} style={{
            display: "inline-block", maxWidth: 200, overflow: "hidden", textOverflow: "ellipsis",
            whiteSpace: "nowrap", verticalAlign: "bottom", fontSize: 12,
          }}>
            <span style={{ color: K.muted }}>{r.resource_type}</span>{r.resource_id ? ` ${r.resource_id}` : ""}
          </span>
        );
      },
    },
    {
      accessorKey: "chain_seq",
      header: t("colChain"),
      cell: (c) => c.row.original.chain_seq == null
        ? <span style={{ color: K.muted }} title={t("preChainTitle")}>{t("preChain")}</span>
        : <span style={{ fontFamily: mono, fontSize: 12 }}>{c.row.original.chain_seq}</span>,
    },
  ], [t, formatDateTime]);

  const apply = (e: React.FormEvent) => {
    e.preventDefault();
    setFilters(draft);
  };
  const reset = () => {
    setDraft(EMPTY_AUDIT_FILTERS);
    setFilters(EMPTY_AUDIT_FILTERS);
  };

  return (
    <PageFrame>
      <PageHeader
        title={t("title")}
        description={
          <>
            {t("description")}{" "}
            <a href={BOOKING_AUDIT_LOG_URL} target="_blank" rel="noopener noreferrer"
              style={{ color: K.accentDeep, fontWeight: 600, whiteSpace: "nowrap", display: "inline-flex", alignItems: "center", gap: 3 }}>
              {t("bookingAudit")} <ExternalLink size={11} />
            </a>
          </>
        }
        actions={
          <Button variant="secondary" size="sm" disabled={list.isFetching || verify.isFetching}
            onClick={() => { list.refetch(); verify.refetch(); }}>
            <RefreshCw size={13} /> {t("refresh")}
          </Button>
        }
      />

      <ChainStatus verify={verify.data} loading={verify.isLoading} error={verify.error as Error | null}
        onRetry={() => verify.refetch()} filtered={JSON.stringify(filters) !== JSON.stringify({ ...EMPTY_AUDIT_FILTERS, scope: "all" })} />

      <FilterForm draft={draft} setDraft={setDraft} onSubmit={apply} onReset={reset} />

      <div style={{ fontSize: 12, color: K.muted, margin: "0 0 10px", fontFamily: sans, lineHeight: 1.5 }}>
        {t("legacyA")} <Badge tone="warning" dot={false}>{t("legacy")}</Badge> {t("legacyNote")}
      </div>

      {list.isError ? (
        <ErrorState message={(list.error as Error)?.message || t("loadError")} onRetry={() => list.refetch()} />
      ) : (
        <DataTable<AuditRow>
          tableId="platform-audit"
          data={rows}
          columns={phone ? phoneColumns : columns}
          getRowId={(r) => r.id}
          loading={list.isLoading}
          paginate={false}
          // The kit exports the visible columns; on a phone those are the two summary cards (actor, time and chain
          // number live inside a cell), so the CSV is offered on the desktop column set only.
          enableCsv={!phone}
          csvFilename="cis-audit-log.csv"
          emptyTitle={t("emptyTitle")}
          emptyDescription={JSON.stringify(filters) === JSON.stringify(EMPTY_AUDIT_FILTERS)
            ? t("emptyNone")
            : t("emptyFiltered")}
          onRowClick={(r) => setOpenId(r.id)}
          tableMinWidth={980}
        />
      )}

      {!list.isLoading && !list.isError && rows.length > 0 && (
        <div data-testid="platform-audit-more" style={{
          display: "flex", justifyContent: "center", alignItems: "center", gap: 12, flexWrap: "wrap", marginTop: 12,
          fontSize: 12, color: K.muted, fontFamily: sans,
        }}>
          <span>
            {t("showing", rows.length.toLocaleString("en-US"), rows.length)}
            {list.hasNextPage ? "" : t("allOfThem")}.
          </span>
          {list.hasNextPage && (
            <Button variant="secondary" size="sm" onClick={() => list.fetchNextPage()} disabled={list.isFetchingNextPage}>
              {list.isFetchingNextPage ? t("loading") : t("loadOlder", limit)}
            </Button>
          )}
        </div>
      )}

      <AuditDrawer row={open} onClose={() => setOpenId(null)} />
    </PageFrame>
  );
}

// ── chain status ────────────────────────────────────────────────────────────────────────────

function ChainStatus({ verify, loading, error, onRetry, filtered }: {
  verify: AuditVerifyResp | undefined;
  loading: boolean;
  error: Error | null;
  onRetry: () => void;
  /** the list below shows a subset (scope or filters): say that the chain covers every row */
  filtered: boolean;
}) {
  const t = useT(AUDIT);
  const { formatDateTime } = useFormat();
  let tone: Tone = "neutral";
  let label = t("checking");
  let icon = <ShieldCheck size={15} />;
  let detail: React.ReactNode = null;
  if (error && !verify) {
    tone = "danger";
    label = t("checkFailed");
    icon = <ShieldAlert size={15} />;
    detail = (
      <>
        {error.message}{" "}
        <LinkButton onClick={onRetry} style={{ fontSize: 12.5 }}>{t("retry")}</LinkButton>
      </>
    );
  } else if (verify) {
    if (verify.ok) {
      tone = "success";
      label = t("verified");
      detail = verify.checked
        ? t("covers", verify.checked.toLocaleString("en-US"), verify.checked, String(verify.last_seq)) +
          (filtered ? t("filteredNote") : "")
        : t("noChained");
    } else {
      tone = "danger";
      label = t("broken");
      icon = <ShieldAlert size={15} />;
      detail = `${verify.first_bad_seq != null ? t("atSeq", verify.first_bad_seq) : ""}${verify.problem ?? t("unknownProblem")}.`;
    }
  } else if (!loading) {
    label = t("notChecked");
  }
  return (
    <section data-testid="platform-audit-chain" style={{
      background: K.card, border: `1px solid ${tone === "danger" ? K.dangerBorder : K.line}`, borderRadius: RADIUS.lg,
      padding: "10px 14px", marginBottom: 14, fontFamily: sans, fontSize: 13, color: K.ink,
      display: "flex", flexDirection: "column", gap: 4,
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <span style={{ display: "inline-flex", color: tone === "danger" ? K.danger : tone === "success" ? K.success : K.muted }}>{icon}</span>
        <span style={{ fontWeight: 600 }}>{t("hashChain")}</span>
        <Badge tone={tone}>{label}</Badge>
        {verify?.verified_at && (
          <span style={{ fontSize: 12, color: K.muted }}>
            {t("checkedAt", formatDateTime(verify.verified_at))}{verify._cached ? t("cached") : ""}
          </span>
        )}
      </div>
      {detail && <div style={{ fontSize: 12.5, color: tone === "danger" ? K.danger : K.body }}>{detail}</div>}
      {verify && (
        <div style={{ fontSize: 12, color: K.muted, lineHeight: 1.5 }}>
          {t("predate", verify.pre_chain_rows.toLocaleString("en-US"), verify.pre_chain_rows)}
          {verify.chain_boundary ? t("started", formatDateTime(verify.chain_boundary)) : ""}.
          {verify.unchained_after_genesis > 0 && t("unchained", verify.unchained_after_genesis)}
          {" "}{t("bypass")}
        </div>
      )}
    </section>
  );
}

// ── filters ─────────────────────────────────────────────────────────────────────────────────

const inputStyle: React.CSSProperties = {
  padding: "7px 10px", borderRadius: RADIUS.md, border: `1px solid ${K.line}`, background: K.card, color: K.ink,
  fontSize: 13, fontFamily: sans, width: "100%", maxWidth: "100%", minWidth: 0, boxSizing: "border-box",
};

const labelStyle: React.CSSProperties = {
  display: "flex", flexDirection: "column", gap: 4, fontSize: 12, color: K.muted, fontFamily: sans, minWidth: 0,
};

function FilterForm({ draft, setDraft, onSubmit, onReset }: {
  draft: AuditFilters;
  setDraft: (f: AuditFilters) => void;
  onSubmit: (e: React.FormEvent) => void;
  onReset: () => void;
}) {
  const t = useT(AUDIT);
  const set = <K2 extends keyof AuditFilters>(k: K2, v: AuditFilters[K2]) => setDraft({ ...draft, [k]: v });
  return (
    <form data-testid="platform-audit-filters" onSubmit={onSubmit} style={{
      display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 150px), 1fr))", gap: 10,
      alignItems: "end", marginBottom: 12,
    }}>
      <label style={labelStyle}>
        {t("scope")}
        <Select value={draft.scope} onChange={(e) => set("scope", e.target.value === "all" ? "all" : "staff")} style={inputStyle}>
          <option value="staff">{t("scopeStaff")}</option>
          <option value="all">{t("scopeAll")}</option>
        </Select>
      </label>
      <label style={labelStyle}>
        {t("actor")}
        <Input value={draft.actor} onChange={(e) => set("actor", e.target.value)} placeholder="admin:<id>"
          maxLength={64} style={inputStyle} />
      </label>
      <label style={labelStyle}>
        {t("actionStarts")}
        <Input value={draft.action} onChange={(e) => set("action", e.target.value)} placeholder="staff."
          maxLength={64} style={inputStyle} />
      </label>
      <label style={labelStyle}>
        {t("route")}
        <Input value={draft.route} onChange={(e) => set("route", e.target.value)} placeholder="/admin/…"
          maxLength={512} style={inputStyle} />
      </label>
      <label style={labelStyle}>
        {t("from")}
        <Input type="date" value={draft.since} onChange={(e) => set("since", e.target.value)} style={inputStyle} />
      </label>
      <label style={labelStyle}>
        {t("to")}
        <Input type="date" value={draft.until} onChange={(e) => set("until", e.target.value)} style={inputStyle} />
      </label>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        <Button type="submit" variant="primary" size="sm">{t("apply")}</Button>
        <Button variant="ghost" size="sm" onClick={onReset}>{t("reset")}</Button>
      </div>
    </form>
  );
}

// ── cells + drawer ──────────────────────────────────────────────────────────────────────────

function ActorCell({ r }: { r: AuditRow }) {
  const t = useT(AUDIT);
  const named = r.actor_name || r.actor_email;
  return (
    <span style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
      <span style={{ display: "inline-flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
        <span style={{ fontWeight: 600, color: K.ink }} title={r.actor ?? undefined}>{actorLabel(r)}</span>
        {r.actor_type === "machine" && <Badge tone="info" dot={false}>{t("machine")}</Badge>}
        {isLegacyUnverified(r.details) && <Badge tone="warning" dot={false}>{t("legacy")}</Badge>}
      </span>
      {named && r.actor && (
        <span title={r.actor} style={{ fontSize: 11.5, color: K.muted, fontFamily: mono, whiteSpace: "nowrap" }}>{shortActor(r.actor)}</span>
      )}
    </span>
  );
}

/** Phone card title: action, who and when on two short lines. */
function PhoneSummary({ r }: { r: AuditRow }) {
  const t = useT(AUDIT);
  const { formatDateTime } = useFormat();
  return (
    <span style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
      <span style={{ fontFamily: mono, fontSize: 12.5 }}>{r.action ?? "—"}</span>
      <span style={{ display: "inline-flex", alignItems: "center", gap: 6, flexWrap: "wrap", fontSize: 12, fontWeight: 400, color: K.muted }}>
        <span style={{ color: K.ink, fontWeight: 600 }}>{actorLabel(r)}</span>
        {r.actor_type === "machine" && <Badge tone="info" dot={false}>{t("machine")}</Badge>}
        {isLegacyUnverified(r.details) && <Badge tone="warning" dot={false}>{t("legacy")}</Badge>}
        <span>{formatDateTime(r.created_at)}</span>
        {r.chain_seq != null && <span style={{ fontFamily: mono }}>#{r.chain_seq}</span>}
      </span>
    </span>
  );
}

function RequestCell({ r }: { r: AuditRow }) {
  if (!r.http_method && !r.route) return <span style={{ color: K.muted }}>—</span>;
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6, flexWrap: "wrap", minWidth: 0 }}>
      {r.http_method && <span style={{ fontFamily: mono, fontSize: 11.5, fontWeight: 600, color: K.ink2 }}>{r.http_method}</span>}
      {r.route && (
        <span title={r.route} style={{
          fontFamily: mono, fontSize: 11.5, color: K.body, maxWidth: 240, overflow: "hidden",
          textOverflow: "ellipsis", whiteSpace: "nowrap",
        }}>{r.route}</span>
      )}
      {r.status != null && <Badge tone={statusTone(r.status)} dot={false}>{r.status}</Badge>}
    </span>
  );
}

function AuditDrawer({ row, onClose }: { row: AuditRow | null; onClose: () => void }) {
  const t = useT(AUDIT);
  const { formatDateTime } = useFormat();
  return (
    <Drawer open={!!row} onClose={onClose} title={row ? (row.action ?? t("auditRow")) : t("auditRow")}>
      {row && (
        <div data-testid="platform-audit-detail" style={{ display: "flex", flexDirection: "column", gap: 16, fontFamily: sans }}>
          <dl style={{ display: "grid", gridTemplateColumns: "max-content minmax(0, 1fr)", gap: "9px 16px", margin: 0, fontSize: 13 }}>
            <DField label={t("colWhen")} value={formatDateTime(row.created_at)} />
            <DField label={t("colWho")} value={<ActorCell r={row} />} />
            <DField label={t("fActor")} value={row.actor ? <code style={codeStyle}>{row.actor}</code> : "—"} />
            <DField label={t("fActorType")} value={row.actor_type ?? "—"} />
            <DField label={t("colAction")} value={<code style={codeStyle}>{row.action ?? "—"}</code>} />
            <DField label={t("fMethod")} value={row.http_method ?? "—"} />
            <DField label={t("fRoute")} value={row.route ? <code style={codeStyle}>{row.route}</code> : "—"} />
            <DField label={t("fStatus")} value={row.status ?? "—"} />
            <DField label={t("colResource")} value={row.resource_type ? `${row.resource_type}${row.resource_id ? ` · ${row.resource_id}` : ""}` : "—"} />
            <DField label={t("fTenant")} value={row.tenant_id ? (
              <><TenantName id={row.tenant_id} /> <span style={{ fontFamily: mono, fontSize: 11.5, color: K.muted }}>{row.tenant_id}</span></>
            ) : "—"} />
            <DField label={t("colChain")} value={row.chain_seq ?? t("preChainLong")} />
            <DField label={t("fRowHash")} value={row.row_hash ? <code style={codeStyle}>{row.row_hash}</code> : "—"} />
            <DField label={t("fRowId")} value={<code style={codeStyle}>{row.id}</code>} />
          </dl>
          <div>
            <div style={{ fontSize: 12, color: K.muted, marginBottom: 6 }}>{t("details")}</div>
            <pre style={{
              margin: 0, padding: "10px 12px", background: K.bg, border: `1px solid ${K.line}`, borderRadius: RADIUS.md,
              fontFamily: mono, fontSize: 12, color: K.ink2, whiteSpace: "pre-wrap", overflowWrap: "anywhere",
              maxHeight: 360, overflowY: "auto",
            }}>
              {row.details == null ? "—" : JSON.stringify(row.details, null, 2)}
            </pre>
          </div>
        </div>
      )}
    </Drawer>
  );
}

const codeStyle: React.CSSProperties = { fontFamily: mono, fontSize: 12, overflowWrap: "anywhere" };

function DField({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <>
      <dt style={{ color: K.muted }}>{label}</dt>
      <dd style={{ margin: 0, minWidth: 0, overflowWrap: "anywhere", color: K.ink }}>{value}</dd>
    </>
  );
}
