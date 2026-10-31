// app/admin/ai-gateway/guide.ts — AA-804: the Guide tab of Platform › AI & spend › AI providers (/admin/ai-gateway).
//
// A plain-words guide for admin staff and the business owner: what the page is for, how to read each column and
// status, how to follow one job's calls (the Trace drawer), and what the page cannot show yet. A kit GuideContent
// (app/_kit/guideContent.ts) rendered by the kit GuidePanel. This is the English original; guide.vi.ts is its
// Vietnamese twin (same section ids). Change this file first, then the twin in the same PR.
//
// Facts come from the code at the time of writing: api/services/ai_gateway.py (sources, statuses, windows, caps),
// api/services/ai_gateway_calls.py, ProvidersTab.tsx, RoutingTab.tsx, ModelsTab.tsx, BudgetsTab.tsx, CallsTab.tsx,
// TraceDrawer.tsx and the *.messages.ts files. Terms in `uiLabels` blocks are the page's own words, verbatim:
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
          + "calls no provider and costs nothing. The page has six tabs: Providers (health, spend and balance, plus "
          + "the connection settings and the Jev credit ledger), Routing (which model each stage uses), Models & "
          + "trials (the model catalog and the shadow comparisons), Budgets (every spending cap), Calls (every "
          + "logged call, one by one) and this Guide. Settings › LLM Models, Settings › Jev Credit and Cost "
          + "Operations › Budgets moved here; their old links open the right tab. The spend totals by tenant, model "
          + "and market stay on Cost Operations, in its AI & data spend tab." },
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
          { term: "Job caps", text: "Caps of one kind of job, per run and per day. Spent today is measured the way "
            + "the guard measures it: the whole provider's day for most jobs, this job kind only for the few jobs "
            + "capped on their own spend (the line under the figure says which)." },
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
      id: "calls",
      heading: "Calls: every logged call, one by one",
      blocks: [
        { kind: "paragraph", text: "The newest call first: each model attempt (a refused attempt too, at zero cost, "
          + "with the reason class under its outcome), each Jev verdict, each DataForSEO and geocoder request, and "
          + "the partner CRM's own AI calls. The CRM rows need your staff sign-in; when they cannot be read the tab "
          + "says why and gives the CRM's totals instead, counted over whole UTC days (so they can reach "
          + "further back than the period)." },
        { kind: "terms", uiLabels: true, items: [
          { term: "Period", text: "How far back the list reaches; the last 24 hours unless you pick another." },
          { term: "Log", text: "Which log a row comes from: LLM, Jev, DataForSEO, Geocoder or CRM." },
          { term: "Outcome", text: "answered, refused (the provider or our own guard said no, nothing billed), error, "
            + "cut short (the answer hit its length limit) or billed, unusable." },
          { term: "Latency of the listed calls", text: "One bar per call on screen, red when refused or failed, with "
            + "the median, the slowest 5 % and the slowest call." },
          { term: "Only this job", text: "Keeps the rows of that row's job; Clear removes the filter." },
        ] },
        { kind: "control", label: "Load older calls", does: "Adds the next 100 older rows of the period.",
          cost: "Free: it reads our logs." },
      ],
    },
    {
      id: "trace",
      heading: "Follow one job's or one tour's calls (Trace)",
      blocks: [
        { kind: "flow", steps: ["Jobs", "Click a job", "Open trace", "Read the calls in time order"] },
        { kind: "paragraph", text: "The trace lists every logged paid call of the job: model calls, Jev decisions, "
          + "DataForSEO requests and geocoder requests, with when, what, the outcome, the cost and the answer time, "
          + "as a Timeline (default) or a Table. The top line gives the totals per log. A tour's trace (Master "
          + "Content › a tour › Open AI trace) gathers every job of that tour and every call marked with it; the "
          + "Jev decisions page links each verdict's job the same way." },
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
        { kind: "callout", tone: "warning", title: "Calls before 31 Oct 2026",
          text: "Calls logged before 31 Oct 2026 have no outcome or answer time, and a refused attempt from before "
            + "that date was not logged at all. Since then every attempt has its row, a refused one at zero cost." },
        { kind: "list", items: [
          "TripPlanner's model calls show from the shared log; its per-stage caps are on Cost Operations › AI & data "
            + "spend and the Apps board.",
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
