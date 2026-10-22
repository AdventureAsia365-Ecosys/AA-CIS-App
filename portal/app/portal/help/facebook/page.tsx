"use client";
// portal/app/portal/help/facebook/page.tsx — AA-831
//
// Tenant guide "Connect your Facebook Page", linked from the Connect Facebook Page card
// (FacebookConnect.tsx, FACEBOOK_GUIDE_HREF). Static content, no API calls. Covered by the
// /portal/:path* middleware rule like every other portal route (lib/rbac.ts: admin + tenant).
//
// What it walks through is what the T11 Facebook adapter needs: a numeric Page ID and a Page
// Access Token with pages_show_list + pages_read_engagement + pages_manage_posts, from a Meta app
// in Live mode (a Development-mode app's posts are visible only to people with a role on the app —
// Meta: developers.facebook.com/docs/development/build-and-test/app-modes). Token lifetimes follow
// Meta's "Get long-lived tokens" page: a long-lived User token lasts about 60 days, a Page token
// taken from it has no expiry date. The Common problems section quotes the exact messages the
// connection test returns (api/routers/v1_integrations.py _classify_facebook_test_failure).
//
// Built on the portal primitives (./_components/ui) like the T11 pages it links back to.
// No screenshots yet: Meta's dashboard changes often, so each step names the menu items instead.

import Link from "next/link";
import { ArrowLeft, ExternalLink, ShieldCheck } from "lucide-react";
import { T, sans, mono, Card, CardHead, PageHeader } from "../../_components/ui";
import { PermissionList } from "../../_components/FacebookConnect";

const META = {
  apps: "https://developers.facebook.com/apps",
  explorer: "https://developers.facebook.com/tools/explorer/",
  debugger: "https://developers.facebook.com/tools/debug/accesstoken/",
  tokensDoc: "https://developers.facebook.com/docs/facebook-login/guides/access-tokens/get-long-lived",
  appModesDoc: "https://developers.facebook.com/docs/development/build-and-test/app-modes",
};

const text: React.CSSProperties = { fontSize: 13.5, color: T.body, lineHeight: 1.65, margin: "0 0 10px" };
// Tailwind preflight sets `list-style: none` on ul/ol; the guide needs visible markers.
const list: React.CSSProperties = { ...text, paddingLeft: 20 };
const bullets: React.CSSProperties = { ...list, listStyleType: "disc" };
const numbers: React.CSSProperties = { ...list, listStyleType: "decimal" };
const codeStyle: React.CSSProperties = {
  fontFamily: mono, fontSize: 12, color: T.ink3, background: T.bg, border: `1px solid ${T.line}`,
  borderRadius: 4, padding: "0 5px", overflowWrap: "anywhere",
};
const sectionTitle: React.CSSProperties = {
  fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em",
  color: T.muted, margin: "8px 0 2px",
};

function Ext({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" style={{
      color: T.goldDeep, fontWeight: 600, textDecoration: "none", overflowWrap: "anywhere",
    }}>
      {children} <ExternalLink size={11} style={{ display: "inline", verticalAlign: "-1px" }} aria-hidden />
    </a>
  );
}

function C({ children }: { children: React.ReactNode }) {
  return <code style={codeStyle}>{children}</code>;
}

function Note({ children }: { children: React.ReactNode }) {
  return (
    <div style={{
      fontSize: 12.5, color: T.goldDeep, background: T.goldTint, border: `1px solid ${T.goldSoft}`,
      borderRadius: 8, padding: "10px 12px", lineHeight: 1.55, marginTop: 4,
    }}>{children}</div>
  );
}

function Step({ n, title, children }: { n: number; title: string; children: React.ReactNode }) {
  return (
    <Card style={{ padding: "20px 22px 14px" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 12 }}>
        <span style={{
          width: 28, height: 28, borderRadius: "50%", background: T.goldTint, color: T.goldDeep,
          border: `1px solid ${T.goldSoft}`, display: "grid", placeItems: "center",
          fontSize: 13, fontWeight: 700, flexShrink: 0, fontFamily: sans,
        }}>{n}</span>
        <h2 style={{ margin: 0, fontSize: 15.5, fontWeight: 600, color: T.ink, fontFamily: sans }}>
          {title}
        </h2>
      </div>
      {children}
    </Card>
  );
}

const PROBLEMS: { title: string; cause: React.ReactNode; fix: React.ReactNode }[] = [
  {
    title: "“Invalid or expired Page Access Token”",
    cause: <>The token was cut off when you copied it, it is the short-lived token from step 3 (it
      expires within a few hours), or Meta has cancelled it (see step 5).</>,
    fix: <>Repeat steps 3 to 5 and paste the new Page token. If you were already connected, use
      Publish &rarr; Manage connections &rarr; Change Page or token.</>,
  },
  {
    title: "“Facebook Page not found — check the Page ID”",
    cause: <>The Page ID is not the numeric ID of the Page, or the token belongs to a different
      Page.</>,
    fix: <>Use the number from step 6, not the Page name or its web address, and the token that
      was listed next to that same Page in step 5.</>,
  },
  {
    title: "Publishing fails with a message about a permission",
    cause: <>The token is missing one of the three permissions, or your Page was not selected when
      Facebook asked which Pages the app may use.</>,
    fix: <>Repeat steps 3 to 5. Add all three permissions and select your Page in the Facebook
      window before you click Continue.</>,
  },
  {
    title: "The connection works, but publishing fails",
    cause: <>You probably pasted the User token instead of the Page token. The User token can
      open your Page, but it cannot post as your Page.</>,
    fix: <>Paste the <C>access_token</C> shown next to your Page in step 5.</>,
  },
  {
    title: "Only you can see the posts; others see “This content isn’t available”",
    cause: <>Your Meta app is still in Development mode. In that mode Meta shows the app&rsquo;s
      posts only to people who have a role on the app, so the posts look fine to you.</>,
    fix: <>Switch the app to Live (step 2). Meta then shows the posts to everyone, including the
      ones made before the switch.</>,
  },
];

export default function FacebookGuidePage() {
  return (
    <div style={{ maxWidth: 760 }}>
      {/* AA-760 — back to Integrations: that page holds the Page ID + token form this guide ends in
          (Publish is not a sidebar item, so "Back to Publish" named a place the tenant could not find). */}
      <Link href="/portal/account/integrations" style={{
        display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12.5, fontWeight: 600,
        color: T.muted, textDecoration: "none", marginBottom: 16, fontFamily: sans,
      }}>
        <ArrowLeft size={13} /> Back to Integrations
      </Link>

      <PageHeader
        title="Connect your Facebook Page"
        sub="A one-time setup of about 15 minutes. At the end you paste a Page ID and a Page Access Token into the portal, and your content can be published to your Page."
      />

      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        <Card>
          <CardHead title="Before you start" />
          <ul style={{ ...bullets, marginBottom: 6 }}>
            <li>A Facebook account that has full control of your Facebook Page (Page admin).</li>
            <li>A Meta developer account. It is free: sign in at <Ext href={META.apps}>developers.facebook.com</Ext> with
              that Facebook account and accept the terms.</li>
            <li>The web address of your privacy policy (for example a page on your website).</li>
          </ul>
          <p style={{ ...text, fontSize: 12.5, color: T.muted, marginBottom: 0 }}>
            Meta changes its screens from time to time. If a button has a slightly different name,
            look for the closest match.
          </p>
        </Card>

        <h2 style={sectionTitle}>Steps</h2>

        <Step n={1} title="Create or choose a Meta app">
          <p style={text}>
            Open <Ext href={META.apps}>My Apps</Ext>. If your business already has an app, you can
            use it. Otherwise click <strong>Create app</strong>, give it a name (for example
            your company name) and a contact email.
          </p>
          <p style={text}>
            When Meta asks what the app is for, choose the option for managing a Facebook Page
            (for example &ldquo;Manage everything on your Page&rdquo;). If Meta asks for an app
            type instead, choose <strong>Business</strong>.
          </p>
        </Step>

        <Step n={2} title="Add a privacy policy and switch the app to Live">
          <p style={text}>
            In the app dashboard, open <strong>App settings &rarr; Basic</strong>. Fill in the
            <strong> Privacy Policy URL</strong> and a category, and save. Meta does not allow
            Live mode without a privacy policy.
          </p>
          <p style={text}>
            Then switch <strong>App Mode</strong> from <strong>Development</strong> to
            <strong> Live</strong>. In newer dashboards this is the <strong>Publish</strong> item
            in the left menu.
          </p>
          <Note>
            Why this matters: while the app is in Development mode, its posts are shown only to
            people with a role on the app, which includes you. Your customers would see
            &ldquo;This content isn&rsquo;t available&rdquo;.{" "}
            <Ext href={META.appModesDoc}>Meta: app modes</Ext>
          </Note>
        </Step>

        <Step n={3} title="Get a User token with the three permissions">
          <p style={text}>Open the <Ext href={META.explorer}>Graph API Explorer</Ext>. On the right:</p>
          <ol style={numbers}>
            <li>Under <strong>Meta App</strong>, select your app.</li>
            <li>Under <strong>User or Page</strong>, choose <strong>Get User Access Token</strong>.</li>
            <li>Under <strong>Permissions</strong>, add <PermissionList />.</li>
            <li>Click <strong>Generate Access Token</strong>. In the Facebook window, select your
              Page and allow the permissions.</li>
          </ol>
          <p style={text}>
            The <strong>Access Token</strong> field now holds a User token. It is short-lived: it
            expires within a few hours, so continue straight to step 4.
          </p>
        </Step>

        <Step n={4} title="Make the User token long-lived">
          <ol style={numbers}>
            <li>Copy the token and open the <Ext href={META.debugger}>Access Token Debugger</Ext>.</li>
            <li>Paste the token and click <strong>Debug</strong>.</li>
            <li>At the bottom, click <strong>Extend Access Token</strong> and copy the new token
              it shows.</li>
          </ol>
          <p style={text}>
            This long-lived User token lasts about 60 days. You only need it for the next step.
          </p>
        </Step>

        <Step n={5} title="Get the Page Access Token">
          <ol style={numbers}>
            <li>Back in the <Ext href={META.explorer}>Graph API Explorer</Ext>, paste the
              long-lived User token into the <strong>Access Token</strong> field.</li>
            <li>In the query box, enter <C>me/accounts</C> and click <strong>Submit</strong>.</li>
            <li>Find your Page in the result. Copy its <C>access_token</C> (the Page Access
              Token) and its <C>id</C> (the Page ID).</li>
          </ol>
          <p style={text}>
            A Page token taken from a long-lived User token has no expiry date. To check, paste it
            into the Access Token Debugger: <strong>Expires</strong> should say
            <strong> Never</strong>.
          </p>
          <p style={text}>
            Meta can still cancel the token, for example when you change your Facebook password,
            lose your admin role on the Page, or remove the app from your account. If that
            happens, repeat steps 3 to 5.{" "}
            <Ext href={META.tokensDoc}>Meta: long-lived tokens</Ext>
          </p>
        </Step>

        <Step n={6} title="Find your Page ID">
          <p style={text}>
            The <C>id</C> next to your Page in step 5 is your Page ID. You can also find it on
            Facebook: open your Page, then <strong>About &rarr; Page transparency</strong>. It is
            a number such as <C>104123456789012</C>.
          </p>
        </Step>

        <Step n={7} title="Test & Connect in the portal">
          <p style={text}>
            Go to <Link href="/portal/content/publish" style={{ color: T.goldDeep, fontWeight: 600, textDecoration: "none" }}>Publish</Link>.
            In the <strong>Connect Facebook Page</strong> card, paste the Page ID and the Page
            Access Token, then click <strong>Test &amp; Connect</strong>.
          </p>
          <p style={text}>
            The test checks that the token can open your Page; it does not post anything. After
            your first post, open it in a private browser window to confirm that people who are
            not logged in can see it.
          </p>
        </Step>

        <Card style={{ display: "flex", gap: 12, alignItems: "flex-start" }}>
          <ShieldCheck size={18} color={T.green} style={{ flexShrink: 0, marginTop: 2 }} aria-hidden />
          <p style={{ ...text, margin: 0 }}>
            <strong>Keep the Page Access Token private.</strong> Anyone who has it can post to
            your Page. Paste it only into the portal. The portal stores it securely and never
            shows it again.
          </p>
        </Card>

        <h2 style={sectionTitle}>Common problems</h2>

        {PROBLEMS.map(p => (
          <Card key={p.title} style={{ padding: "18px 22px 12px" }}>
            <h3 style={{ margin: "0 0 10px", fontSize: 14, fontWeight: 600, color: T.ink, fontFamily: sans }}>
              {p.title}
            </h3>
            <p style={text}><strong style={{ color: T.ink3 }}>Why:</strong> {p.cause}</p>
            <p style={text}><strong style={{ color: T.ink3 }}>Fix:</strong> {p.fix}</p>
          </Card>
        ))}
      </div>
    </div>
  );
}
