"use client";
// app/admin/my-work/page.tsx — AA-1156: My work. AA-1190 (slice 1): the landing page of every staff role (lib/rbac.ts
// landingPage, admin included). The tiles the signed-in person may act on (shown by capability, never by person: the
// backend sends only those), in bands: Needs your approval, Content work, Platform, Keep an eye on. A tile with work
// shows its count and how long the oldest item has waited and opens its target page with the same filter the count
// used (lib/myWork.ts tileHref), so the two numbers match. Tiles with nothing waiting are folded into one line at the
// end ("Nothing waiting in N queues", opens to show them); "Keep an eye on" (backlog, policy decisions) is never in
// the total.
//
// API: GET /api/admin/my-work/summary (BFF → api/routers/admin_my_work.py). Loaded on open and on Refresh: no polling.
// "Booking: My work" opens the Booking staff dashboard (lib/apps.ts appPageUrl, same WORKFORCE sign-in) for the content
// roles; Booking checks its own grant (a person without one sees Booking's refusal).

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, ExternalLink, RefreshCw } from "lucide-react";
import Link from "next/link";
import {
  Button, EmptyState, ErrorState, K, KpiTile, PageFrame, PageHeader, Section, apiGet, relativeTime, sans,
  useAdminIdentity, useFormat, useLang, useT,
} from "../../_kit";
import { appPageUrl } from "@/lib/apps";
import {
  MY_WORK_API, knownTiles, splitBands, tileHref, totalWaiting, type MyWorkGroup, type MyWorkSummary, type MyWorkTile,
  type MyWorkTileDef,
} from "@/lib/myWork";
import { can } from "@/lib/rbac";
import { MY_WORK } from "./messages";

const KEY = ["my-work-summary"] as const;

export default function MyWorkPage() {
  const t = useT(MY_WORK);
  const qc = useQueryClient();
  const q = useQuery({ queryKey: KEY, queryFn: () => apiGet<MyWorkSummary>(MY_WORK_API), retry: false });
  const refresh = () => { void qc.invalidateQueries({ queryKey: KEY }); };
  const tiles = knownTiles(q.data);

  let body: React.ReactNode;
  if (q.isError) body = <ErrorState message={(q.error as Error)?.message || t("loadError")} onRetry={refresh} />;
  else if (q.data && tiles.length === 0) body = <EmptyState title={t("nothing")} description={t("nothingDesc")} />;
  else body = <Bands data={q.data} tiles={tiles} />;

  return (
    <PageFrame>
      <PageHeader
        title={t("title")}
        description={t("description")}
        actions={<HeaderActions fetching={q.isFetching} onRefresh={refresh} />}
      />
      {body}
    </PageFrame>
  );
}

/** "Booking: My work" (content roles, until the Booking grant comes with the session) and Refresh. */
function HeaderActions({ fetching, onRefresh }: { fetching: boolean; onRefresh: () => void }) {
  const t = useT(MY_WORK);
  const role = useAdminIdentity().role;
  return (
    <span style={{ display: "inline-flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
      {can(role, "content.write") && (
        <a href={appPageUrl("booking", "/")} title={t("bookingLinkTitle")} data-testid="my-work-booking-link"
          style={bookingLink}>
          {t("bookingLink")} <ExternalLink size={12} />
        </a>
      )}
      <Button variant="secondary" size="sm" onClick={onRefresh} disabled={fetching}>
        <RefreshCw size={13} /> {fetching ? t("refreshing") : t("refresh")}
      </Button>
    </span>
  );
}

const BAND_TITLE: Record<MyWorkGroup, "bandApprove" | "bandWork" | "bandPlatform" | "bandWatch"> = {
  approve: "bandApprove", work: "bandWork", platform: "bandPlatform", watch: "bandWatch",
};
const BAND_DESC: Record<MyWorkGroup, "bandApproveDesc" | "bandWorkDesc" | "bandPlatformDesc" | "bandWatchDesc"> = {
  approve: "bandApproveDesc", work: "bandWorkDesc", platform: "bandPlatformDesc", watch: "bandWatchDesc",
};

/** The summary line, the bands with work, then the empty queues folded into one line (skeletons while loading). */
function Bands({ data, tiles }: { data?: MyWorkSummary; tiles: ReturnType<typeof knownTiles> }) {
  const t = useT(MY_WORK);
  const { formatDateTime } = useFormat();
  const [showEmpty, setShowEmpty] = useState(false);
  if (!data) {
    return (
      <Section title={t("title")} description={t("contentBandDesc")} testId="my-work-loading">
        <div style={grid}>{[0, 1, 2, 3].map((i) => <KpiTile key={i} label="" value={undefined} />)}</div>
      </Section>
    );
  }
  const total = totalWaiting(tiles.map((x) => x.tile));
  const summary = total ? t("totalWaiting", total) : t("allClear");
  const now = Date.parse(data.generated_at);
  const { bands, empty } = splitBands(tiles);
  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 16 }}>
      <p data-testid="my-work-total" style={summaryLine}>
        {summary} {t("updated", formatDateTime(data.generated_at))}
      </p>
      {bands.map((band) => (
        <Section key={band.group} title={t(BAND_TITLE[band.group])} description={t(BAND_DESC[band.group])}
          testId={`my-work-band-${band.group}`}>
          <div style={grid}>
            {band.tiles.map(({ tile, def }) => <Tile key={tile.key} tile={tile} def={def} now={now} />)}
          </div>
        </Section>
      ))}
      {empty.length > 0 && (
        <Section testId="my-work-empty">
          <Button variant="ghost" size="sm" onClick={() => setShowEmpty((v) => !v)} aria-expanded={showEmpty}
            testId="my-work-empty-toggle">
            {showEmpty ? <ChevronDown size={13} /> : <ChevronRight size={13} />} {t("emptyQueues", empty.length)}
          </Button>
          {showEmpty && (
            <div style={{ ...grid, marginTop: 12 }}>
              {empty.map(({ tile, def }) => <Tile key={tile.key} tile={tile} def={def} now={now} />)}
            </div>
          )}
        </Section>
      )}
    </div>
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
        valueTone={tile.available && tile.count && tile.counts_toward_total !== false ? "warning" : undefined}
        hint={hint}
        style={{ height: "100%" }}
      />
    </Link>
  );
}

const grid: React.CSSProperties = {
  display: "grid", gap: 12, gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 220px), 1fr))", minWidth: 0,
};

const summaryLine: React.CSSProperties = { margin: 0, fontSize: 13, color: K.muted, fontFamily: sans };

const bookingLink: React.CSSProperties = {
  display: "inline-flex", alignItems: "center", gap: 5, fontSize: 12.5, fontWeight: 600,
  color: K.accentDeep, textDecoration: "none", whiteSpace: "nowrap", fontFamily: sans,
};

const tileLink: React.CSSProperties = { display: "block", minWidth: 0, color: "inherit", textDecoration: "none" };
