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

/** The line shown instead of the Publish button when publishing is closed (null = open), with the 409's meaning:
 *  retired = closed for good; paused = for now; a supersede waiting for its new version (paused /
 *  superseded_pending) = a neutral line, never a discontinuation (Q2). */
export function publishClosedLabel(f: ServedFields | null | undefined): string | null {
  if (f?.served_status === "retired") return "Publishing closed: tour no longer served";
  if (f?.served_status === "paused") {
    return f.served_reason === "superseded_pending"
      ? "A new version of this tour is on the way: publish once it is out"
      : "Publishing paused while Adventure Asia has paused this tour";
  }
  return null;
}

/** The unpublish dialog's sentence about what happens to the piece afterwards, per served state (only `retired`
 *  says the tour is no longer served; a pause is temporary; a supersede stays neutral, Q2). */
export function afterUnpublishSentence(f: ServedFields | null | undefined): string {
  if (f?.served_status === "retired") {
    return "The piece stays in My Content. Adventure Asia no longer serves this tour, so it cannot be published again.";
  }
  if (f?.served_status === "paused") {
    return f.served_reason === "superseded_pending"
      ? "The piece stays in My Content and goes back to Ready to Publish; you can publish it again once the new "
        + "version of this tour is out."
      : "The piece stays in My Content and goes back to Ready to Publish. Adventure Asia has paused this tour for "
        + "now, so you can publish it again once the tour is back.";
  }
  return "The piece stays in My Content and goes back to Ready to Publish, so you can publish it again.";
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
