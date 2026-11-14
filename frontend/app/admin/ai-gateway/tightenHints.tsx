"use client";
// app/admin/ai-gateway/tightenHints.tsx — S280 (AA-1096) review round: what a tighten-only role (no settings.write,
// S279) sees in the two AI providers editors that change a limit outside the Budgets tab: an app profile
// (AppProfilesCard) and a connection's limits and cost policy (ConnectionsPanel › Settings).
//
// The role reads each limit now in force beside its field, the switches it may not loosen locked (AI off, bulk paused,
// the budget stop on), and what its change would loosen before Save; Save stays blocked until the change only
// tightens. The API stays the authority (admin_ai_gateway._check_profile_tighten_only, llm_connections.
// _refuse_loosening, a 403); these are hints. Kit only.

import { K, useT } from "../../_kit";
import { usd, type AppProfile } from "@/lib/aiGateway";
import type { Connection, EditField } from "@/lib/llmConnections";
import { DEFAULT_ALERT_PCT } from "@/lib/llmConnections";
import {
  APP_PROFILE_KINDS, CONNECTION_KINDS, connectionLimits, loosenedFields, profileLimits,
} from "@/lib/capTighten";
import { PROFILES } from "./appProfiles.messages";
import { CONNECTIONS } from "./connections.messages";
import { usePageAccess } from "./usePageAccess";

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;

/** A muted note line (nothing when there is no text). */
export function NoteLine({ text, testId }: { text: string | null; testId?: string }) {
  if (!text) return null;
  return <p style={{ ...small, margin: 0 }} data-testid={testId}>{text}</p>;
}

/** An alert line (nothing when there is no text). */
export function AlertLine({ text, testId }: { text: string | null; testId?: string }) {
  if (!text) return null;
  return <span role="alert" data-testid={testId} style={{ fontSize: 12, color: K.danger }}>{text}</span>;
}

/** The fields of an app profile PATCH that a tighten-only role may not loosen (none for another role). */
function profileWorse(tightenOnly: boolean, profile: AppProfile, body: Record<string, unknown> | string): string[] {
  if (!tightenOnly || typeof body === "string") return [];
  return loosenedFields(profileLimits(profile), profileLimits(profile, body), APP_PROFILE_KINDS);
}

/** Which profile switches a tighten-only role may not move (AI off, bulk paused) and what is disabled while saving. */
function profileLocks(tightenOnly: boolean, profile: AppProfile, busy: boolean, loosens: boolean) {
  const ai = tightenOnly && !profile.ai_enabled;
  const bulk = tightenOnly && profile.bulk_paused;
  return { ai, bulk, aiDisabled: busy || ai, bulkDisabled: busy || bulk, blocked: busy || loosens };
}

/** The app profile editor for a tighten-only role: it may switch AI off, pause bulk work and set or lower the default
 *  budget, never the reverse. `busy`: a save is running (every control is off). */
export function useProfileTighten(profile: AppProfile, body: Record<string, unknown> | string, busy: boolean) {
  const t = useT(PROFILES);
  const { tightenOnly } = usePageAccess();
  const worse = profileWorse(tightenOnly, profile, body);
  const name: Record<string, string> = {
    ai_off: t("fieldAi"), bulk_paused: t("fieldBulk"), default_daily_budget_usd: t("fieldBudget"),
  };
  const lock = profileLocks(tightenOnly, profile, busy, worse.length > 0);
  const budget = tightenOnly ? profile.default_daily_budget_usd : null;
  return {
    note: tightenOnly ? t("tightenNote") : null,
    worse: worse.length ? t("tightenWorse", worse.map((f) => name[f] ?? f).join(", ")) : null,
    aiHint: lock.ai ? t("tightenAiLocked") : undefined,
    bulkHint: lock.bulk ? t("tightenBulkLocked") : t("bulkHint"),
    budgetHint: budget != null ? t("tightenBudgetHint", usd(budget, 2)) : t("budgetHint"),
    aiDisabled: lock.aiDisabled,
    bulkDisabled: lock.bulkDisabled,
    blocked: lock.blocked,
  };
}

/** A connection's settings form for a tighten-only role: each limit now in force as its hint, the budget stop locked
 *  on while it is on, and what the PATCH body would loosen. `locked`: the session may not edit at all (read-only). */
export function useConnectionTighten(conn: Connection, body: Record<string, unknown>, locked: boolean, busy: boolean) {
  const t = useT(CONNECTIONS);
  const tightenOnly = usePageAccess().tightenOnly && !locked;
  const now = connectionLimits(conn, DEFAULT_ALERT_PCT);
  const worse = tightenOnly ? loosenedFields(now, connectionLimits(conn, DEFAULT_ALERT_PCT, body), CONNECTION_KINDS) : [];
  const stopLocked = tightenOnly && now.budget_hard_stop === true;
  const name: Partial<Record<string, string>> = {
    max_retries: t("retries"), max_concurrency: t("concurrency"), rpm_limit: t("rpm"), monthly_budget_usd: t("budget"),
    budget_alert_pct: t("alertPct"), budget_hard_stop: t("hardStop"),
  };
  const hintOf = (f: EditField) => {
    const v = now[f];
    return tightenOnly && typeof v === "number" ? t("tightenNow", String(v)) : t("emptyMeansDefault");
  };
  return {
    note: tightenOnly ? t("tightenNote") : null,
    worse: worse.length ? t("tightenWorse", worse.map((f) => name[f] ?? f).join(", ")) : null,
    hintOf,
    stopHint: stopLocked ? t("tightenStopLocked") : t("hardStopHint"),
    stopDisabled: busy || locked || stopLocked,
    blocked: busy || worse.length > 0,
  };
}
