// app/admin/ai-gateway/guide.ts — AA-804: the Guide tab of Platform › AI & spend › AI providers (/admin/ai-gateway).
//
// A plain-words guide for admin staff and the business owner: what the page is for, how to read each column and
// status, how to follow one job's calls (the Trace drawer), and what the page cannot show yet. A kit GuideContent
// (app/_kit/guideContent.ts) rendered by the kit GuidePanel. This is the English original; guide.vi.ts is its
// Vietnamese twin (same section ids). Change this file first, then the twin in the same PR.
//
// Facts come from the code at the time of writing: api/services/ai_gateway.py (sources, statuses, windows),
// ProvidersTab.tsx, TraceDrawer.tsx and messages.ts. Terms in `uiLabels` blocks are the page's own words, verbatim:
// lib/guides.test.mjs fails when the page stops printing one. Public repo: no ids or account numbers.
// Data only (the type import is erased), so node --test loads it directly.

import type { GuideContent } from "../../_kit/guideContent";

export const AI_GATEWAY_GUIDE: GuideContent = {
  title: "How to use AI providers",
  intro: "One page for every paid AI and data source. Read it to answer three questions: is each source answering, "
    + "what did it cost, and is anything close to a limit.",
  sections: [
    {
      id: "purpose",
      heading: "What this page is for",
      blocks: [
        { kind: "paragraph", text: "The platform pays several outside services: the language models (through AWS "
          + "Bedrock or a vendor API), Jev for yes / no decisions, DataForSEO for search data and Geoapify for place "
          + "coordinates. This page shows all of them side by side." },
        { kind: "paragraph", text: "It only reads. Every number comes from our own logs and stored balance checks; "
          + "opening the page calls no provider and costs nothing. Switching a connection on or off and editing its "
          + "limits still happens in Settings › LLM Models; budgets are still set in Cost Operations." },
      ],
    },
    {
      id: "tiles",
      heading: "The three tiles",
      blocks: [
        { kind: "terms", uiLabels: true, items: [
          { term: "Spend this month", text: "Every logged paid call since the first of the month (UTC), all sources "
            + "together." },
          { term: "Spend today", text: "The same since midnight UTC. Daily caps reset at that moment." },
          { term: "Need a look", text: "How many sources are not OK and not switched off. Zero means all fine." },
        ] },
      ],
    },
    {
      id: "columns",
      heading: "How to read a row",
      blocks: [
        { kind: "terms", uiLabels: true, items: [
          { term: "LLM connections", text: "One row per way the platform reaches a model, for example Bedrock in an "
            + "AWS account (shown by its account number) or the OpenAI API." },
          { term: "Decisions (Jev)", text: "Jev's row. Its balance is an estimate: the top-ups recorded in Settings "
            + "minus the spend we logged, because Jev has no balance service." },
          { term: "Data providers", text: "DataForSEO (balance from the daily check) and Geoapify (credits used "
            + "today against the daily limit; the limit resets each UTC day)." },
          { term: "Health", text: "For a connection: when it last answered and the last error type. For Jev: calls "
            + "and errors of the last 24 hours and the mean answer time." },
          { term: "Budget", text: "The monthly budget of a connection and the daily cap of its provider, with how "
            + "much is used. A hard stop blocks further calls at the cap; alert only just warns." },
          { term: "Balance / credits", text: "What is left with the provider, or today's credits for Geoapify." },
        ] },
      ],
    },
    {
      id: "statuses",
      heading: "Statuses",
      blocks: [
        { kind: "terms", uiLabels: true, items: [
          { term: "OK", text: "Answering, nothing near a limit." },
          { term: "Degraded", text: "Answering, but recent calls failed (for Jev: most calls of the last 24 hours)." },
          { term: "Circuit open", text: "Several calls in a row failed, so the gateway skips this connection for a "
            + "while and uses the next route. It retries by itself." },
          { term: "Low", text: "The balance is below its alert threshold, or over 80 % of today's credits are used." },
          { term: "Cap reached", text: "A budget, the balance or today's credits are used up. Calls stop or fall back "
            + "until the cap is raised or the period ends." },
          { term: "Off", text: "Switched off on purpose. Not counted as a problem." },
          { term: "Unknown", text: "No recent call, no balance read yet, or the source could not be read. The row "
            + "says which." },
        ] },
      ],
    },
    {
      id: "trace",
      heading: "Follow one job's calls (Trace)",
      blocks: [
        { kind: "flow", steps: ["Jobs", "Click a job", "Open trace", "Read the calls in time order"] },
        { kind: "paragraph", text: "The trace lists every logged paid call of the job: model calls, Jev decisions, "
          + "DataForSEO requests and geocoder requests, with when, what, the outcome, the cost and (for Jev) the "
          + "answer time. The top line gives the totals per log." },
        { kind: "control", label: "Open trace", does: "Opens this page with the job's trace drawer.",
          cost: "Free: it reads our logs.", when: "A job cost more than expected, or failed and you want to see which "
            + "call went wrong." },
        { kind: "control", label: "Refresh", does: "Reads every source again.", cost: "Free." },
      ],
    },
    {
      id: "limits",
      heading: "What the page cannot show yet",
      blocks: [
        { kind: "callout", tone: "warning", title: "Refused calls",
          text: "A call the provider refused is not logged yet, so it does not appear in the trace or the spend. The "
            + "Health column still shows the error. The new call log adds these rows at zero cost." },
        { kind: "list", items: [
          "Answer time per model call is not logged yet; only Jev has it.",
          "Bedrock Guardrails and Mapbox write no log yet, so they have no row.",
          "Spend is what we logged at our prices. The AWS bill can differ slightly; Cost Operations shows the bill.",
        ] },
      ],
    },
  ],
  glossary: [
    { term: "Connection", text: "One way to reach a model provider: an AWS account with Bedrock, or a vendor API key." },
    { term: "Gateway", text: "The shared code every model call goes through; it picks the route, logs the call and "
      + "enforces the budgets." },
    { term: "Daily cap", text: "The most a provider may cost in one UTC day across the whole platform." },
    { term: "Trace", text: "All the logged paid calls of one job, in time order." },
  ],
};
