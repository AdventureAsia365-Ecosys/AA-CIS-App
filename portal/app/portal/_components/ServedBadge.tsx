// portal/app/portal/_components/ServedBadge.tsx
// AA-986 L3 (root ADR 0015 decision 5, Q1 / Q2 / Q7) — the persistent badge on My Tours, Content and Publish.
// The API decides what to show (`served_notice` on every /v1 row: shared/tour_liveness.py ServedState.notice):
//   "discontinued" → "AA no longer serves this tour" (the tour was retired: deleted at the source or its Master
//                    trashed). The tenant keeps everything it made; new work on it is refused with 409.
//   "paused"       → "Paused" (AA paused the tour; it may come back). No notification is sent for a pause (Q1).
//   null           → nothing (served, or a supersede waiting for its new version: invisible to the tenant, Q2).
import { Badge, fmtDate } from "./ui";

/** The served fields every tenant /v1 row about a tour carries (AA-986 L3). */
export interface ServedFields {
  served_status?: "served" | "paused" | "retired" | null;
  served_reason?: string | null;
  discontinued_at?: string | null;
  served_notice?: "discontinued" | "paused" | null;
}

export function servedTitle(f: ServedFields | null | undefined): string | undefined {
  if (!f?.served_notice) return undefined;
  const since = f.discontinued_at ? ` since ${fmtDate(f.discontinued_at)}` : "";
  return f.served_notice === "discontinued"
    ? `Adventure Asia no longer serves this tour${since}. What you made for it stays here; new rewrites, posts and publishing are closed.`
    : `Adventure Asia has paused this tour${since}. New work on it is closed until it is back.`;
}

/** True when new work on the row's tour is refused (the API answers 409 tour_discontinued / tour_paused). */
export function workClosed(f: ServedFields | null | undefined): boolean {
  return f?.served_status === "retired" || f?.served_status === "paused";
}

export function ServedBadge({ fields }: { fields: ServedFields | null | undefined }) {
  if (!fields?.served_notice) return null;
  return (
    <span title={servedTitle(fields)} style={{ display: "inline-flex", maxWidth: "100%" }}>
      {fields.served_notice === "discontinued"
        ? <Badge variant="error">AA no longer serves this tour</Badge>
        : <Badge variant="warning">Paused</Badge>}
    </span>
  );
}

/** The tenant sentence of an API error: drops the machine code the 409s of AA-986 start with
 *  ("tour_discontinued: …", "tour_paused: …"), keeps any other message as it is. */
export function plainError(e: unknown, fallback: string): string {
  const msg = e instanceof Error ? e.message : typeof e === "string" ? e : "";
  return (msg || fallback).replace(/^(tour_discontinued|tour_paused):\s*/, "");
}
