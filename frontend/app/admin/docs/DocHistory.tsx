"use client";
// app/admin/docs/DocHistory.tsx — AA-1132: a page's change history in the docs reader header.
//
// The root docs bundle gives each page `history` (root build_docs_reader.py rule 13): the newest commits that touched
// the file, newest first, each {sha, date, subject, pr}; a redacted line (a change to pages the reader may not open)
// has no sha or PR. A collapsed disclosure ("Page history (n)") lists date, subject and, while the bundle names a public
// repo (`source.github`, the same rule as the Source / History links), links to the commit and the pull request on
// GitHub. Nothing shows when the page has no history (an older bundle, a shallow build, the generated changes page).
// NeedsReviewBadge: the kit Badge "Needs review" (warning) when the docs sync engine flags the page, with its reason
// (or a general hint) as the hover text.

import { ExternalLink } from "lucide-react";
import { Badge, K, RADIUS, mono, useFormat, useT } from "../../_kit";
import { M } from "./messages";
import { historyLinks, needsReview, pageHistory, type DocEntry, type DocHistoryItem } from "@/lib/docsReader";

function HistoryLink({ href, label, testId }: { href: string; label: string; testId: string }) {
  const t = useT(M);
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer nofollow"
      title={t("githubTitle")}
      data-testid={testId}
      style={{ display: "inline-flex", alignItems: "center", gap: 3, color: K.accentDeep, textDecoration: "none",
               whiteSpace: "nowrap" }}
    >
      {label} <ExternalLink size={10} aria-hidden="true" />
    </a>
  );
}

function HistoryRow({ item, github }: { item: DocHistoryItem; github: string | null | undefined }) {
  const t = useT(M);
  const { formatDate } = useFormat();
  const links = historyLinks(item, github);
  return (
    <li data-testid="docs-history-item" style={{ display: "grid", gap: 2, padding: "6px 0", borderTop: `1px solid ${K.line}` }}>
      <span style={{ fontSize: 13, color: K.ink, overflowWrap: "anywhere" }}>{item.subject || t("historyNoSubject")}</span>
      <span style={{ display: "inline-flex", flexWrap: "wrap", gap: 10, fontSize: 12, color: K.muted }}>
        <span>{formatDate(item.date)}</span>
        {links.commit && item.sha && (
          <HistoryLink href={links.commit} label={t("historyCommit", item.sha.slice(0, 8))} testId="docs-history-commit" />
        )}
        {links.pr && item.pr && <HistoryLink href={links.pr} label={t("historyPr", item.pr)} testId="docs-history-pr" />}
        {!links.commit && item.sha && <span style={{ fontFamily: mono }}>{item.sha.slice(0, 8)}</span>}
      </span>
    </li>
  );
}

/** The collapsible "Page history" list; null when the page has no history. */
export function DocHistory({ page, github }: { page: DocEntry; github: string | null | undefined }) {
  const t = useT(M);
  const items = pageHistory(page);
  if (!items.length) return null;
  return (
    <details
      data-testid="docs-history"
      style={{ marginTop: 10, border: `1px solid ${K.line}`, borderRadius: RADIUS.md, padding: "6px 12px" }}
    >
      <summary style={{ cursor: "pointer", fontSize: 13, fontWeight: 600, color: K.ink }}>
        {t("pageHistory", items.length)}
      </summary>
      <ol aria-label={t("historyAria")} style={{ listStyle: "none", margin: "6px 0 0", padding: 0 }}>
        {items.map((item, i) => (
          <HistoryRow key={`${item.sha ?? "x"}-${item.date}-${i}`} item={item} github={github} />
        ))}
      </ol>
    </details>
  );
}

/** "Needs review" (warning) when the bundle flags the page; the reason, or a general hint, as the hover text. */
export function NeedsReviewBadge({ page }: { page: DocEntry }) {
  const t = useT(M);
  const flag = needsReview(page);
  if (!flag) return null;
  const hint = flag.reason ?? t("needsReviewHint");
  // Kit components take no data-* props: the test id and the hint sit on a wrapper (lessons, Frontend).
  return (
    <span data-testid="docs-needs-review" title={hint} aria-label={`${t("needsReview")}: ${hint}`} role="note">
      <Badge tone="warning">{t("needsReview")}</Badge>
    </span>
  );
}
