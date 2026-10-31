// app/admin/ai-gateway/guide.ts — AA-804: the Guide tab of Platform › AI & spend › AI providers (/admin/ai-gateway).
//
// A plain-words guide for admin staff and the business owner: what the page is for, how to read each column and
// status, how to follow one job's calls (the Trace drawer), and what the page cannot show yet. A kit GuideContent
// (app/_kit/guideContent.ts) rendered by the kit GuidePanel. This is the English original; guide.vi.ts is its
// Vietnamese twin (same section ids). Change this file first, then the twin in the same PR.
//
// Facts come from the code at the time of writing: api/services/ai_gateway.py (sources, statuses, windows, caps),
// ProvidersTab.tsx, RoutingTab.tsx, ModelsTab.tsx, BudgetsTab.tsx, TraceDrawer.tsx and the *.messages.ts files. Terms in `uiLabels` blocks are the page's own words, verbatim:
// lib/guides.test.mjs fails when the page stops printing one. Public repo: no ids or account numbers.
// Data only (the type import is erased), so node --test loads it directly.

import type { GuideContent } from "../../_kit/guideContent";

export const AI_GATEWAY_GUIDE: GuideContent = {
  title: "How to use AI providers",
  intro: "One page for every paid AI and data source. Read it to answer four questions: is each source answering, "
    + "what did it cost, which model does each stage use, and is anything close to a limit.",
  sections: [
    {
      id: "purpose",
      heading: "What this page is for",
      blocks: [
        { kind: "paragraph", text: "The platform pays several outside services: the language models (through AWS "
          + "Bedrock or a vendor API), Jev for yes / no decisions, DataForSEO for search data and Geoapify for place "
          + "coordinates. This page shows all of them side by side." },
        { kind: "paragraph", text: "Every number comes from our own logs and stored balance checks; opening the page "
          + "calls no provider and costs nothing. The page has five tabs: Providers (health, spend and balance, plus "
          + "the connection settings and the Jev credit ledger), Routing (which model each stage uses), Models & "
          + "trials (the model catalog and the shadow comparisons), Budgets (every spending cap) and this Guide. "
          + "Settings › LLM Models, Settings › Jev Credit and Cost Operations › Budgets moved here; their old links "
          + "open the right tab." },
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
          { term: "Decisions (Jev)", text: "Jev's row. Its balance is an estimate: the top-ups recorded in Jev credit "
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
      id: "views",
      heading: "Inside the Providers tab",
      blocks: [
        { kind: "terms", uiLabels: true, items: [
          { term: "Overview", text: "The read-only list above: one row per paid source." },
          { term: "Connections", text: "Switch an LLM connection on or off, change its timeout, retries, limits "
            + "and monthly budget, and send one small test call. The secret name and the base URL are shown but not "
            + "edited here: they change only through a reviewed migration." },
          { term: "Jev credit", text: "Record each Jev top-up by hand (Jev has no balance service), see the "
            + "estimated credit left, and send test alerts." },
        ] },
      ],
    },
    {
      id: "routing",
      heading: "Routing: which model each stage uses",
      blocks: [
        { kind: "paragraph", text: "One row per stage of the pipeline. The primary model answers first; the fallback "
          + "chain is tried in order when it fails; a shadow model runs a second call on a share of calls only to "
          + "compare (it doubles the cost of those calls). The LLM availability card on top says whether the models "
          + "answered recently, and the Jev questions per stage are listed at the bottom." },
        { kind: "control", label: "Save", does: "Saves the primary model of one stage, for every call of that stage.",
          cost: "Free to save; the next calls run on the new model at its price.",
          when: "A model is retired, refused, or a cheaper one passed its trial." },
        { kind: "control", label: "Save route", does: "Saves the fallback chain and the shadow of one stage.",
          cost: "Free to save; a shadow doubles the cost of the calls it samples.",
          when: "A fallback is missing, or you want to compare a new model on real calls." },
        { kind: "callout", tone: "note", title: "AWS account per stage",
          text: "While every stage runs in the LLM account there is nothing to choose, so the account picker is "
            + "hidden; it shows again only if a stage still routes to another account." },
      ],
    },
    {
      id: "models",
      heading: "Models & trials",
      blocks: [
        { kind: "paragraph", text: "The model catalog lists every model the platform can call, with its vendor, where "
          + "it runs and its price. Prices are view-only: they change only through a reviewed configuration change. "
          + "A model can be switched on or off; one with no price cannot be switched on." },
        { kind: "paragraph", text: "The shadow comparisons show, per stage, how often the shadow model agreed with the "
          + "primary, the score gap, the cost per call and the shadow's answer time, each with its sample size." },
      ],
    },
    {
      id: "budgets",
      heading: "Budgets: every spending cap",
      blocks: [
        { kind: "terms", uiLabels: true, items: [
          { term: "Provider daily caps", text: "The most one provider may cost in a UTC day across the whole "
            + "platform." },
          { term: "Job caps", text: "Caps of one kind of job, per run and per day." },
          { term: "LLM connection budgets", text: "The monthly budget of each connection; edit it in Providers › "
            + "Connections." },
          { term: "Tenant AI budgets", text: "Empty for now: per-tenant budgets come later and are seen by staff only." },
          { term: "Stops at the cap", text: "The run ends when the next call would pass the cap." },
          { term: "Alert only", text: "Never stops; an admin alert goes out at the alert %." },
        ] },
        { kind: "control", label: "Add a cap", does: "Creates a provider-wide or a job cap.",
          cost: "Free.", when: "A paid source shows No in the coverage list." },
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
          "Trial runs of new models (quick test, judge check, compare) are not on the Models & trials tab yet.",
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
    { term: "Fallback", text: "The next model a stage tries when its primary fails." },
    { term: "Shadow", text: "A second model that answers the same call only to be compared; its answer is never used." },
  ],
};
