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
// AA-1035 (directory capability UI): the plan select is disabled for a role that may not send plan_tier (neither
// tenant.write nor billing.write, e.g. customer_success, whose PATCH may still change the RPM); lib/tenantDirectory.ts.
// S280 (AA-1096): the daily publish default is the one the API reports (setting cis.publish.daily_limit_default), not
// a number in the UI; a tighten-only role (customer_success, S279) is told it can lower the RPM, the publish limit and
// the posts per week only, and a raise is blocked here with that reason instead of a 403.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle } from "lucide-react";
import {
  K, mono, sans,
  Badge, Button, ErrorState, Section, Spinner, ToastProvider, apiGet, apiSend, useLang, useT, useToast,
  useAdminIdentity,
  Input, Select,
  ChipButton,
} from "../../../_kit";
import {
  DAILY_PUBLISH_LIMIT_MAX, PLANNING_CHANNELS, RATE_LIMIT_RPM_MAX,
  SEO_SUPPORTED_MARKETS, type TenantConfig, dailyPublishDefaultText, fmtDailyPublishLimit, parseDailyPublishLimit,
  parseMarkets, validateConfig, validateDailyPublishLimit, validateRpm,
} from "./tenant360";
import { T360, T360_WORDS_MESSAGES } from "./tenant360.messages";
import { useContentTiers } from "./useContentTiers";
import { canPatchBillingFields } from "@/lib/tenantDirectory";
import { isTightenOnly } from "@/lib/rbac";
import { anyOf, isTightenOnlyRefusal } from "@/lib/capTighten";


const labelStyle: React.CSSProperties = {
  fontSize: 10.5, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em",
  color: K.muted, display: "block", marginBottom: 6,
};
const inputStyle: React.CSSProperties = {
  padding: "8px 11px", background: K.card, border: `1px solid ${K.line}`, borderRadius: 8,
  color: K.body, fontSize: 13, fontFamily: sans, outline: "none", boxSizing: "border-box",
  maxWidth: "100%",
};

function FieldError({ msg }: { msg?: string | null }) {
  if (!msg) return null;
  return (
    <div role="alert" style={{ marginTop: 5, fontSize: 11.5, color: K.danger, display: "flex", alignItems: "center", gap: 5 }}>
      <AlertCircle size={12} /> {msg}
    </div>
  );
}

// ── S280: tighten-only roles (no settings.write) may lower these limits only ──────────────────────────────────────

/** A refused save's text: the tighten-only 403 (admin.py TIGHTEN_ONLY) in the UI language, else the API's message. */
function saveErrorText(err: unknown, tightenRaise: string): string {
  return isTightenOnlyRefusal(err) ? tightenRaise : err instanceof Error ? err.message : String(err);
}

/** True when a tighten-only role's valid new value raises the limit now in force. */
function raisesLimit(tightenOnly: boolean, error: string | null | undefined, next: number, now: number): boolean {
  return tightenOnly && !error && next > now;
}

/** A field's error, else (S280) for a tighten-only role the limit now in force and that it may lower it only, or that
 *  a raise needs an administrator. */
function LimitFieldNote({ error, raise, show, nowText }: {
  error: string | null | undefined; raise: boolean; show: boolean; nowText: string;
}) {
  const t = useT(T360);
  if (error) return <FieldError msg={error} />;
  if (raise) return <FieldError msg={t("tightenRaise")} />;
  if (!show) return null;
  return <div style={{ marginTop: 5, fontSize: 11.5, color: K.muted }}>{t("tightenNow", nowText)}</div>;
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
  // AA-1028 R1 slice 3: the tiers of the plan catalogue (live public content plans), plus the tenant's own
  const { tiers: choices } = useContentTiers(saved.plan);
  const role = useAdminIdentity().role;
  const mayChangePlan = canPatchBillingFields(role);
  // S280: a tighten-only role may lower the RPM only (admin.py PATCH; a plan change is not open to it).
  const tightenOnly = isTightenOnly(role);
  const rpmRaise = raisesLimit(tightenOnly, rpmError, Number(rpm), saved.rpm);

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
      toast.error(t("planNotSaved", saveErrorText(err, t("tightenRaise"))));
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
    if (rpmError || rpmRaise || (!planDirty && !rpmDirty)) return;
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
          <Select id={`plan-${tenantId}`} value={plan} disabled={locked || m.isPending || !mayChangePlan}
            onChange={e => setPlan(e.target.value)} style={{ ...inputStyle, minWidth: 140 }}>
            {choices.map(p => <option key={p} value={p}>{p}</option>)}
          </Select>
        </div>
        <div>
          <label style={labelStyle} htmlFor={`rpm-${tenantId}`}>{t("rateLimit")}</label>
          <Input id={`rpm-${tenantId}`} type="number" min={1} max={RATE_LIMIT_RPM_MAX} value={rpm}
            disabled={locked || m.isPending} onChange={e => setRpm(e.target.value)}
            aria-invalid={!!rpmError} style={{ ...inputStyle, width: 130 }} />
          <LimitFieldNote error={rpmError} raise={rpmRaise} show={tightenOnly} nowText={t("perMin", saved.rpm)} />
        </div>
      </div>
      <div style={{ display: "flex", gap: 8, marginTop: 14, alignItems: "center", flexWrap: "wrap" }}>
        <Button variant="primary" size="sm" onClick={save}
          disabled={anyOf(locked, m.isPending, rpmError, rpmRaise, !planDirty && !rpmDirty)}>
          {m.isPending ? <><Spinner size={12} /> {t("saving")}</> : t("savePlan")}
        </Button>
        {(planDirty || rpmDirty) && !m.isPending && (
          <Button variant="ghost" size="sm" onClick={() => { setPlan(saved.plan); setRpm(String(saved.rpm)); }}>{t("reset")}</Button>
        )}
        <span style={{ fontSize: 11.5, color: K.muted2 }}>
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

/** S280: whether a tighten-only role's change raises the publish limit in force (null = the platform default), and
 *  whether "Use default" would. An unknown default (older API) counts as a raise, as the API may refuse it. */
function publishRaises(tightenOnly: boolean, saved: number | null, next: number | null, defaultLimit: number | null) {
  if (!tightenOnly) return { raise: false, defaultRaises: false };
  const raises = (v: number | null) => {
    const now = saved ?? defaultLimit;
    const after = v ?? defaultLimit;
    return now == null || after == null || after > now;
  };
  return { raise: next !== saved && raises(next), defaultRaises: saved != null && raises(null) };
}

function PublishLimitForm({ tenantId, dailyPublishLimit, defaultLimit, locked, onChanged }: {
  tenantId: string; dailyPublishLimit: number | null | undefined; defaultLimit: number | null; locked: boolean;
  onChanged: () => void;
}) {
  const t = useT(T360);
  const words = T360_WORDS_MESSAGES[useLang()];
  const tightenOnly = isTightenOnly(useAdminIdentity().role);
  const dflt = dailyPublishDefaultText(defaultLimit, words);
  const toast = useToast();
  // undefined = GET /admin/tenants does not return the key: the AA-819 backend is not deployed, so
  // there is no limit to show or set yet. Keep the field read-only rather than let a save 422.
  const supported = dailyPublishLimit !== undefined;
  const [saved, setSaved] = useState<number | null>(dailyPublishLimit ?? null);
  const [value, setValue] = useState(dailyPublishLimit == null ? "" : String(dailyPublishLimit));
  const error = validateDailyPublishLimit(value, words);
  const dirty = !error && parseDailyPublishLimit(value) !== saved;
  const { raise, defaultRaises } = publishRaises(tightenOnly, saved, dirty ? parseDailyPublishLimit(value) : saved,
    defaultLimit);

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
      toast.error(t("dplNotSaved", saveErrorText(err, t("tightenRaise"))));
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
      description={supported ? t("dplHint", dflt) : t("dplUnsupported")}>
      <div>
        <label style={labelStyle} htmlFor={`dpl-${tenantId}`}>{t("dplLabel")}</label>
        <Input id={`dpl-${tenantId}`} type="number" min={1} max={DAILY_PUBLISH_LIMIT_MAX} value={value}
          placeholder={t("dplPlaceholder", dflt)}
          disabled={!supported || locked || m.isPending} onChange={e => setValue(e.target.value)}
          aria-invalid={!!error} style={{ ...inputStyle, width: 160 }} />
        <LimitFieldNote error={error} raise={raise} show={tightenOnly && supported}
          nowText={fmtDailyPublishLimit(saved, words, defaultLimit)} />
      </div>
      <div style={{ display: "flex", gap: 8, marginTop: 14, alignItems: "center", flexWrap: "wrap" }}>
        <Button variant="primary" size="sm" onClick={() => m.mutate(parseDailyPublishLimit(value))}
          disabled={anyOf(!supported, locked, m.isPending, error, !dirty, raise)}>
          {m.isPending ? <><Spinner size={12} /> {t("saving")}</> : t("saveLimit")}
        </Button>
        {supported && saved != null && !m.isPending && !defaultRaises && (
          <Button variant="ghost" size="sm" disabled={locked} onClick={() => m.mutate(null)}>{t("useDefault")}</Button>
        )}
        <span style={{ fontSize: 11.5, color: K.muted2 }}>
          {t("current")} <span style={{ fontFamily: mono }}>{fmtDailyPublishLimit(supported ? saved : undefined, words, defaultLimit)}</span>
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
  // S280: a tighten-only role may lower the posts per week only (admin.py PUT …/config).
  const tightenOnly = isTightenOnly(useAdminIdentity().role);
  const ppwRaise = raisesLimit(tightenOnly, errors.posts_per_week, Number(ppw), server.posts_per_week);

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
      toast.error(t("configNotSaved", saveErrorText(err, t("tightenRaise"))));
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
          <Input id={`markets-${tenantId}`} value={marketsText} disabled={locked || m.isPending}
            onChange={e => setMarketsText(e.target.value)} placeholder="US, UK, DE"
            aria-invalid={!!errors.markets} style={{ ...inputStyle, width: 320 }} />
          <FieldError msg={errors.markets} />
          {!errors.markets && notResearched.length > 0 && (
            <div style={{ marginTop: 5, fontSize: 11.5, color: K.amber }}>
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
                <ChipButton key={c} size="sm" selected={on} disabled={locked || m.isPending}
                  onClick={() => toggleChannel(c)}>{c}</ChipButton>
              );
            })}
          </div>
          <FieldError msg={errors.channels} />
        </div>
        <div>
          <label style={labelStyle} htmlFor={`ppw-${tenantId}`}>{t("postsPerWeek")}</label>
          <Input id={`ppw-${tenantId}`} type="number" min={1} max={14} value={ppw}
            disabled={locked || m.isPending} onChange={e => setPpw(e.target.value)}
            aria-invalid={!!errors.posts_per_week} style={{ ...inputStyle, width: 100 }} />
          <LimitFieldNote error={errors.posts_per_week} raise={ppwRaise} show={tightenOnly}
            nowText={t("perWeek", server.posts_per_week)} />
        </div>
      </div>
      <div style={{ display: "flex", gap: 8, marginTop: 14, alignItems: "center", flexWrap: "wrap" }}>
        <Button variant="primary" size="sm" disabled={anyOf(locked, m.isPending, !valid, !dirty, ppwRaise)}
          onClick={() => m.mutate({ markets, channels, posts_per_week: Number(ppw) })}>
          {m.isPending ? <><Spinner size={12} /> {t("saving")}</> : t("saveConfig")}
        </Button>
        {dirty && !m.isPending && (
          <Button variant="ghost" size="sm" onClick={() => {
            setMarketsText(server.markets.join(", ")); setChannels(server.channels); setPpw(String(server.posts_per_week));
          }}>{t("reset")}</Button>
        )}
        <span style={{ fontSize: 11.5, color: K.muted2, display: "inline-flex", gap: 4, flexWrap: "wrap", alignItems: "center" }}>
          {t("savedLabel")} {server.markets.map(mk => <Badge key={mk} tone="neutral" dot={false}>{mk}</Badge>)}
          <span style={{ fontFamily: mono }}>· {server.channels.join(", ")} · {t("perWeek", server.posts_per_week)}</span>
        </span>
      </div>
    </Section>
  );
}

function SettingsInner({ tenantId, planTier, rateLimitRpm, dailyPublishLimit, dailyPublishDefault = null, status,
  onChanged }: Tenant360SettingsProps) {
  const t = useT(T360);
  const locked = status === "offboarded";
  const cfg = useQuery({
    queryKey: ["tenant360", tenantId, "config"],
    queryFn: () => apiGet<TenantConfig>(`/api/admin/tenants/${tenantId}/config`, { admin: true }),
  });
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14, maxWidth: 640 }}>
      {locked && (
        <div style={{ padding: "9px 12px", background: K.amberSoft, border: `1px solid ${K.amber}`, borderRadius: 8, fontSize: 12, color: K.ink }}>
          {t("offboardedLock")}
        </div>
      )}
      <PlanForm tenantId={tenantId} planTier={planTier} rateLimitRpm={rateLimitRpm} locked={locked} onChanged={onChanged} />
      <PublishLimitForm tenantId={tenantId} dailyPublishLimit={dailyPublishLimit} defaultLimit={dailyPublishDefault}
        locked={locked} onChanged={onChanged} />
      {cfg.isLoading ? (
        <div style={{ fontSize: 12, color: K.muted, display: "flex", gap: 6, alignItems: "center" }}><Spinner size={12} /> {t("loadingConfig")}</div>
      ) : cfg.isError || !cfg.data ? (
        <ErrorState message={t("configLoadError", (cfg.error as Error)?.message ?? t("unknownError"))} onRetry={() => cfg.refetch()} />
      ) : (
        <ConfigForm tenantId={tenantId} server={cfg.data} locked={locked} />
      )}
      <div style={{ fontSize: 11, color: K.muted2 }}>
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
  /** S280 — the platform default in force (tenant360 dailyPublishDefault); null when the API did not report it. */
  dailyPublishDefault?: number | null;
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
