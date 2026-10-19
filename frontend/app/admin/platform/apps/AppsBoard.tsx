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
  formatDateTime,
  mono,
  sans,
  type Tone,
} from "../../../_kit";
import { APP_REGISTRY, PLATFORM_APP_IDS, isPlatformAppId } from "@/lib/apps";
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
  type TripPlannerCard,
} from "@/lib/platform";

const APPS_KEY = ["platform-apps"] as const;
const APPS_PATH = "/api/admin/platform/apps";


export default function AppsBoard() {
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
    <main className="aa-admin-main" style={{ flex: 1, minWidth: 0, overflowY: "auto", padding: "28px 36px 56px" }}>
      <SkeletonStyle />
      <PageHeader
        title="Apps"
        description="Health and the running commit of each app in the ecosystem, read only. An app's status is the worse of its own health check and its traffic over the last 24 hours — the same rule as the Overview page. Built when you open the page (cached 60 s); nothing polls in the background."
        actions={
          <Button variant="secondary" size="sm" onClick={() => refresh.mutate()} disabled={busy}
            title="Ask every app again now (skips the 60 s cache)">
            <RefreshCw size={13} /> {refresh.isPending ? "Checking…" : "Refresh"}
          </Button>
        }
      />

      {error && !data ? (
        <ErrorState message={error.message || "Could not load the apps"} onRetry={() => apps.refetch()} />
      ) : !data ? (
        <CardsSkeleton />
      ) : (
        <>
          {refresh.isError && (
            <div style={{ ...noteStyle, color: K.danger, marginBottom: 12 }}>
              Refresh failed: {(refresh.error as Error)?.message || "unknown error"}. Showing the previous answer.
            </div>
          )}
          <div data-testid="platform-apps-ready" style={gridStyle}>
            {data.apps.map((a) => <AppCardView key={a.id} card={a} />)}
          </div>
          <FrontendsRow />
          <div style={{ ...noteStyle, marginTop: 14 }}>
            Built {formatDateTime(data.generated_at)}{data._cached ? " (from the 60 s cache)" : ""}.
          </div>
        </>
      )}
    </main>
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
  const href = commitUrl ?? (isPlatformAppId(appId) ? APP_REGISTRY[appId].commitUrl(sha) : null);
  const text = shortSha(sha);
  if (!href) {
    return <span style={{ fontFamily: mono, color: K.muted }} title={sha ?? "no commit reported"}>{text}</span>;
  }
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" title={`${sha} — open the commit on GitHub`}
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
  const reg = isPlatformAppId(card.id) ? APP_REGISTRY[card.id] : null;
  const st = APP_STATUS_LABEL[card.status] ?? APP_STATUS_LABEL.unknown;
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
            <span style={{ color: K.muted }}> · {basisText(card.overall.basis)}</span>
          )}
        </div>
      )}
      <TrafficLine traffic={card.traffic ?? null} />
      {card.id === "cis" && <CisBody card={card} />}
      {card.id === "booking" && <BookingBody card={card} />}
      {card.id === "tripplanner" && <TripPlannerBody card={card} />}
      <div style={{ display: "flex", gap: 14, flexWrap: "wrap", borderTop: `1px solid ${K.line}`, paddingTop: 10 }}>
        {reg && <ExtLink href={reg.adminUrl}>{reg.adminLabel}</ExtLink>}
        <ExtLink href={card.repo_url}>Repository</ExtLink>
      </div>
    </section>
  );
}

function CisBody({ card }: { card: CisCard }) {
  const workers = card.workers ?? [];
  let parity: { tone: Tone; label: string; title: string };
  if (card.parity === true) {
    parity = { tone: "success", label: "Same commit", title: "Every live worker runs the API's commit" };
  } else if (card.parity === false) {
    parity = { tone: "danger", label: "Mismatch", title: "A live worker runs another commit than the API, or no worker is live" };
  } else {
    parity = { tone: "neutral", label: "Not compared", title: "The API reports no commit (local / CI process) or the workers could not be read" };
  }
  const gate = card.deploy_gate;
  const gateDetail = gate
    ? Object.entries(gate.by_status).map(([s, n]) => `${n} ${s}`).join(", ")
    : "";
  return (
    <>
      <Fields>
        <Field label="API commit" title="APP_GIT_SHA of the image this API task runs (the merge commit)">
          <ShaLink appId="cis" sha={card.api.git_sha} commitUrl={card.api.commit_url} />
          {card.api.task_revision != null && (
            <span style={{ color: K.muted, fontSize: 12 }}>task def rev {card.api.task_revision}</span>
          )}
        </Field>
        <Field label="Workers" title="Live job workers (shared.job_worker, seen in the last 60 s)">
          <Badge tone={parity.tone} dot={false} style={{ cursor: "help" }}>
            <span title={parity.title}>{parity.label}</span>
          </Badge>
          <span style={{ color: K.muted, fontSize: 12 }}>
            {card.workers === null ? "unreadable" : `${workers.length} live`}
          </span>
        </Field>
        <Field label="Deploy gate" title="Queued + running jobs a deploy would interrupt (same answer as the CI guard)">
          {gate === null ? (
            <span style={{ color: K.muted }}>unreadable</span>
          ) : gate.safe_to_deploy ? (
            <Badge tone="success" dot={false}>Safe to deploy</Badge>
          ) : (
            <>
              <Badge tone="warning" dot={false}>{gate.active} active job{gate.active === 1 ? "" : "s"}</Badge>
              {gateDetail && <span style={{ color: K.muted, fontSize: 12 }}>{gateDetail}</span>}
            </>
          )}
        </Field>
        <Field label="Schema" title="Newest numeric CIS migration in shared.schema_versions vs the one this code needs">
          <span style={{ fontFamily: mono }}>{card.schema.version ?? "—"}</span>
          <span style={{ color: K.muted, fontSize: 12 }}>needs ≥ {card.schema.required ?? "—"}</span>
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
                <Badge tone="danger" dot={false}>differs</Badge>
              )}
              {w.task_revision != null && <span style={{ color: K.muted }}>rev {w.task_revision}</span>}
              <span style={{ color: K.muted }}>{w.running_jobs ?? 0} running</span>
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
  if (!traffic) {
    return <div style={noteStyle}>Traffic numbers (AWS CloudWatch) are not available right now.</div>;
  }
  const src = traffic.kind === "lambda" ? "Lambda" : "API Gateway";
  return (
    <div data-testid="platform-app-traffic" style={{
      display: "flex", flexWrap: "wrap", gap: "2px 12px", fontSize: 12, color: K.muted, padding: "6px 8px",
      borderRadius: RADIUS.md, background: K.bg, minWidth: 0,
    }} title={`AWS CloudWatch, ${src} ${(traffic.resources ?? []).join(" + ")}, last ${traffic.window_hours ?? 24} hours`}>
      <span style={{ color: K.body, fontWeight: 600 }}>Last {traffic.window_hours ?? 24} h</span>
      <span>{(traffic.requests ?? 0).toLocaleString("en-US")} requests</span>
      <span style={{ color: traffic.errors ? K.danger : K.muted }}>
        {(traffic.errors ?? 0).toLocaleString("en-US")} server errors
      </span>
      <span>95% within {fmtMs(traffic.p95_ms)}</span>
      {traffic.alarms_total != null && (
        <span style={{ color: traffic.alarms_firing ? K.danger : K.muted }}>
          {traffic.alarms_firing ? `${traffic.alarms_firing} alarm${traffic.alarms_firing > 1 ? "s" : ""} firing` : `${traffic.alarms_total} alarms quiet`}
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

function BookingBody({ card }: { card: BookingCard }) {
  if (card.status === "unknown" && card.error) {
    return (
      <>
        <div style={noteStyle}>
          Could not reach Booking&apos;s health endpoint ({card.error}). The card says unknown, not down: the probe
          has a 2 s timeout.
        </div>
        <ExtLink href={card.health_url} title="Booking's public health endpoint">/booking/health</ExtLink>
      </>
    );
  }
  return (
    <>
      <Fields>
        <Field label="Commit" title="git_sha from GET /booking/health">
          <ShaLink appId="booking" sha={card.git_sha} commitUrl={card.commit_url} />
        </Field>
        <Field label="/health" title="GET /booking/health">
          <Badge tone={httpTone(card.health?.http_status, card.health?.status)} dot={false}>
            {card.health?.http_status ?? "—"} {card.health?.status ?? ""}
          </Badge>
        </Field>
        <Field label="/health/ready" title="GET /booking/health/ready (DB + schema)">
          <Badge tone={httpTone(card.ready?.http_status, card.ready?.status)} dot={false}>
            {card.ready?.http_status ?? "—"} {card.ready?.status ?? ""}
          </Badge>
          {card.ready?.db && <span style={{ color: K.muted, fontSize: 12 }}>db {card.ready.db}</span>}
        </Field>
        <Field label="Schema" title="Booking's newest applied migration (bk-NNN)">
          <span style={{ fontFamily: mono }}>{card.ready?.schema_version ?? "—"}</span>
        </Field>
      </Fields>
      <ExtLink href={card.health_url} title="Booking's public health endpoint">/booking/health</ExtLink>
    </>
  );
}

const LEVEL: Record<string, { tone: Tone; color: string; label: string }> = {
  ok: { tone: "success", color: K.success, label: "Under 80 % of the cap" },
  warn: { tone: "warning", color: K.amber, label: "Over 80 % of the cap" },
  cap_reached: { tone: "danger", color: K.danger, label: "Cap reached" },
};

function TripPlannerBody({ card }: { card: TripPlannerCard }) {
  const s = card.spend_today;
  return (
    <>
      {card.note && <div style={noteStyle}>{card.note}</div>}
      {!s ? (
        <div style={{ ...noteStyle, color: K.danger }}>
          Spend unreadable{card.error ? ` (${card.error})` : ""}.
        </div>
      ) : (
        <div data-testid="platform-tp-spend" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <div style={{ display: "flex", alignItems: "baseline", gap: 8, flexWrap: "wrap" }}>
            <span style={{ fontSize: 12.5, color: K.muted }}>LLM spend today</span>
            <span style={{ fontSize: 18, fontWeight: 600 }}>{usd(s.cost_usd)}</span>
            <span style={{ fontSize: 12.5, color: K.muted }}>of {usd(s.cap_usd)}</span>
            <Badge tone={(LEVEL[s.level] ?? LEVEL.ok).tone} dot={false}>
              {s.pct_of_cap == null ? "—" : `${s.pct_of_cap} %`}
            </Badge>
          </div>
          <SpendBar pct={s.pct_of_cap} color={(LEVEL[s.level] ?? LEVEL.ok).color} label={(LEVEL[s.level] ?? LEVEL.ok).label} />
          <div style={noteStyle}>
            {s.calls} call{s.calls === 1 ? "" : "s"} since {s.since} on the customer-facing stages
            ({s.stages.join(", ")}). {capitalize(s.cap_label)}.
          </div>
          {s.by_stage.length > 0 && (
            <ul style={{ margin: 0, padding: 0, listStyle: "none", fontSize: 12, display: "flex", flexDirection: "column", gap: 3 }}>
              {s.by_stage.map((b) => (
                <li key={b.stage} style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                  <span style={{ fontFamily: mono, color: K.ink2 }}>{b.stage}</span>
                  <span style={{ color: K.muted }}>{b.calls} calls</span>
                  <span>{usd(b.cost_usd)}</span>
                </li>
              ))}
            </ul>
          )}
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
  return (
    <section data-testid="platform-frontends" style={{ ...cardStyle, marginTop: 16, gap: 8 }}>
      <div style={{ fontWeight: 600, fontSize: 14 }}>Frontends</div>
      <div style={noteStyle}>
        The three web frontends (Vercel) do not report their version yet, so only links are shown here.
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: "8px 20px" }}>
        {PLATFORM_APP_IDS.map((id) => {
          const app = APP_REGISTRY[id];
          return (
            <span key={id} style={{ display: "inline-flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              <ExtLink href={app.adminUrl}>{app.adminLabel}</ExtLink>
              <span style={{ fontSize: 12, color: K.muted }}>version not reported yet</span>
            </span>
          );
        })}
      </div>
    </section>
  );
}
