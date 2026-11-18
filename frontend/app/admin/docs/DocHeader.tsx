"use client";
// app/admin/docs/DocHeader.tsx — AA-1133 B1: the page header of the docs reader, like market docs sites.
//
// Breadcrumb (Docs › sidebar group), the title (the body's own H1 is taken out by the page: lib/docsReader.ts
// splitLeadingH1), the description as the lead, then one line of facts: type, status, owner role, reviewed date, the
// date of the last commit to the file (root bundle `last_changed`), reading time, owner-only and Vietnamese-copy
// marks. Links (tech lead S283 c-s283-docs-github-links): Source and History on GitHub for every reader, Edit on
// GitHub for admins only, and only while the bundle names a public repo (lib/docsReader.ts githubLinks). The catalog id
// and path sit in a muted line under the facts. A page with `known_issue` shows it first as a warning (tech lead S283
// c-s283-docs-reader-access: the reader opens to all staff once the wrong staff pages are fixed or carry a banner).
// AA-1132: the facts line says "Last changed <date>" (bundle `last_changed`, else the newest `history` item), a page the
// docs sync engine flags shows a "Needs review" badge, and the collapsible "Page history" list follows (DocHistory.tsx).

import { ExternalLink, Lock } from "lucide-react";
import { Badge, K, RADIUS, mono, useFormat, useT, type Tone } from "../../_kit";
import { M } from "./messages";
import { DocHistory, NeedsReviewBadge } from "./DocHistory";
import { DOC_NAV_MESSAGES, DOC_TYPE_MESSAGES } from "@/lib/docsReader.messages";
import { lastChangedDate, navOf, ownerLabel, readingMinutes, type DocEntry, type DocGithubLinks } from "@/lib/docsReader";

const STATUS_TONE: Record<string, Tone> = {
  active: "success",
  accepted: "success",
  draft: "warning",
  proposed: "warning",
  deprecated: "neutral",
  superseded: "neutral",
};

function GithubLink({ href, label, testId }: { href: string; label: string; testId: string }) {
  const t = useT(M);
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer nofollow"
      title={t("githubTitle")}
      data-testid={testId}
      style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: 12, color: K.accentDeep,
               textDecoration: "none", whiteSpace: "nowrap" }}
    >
      {label} <ExternalLink size={11} aria-hidden="true" />
    </a>
  );
}

function KnownIssue({ note }: { note: string | null | undefined }) {
  const t = useT(M);
  if (!note) return null;
  return (
    <div
      role="note"
      data-testid="docs-known-issue"
      style={{ margin: "0 0 12px", padding: "8px 12px", borderLeft: `3px solid ${K.amber}`, background: K.amberSoft,
               borderRadius: RADIUS.sm, fontSize: 13, color: K.ink }}
    >
      <strong style={{ display: "block", fontSize: 11, letterSpacing: "0.04em", textTransform: "uppercase",
                       color: K.amber, marginBottom: 2 }}>
        {t("knownIssue")}
      </strong>
      {note}
    </div>
  );
}

function Breadcrumb({ page }: { page: DocEntry }) {
  const t = useT(M);
  const navLabel = useT(DOC_NAV_MESSAGES);
  const nav = navOf(page);
  return (
    <nav aria-label={t("breadcrumbAria")} style={{ fontSize: 12, color: K.muted, marginBottom: 6 }}>
      {t("title")}
      {nav ? <> › <span data-testid="docs-breadcrumb-group">{navLabel(nav)}</span></> : null}
    </nav>
  );
}

function Badges({ page }: { page: DocEntry }) {
  const t = useT(M);
  const typeLabel = useT(DOC_TYPE_MESSAGES);
  return (
    <>
      <Badge tone="info">{typeLabel(page.type)}</Badge>
      {page.status && <Badge tone={STATUS_TONE[page.status] ?? "neutral"}>{page.status}</Badge>}
      {page.access === "owner" && (
        <Badge tone="warning">
          <Lock size={11} /> {t("ownerOnly")}
        </Badge>
      )}
      {page.generated && <Badge tone="neutral">{t("generated")}</Badge>}
      <NeedsReviewBadge page={page} />
    </>
  );
}

/** Owner, reviewed, updated (last commit), reading time, Vietnamese copy: the ones the page has, "·" between. */
function Facts({ page, markdown, hasVietnamese }: { page: DocEntry; markdown: string; hasVietnamese: boolean }) {
  const t = useT(M);
  const { formatDate } = useFormat();
  const changed = lastChangedDate(page);
  const facts: React.ReactNode[] = [
    page.owner && <span key="owner">{t("ownerLabel", ownerLabel(page.owner))}</span>,
    page.last_reviewed && <span key="rev">{t("reviewedOn", page.last_reviewed)}</span>,
    changed && (
      <span key="upd" title={page.last_changed?.sha.slice(0, 12)} data-testid="docs-updated">
        {t("lastChangedOn", formatDate(changed))}
      </span>
    ),
    <span key="min">{t("minutesRead", readingMinutes(markdown))}</span>,
    hasVietnamese && <span key="vi">{t("hasVietnamese")}</span>,
  ].filter(Boolean);
  return (
    <span style={{ display: "inline-flex", flexWrap: "wrap", gap: 6, fontSize: 12, color: K.muted }}>
      {facts.flatMap((f, i) => (i ? [<span key={`s${i}`} aria-hidden="true" style={{ color: K.muted2 }}>·</span>, f] : [f]))}
    </span>
  );
}

function GithubLinks({ links }: { links: DocGithubLinks | null }) {
  const t = useT(M);
  if (!links) return null;
  return (
    <span style={{ display: "inline-flex", flexWrap: "wrap", gap: 12 }} data-testid="docs-github-links">
      <GithubLink href={links.source} label={t("viewSource")} testId="docs-link-source" />
      <GithubLink href={links.history} label={t("viewHistory")} testId="docs-link-history" />
      {links.edit && <GithubLink href={links.edit} label={t("editPage")} testId="docs-link-edit" />}
    </span>
  );
}

export function DocHeader({
  page, title, markdown, links, hasVietnamese, github,
}: {
  page: DocEntry;
  title: string;
  markdown: string;
  links: DocGithubLinks | null;
  hasVietnamese: boolean;
  /** The bundle's public repo ("<org>/<repo>", source.github), for the history links; null while it is private. */
  github?: string | null;
}) {
  const t = useT(M);
  return (
    <header style={{ marginBottom: 18 }} data-testid="docs-header">
      <Breadcrumb page={page} />
      <KnownIssue note={page.known_issue} />
      <h1 style={{ fontSize: 26, lineHeight: 1.25, fontWeight: 700, color: K.ink, margin: "0 0 6px" }}>{title}</h1>
      {page.description && (
        <p style={{ fontSize: 15, lineHeight: 1.55, color: K.ink2, margin: "0 0 10px" }} data-testid="docs-lead">
          {page.description}
        </p>
      )}
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6, marginBottom: 6 }}>
        <Badges page={page} />
        <Facts page={page} markdown={markdown} hasVietnamese={hasVietnamese} />
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
        <span style={{ fontSize: 11, color: K.muted2, fontFamily: mono, overflowWrap: "anywhere" }}>
          {t("pageInfo", page.id, page.path)}
        </span>
        <GithubLinks links={links} />
      </div>
      <DocHistory page={page} github={github} />
    </header>
  );
}
