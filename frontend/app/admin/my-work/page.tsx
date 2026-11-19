"use client";
// app/admin/my-work/page.tsx — AA-1156: My work, the landing page of the content roles (lib/rbac.ts landingPage; admin
// lands on Overview and finds My work in the Home group). One band, Content: a tile per work queue the signed-in role
// may work (shown by capability, never by person: the backend sends only the tiles of the caller's capabilities). Each
// tile shows the count, how long the oldest item has waited and opens its target page with the same filter the count
// used (lib/myWork.ts tileHref), so the two numbers match.
//
// API: GET /api/admin/my-work/summary (BFF → api/routers/admin_my_work.py). Loaded on open and on Refresh: no polling.
// "Booking: My work" opens the Booking staff dashboard (lib/apps.ts appPageUrl, same WORKFORCE sign-in) for the content
// roles; Booking checks its own grant (a person without one sees Booking's refusal).

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, RefreshCw } from "lucide-react";
import Link from "next/link";
import {
  Button, EmptyState, ErrorState, K, KpiTile, PageFrame, PageHeader, Section, apiGet, relativeTime, sans,
  useAdminIdentity, useFormat, useLang, useT,
} from "../../_kit";
import { appPageUrl } from "@/lib/apps";
import { MY_WORK_API, knownTiles, tileHref, totalWaiting, type MyWorkSummary, type MyWorkTile, type MyWorkTileDef } from "@/lib/myWork";
import { can } from "@/lib/rbac";
import { MY_WORK } from "./messages";

const KEY = ["my-work-summary"] as const;

export default function MyWorkPage() {
  const t = useT(MY_WORK);
  const { formatDateTime } = useFormat();
  const qc = useQueryClient();
  const role = useAdminIdentity().role;
  const q = useQuery({ queryKey: KEY, queryFn: () => apiGet<MyWorkSummary>(MY_WORK_API), retry: false });
  const refresh = () => { void qc.invalidateQueries({ queryKey: KEY }); };
  const tiles = knownTiles(q.data);
  const total = totalWaiting(tiles.map((x) => x.tile));
  // Until Booking grants come with the session (a later slice), the content roles (content.write) get the link.
  const showBooking = can(role, "content.write");

  return (
    <PageFrame>
      <PageHeader
        title={t("title")}
        description={t("description")}
        actions={
          <span style={{ display: "inline-flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
            {showBooking && (
              <a href={appPageUrl("booking", "/")} title={t("bookingLinkTitle")} data-testid="my-work-booking-link"
                style={{
                  display: "inline-flex", alignItems: "center", gap: 5, fontSize: 12.5, fontWeight: 600,
                  color: K.accentDeep, textDecoration: "none", whiteSpace: "nowrap", fontFamily: sans,
                }}>
                {t("bookingLink")} <ExternalLink size={12} />
              </a>
            )}
            <Button variant="secondary" size="sm" onClick={refresh} disabled={q.isFetching}>
              <RefreshCw size={13} /> {q.isFetching ? t("refreshing") : t("refresh")}
            </Button>
          </span>
        }
      />

      {q.isError ? (
        <ErrorState message={(q.error as Error)?.message || t("loadError")} onRetry={refresh} />
      ) : q.data && tiles.length === 0 ? (
        <EmptyState title={t("nothing")} description={t("nothingDesc")} />
      ) : (
        <Section
          title={t("contentBand")}
          description={q.data
            ? `${total ? t("totalWaiting", total) : t("allClear")} ${t("updated", formatDateTime(q.data.generated_at))}`
            : t("contentBandDesc")}
          testId="my-work-content"
        >
          <div style={grid}>
            {q.data
              ? tiles.map(({ tile, def }) => (
                <Tile key={tile.key} tile={tile} def={def} now={Date.parse(q.data!.generated_at)} />))
              : [0, 1, 2, 3].map((i) => <KpiTile key={i} label="" value={undefined} />)}
          </div>
        </Section>
      )}
    </PageFrame>
  );
}

/** `now` = the summary's generated_at (when the counts were taken), so the age is stable across re-renders. */
function Tile({ tile, def, now }: { tile: MyWorkTile; def: MyWorkTileDef; now: number }) {
  const t = useT(MY_WORK);
  const lang = useLang();
  const label = t(def.key);
  const info = t(`${def.key}_info` as Parameters<typeof t>[0]);
  const hint = !tile.available
    ? t("unavailable")
    : tile.count && tile.oldest_at
      ? t("oldest", relativeTime(tile.oldest_at, now, lang))
      : t("noneWaiting");
  return (
    // The whole tile is the link (no InfoTip button inside it: no control nested in a link); what it counts is the
    // link's title.
    <Link href={tileHref(def, tile.filter)} data-testid={`my-work-tile-${tile.key}`} title={info} style={tileLink}>
      <KpiTile
        label={label}
        value={tile.available ? (tile.count ?? 0) : null}
        valueTone={tile.available && tile.count ? "warning" : undefined}
        hint={hint}
        style={{ height: "100%" }}
      />
    </Link>
  );
}

const grid: React.CSSProperties = {
  display: "grid", gap: 12, gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 220px), 1fr))", minWidth: 0,
};

const tileLink: React.CSSProperties = { display: "block", minWidth: 0, color: "inherit", textDecoration: "none" };
