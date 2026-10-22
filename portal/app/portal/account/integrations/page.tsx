"use client";
// portal/app/portal/account/integrations/page.tsx — AA-457 [T11 PR1] + AA-815
// AA-759: was /portal/t11-publish/connection (308 from the old slug, next.config.ts).
//
// The standalone "manage connections" half of Option 3+2 (AA-456 STEP0 §9): re-test, change
// credentials or disconnect each publish channel after the first inline connect on
// /portal/content/publish. Shares the connect forms / status cards with that page.
// AA-815: Facebook Page next to WordPress, and Disconnect (confirm step) on both cards.
import { useState } from "react";
import { T, sans, CardSkeleton, PageHeader } from "../../_components/ui";
import { useWordPressStatus, WordPressConnectForm, WordPressStatusCard } from "../../_components/WordPressConnect";
import { useFacebookStatus, FacebookConnectForm, FacebookStatusCard } from "../../_components/FacebookConnect";

const changeLinkStyle: React.CSSProperties = {
  alignSelf: "flex-start", background: "none", border: "none", cursor: "pointer",
  fontSize: 12.5, fontWeight: 600, color: T.muted, textDecoration: "underline",
  fontFamily: sans, padding: 0,
};

const sectionTitle: React.CSSProperties = {
  fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em",
  color: T.muted, margin: "0 0 2px",
};

export default function T11PublishConnectionPage() {
  const wp = useWordPressStatus();
  const fb = useFacebookStatus();
  const [changingWp, setChangingWp] = useState(false);
  const [changingFb, setChangingFb] = useState(false);

  // AA-760 — the header and section titles render at once; each section shows a card skeleton until
  // its own status answers (was one full-page spinner waiting for both calls).
  const wpPending = wp.loading && !wp.status;
  const fbPending = fb.loading && !fb.status;
  const wpConnected = !!wp.status?.connected;
  const fbConnected = !!fb.status?.connected;

  return (
    <div style={{ maxWidth: 640 }}>
      {/* AA-760 — no "Back to Publish" link: this page is an Account item in the sidebar, and Publish
          is not a sidebar item, so the link named a place the tenant could not find. */}
      <PageHeader
        title="Integrations"
        sub="Manage the WordPress site and the Facebook Page your content is published to."
      />

      <div style={{ display: "flex", flexDirection: "column", gap: 28 }}>
        <section style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <h2 style={sectionTitle}>WordPress</h2>
          {wpPending && <CardSkeleton label="Loading WordPress connection" />}
          {!wpPending && wpConnected && wp.status && !changingWp && (
            <>
              <WordPressStatusCard status={wp.status} onRetest={wp.refresh} onDisconnected={wp.refresh} />
              <button onClick={() => setChangingWp(true)} style={changeLinkStyle}>Change credentials</button>
            </>
          )}
          {!wpPending && (!wpConnected || changingWp) && (
            <WordPressConnectForm onConnected={() => { setChangingWp(false); wp.refresh(); }} />
          )}
        </section>

        <section style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <h2 style={sectionTitle}>Facebook Page</h2>
          {fbPending && <CardSkeleton label="Loading Facebook connection" />}
          {!fbPending && fbConnected && fb.status && !changingFb && (
            <>
              <FacebookStatusCard status={fb.status} onRetest={fb.refresh} onDisconnected={fb.refresh} />
              <button onClick={() => setChangingFb(true)} style={changeLinkStyle}>Change Page or token</button>
            </>
          )}
          {!fbPending && (!fbConnected || changingFb) && (
            <FacebookConnectForm onConnected={() => { setChangingFb(false); fb.refresh(); }} />
          )}
        </section>
      </div>
    </div>
  );
}
