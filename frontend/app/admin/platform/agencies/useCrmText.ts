"use client";
// app/admin/platform/agencies/useCrmText.ts — AA-994: the lib/crmConsole.ts helpers bound to the UI language (words
// from lib/crmConsole.messages.ts). English outside an I18nProvider. A code the page does not know prints as it is.

import { useMemo } from "react";
import { useLang } from "../../../_kit";
import type { Catalog, Lang, Messages } from "../../../_kit/core/i18n";
import {
  DATA_REQUEST_KINDS,
  FEATURES,
  FEED_STATE_LABEL,
  LOCK_REASONS,
  LOCK_SOURCE_LABEL,
  RECOVERY_REASONS,
  crmErrorText,
  crmWrite,
  dataRequestStatus,
  lockSourceText,
  membershipFeed,
  platformMembershipFeed,
  stateLabel,
  type CrmWords,
  type LockSource,
  type MembershipFeedState,
  type TenantSummary,
  type WriteResult,
} from "@/lib/crmConsole";
import {
  CRM_DATA_KIND,
  CRM_DATA_STATUS,
  CRM_ERRORS,
  CRM_FEATURE,
  CRM_FEED,
  CRM_LOCK_REASON,
  CRM_LOCK_SOURCE,
  CRM_RECOVERY_REASON,
  CRM_STATE,
  CRM_WORDS_MESSAGES,
} from "@/lib/crmConsole.messages";

type FeedSummary = Pick<TenantSummary, "membership_feed_state" | "membership_feed_lag_s">;

function table<E extends Catalog>(m: Messages<E>, lang: Lang): Record<string, string> {
  return (lang === "vi" ? m.vi : m.en) as unknown as Record<string, string>;
}

const label = (t: Record<string, string>, code: string) => (Object.hasOwn(t, code) ? t[code] : code);

export function useCrmText() {
  const lang = useLang();
  return useMemo(() => {
    const w = (lang === "vi" ? CRM_WORDS_MESSAGES.vi : CRM_WORDS_MESSAGES.en) as CrmWords;
    const errors = table(CRM_ERRORS, lang);
    const state = table(CRM_STATE, lang);
    const ls = table(CRM_LOCK_SOURCE, lang);
    const fs = table(CRM_FEED, lang);
    const reasons = table(CRM_LOCK_REASON, lang);
    const features = table(CRM_FEATURE, lang);
    const recovery = table(CRM_RECOVERY_REASON, lang);
    const kinds = table(CRM_DATA_KIND, lang);
    const statuses = table(CRM_DATA_STATUS, lang);
    const lockLabels = Object.fromEntries(
      (Object.keys(LOCK_SOURCE_LABEL) as LockSource[]).map((k) => [
        k, { label: ls[k], short: ls[`${k}Short`], detail: ls[`${k}Detail`] },
      ]),
    ) as typeof LOCK_SOURCE_LABEL;
    const feedLabels = Object.fromEntries(
      (Object.keys(FEED_STATE_LABEL) as MembershipFeedState[]).map((k) => [
        k, { ...FEED_STATE_LABEL[k], label: fs[k], detail: fs[`${k}Detail`] },
      ]),
    ) as typeof FEED_STATE_LABEL;
    return {
      words: w,
      /** The agency state badge (label in the UI language, tone from the lib). */
      state: (s: string) => ({ ...stateLabel(s), label: label(state, s) }),
      lockSource: (s: Pick<TenantSummary, "state" | "lock_source">) => lockSourceText(s, w, lockLabels),
      feed: (s: FeedSummary) => membershipFeed(s, w, feedLabels),
      platformFeed: (items: ReadonlyArray<FeedSummary>) => platformMembershipFeed(items, w, feedLabels),
      errorText: (status: number, detail: unknown, message?: unknown) => crmErrorText(status, detail, message, w, errors),
      write: <T = Record<string, unknown>>(method: "POST" | "PUT", path: string, body?: unknown): Promise<WriteResult<T>> =>
        crmWrite<T>(method, path, body, w, errors),
      lockReasons: LOCK_REASONS.map((r) => ({ code: r.code, label: label(reasons, r.code) })),
      features: FEATURES.map((f) => ({ code: f.code, label: label(features, f.code) })),
      recoveryReasons: RECOVERY_REASONS.map((r) => ({ code: r.code, label: label(recovery, r.code) })),
      dataKinds: DATA_REQUEST_KINDS.map((k) => ({ code: k.code, label: label(kinds, k.code) })),
      featureLabel: (code: string) => label(features, code),
      kindLabel: (code: string) => label(kinds, code),
      dataStatus: (s: string) => ({ ...dataRequestStatus(s), label: label(statuses, s) }),
    };
  }, [lang]);
}
