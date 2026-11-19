// frontend/lib/appProfileApplies.ts — AA-1127 (S285): the "Takes effect" words of the App profiles card (AI providers ›
// Providers), per app. Its own file: lib/aiGateway.ts is at its code-health size baseline.

import type { AppProfile } from "./aiGateway.ts";

/** The "Takes effect" words of one app profile row (appProfiles.messages.ts PROFILES). */
export type AppliesKey = "appliesCis" | "appliesTripplanner" | "appliesCrm" | "appliesHere" | "appliesLater";

/** AA-1127 (S285 live evidence): "Takes effect" said "(TripPlanner within 20 s)" on every app's row. Each app now
 *  gets its own words: CIS and TripPlanner read the row through a 20 s per-process cache
 *  (shared/llm_client/app_profile.py CACHE_TTL_SECONDS, TripPlanner's own cache of catalog.v_llm_app_profile_v1); the
 *  CRM reads its copy on every AI call (the edit is relayed to it first); an app whose gateway does not act on the
 *  profile makes no AI call. */
export function appliesKey(p: Pick<AppProfile, "app" | "enforced_here">): AppliesKey {
  if (!p.enforced_here) return "appliesLater";
  if (p.app === "cis") return "appliesCis";
  if (p.app === "tripplanner") return "appliesTripplanner";
  if (p.app === "crm") return "appliesCrm";
  return "appliesHere";
}
