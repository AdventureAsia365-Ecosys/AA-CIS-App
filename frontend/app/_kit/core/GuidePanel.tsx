// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
// app/_kit/core/GuidePanel.tsx — AA-919: renders a page's Guide tab from a GuideContent (core/guide.ts).
//
// The content is a prop: the page keeps its guide as data (CIS: a GuideContent in app/admin/<page>/guide.ts) and
// renders <GuidePanel content={…} /> when the "Guide" tab is active; GuidePanel knows nothing about where it came
// from. One look for every guide, from the kit tokens (light and dark through the --aa-* variables): numbered
// sections, an "On this page" table of contents for long guides (GUIDE_TOC_MIN_SECTIONS), sub-headings, lists, note /
// warning callouts, term lists (page labels shown as chips), step diagrams and control cards. Phone width (390 px)
// works: everything wraps, nothing scrolls sideways, and long words break.

import { AlertTriangle, ArrowRight, Info } from "lucide-react";
import { K, RADIUS, alpha, sans } from "./tokens";
import { TONE } from "./primitives";
import {
  GUIDE_CONTROL_CAPTIONS, GUIDE_GLOSSARY_HEADING, GUIDE_GLOSSARY_ID, GUIDE_TOC_MIN_SECTIONS, guideAnchor, guideOutline,
  type GuideBlock, type GuideContent, type GuideTerm,
} from "./guide";

const PROSE: React.CSSProperties = { margin: "8px 0", color: K.body };

/** A label the page prints, shown as a neutral pill (same tone as the kit Badge, without its caps). */
function Chip({ children }: { children: React.ReactNode }) {
  const t = TONE.neutral;
  return (
    <span style={{ display: "inline-block", padding: "1px 9px", borderRadius: RADIUS.pill, background: t.bg,
      color: t.fg, fontSize: 12, fontWeight: 500, lineHeight: "20px" }}>
      {children}
    </span>
  );
}

function Terms({ items, uiLabels }: { items: GuideTerm[]; uiLabels?: boolean }) {
  return (
    <dl style={{ margin: "8px 0" }}>
      {items.map((t) => (
        <div key={t.term} style={{ display: "flex", flexWrap: "wrap", gap: "4px 16px", padding: "8px 0",
          borderBottom: `1px solid ${K.line2}` }}>
          <dt style={{ flex: "0 0 210px", maxWidth: "100%", fontWeight: 600, color: K.ink }}>
            {uiLabels ? (
              <span style={{ display: "inline-flex", flexWrap: "wrap", gap: 4 }}>
                {t.term.split(" / ").map((x) => <Chip key={x}>{x}</Chip>)}
              </span>
            ) : t.term}
          </dt>
          <dd style={{ flex: "1 1 280px", minWidth: 0, margin: 0, color: K.body }}>{t.text}</dd>
        </div>
      ))}
    </dl>
  );
}

function Callout({ tone, title, text }: { tone: "note" | "warning"; title?: string; text: string }) {
  const warn = tone === "warning";
  const line = warn ? K.amber : K.info;
  const Icon = warn ? AlertTriangle : Info;
  return (
    <div role="note" style={{ display: "flex", gap: 10, margin: "12px 0", padding: "10px 12px",
      borderRadius: RADIUS.md, border: `1px solid ${alpha(line, 35)}`, borderLeft: `3px solid ${line}`,
      background: warn ? K.amberSoft : K.infoSoft }}>
      <Icon size={16} color={line} aria-hidden style={{ flexShrink: 0, marginTop: 3 }} />
      <div style={{ minWidth: 0 }}>
        <div style={{ fontWeight: 600, color: K.ink }}>{title ?? (warn ? "Watch out" : "Good to know")}</div>
        <div style={{ color: K.body }}>{text}</div>
      </div>
    </div>
  );
}

function Flow({ steps }: { steps: string[] }) {
  return (
    <ol aria-label="Steps, in order" style={{ listStyle: "none", padding: 0, margin: "12px 0", display: "flex",
      flexWrap: "wrap", alignItems: "center", gap: 6 }}>
      {steps.map((s, i) => (
        <li key={`${i}-${s}`} style={{ display: "inline-flex", alignItems: "center", gap: 6, minWidth: 0 }}>
          <span style={{ padding: "6px 10px", borderRadius: RADIUS.md, border: `1px solid ${K.line}`,
            background: K.bg, color: K.ink, fontSize: 12.5, lineHeight: 1.35 }}>{s}</span>
          {i < steps.length - 1 && <ArrowRight size={14} color={K.muted2} aria-hidden style={{ flexShrink: 0 }} />}
        </li>
      ))}
    </ol>
  );
}

function Control({ b }: { b: Extract<GuideBlock, { kind: "control" }> }) {
  const rows = (["does", "cost", "when", "avoid"] as const).filter((k) => b[k]);
  return (
    <div style={{ margin: "12px 0", padding: "12px 14px", borderRadius: RADIUS.lg, border: `1px solid ${K.line}`,
      background: K.bg }}>
      {/* Plain bold text, not a button look-alike: the card describes the page's button, it is not clickable. */}
      <div style={{ color: K.ink, fontSize: 13.5, fontWeight: 600 }}>{b.label}</div>
      <dl style={{ margin: "8px 0 0", display: "grid", gap: 8 }}>
        {rows.map((k) => (
          <div key={k}>
            <dt style={{ fontSize: 11.5, fontWeight: 600, color: k === "avoid" ? K.danger : K.muted }}>
              {GUIDE_CONTROL_CAPTIONS[k]}</dt>
            <dd style={{ margin: "2px 0 0", color: K.body }}>{b[k]}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function Block({ b }: { b: GuideBlock }) {
  switch (b.kind) {
    case "paragraph":
      return <p style={PROSE}>{b.text}</p>;
    case "subheading":
      return <h4 style={{ margin: "18px 0 6px", fontSize: 13.5, fontWeight: 600, color: K.ink }}>{b.text}</h4>;
    case "list": {
      const Tag = b.ordered ? "ol" : "ul";
      return (
        <Tag style={{ margin: "8px 0", paddingLeft: 22, color: K.body }}>
          {b.items.map((x, i) => <li key={i} style={{ marginBottom: 5, paddingLeft: 2 }}>{x}</li>)}
        </Tag>
      );
    }
    case "callout":
      return <Callout tone={b.tone} title={b.title} text={b.text} />;
    case "terms":
      return <Terms items={b.items} uiLabels={b.uiLabels} />;
    case "flow":
      return <Flow steps={b.steps} />;
    case "control":
      return <Control b={b} />;
    default:
      return null;
  }
}

function Section({ id, n, heading, children }: { id: string; n: number; heading: string; children: React.ReactNode }) {
  const anchor = guideAnchor(id);
  return (
    <section id={anchor} aria-labelledby={`${anchor}-h`}
      style={{ scrollMarginTop: 80, marginTop: 22, paddingTop: 18, borderTop: `1px solid ${K.line}` }}>
      <h3 id={`${anchor}-h`} style={{ margin: "0 0 6px", fontSize: 16, fontWeight: 600, color: K.ink }}>
        <span style={{ color: K.accentDeep, marginRight: 8, fontVariantNumeric: "tabular-nums" }}>{n}.</span>
        {heading}
      </h3>
      {children}
    </section>
  );
}

/** A page's Guide tab: renders the GuideContent it is given (the page owns the content; core/guide.ts). */
export function GuidePanel({ content, testId = "guide-panel" }: { content: GuideContent; testId?: string }) {
  const outline = guideOutline(content);
  const n = new Map(outline.map((o) => [o.id, o.n]));
  return (
    <article data-testid={testId} aria-labelledby={`${testId}-title`}
      style={{ width: "100%", minWidth: 0, boxSizing: "border-box", padding: "clamp(14px, 3vw, 24px)",
        background: K.card, border: `1px solid ${K.line}`, borderRadius: RADIUS.lg, fontFamily: sans,
        fontSize: 13.5, lineHeight: 1.6, color: K.body, overflowWrap: "anywhere" }}>
      <h2 id={`${testId}-title`} style={{ margin: 0, fontSize: 19, fontWeight: 600, color: K.ink }}>
        {content.title}
      </h2>
      {content.intro && <p style={{ margin: "6px 0 0", color: K.muted }}>{content.intro}</p>}
      <div className={outline.length >= GUIDE_TOC_MIN_SECTIONS ? "aa-guide-layout aa-guide-layout--toc" : "aa-guide-layout"}>
      {outline.length >= GUIDE_TOC_MIN_SECTIONS && (
        <nav aria-label="On this page" className="aa-guide-toc" style={{ marginTop: 16, padding: "12px 14px",
          borderRadius: RADIUS.lg, border: `1px solid ${K.line}`, background: K.bg }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: K.muted, marginBottom: 6 }}>On this page</div>
          <ol className="aa-guide-toc-list" style={{ listStyle: "none", margin: 0, padding: 0, display: "grid",
            gap: "4px 16px" }}>
            {outline.map((o) => (
              <li key={o.id}>
                <a href={`#${guideAnchor(o.id)}`} style={{ color: K.ink2, textDecoration: "none", fontSize: 13 }}>
                  <span style={{ color: K.muted2, marginRight: 6, fontVariantNumeric: "tabular-nums" }}>{o.n}.</span>
                  {o.heading}
                </a>
              </li>
            ))}
          </ol>
        </nav>
      )}
      <div className="aa-guide-body" style={{ minWidth: 0 }}>
      {content.sections.map((s) => (
        <Section key={s.id} id={s.id} n={n.get(s.id) ?? 0} heading={s.heading}>
          {s.blocks.map((b, i) => <Block key={i} b={b} />)}
        </Section>
      ))}
      {content.glossary && content.glossary.length > 0 && (
        <Section id={GUIDE_GLOSSARY_ID} n={n.get(GUIDE_GLOSSARY_ID) ?? 0} heading={GUIDE_GLOSSARY_HEADING}>
          <Terms items={content.glossary} />
        </Section>
      )}
      </div>
      </div>
    </article>
  );
}
