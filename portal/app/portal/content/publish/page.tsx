"use client";
// portal/app/portal/content/publish/page.tsx — AA-457 [T11 PR1] + AA-458 [T11 PR2] + AA-815
// AA-759: was /portal/t11-publish (308 from the old slug, next.config.ts).
//
// AA-457 built the connect flow (Option 3+2, AA-456 STEP0 §9): a channel that isn't connected yet
// shows its connect form inline here. AA-458 added the "Ready to Publish" list (PublishPendingList),
// which stays visible even when nothing is connected (its Publish buttons disable with "Connect
// <channel> to publish") so a tenant always sees what they have ready.
//
// AA-815: two channels — WordPress (blog) and a Facebook Page — each with its own connect form /
// status card, the pending list gated per channel, and a "Published" list with Unpublish
// (PublishedList). An Unpublish puts the piece back in Ready to Publish (reloadSignal).
import Link from "next/link";
import { useState } from "react";
import { Settings2 } from "lucide-react";
import { T, sans, CardSkeleton, PageHeader } from "../../_components/ui";
import { useWordPressStatus, WordPressConnectForm, WordPressStatusCard } from "../../_components/WordPressConnect";
import { useFacebookStatus, FacebookConnectForm, FacebookStatusCard } from "../../_components/FacebookConnect";
import { PublishPendingList } from "../../_components/PublishPendingList";
import { PublishedList } from "../../_components/PublishedList";

export default function T11PublishPage() {
  const wp = useWordPressStatus();
  const fb = useFacebookStatus();
  const [pendingReload, setPendingReload] = useState(0);

  // AA-760 — no full-page spinner any more: the header and both lists render at once, and each
  // channel card shows its own skeleton until that channel's status answers (one slow call used to
  // blank the whole page). A later refresh (after connect / re-test) keeps the card up.
  const wpPending = wp.loading && !wp.status;
  const fbPending = fb.loading && !fb.status;
  const wpConnected = !!wp.status?.connected;
  const fbConnected = !!fb.status?.connected;

  return (
    // AA-524 — the connection cards keep a form-comfortable width; the content lists are full
    // width. The H1 is not sticky; PublishPendingList's own StickyBar is the one sticky point.
    <div>
      <PageHeader
        title="Publish"
        sub="Publish your content to your WordPress blog and your Facebook Page, and unpublish it from here when you need to."
        action={(wpConnected || fbConnected) ? (
          <Link href="/portal/account/integrations" style={{
            display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12.5,
            fontWeight: 600, color: T.ink3, textDecoration: "none", whiteSpace: "nowrap", fontFamily: sans,
          }}>
            <Settings2 size={13} /> Manage connections
          </Link>
        ) : undefined}
      />

      <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
        <div style={{
          display: "grid", gap: 16, alignItems: "start",
          gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 340px), 1fr))",
        }}>
          {wpPending ? <CardSkeleton label="Loading WordPress connection" />
            : wpConnected && wp.status
              ? <WordPressStatusCard status={wp.status} onRetest={wp.refresh} />
              : <WordPressConnectForm onConnected={wp.refresh} />}
          {fbPending ? <CardSkeleton label="Loading Facebook connection" />
            : fbConnected && fb.status
              ? <FacebookStatusCard status={fb.status} onRetest={fb.refresh} />
              : <FacebookConnectForm onConnected={fb.refresh} />}
        </div>

        <PublishPendingList
          connected={{ blog: wpConnected, facebook: fbConnected }}
          reloadSignal={pendingReload}
        />

        <PublishedList onUnpublished={() => setPendingReload(n => n + 1)} />
      </div>
    </div>
  );
}
