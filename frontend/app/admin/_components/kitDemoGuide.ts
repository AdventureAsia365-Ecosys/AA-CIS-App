// app/admin/_components/kitDemoGuide.ts — the demo GuideContent of Settings › Kit (SettingsKitTab.tsx). Its
// Vietnamese twin is kitDemoGuide.vi.ts (AA-994), picked with useLocalized({ en, vi }).

import type { GuideContent } from "../../_kit";

export const DEMO_GUIDE: GuideContent = {
  title: "How to use the demo page",
  intro: "Every admin page with a Guide tab renders its GuideContent with GuidePanel: same look everywhere.",
  sections: [
    { id: "purpose", heading: "What this page is for", blocks: [
      { kind: "paragraph", text: "A paragraph in plain words." },
      { kind: "flow", steps: ["Draft", "Check", "Approve", "Live"] },
      { kind: "callout", tone: "note", text: "A note: useful to know." },
    ] },
    { id: "buttons", heading: "Buttons", blocks: [
      { kind: "control", label: "Run check", does: "What the button does.", cost: "$0. About a minute.",
        when: "When to use it.", avoid: "When not to." },
      { kind: "callout", tone: "warning", title: "A trap", text: "A warning: a cost, a trap, a known gap." },
    ] },
    { id: "labels", heading: "Reading the labels", blocks: [
      { kind: "subheading", text: "Status" },
      { kind: "terms", uiLabels: true, items: [{ term: "active / paused", text: "Labels the page prints, as chips." }] },
      { kind: "list", ordered: true, items: ["A numbered step.", "Another step."] },
    ] },
  ],
  glossary: [{ term: "Term", text: "What it means." }],
};
