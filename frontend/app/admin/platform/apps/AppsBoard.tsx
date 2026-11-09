"use client";
// app/admin/platform/apps/AppsBoard.tsx
// AA-884 (S241 admin console GĐ7) — Platform › Apps: one card per ecosystem app with its health,
// the commit it runs and links to its admin, repo and commit.
//
// API: GET /api/admin/platform/apps (backend api/routers/admin_platform_ops.py → services/platform_apps.py
// collect_apps(); ops.read = admin; BFF API_READ_RULES "platform"). Built on a page view only and cached
// 60 s in Redis; there is no poller and this page does not poll either (S240 Fluid CPU lesson): one load
// on open, and Refresh sends ?refresh=1 once to rebuild the cache.
//
// What each card says (all read only; no deploy / scale button, S241):
//   - CIS: the API's commit + ECS task revision, the live workers' commits (SHA parity = every live worker
//     runs the API's commit), the deploy gate (same answer as the CI guard), schema readiness (/health/ready).
//   - Booking: its public /booking/health and /booking/health/ready over HTTP (commit, DB, schema version).
//   - Partner CRM (AA-936): its public /crm/health and /crm/health/ready like Booking, plus its job backlog per kind
//     (GET /api/admin/platform/apps/crm/jobs → the CRM's crm.ops.v1 /jobs/summary with this staff member's own
//     Cognito token; read once on open, never polled).
//   - TripPlanner: no health endpoint yet; today's customer-facing LLM spend vs the DEFAULT daily cap
//     (the live cap is a TripPlanner env var CIS cannot see).
// Frontends report no version yet (deferred): the row only links them.
// S243 — one health definition: the badge is `overall.status` (backend api/services/app_health.py: the worst of the
// app's own check and its CloudWatch traffic of the last 24 h), the same verdict the Overview tiles show, with the
// same words (lib/platform APP_STATUS_LABEL). Each card shows the traffic numbers it is based on.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ExternalLink, GitCommitHorizontal, RefreshCw } from "lucide-react";
import {
  Badge,
  Button,
  ErrorState,
  K,
  PageHeader,
  RADIUS,
  SkeletonBar,
  SkeletonStyle,
  apiGet,
  mono,
  sans,
  useFormat,
  useLang,
  useT,
  type Tone, PageFrame,
  DataTable,
} from "../../../_kit";
import { APP_REGISTRY, PLATFORM_APP_IDS, isPlatformAppId, type PlatformAppId } from "@/lib/apps";
import { APP_MESSAGES } from "@/lib/apps.messages";
import {
  shortSha,
  usd,
  APP_STATUS_LABEL,
  basisText,
  type AppCard,
  type AppTraffic,
  type AppsResp,
  type BookingCard,
  type CisCard,
  type CrmCard,
  type CrmJobsResp,
  type TripPlannerCard,
} from "@/lib/platform";
import { APP_STATUS_MESSAGES, BASIS_MESSAGES } from "@/lib/platform.messages";
import { APPS } from "./messages";
import { TpStageCaps } from "./TpStageCaps";

/** The admin link label of a registered app in the UI language (lib/apps.messages.ts). */
function useAdminLabel(): (id: PlatformAppId) => string {
  const t = useT(APP_MESSAGES);
  return (id) => t(`${id}Admin`);
}

const APPS_KEY = ["platform-apps"] as const;
const APPS_PATH = "/api/admin/platform/apps";


export default function AppsBoard() {
  const t = useT(APPS);
  const { formatDateTime } = useFormat();
  const qc = useQueryClient();
  const apps = useQuery({
    queryKey: APPS_KEY,
    queryFn: () => apiGet<AppsResp>(APPS_PATH),
    staleTime: 60_000,
  });
  // One rebuild on demand; the answer replaces the cached one (no second GET).
  const refresh = useMutation({
    mutationFn: () => apiGet<AppsResp>(`${APPS_PATH}?refresh=1`),
    onSuccess: (data) => qc.setQueryData(APPS_KEY, data),
  });

  const data = apps.data;
  const busy = apps.isFetching || refresh.isPending;
  const error = (refresh.error as Error | null) ?? (apps.error as Error | null);

  return (
    <PageFrame>
      <SkeletonStyle />
      <PageHeader
        title={t("title")}
        description={t("description")}
        actions={
          <Button variant="secondary" size="sm" onClick={() => refresh.mutate()} disabled={busy}
            title={t("refreshTitle")}>
            <RefreshCw size={13} /> {refresh.isPending ? t("checking") : t("refresh")}
          </Button>
        }
      />

      {error && !data ? (
        <ErrorState message={error.message || t("loadError")} onRetry={() => apps.refetch()} />
      ) : !data ? (
        <CardsSkeleton />
      ) : (
        <>
          {refresh.isError && (
            <div style={{ ...noteStyle, color: K.danger, marginBottom: 12 }}>
              {t("refreshFailed", (refresh.error as Error)?.message || t("unknownError"))}
            </div>
          )}
          <div data-testid="platform-apps-ready" style={gridStyle}>
            {data.apps.map((a) => <AppCardView key={a.id} card={a} />)}
          </div>
          <FrontendsRow />
          <div style={{ ...noteStyle, marginTop: 14 }}>
            {t("built", formatDateTime(data.generated_at))}{data._cached ? t("fromCache") : ""}.
          </div>
        </>
      )}
    </PageFrame>
  );
}

// ── layout helpers ──────────────────────────────────────────────────────────────────────────

const gridStyle: React.CSSProperties = {
  display: "grid",
  // min(100%, 320px): one column on a phone, never wider than the screen (no 390px overflow).
  gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 320px), 1fr))",
  gap: 16,
  alignItems: "start",
};

const cardStyle: React.CSSProperties = {
  background: K.card, border: `1px solid ${K.line}`, borderRadius: RADIUS.lg, padding: "14px 16px",
  fontFamily: sans, fontSize: 13, color: K.ink, minWidth: 0, display: "flex", flexDirection: "column", gap: 12,
};

const noteStyle: React.CSSProperties = { fontSize: 12, color: K.muted, fontFamily: sans, lineHeight: 1.5 };

const linkStyle: React.CSSProperties = {
  display: "inline-flex", alignItems: "center", gap: 4, color: K.accentDeep, fontWeight: 600,
  textDecoration: "none", fontSize: 12.5, whiteSpace: "nowrap",
};

function CardsSkeleton() {
  return (
    <div style={gridStyle}>
      {PLATFORM_APP_IDS.map((id) => (
        <div key={id} style={cardStyle}>
          <SkeletonBar w="45%" h={16} />
          <SkeletonBar w="80%" />
          <SkeletonBar w="65%" />
          <SkeletonBar w="70%" />
        </div>
      ))}
    </div>
  );
}

function ExtLink({ href, children, title }: { href: string; children: React.ReactNode; title?: string }) {
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" title={title} style={linkStyle}>
      {children} <ExternalLink size={11} />
    </a>
  );
}

/** Short SHA linked to its GitHub commit (the backend's commit_url, else the frontend registry). */
function ShaLink({ appId, sha, commitUrl }: { appId: string; sha: string | null | undefined; commitUrl?: string | null }) {
  const t = useT(APPS);
  const href = commitUrl ?? (isPlatformAppId(appId) ? APP_REGISTRY[appId].commitUrl(sha) : null);
  const text = shortSha(sha);
  if (!href) {
    return <span style={{ fontFamily: mono, color: K.muted }} title={sha ?? t("noCommit")}>{text}</span>;
  }
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" title={t("openCommit", sha ?? "")}
      style={{ ...linkStyle, fontFamily: mono, fontWeight: 500 }}>
      <GitCommitHorizontal size={12} /> {text}
    </a>
  );
}

function Fields({ children }: { children: React.ReactNode }) {
  return (
    <dl style={{ display: "grid", gridTemplateColumns: "max-content minmax(0, 1fr)", gap: "8px 14px", margin: 0 }}>
      {children}
    </dl>
  );
}

function Field({ label, children, title }: { label: string; children: React.ReactNode; title?: string }) {
  return (
    <>
      <dt style={{ color: K.muted, fontSize: 12.5 }} title={title}>{label}</dt>
      <dd style={{ margin: 0, minWidth: 0, overflowWrap: "anywhere", display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center" }}>
        {children}
      </dd>
    </>
  );
}

function Problems({ items }: { items: string[] }) {
  if (!items.length) return null;
  return (
    <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: 4 }}>
      {items.map((p) => (
        <li key={p} style={{ display: "flex", gap: 6, alignItems: "flex-start", color: K.amber, fontSize: 12.5 }}>
          <AlertTriangle size={13} style={{ flexShrink: 0, marginTop: 2 }} /> <span>{p}</span>
        </li>
      ))}
    </ul>
  );
}

// ── cards ───────────────────────────────────────────────────────────────────────────────────

function AppCardView({ card }: { card: AppCard }) {
  const t = useT(APPS);
  const status = useT(APP_STATUS_MESSAGES);
  const lang = useLang();
  const adminLabel = useAdminLabel();
  const reg = isPlatformAppId(card.id) ? APP_REGISTRY[card.id] : null;
  const code = Object.hasOwn(APP_STATUS_LABEL, card.status) ? card.status : "unknown";
  const st = { ...APP_STATUS_LABEL[code], label: status(code) };
  return (
    <section data-testid={`platform-app-${card.id}`} style={cardStyle}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <span style={{ fontWeight: 600, fontSize: 15 }}>{card.label}</span>
        <Badge tone={st.tone}>{st.label}</Badge>
      </div>
      {card.overall && (
        <div data-testid={`platform-app-${card.id}-health`} style={{ fontSize: 12.5, color: K.body, lineHeight: 1.45 }}>
          {card.overall.reason}
          {card.overall.basis.length > 0 && (
            <span style={{ color: K.muted }}> · {basisText(card.overall.basis, BASIS_MESSAGES[lang])}</span>
          )}
        </div>
      )}
      <TrafficLine traffic={card.traffic ?? null} />
      {card.id === "cis" && <CisBody card={card} />}
      {card.id === "booking" && <BookingBody card={card} />}
      {card.id === "crm" && <CrmBody card={card} />}
      {card.id === "tripplanner" && <TripPlannerBody card={card} />}
      <div style={{ display: "flex", gap: 14, flexWrap: "wrap", borderTop: `1px solid ${K.line}`, paddingTop: 10 }}>
        {reg && isPlatformAppId(card.id) && <ExtLink href={reg.adminUrl}>{adminLabel(card.id)}</ExtLink>}
        <ExtLink href={card.repo_url}>{t("repository")}</ExtLink>
      </div>
    </section>
  );
}

function CisBody({ card }: { card: CisCard }) {
  const t = useT(APPS);
  const workers = card.workers ?? [];
  let parity: { tone: Tone; label: string; title: string };
  if (card.parity === true) {
    parity = { tone: "success", label: t("sameCommit"), title: t("sameCommitTitle") };
  } else if (card.parity === false) {
    parity = { tone: "danger", label: t("mismatch"), title: t("mismatchTitle") };
  } else {
    parity = { tone: "neutral", label: t("notCompared"), title: t("notComparedTitle") };
  }
  const gate = card.deploy_gate;
  // AA-993 G5: the jobs a deploy releases (info); the gate blocks only on an open wave or a deploy-blocking kind.
  const gateDetail = gate
    ? Object.entries(gate.by_status).map(([s, n]) => `${n} ${s}`).join(", ")
    : "";
  const gateReasons = gate?.blocked_by?.length ? gate.blocked_by.join("; ") : "";
  return (
    <>
      <Fields>
        <Field label={t("apiCommit")} title={t("apiCommitTitle")}>
          <ShaLink appId="cis" sha={card.api.git_sha} commitUrl={card.api.commit_url} />
          {card.api.task_revision != null && (
            <span style={{ color: K.muted, fontSize: 12 }}>{t("taskRev", card.api.task_revision)}</span>
          )}
        </Field>
        <Field label={t("workers")} title={t("workersTitle")}>
          <Badge tone={parity.tone} dot={false} style={{ cursor: "help" }}>
            <span title={parity.title}>{parity.label}</span>
          </Badge>
          <span style={{ color: K.muted, fontSize: 12 }}>
            {card.workers === null ? t("unreadable") : t("live", workers.length)}
          </span>
        </Field>
        <Field label={t("deployGate")} title={t("deployGateTitle")}>
          {gate === null ? (
            <span style={{ color: K.muted }}>{t("unreadable")}</span>
          ) : gate.safe_to_deploy ? (
            <>
              <Badge tone="success" dot={false}>{t("safeToDeploy")}</Badge>
              {gateDetail && (
                <span style={{ color: K.muted, fontSize: 12 }} title={t("releasedTitle")}>
                  {t("released", gateDetail)}
                </span>
              )}
            </>
          ) : (
            <>
              <Badge tone="warning" dot={false}>{t("deployWaits")}</Badge>
              <span style={{ color: K.muted, fontSize: 12 }}>
                {gateReasons || t("activeJobs", gate.active)}
              </span>
            </>
          )}
        </Field>
        <Field label={t("schema")} title={t("schemaTitle")}>
          <span style={{ fontFamily: mono }}>{card.schema.version ?? "—"}</span>
          <span style={{ color: K.muted, fontSize: 12 }}>{t("needs", card.schema.required ?? "—")}</span>
          {card.schema.status && card.schema.status !== "ok" && (
            <Badge tone="danger" dot={false}>{card.schema.status.replace(/_/g, " ")}</Badge>
          )}
        </Field>
      </Fields>
      {workers.length > 0 && (
        <ul data-testid="platform-cis-workers" style={{ margin: 0, padding: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: 6 }}>
          {workers.map((w) => (
            <li key={w.worker_id} style={{
              display: "flex", flexWrap: "wrap", gap: "4px 10px", alignItems: "center", fontSize: 12,
              padding: "6px 8px", borderRadius: RADIUS.md, background: K.bg, minWidth: 0,
            }}>
              {/* S243 — the full worker id (host:pid:suffix) wraps instead of being cut off. */}
              <span title={w.worker_id} style={{
                fontFamily: mono, color: K.ink2, minWidth: 0, maxWidth: "100%", overflowWrap: "anywhere",
                wordBreak: "break-all", flex: "1 1 100%",
              }}>{w.worker_id}</span>
              <ShaLink appId="cis" sha={w.git_sha} commitUrl={w.commit_url} />
              {w.git_sha && card.api.git_sha !== "unknown" && w.git_sha !== card.api.git_sha && (
                <Badge tone="danger" dot={false}>{t("differs")}</Badge>
              )}
              {w.task_revision != null && <span style={{ color: K.muted }}>{t("rev", w.task_revision)}</span>}
              <span style={{ color: K.muted }}>{t("running", w.running_jobs ?? 0)}</span>
            </li>
          ))}
        </ul>
      )}
      <Problems items={card.problems ?? []} />
    </>
  );
}

function fmtMs(ms: number | null): string {
  if (ms == null) return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`;
}

/** The CloudWatch traffic the status is partly based on — the same numbers as the Overview tile. */
function TrafficLine({ traffic }: { traffic: AppTraffic | null }) {
  const t = useT(APPS);
  if (!traffic) {
    return <div style={noteStyle}>{t("noTraffic")}</div>;
  }
  const src = traffic.kind === "lambda" ? "Lambda" : "API Gateway";
  return (
    <div data-testid="platform-app-traffic" style={{
      display: "flex", flexWrap: "wrap", gap: "2px 12px", fontSize: 12, color: K.muted, padding: "6px 8px",
      borderRadius: RADIUS.md, background: K.bg, minWidth: 0,
    }} title={t("trafficTitle", src, (traffic.resources ?? []).join(" + "), traffic.window_hours ?? 24)}>
      <span style={{ color: K.body, fontWeight: 600 }}>{t("lastHours", traffic.window_hours ?? 24)}</span>
      <span>{t("requests", (traffic.requests ?? 0).toLocaleString("en-US"))}</span>
      <span style={{ color: traffic.errors ? K.danger : K.muted }}>
        {t("serverErrors", (traffic.errors ?? 0).toLocaleString("en-US"))}
      </span>
      <span>{t("within95", fmtMs(traffic.p95_ms))}</span>
      {traffic.alarms_total != null && (
        <span style={{ color: traffic.alarms_firing ? K.danger : K.muted }}>
          {traffic.alarms_firing ? t("alarmsFiring", traffic.alarms_firing) : t("alarmsQuiet", traffic.alarms_total)}
        </span>
      )}
    </div>
  );
}

function httpTone(code: number | undefined, status: string | null | undefined): Tone {
  if (code === 200 && status === "ok") return "success";
  if (!code) return "neutral";
  return code >= 500 || code === 0 ? "danger" : "warning";
}

function BookingBody({ card, name: nameIn, base = "/booking" }: { card: BookingCard | CrmCard; name?: string; base?: string }) {
  const t = useT(APPS);
  const name = nameIn ?? t("nameBooking");
  if (card.status === "unknown" && card.error) {
    return (
      <>
        <div style={noteStyle}>{t("unreachable", name, card.error)}</div>
        <ExtLink href={card.health_url} title={t("publicHealth", name)}>{base}/health</ExtLink>
      </>
    );
  }
  return (
    <>
      <Fields>
        <Field label={t("commit")} title={t("commitTitle", base)}>
          <ShaLink appId={card.id} sha={card.git_sha} commitUrl={card.commit_url} />
        </Field>
        <Field label="/health" title={`GET ${base}/health`}>
          <Badge tone={httpTone(card.health?.http_status, card.health?.status)} dot={false}>
            {card.health?.http_status ?? "—"} {card.health?.status ?? ""}
          </Badge>
        </Field>
        <Field label="/health/ready" title={t("readyTitle", base)}>
          <Badge tone={httpTone(card.ready?.http_status, card.ready?.status)} dot={false}>
            {card.ready?.http_status ?? "—"} {card.ready?.status ?? ""}
          </Badge>
          {card.ready?.db && <span style={{ color: K.muted, fontSize: 12 }}>{t("db", card.ready.db)}</span>}
        </Field>
        <Field label={t("schema")} title={t("newestMigration", name)}>
          <span style={{ fontFamily: mono }}>{card.ready?.schema_version ?? "—"}</span>
        </Field>
      </Fields>
      <ExtLink href={card.health_url} title={t("publicHealth", name)}>{base}/health</ExtLink>
    </>
  );
}

const CRM_JOBS_KEY = ["platform-apps", "crm-jobs"] as const;

function CrmBody({ card }: { card: CrmCard }) {
  const t = useT(APPS);
  return (
    <>
      <BookingBody card={card} name={t("nameCrm")} base="/crm" />
      <CrmJobs />
    </>
  );
}

/** AA-936 — the CRM's job backlog per kind, read with the staff member's own token (no cache, no polling). */
function CrmJobs() {
  const t = useT(APPS);
  const jobs = useQuery({
    queryKey: CRM_JOBS_KEY,
    queryFn: () => apiGet<CrmJobsResp>(`${APPS_PATH}/crm/jobs`),
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });
  const data = jobs.data;
  return (
    <div data-testid="platform-app-crm-jobs" style={{ display: "grid", gap: 6 }}>
      <div style={{ fontSize: 12, fontWeight: 600, color: K.muted, letterSpacing: "0.06em", textTransform: "uppercase" }}>
        {t("crmJobs")}
      </div>
      {jobs.isLoading && <SkeletonBar w="60%" />}
      {jobs.isError && <div style={noteStyle}>{t("crmJobsError")}</div>}
      {data && data.status !== "ok" && (
        <div style={noteStyle}>
          {data.reason === "cognito_session_required"
            ? t("crmSignIn")
            : t("crmNoAnswer", String(data.http_status ?? data.reason ?? data.status))}
        </div>
      )}
      {data && data.status === "ok" && data.kinds.length === 0 && <div style={noteStyle}>{t("noJobs")}</div>}
      {data && data.status === "ok" && data.kinds.length > 0 && (
        <DataTable<CrmJobKind>
          tableId="platform-crm-jobs"
          data={data.kinds}
          getRowId={(k) => k.kind}
          searchable={false}
          paginate={false}
          compact
          columns={[
            { id: "kind", header: t("colKind"), accessorKey: "kind",
              cell: ({ row }) => <span style={{ fontFamily: mono }}>{row.original.kind}</span> },
            { id: "queued", header: t("colQueued"), accessorKey: "queued" },
            { id: "running", header: t("colRunning"), accessorKey: "running" },
            { id: "failed", header: t("colFailed"), accessorKey: "failed_24h",
              cell: ({ row }) => <span style={{ color: row.original.failed_24h > 0 ? K.danger : K.body }}>{row.original.failed_24h}</span> },
            { id: "oldest", header: t("colOldest"), accessorFn: (k) => k.oldest_queued_age_s ?? -1,
              cell: ({ row }) => row.original.oldest_queued_age_s == null ? "—" : `${row.original.oldest_queued_age_s} s` },
          ]}
        />
      )}
    </div>
  );
}

const LEVEL: Record<string, { tone: Tone; color: string; label: "levelOk" | "levelWarn" | "levelCap" }> = {
  ok: { tone: "success", color: K.success, label: "levelOk" },
  warn: { tone: "warning", color: K.amber, label: "levelWarn" },
  cap_reached: { tone: "danger", color: K.danger, label: "levelCap" },
};

type CrmJobKind = CrmJobsResp["kinds"][number];

function TripPlannerBody({ card }: { card: TripPlannerCard }) {
  const t = useT(APPS);
  const s = card.spend_today;
  return (
    <>
      {card.note && <div style={noteStyle}>{card.note}</div>}
      {!s ? (
        <div data-testid="platform-tp-ai-paused" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {/* AA-804: TripPlanner fails closed when its spend is unreadable (root ADR 0009), so AI is paused. */}
          {card.ai?.paused !== false && (
            <span><Badge tone="danger" dot={false}>{t("aiPaused")}</Badge></span>
          )}
          <div style={{ ...noteStyle, color: K.danger }}>
            {t("spendUnreadable", card.error ?? "")} {card.ai?.paused !== false ? t("aiPausedNote") : ""}
          </div>
        </div>
      ) : (
        <div data-testid="platform-tp-spend" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <div style={{ display: "flex", alignItems: "baseline", gap: 8, flexWrap: "wrap" }}>
            <span style={{ fontSize: 12.5, color: K.muted }}>{t("spendToday")}</span>
            <span style={{ fontSize: 18, fontWeight: 600 }}>{usd(s.cost_usd)}</span>
            <span style={{ fontSize: 12.5, color: K.muted }}>{t("ofCap", usd(s.cap_usd))}</span>
            <Badge tone={(LEVEL[s.level] ?? LEVEL.ok).tone} dot={false}>
              {s.pct_of_cap == null ? "—" : `${s.pct_of_cap} %`}
            </Badge>
          </div>
          <SpendBar pct={s.pct_of_cap} color={(LEVEL[s.level] ?? LEVEL.ok).color} label={t((LEVEL[s.level] ?? LEVEL.ok).label)} />
          <div style={noteStyle}>
            {t("callsSince", s.calls, s.since, s.stages.join(", "))} {capitalize(s.cap_label)}.
          </div>
          {/* AA-1015: per-stage caps and the near-cap state (each stage with its spend today). */}
          <TpStageCaps spend={s} testId="platform-tp-stage-caps" />
        </div>
      )}
    </>
  );
}

function capitalize(text: string): string {
  return text ? text[0].toUpperCase() + text.slice(1) : text;
}

function SpendBar({ pct, color, label }: { pct: number | null; color: string; label: string }) {
  const w = Math.max(0, Math.min(100, pct ?? 0));
  return (
    <div role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={w} title={label}
      style={{ height: 8, borderRadius: RADIUS.pill, background: K.line2, overflow: "hidden" }}>
      <div style={{ width: `${w}%`, height: "100%", background: color, borderRadius: RADIUS.pill }} />
    </div>
  );
}

// ── frontends ───────────────────────────────────────────────────────────────────────────────

function FrontendsRow() {
  const t = useT(APPS);
  const adminLabel = useAdminLabel();
  return (
    <section data-testid="platform-frontends" style={{ ...cardStyle, marginTop: 16, gap: 8 }}>
      <div style={{ fontWeight: 600, fontSize: 14 }}>{t("frontends")}</div>
      <div style={noteStyle}>{t("frontendsNote")}</div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: "8px 20px" }}>
        {PLATFORM_APP_IDS.map((id) => {
          const app = APP_REGISTRY[id];
          return (
            <span key={id} style={{ display: "inline-flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              <ExtLink href={app.adminUrl}>{adminLabel(id)}</ExtLink>
              <span style={{ fontSize: 12, color: K.muted }}>{t("noVersion")}</span>
            </span>
          );
        })}
      </div>
    </section>
  );
}
