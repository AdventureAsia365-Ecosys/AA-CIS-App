"use client";
// portal/app/portal/t11-publish/connection/page.tsx — AA-457 [T11 PR1] + AA-815
//
// The standalone "manage connections" half of Option 3+2 (AA-456 STEP0 §9): re-test, change
// credentials or disconnect each publish channel after the first inline connect on
// /portal/t11-publish. Shares the connect forms / status cards with that page.
// AA-815: Facebook Page next to WordPress, and Disconnect (confirm step) on both cards.
import Link from "next/link";
import { useState } from "react";
import { ArrowLeft } from "lucide-react";
import { T, sans, LoadingScreen, PageHeader } from "../../_components/ui";
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

  if ((wp.loading && !wp.status) || (fb.loading && !fb.status)) {
    return <LoadingScreen message="Loading connection status…" />;
  }

  const wpConnected = !!wp.status?.connected;
  const fbConnected = !!fb.status?.connected;

  return (
    <div style={{ maxWidth: 640 }}>
      <Link href="/portal/t11-publish" style={{
        display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12.5, fontWeight: 600,
        color: T.muted, textDecoration: "none", marginBottom: 16, fontFamily: sans,
      }}>
        <ArrowLeft size={13} /> Back to Publish
      </Link>

      <PageHeader
        title="Publish Connections"
        sub="Manage the WordPress site and the Facebook Page your content is published to."
      />

      <div style={{ display: "flex", flexDirection: "column", gap: 28 }}>
        <section style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <h2 style={sectionTitle}>WordPress</h2>
          {wpConnected && wp.status && !changingWp && (
            <>
              <WordPressStatusCard status={wp.status} onRetest={wp.refresh} onDisconnected={wp.refresh} />
              <button onClick={() => setChangingWp(true)} style={changeLinkStyle}>Change credentials</button>
            </>
          )}
          {(!wpConnected || changingWp) && (
            <WordPressConnectForm onConnected={() => { setChangingWp(false); wp.refresh(); }} />
          )}
        </section>

        <section style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <h2 style={sectionTitle}>Facebook Page</h2>
          {fbConnected && fb.status && !changingFb && (
            <>
              <FacebookStatusCard status={fb.status} onRetest={fb.refresh} onDisconnected={fb.refresh} />
              <button onClick={() => setChangingFb(true)} style={changeLinkStyle}>Change Page or token</button>
            </>
          )}
          {(!fbConnected || changingFb) && (
            <FacebookConnectForm onConnected={() => { setChangingFb(false); fb.refresh(); }} />
          )}
        </section>
      </div>
    </div>
  );
}
