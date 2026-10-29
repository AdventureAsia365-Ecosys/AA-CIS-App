"use client";
// app/admin/tenants/_components/Tenant360Settings.tsx
// AA-766 — Tenant 360 Settings tab: the editors AA-666 left as "a follow-up".
//   - Plan & rate limit -> PATCH /admin/tenants/{id} {plan_tier?, rate_limit_rpm?}
//   - Daily publish limit (AA-819) -> PATCH /admin/tenants/{id} {daily_publish_limit: int | null}
//     (null clears the per-tenant override; the publish route then uses its code default)
//   - Planning config   -> PUT   /admin/tenants/{id}/config {markets, channels, posts_per_week}
// Both writes are admin-only (lib/rbac.ts API_WRITE_RULES "tenants" + backend
// _require_tenant_admin) and each writes an acp_shared.audit_log row with before/after (AA-797),
// which the Audit tab shows. Optimistic: the saved values switch immediately and roll back with
// an error toast if the server refuses. AA-994: text in the UI language (tenant360.messages.ts).

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle } from "lucide-react";
import { A, mono, sans } from "../../_components/adminUi";
import {
  Badge, Button, ErrorState, Section, Spinner, ToastProvider, apiGet, apiSend, useLang, useT, useToast,
} from "../../../_kit";
import {
  DAILY_PUBLISH_LIMIT_DEFAULT, DAILY_PUBLISH_LIMIT_MAX, PLANNING_CHANNELS, RATE_LIMIT_RPM_MAX,
  SEO_SUPPORTED_MARKETS, type TenantConfig, fmtDailyPublishLimit, parseDailyPublishLimit,
  parseMarkets, validateConfig, validateDailyPublishLimit, validateRpm,
} from "./tenant360";
import { T360, T360_WORDS_MESSAGES } from "./tenant360.messages";

const PLAN_CHOICES = ["starter", "growth", "business"];

const labelStyle: React.CSSProperties = {
  fontSize: 10.5, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em",
  color: A.muted, display: "block", marginBottom: 6,
};
const inputStyle: React.CSSProperties = {
  padding: "8px 11px", background: A.card, border: `1px solid ${A.line}`, borderRadius: 8,
  color: A.body, fontSize: 13, fontFamily: sans, outline: "none", boxSizing: "border-box",
  maxWidth: "100%",
};

function FieldError({ msg }: { msg?: string | null }) {
  if (!msg) return null;
  return (
    <div role="alert" style={{ marginTop: 5, fontSize: 11.5, color: A.red, display: "flex", alignItems: "center", gap: 5 }}>
      <AlertCircle size={12} /> {msg}
    </div>
  );
}

// ── Plan & rate limit ─────────────────────────────────────────────────────────

interface PatchResult { changed: string[]; plan_tier: string; rate_limit_rpm: number }

function PlanForm({ tenantId, planTier, rateLimitRpm, locked, onChanged }: {
  tenantId: string; planTier: string; rateLimitRpm: number; locked: boolean; onChanged: () => void;
}) {
  const t = useT(T360);
  const words = T360_WORDS_MESSAGES[useLang()];
  const toast = useToast();
  const [saved, setSaved] = useState({ plan: planTier, rpm: rateLimitRpm });
  const [plan, setPlan] = useState(planTier);
  const [rpm, setRpm] = useState(String(rateLimitRpm));
  const rpmError = validateRpm(rpm, words);
  const planDirty = plan !== saved.plan;
  const rpmDirty = Number(rpm) !== saved.rpm;
  const choices = PLAN_CHOICES.includes(saved.plan) ? PLAN_CHOICES : [saved.plan, ...PLAN_CHOICES];

  const m = useMutation({
    mutationFn: (body: { plan_tier?: string; rate_limit_rpm?: number }) =>
      apiSend<PatchResult>(`/api/admin/tenants/${tenantId}`, { method: "PATCH", body, admin: true }),
    onMutate: (body) => {
      const prev = saved;
      setSaved({ plan: body.plan_tier ?? prev.plan, rpm: body.rate_limit_rpm ?? prev.rpm });
      return { prev };
    },
    onError: (err, _body, ctx) => {
      if (ctx) { setSaved(ctx.prev); setPlan(ctx.prev.plan); setRpm(String(ctx.prev.rpm)); }
      toast.error(t("planNotSaved", (err as Error).message));
    },
    onSuccess: (res) => {
      // A plan change without an explicit RPM applies that plan's default — show what was stored.
      setSaved({ plan: res.plan_tier, rpm: res.rate_limit_rpm });
      setPlan(res.plan_tier); setRpm(String(res.rate_limit_rpm));
      toast.success(res.changed.length ? t("savedFields", res.changed.join(", ")) : t("nothingChanged"));
      onChanged();
    },
  });

  function save() {
    if (rpmError || (!planDirty && !rpmDirty)) return;
    const body: { plan_tier?: string; rate_limit_rpm?: number } = {};
    if (planDirty) body.plan_tier = plan;
    if (rpmDirty) body.rate_limit_rpm = Number(rpm);
    m.mutate(body);
  }

  return (
    <Section title={t("planTitle")} description={t("planHint")}>
      <div style={{ display: "flex", gap: 16, flexWrap: "wrap", alignItems: "flex-start" }}>
        <div>
          <label style={labelStyle} htmlFor={`plan-${tenantId}`}>{t("plan")}</label>
          <select id={`plan-${tenantId}`} value={plan} disabled={locked || m.isPending}
            onChange={e => setPlan(e.target.value)} style={{ ...inputStyle, minWidth: 140 }}>
            {choices.map(p => <option key={p} value={p}>{p}</option>)}
          </select>
        </div>
        <div>
          <label style={labelStyle} htmlFor={`rpm-${tenantId}`}>{t("rateLimit")}</label>
          <input id={`rpm-${tenantId}`} type="number" min={1} max={RATE_LIMIT_RPM_MAX} value={rpm}
            disabled={locked || m.isPending} onChange={e => setRpm(e.target.value)}
            aria-invalid={!!rpmError} style={{ ...inputStyle, width: 130 }} />
          <FieldError msg={rpmError} />
        </div>
      </div>
      <div style={{ display: "flex", gap: 8, marginTop: 14, alignItems: "center", flexWrap: "wrap" }}>
        <Button variant="primary" size="sm" onClick={save}
          disabled={locked || m.isPending || !!rpmError || (!planDirty && !rpmDirty)}>
          {m.isPending ? <><Spinner size={12} /> {t("saving")}</> : t("savePlan")}
        </Button>
        {(planDirty || rpmDirty) && !m.isPending && (
          <Button variant="ghost" size="sm" onClick={() => { setPlan(saved.plan); setRpm(String(saved.rpm)); }}>{t("reset")}</Button>
        )}
        <span style={{ fontSize: 11.5, color: A.muted2 }}>
          {t("current")} <span style={{ fontFamily: mono }}>{saved.plan} · {t("perMin", saved.rpm)}</span>
        </span>
      </div>
    </Section>
  );
}

// ── Daily publish limit (AA-819) ──────────────────────────────────────────────

// The PATCH answers with the stored override when the backend knows the column; until then the
// key may be absent, so fall back to what was sent.
interface PublishLimitPatchResult { changed: string[]; daily_publish_limit?: number | null }

function PublishLimitForm({ tenantId, dailyPublishLimit, locked, onChanged }: {
  tenantId: string; dailyPublishLimit: number | null | undefined; locked: boolean; onChanged: () => void;
}) {
  const t = useT(T360);
  const words = T360_WORDS_MESSAGES[useLang()];
  const toast = useToast();
  // undefined = GET /admin/tenants does not return the key: the AA-819 backend is not deployed, so
  // there is no limit to show or set yet. Keep the field read-only rather than let a save 422.
  const supported = dailyPublishLimit !== undefined;
  const [saved, setSaved] = useState<number | null>(dailyPublishLimit ?? null);
  const [value, setValue] = useState(dailyPublishLimit == null ? "" : String(dailyPublishLimit));
  const error = validateDailyPublishLimit(value, words);
  const dirty = !error && parseDailyPublishLimit(value) !== saved;

  const m = useMutation({
    mutationFn: (limit: number | null) =>
      apiSend<PublishLimitPatchResult>(`/api/admin/tenants/${tenantId}`, {
        method: "PATCH", body: { daily_publish_limit: limit }, admin: true,
      }),
    onMutate: (limit) => {
      const prev = saved;
      setSaved(limit);
      return { prev };
    },
    onError: (err, _limit, ctx) => {
      if (ctx) { setSaved(ctx.prev); setValue(ctx.prev == null ? "" : String(ctx.prev)); }
      toast.error(t("dplNotSaved", (err as Error).message));
    },
    onSuccess: (res, limit) => {
      const stored = res.daily_publish_limit === undefined ? limit : res.daily_publish_limit;
      setSaved(stored);
      setValue(stored == null ? "" : String(stored));
      toast.success(res.changed.length ? t("savedFields", res.changed.join(", ")) : t("nothingChanged"));
      onChanged();
    },
  });

  return (
    <Section title={t("dplTitle")}
      description={supported ? t("dplHint", DAILY_PUBLISH_LIMIT_DEFAULT) : t("dplUnsupported")}>
      <div>
        <label style={labelStyle} htmlFor={`dpl-${tenantId}`}>{t("dplLabel")}</label>
        <input id={`dpl-${tenantId}`} type="number" min={1} max={DAILY_PUBLISH_LIMIT_MAX} value={value}
          placeholder={t("dplPlaceholder", DAILY_PUBLISH_LIMIT_DEFAULT)}
          disabled={!supported || locked || m.isPending} onChange={e => setValue(e.target.value)}
          aria-invalid={!!error} style={{ ...inputStyle, width: 160 }} />
        <FieldError msg={error} />
      </div>
      <div style={{ display: "flex", gap: 8, marginTop: 14, alignItems: "center", flexWrap: "wrap" }}>
        <Button variant="primary" size="sm" onClick={() => m.mutate(parseDailyPublishLimit(value))}
          disabled={!supported || locked || m.isPending || !!error || !dirty}>
          {m.isPending ? <><Spinner size={12} /> {t("saving")}</> : t("saveLimit")}
        </Button>
        {supported && saved != null && !m.isPending && (
          <Button variant="ghost" size="sm" disabled={locked} onClick={() => m.mutate(null)}>{t("useDefault")}</Button>
        )}
        <span style={{ fontSize: 11.5, color: A.muted2 }}>
          {t("current")} <span style={{ fontFamily: mono }}>{fmtDailyPublishLimit(supported ? saved : undefined, words)}</span>
        </span>
      </div>
    </Section>
  );
}

// ── Planning config (markets / channels / posts per week) ─────────────────────

// `server` is the react-query cache value (the parent's useQuery re-renders on every optimistic
// write / rollback); the form's own state is seeded from it once, at mount.
function ConfigForm({ tenantId, server, locked }: { tenantId: string; server: TenantConfig; locked: boolean }) {
  const t = useT(T360);
  const words = T360_WORDS_MESSAGES[useLang()];
  const toast = useToast();
  const qc = useQueryClient();
  const key = ["tenant360", tenantId, "config"];
  const [marketsText, setMarketsText] = useState(server.markets.join(", "));
  const [channels, setChannels] = useState<string[]>(server.channels);
  const [ppw, setPpw] = useState(String(server.posts_per_week));

  const markets = parseMarkets(marketsText);
  const errors = validateConfig(markets, channels, ppw, words);
  const valid = Object.keys(errors).length === 0;
  const dirty = markets.join(",") !== server.markets.join(",")
    || [...channels].sort().join(",") !== [...server.channels].sort().join(",")
    || Number(ppw) !== server.posts_per_week;
  const notResearched = markets.filter(mk => /^[A-Z]{2}$/.test(mk) && !SEO_SUPPORTED_MARKETS.includes(mk));

  const m = useMutation({
    mutationFn: (body: Omit<TenantConfig, "tenant_id">) =>
      apiSend<TenantConfig>(`/api/admin/tenants/${tenantId}/config`, { method: "PUT", body, admin: true }),
    onMutate: async (body) => {
      await qc.cancelQueries({ queryKey: key });
      const prev = qc.getQueryData<TenantConfig>(key);
      qc.setQueryData<TenantConfig>(key, { tenant_id: tenantId, ...body });
      return { prev };
    },
    onError: (err, _body, ctx) => {
      // Roll the saved values back; the form keeps the admin's edits so they can retry.
      if (ctx?.prev) qc.setQueryData(key, ctx.prev);
      toast.error(t("configNotSaved", (err as Error).message));
    },
    onSuccess: (res) => {
      qc.setQueryData(key, res);
      toast.success(t("configSaved"));
    },
  });

  function toggleChannel(c: string) {
    setChannels(prev => (prev.includes(c) ? prev.filter(x => x !== c) : [...prev, c]));
  }

  return (
    <Section title={t("configTitle")} description={t("configHint")}>
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <div>
          <label style={labelStyle} htmlFor={`markets-${tenantId}`}>{t("marketsLabel")}</label>
          <input id={`markets-${tenantId}`} value={marketsText} disabled={locked || m.isPending}
            onChange={e => setMarketsText(e.target.value)} placeholder="US, UK, DE"
            aria-invalid={!!errors.markets} style={{ ...inputStyle, width: 320 }} />
          <FieldError msg={errors.markets} />
          {!errors.markets && notResearched.length > 0 && (
            <div style={{ marginTop: 5, fontSize: 11.5, color: A.amber }}>
              {t("notResearched", notResearched.join(", "), SEO_SUPPORTED_MARKETS.join(", "))}
            </div>
          )}
        </div>
        <div>
          <span style={labelStyle}>{t("channels")}</span>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            {PLANNING_CHANNELS.map(c => {
              const on = channels.includes(c);
              return (
                <button key={c} type="button" aria-pressed={on} disabled={locked || m.isPending}
                  onClick={() => toggleChannel(c)}
                  style={{
                    padding: "5px 11px", borderRadius: 20, fontSize: 12, fontFamily: sans,
                    cursor: locked ? "not-allowed" : "pointer", fontWeight: on ? 700 : 400,
                    border: `1px solid ${on ? A.accent : A.line}`, background: on ? A.accentTint : A.bg,
                    color: on ? A.accentDeep : A.muted,
                  }}>{c}</button>
              );
            })}
          </div>
          <FieldError msg={errors.channels} />
        </div>
        <div>
          <label style={labelStyle} htmlFor={`ppw-${tenantId}`}>{t("postsPerWeek")}</label>
          <input id={`ppw-${tenantId}`} type="number" min={1} max={14} value={ppw}
            disabled={locked || m.isPending} onChange={e => setPpw(e.target.value)}
            aria-invalid={!!errors.posts_per_week} style={{ ...inputStyle, width: 100 }} />
          <FieldError msg={errors.posts_per_week} />
        </div>
      </div>
      <div style={{ display: "flex", gap: 8, marginTop: 14, alignItems: "center", flexWrap: "wrap" }}>
        <Button variant="primary" size="sm" disabled={locked || m.isPending || !valid || !dirty}
          onClick={() => m.mutate({ markets, channels, posts_per_week: Number(ppw) })}>
          {m.isPending ? <><Spinner size={12} /> {t("saving")}</> : t("saveConfig")}
        </Button>
        {dirty && !m.isPending && (
          <Button variant="ghost" size="sm" onClick={() => {
            setMarketsText(server.markets.join(", ")); setChannels(server.channels); setPpw(String(server.posts_per_week));
          }}>{t("reset")}</Button>
        )}
        <span style={{ fontSize: 11.5, color: A.muted2, display: "inline-flex", gap: 4, flexWrap: "wrap", alignItems: "center" }}>
          {t("savedLabel")} {server.markets.map(mk => <Badge key={mk} tone="neutral" dot={false}>{mk}</Badge>)}
          <span style={{ fontFamily: mono }}>· {server.channels.join(", ")} · {t("perWeek", server.posts_per_week)}</span>
        </span>
      </div>
    </Section>
  );
}

function SettingsInner({ tenantId, planTier, rateLimitRpm, dailyPublishLimit, status, onChanged }: Tenant360SettingsProps) {
  const t = useT(T360);
  const locked = status === "offboarded";
  const cfg = useQuery({
    queryKey: ["tenant360", tenantId, "config"],
    queryFn: () => apiGet<TenantConfig>(`/api/admin/tenants/${tenantId}/config`, { admin: true }),
  });
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14, maxWidth: 640 }}>
      {locked && (
        <div style={{ padding: "9px 12px", background: A.amberSoft, border: `1px solid ${A.amber}`, borderRadius: 8, fontSize: 12, color: A.ink }}>
          {t("offboardedLock")}
        </div>
      )}
      <PlanForm tenantId={tenantId} planTier={planTier} rateLimitRpm={rateLimitRpm} locked={locked} onChanged={onChanged} />
      <PublishLimitForm tenantId={tenantId} dailyPublishLimit={dailyPublishLimit} locked={locked} onChanged={onChanged} />
      {cfg.isLoading ? (
        <div style={{ fontSize: 12, color: A.muted, display: "flex", gap: 6, alignItems: "center" }}><Spinner size={12} /> {t("loadingConfig")}</div>
      ) : cfg.isError || !cfg.data ? (
        <ErrorState message={t("configLoadError", (cfg.error as Error)?.message ?? t("unknownError"))} onRetry={() => cfg.refetch()} />
      ) : (
        <ConfigForm tenantId={tenantId} server={cfg.data} locked={locked} />
      )}
      <div style={{ fontSize: 11, color: A.muted2 }}>
        {t("settingsFootnote")}
      </div>
    </div>
  );
}

export interface Tenant360SettingsProps {
  tenantId: string;
  planTier: string;
  rateLimitRpm: number;
  /** AA-819 — null: no override, the code default applies; undefined: the server does not
   *  report the field yet (backend not deployed), so the editor stays read-only. */
  dailyPublishLimit?: number | null;
  status: "active" | "suspended" | "offboarded";
  onChanged: () => void;
}

export function Tenant360Settings(props: Tenant360SettingsProps) {
  return (
    <ToastProvider>
      <SettingsInner {...props} />
    </ToastProvider>
  );
}
