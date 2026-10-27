// app/admin/writing-memory/guide.ts — AA-919: the Guide tab of Tenant output › Writing memory (/admin/writing-memory).
//
// A plain-words guide for Nghiệp and admin staff (not engineers): what the page is for, the loop, each tab and button
// (what it does, what it costs, when to use it and when not), how to read the labels, a weekly routine, what has no UI
// yet, and a glossary. A kit GuideContent (app/_kit/guideContent.ts), rendered by the kit GuidePanel like every other
// admin guide; English only (admin UI default; a Vietnamese version would come as an admin-wide language switch, not
// as a second copy here).
//
// Facts come from the code at the time of writing: WritingMemory.tsx, services/writing_memory/* (config, mining,
// drafter, lint, l0, replay, releases, release0, drill, insertion), api/migrations/280_writing_rule.sql and app ADR
// 0010 (docs/adr/0010-governed-writing-memory.md). Terms in `uiLabels` blocks are the page's own words, verbatim: the
// guide test (lib/guides.test.mjs) fails when the page stops printing one. Public repo: no ids, ARNs or tenant names.
// Data only (the type import is erased), so node --test loads it directly.

import type { GuideContent } from "../../_kit/guideContent";

export const WRITING_MEMORY_GUIDE: GuideContent = {
  title: "How to use Writing memory",
  intro: "Read this once from top to bottom. After that, the weekly routine is all you need.",
  sections: [
    {
      id: "purpose",
      heading: "What this page is for",
      blocks: [
        { kind: "paragraph", text: "Our writers (A1 tours, T2 tenant tours, T9 blog and social posts) make the same "
          + "mistakes again and again, and people fix them by hand. This page collects those fixes and finds the ones "
          + "that repeat. It then turns them into short rules that the writers follow next time." },
        { kind: "paragraph", text: "Nothing here changes a writer by itself. A rule reaches a writer only after a "
          + "check and a human click. Tenants never see this page or its rules. They only get text that needs less "
          + "editing." },
        { kind: "callout", tone: "note", title: "The one live path today",
          text: "Today only one path really changes what a writer writes: a lexicon rule (ban or replace a phrase) "
            + "for T9 blog or social, through Run R0 and then Activate (L0). Everything else on the page collects "
            + "evidence for later." },
      ],
    },
    {
      id: "loop",
      heading: "How it works, in 6 steps",
      blocks: [
        { kind: "flow", steps: [
          "Capture (edits, gate failures → signals)",
          "Fact filter",
          "Cluster",
          "Draft (Mine rules)",
          "Check (lint + R0)",
          "Go live (Activate (L0) or a release)",
        ] },
        { kind: "list", ordered: true, items: [
          "Capture. An admin cuts or rewrites a sentence of a T2 or T9 piece, or edits an A1 field. Each change is "
            + "saved at once as a signal. A tenant who edits a T9 piece in the portal also makes a signal, silently. "
            + "An admin who rejects, dismisses or regenerates a review row makes a signal only when they pick a "
            + "reason chip. Three sources are never captured live. They arrive only through Backfill signals: "
            + "tenant T2 edits, failed T9 attempts and A1 brand-audit lessons.",
          "Fact filter. Once a day, or when you click Run fact filter, each new signal is marked pass, grey or fail. "
            + "Fail: the edit added a fact the source does not have, so we never learn from it. Grey: the edit added "
            + "a name the source does not have, so an admin decides. Pass: we can learn from it.",
          "Cluster. Pass signals from the last 90 days that point at the same problem form a cluster. A cluster "
            + "that repeats often enough is 'over threshold'.",
          "Draft (Mine rules, paid). One model call per ready cluster drafts one short rule. An automatic check, "
            + "the lint, tests the draft. A draft that passes is stored as a candidate; a draft that fails is stored "
            + "as rejected by the lint. Some paid calls store nothing at all: the model answers that no rule fits "
            + "(NONE), its reply is unusable, or the database refuses the row. Then no rule exists, the cluster keeps "
            + "its 'next run drafts it' badge, and the next run pays for it again.",
          "Check (free). An admin reads the candidate, and edits or rejects it. Then the admin runs R0: a replay "
            + "over stored texts and signals. It shows where the rule would fire and which past failures it would "
            + "have prevented. R0 makes no model call.",
          "Go live. A T9 lexicon rule whose R0 passed: click Activate (L0), and the next T9 write uses it. Every "
            + "other rule must go into a release: evaluation, then one human approval, then activation, on the "
            + "Releases tab.",
        ] },
        { kind: "subheading", text: "Undo (rollback)" },
        { kind: "list", items: [
          "An L0 rule: open it in Candidates (status filter: active), type a reason, click Roll back. The next write "
            + "no longer gets it.",
          "A release: open it in Releases, type a reason, click Roll back. The previous release is active again. "
            + "No deploy is needed. Release 0 has no previous release, so it cannot be rolled back: click Turn "
            + "memory off for that writer instead.",
        ] },
      ],
    },
    {
      id: "tabs",
      heading: "Tabs and buttons",
      blocks: [
        { kind: "subheading", text: "The tiles at the top" },
        { kind: "list", items: [
          "Signals: the total, then how many are pass, grey, fail and pending.",
          "Candidates: rules waiting for a human, and how many were rejected.",
          "Learning-loop spend this month: what mining and the other learning runs spent, against the $25 month "
            + "cap and the $1 cap per mining run.",
          "Next automatic run: off, waiting or due. Automatic mining is off by default, so 'off' is normal.",
          "The line under the tiles: the status of the last job of each kind. Status only, no numbers. Open "
            + "Platform › Jobs for the numbers.",
        ] },
        { kind: "callout", tone: "warning", title: "The 'Next automatic run' tile can be wrong",
          text: "It reads the switch on the API service, but the jobs run on the worker service. If the switch were "
            + "set only on the worker, the tile would still say 'off'." },

        { kind: "subheading", text: "Signals tab" },
        { kind: "list", items: [
          "Clusters over threshold: What repeats (hover for its label), Support (signals, pieces, tenants, "
            + "reviewers), Basis, State and one masked Sample. Looking costs nothing.",
          "Signals: the newest 200 signals, with a filter on fact status. There is no next page. Each row shows "
            + "When, Scope, Writer, Action (what was done, by whom, the reason code, the gate), Before → after, and "
            + "Facts.",
          "A grey signal has Pass and Fail buttons in the Facts column. It is grey because the source does not have "
            + "the name the edit added, so the source cannot settle it. Click Pass when the added name is correct "
            + "and not an invented fact. Click Fail otherwise. A signal whose reason code is fact_not_in_source "
            + "(printed as is in the Action column) can never be set to pass.",
        ] },
        { kind: "callout", tone: "note",
          text: "Scope shows 'Platform' (the AA master tenant) or 'Tenant' plus the first 8 characters of its id. "
            + "The Signals, Candidates and Rules lists never show tenant names. Only the Releases tab names the "
            + "tenant that owns a release." },

        { kind: "subheading", text: "Candidates tab" },
        { kind: "paragraph", text: "The rules the miner drafted. The status filter starts at candidate. Switch it to "
          + "active to see the live rules, or to rejected to see what failed. Columns: Drafted, Scope, Writer, Kind, "
          + "Rule (→ replacement), Support, Lint, Status. Click a row to open its drawer." },
        { kind: "paragraph", text: "In the drawer:" },
        { kind: "list", items: [
          "Edit (candidates only): change the rule (≤ 25 words), the replacement (lexicon rules only; empty means "
            + "ban the phrase) and the match phrases (one per line, ≤ 5). For a lexicon rule, the phrase that gets "
            + "banned is the Rule text itself, not the match phrases. 'Save and lint' runs the lint again. If the "
            + "lint fails, the rule becomes rejected, recorded as rejected by you (not by the lint). So the next "
            + "Mine rules run drafts its cluster again, with a new paid call. If the edit contains tenant text, a "
            + "tenant brand name or a forbidden word, or is too long, nothing is saved and an error message says why.",
          "Reject (candidates): type a reason (at least 3 characters), then click Reject.",
          "Run R0: free, no model call. For a T9 rule it replays the rule over the newest 300 stored T9 pieces "
            + "(approved or held, last 90 days) and the writer's signals. For an A1 or T2 rule it reads the signals "
            + "only: no piece is replayed. It runs on the worker and takes minutes. When the job ends, close and "
            + "reopen the drawer to see the result.",
          "Activate (L0): only for a lexicon rule of t9_blog or t9_social. It stays greyed out, with 'Not "
            + "activatable yet: …', until every activation condition holds (listed under 'Reading the statuses and "
            + "badges'). After the click, the next T9 write gets the rule. A rule with Scope Platform reaches every "
            + "tenant's T9 writes, so it needs the program owner's (Nghiệp's) OK before you click. The page does not "
            + "check this.",
          "Roll back (active rules only): type a reason (at least 3 characters), then click. The rule becomes "
            + "rejected, and the next write no longer gets it.",
          "Provenance: every signal behind the rule, with its source and its before/after. Read it before you "
            + "activate anything.",
        ] },
        { kind: "callout", tone: "note",
          text: "Any edit makes the old R0 result stale. Run R0 again before Activate (L0)." },

        { kind: "subheading", text: "What an active L0 rule does" },
        { kind: "list", items: [
          "The phrase joins the brand's 'Never use' words for T9 writes. A platform rule reaches every tenant's T9 "
            + "writes. A tenant rule reaches only that tenant's. When the rule lists channels, it applies only "
            + "on those channels.",
          "With a replacement, the writer is also told 'Write X instead of Y'.",
          "If the phrase still appears, the lexicon gate flags it as blocking, and the repair step can fix it.",
          "It is skipped when the phrase is one of the tenant's signature words, or part of the CTA, the brand name "
            + "or the trip name. A tenant's own rule wins over a platform rule.",
          "It guides the writer before the gates. It never swaps words in the text afterwards.",
        ] },

        { kind: "subheading", text: "Releases tab" },
        { kind: "paragraph", text: "Writers, at the top: one row per writer with its Memory switch (on or off), the "
          + "number of its active platform release, how many tenant releases are active, and the month its release "
          + "train is closed through. Below: every release, newest number first, with filters on writer and status. "
          + "Columns: Writer, Owner, No., Status, Content (class and rule count), Evidence, Drafted / approved by, "
          + "Created, Activated, Prompt (the start of the prompt hash its R0 replay measured). Owner is 'Platform' "
          + "or the tenant's name (its slug when it has no name, or 'Tenant' plus the first 8 characters of its id "
          + "when the tenant list does not know it; hover for the full id). Click a row to open its drawer: it also "
          + "shows the tenant's full id, for support." },
        { kind: "list", items: [
          "Turn memory off / Turn memory on (Writers): the kill switch, one click and a confirm. Off: the next write "
            + "of that writer ignores every release. Turning it on needs an active platform release. It does not "
            + "affect L0 rules.",
          "Close month (Writers): pick a month. No regular release of that month or earlier can be activated after "
            + "it. Lexicon releases, emergencies and rollbacks are never blocked. It cannot be reopened here.",
          "New draft: pick the writer and the owner (Platform, or One tenant: then pick the tenant by name in the "
            + "Tenant list, searchable when there are many), optionally rule ids. No rule "
            + "ids makes an empty release. The drawer of the new draft opens.",
          "Reverify scan: flags every active release whose golden set, model route or gates changed since its "
            + "evidence. A flagged release keeps serving.",
          "Attach evidence (drawer, draft or evaluated): the run ids the release needs. For an empty release, 'Use "
            + "the active platform release's runs' fills in the R0 replay and baseline of that writer's live release. "
            + "When every run it needs is attached, the release becomes evaluated.",
          "Approve (drawer, evaluated): your one click, as the signed-in admin. The approval reference is optional: "
            + "a link to the decision.",
          "Activate (drawer, approved): greyed out, with 'Not activatable yet: …', while the evidence does not hold "
            + "or the release has rules (the writers do not read rules yet). After the confirm, the release before "
            + "it retires and this one serves the next write.",
          "Roll back (drawer, active, with a previous release) and Reject (draft, evaluated or approved): type a "
            + "reason of at least 3 characters first. A rolled-back release never comes back: a fix is a new release.",
        ] },
        { kind: "paragraph", text: "The drawer also shows each evidence run (kind, verdict, inputs, byte diffs, "
          + "stored hashes reproduced, the prompt hash) and the fingerprint the reverify scan compares: golden set, "
          + "gate taxonomy, writer and judge models, prompt." },

        { kind: "subheading", text: "How a release works" },
        { kind: "flow", steps: ["draft", "evaluated", "approved", "active"] },
        { kind: "list", items: [
          "A release is one versioned bundle of rules for one writer and one owner (the platform, or one tenant).",
          "Activating a release retires the one before it. A release can be rejected at any step before active.",
          "Evaluated means its evidence is attached. Empty release: an R0 replay with 0 differences over at least "
            + "20 inputs, plus the baseline evaluation. Lexicon-only release: R0. Release with other rules: R2 and "
            + "shadow runs.",
          "Every release needs one approval by a signed-in admin (Approve on the Releases tab). None goes live by "
            + "itself. One exception: an empty "
            + "release (no rules), whatever its number or owner, may be approved by an agent when it carries a "
            + "reference to the program owner's decision. Two command-line tools do this: the release 0 tool (it "
            + "approved the platform release 0) and the rollback drill (it approved the test tenant's releases 0 and "
            + "1).",
          "One regular release per writer and owner per month (the monthly train). Lexicon rules, emergencies and "
            + "rollbacks do not wait for it.",
          "A live release is flagged 'reverify' when the golden set, a model route or the gates changed since its "
            + "evidence. It keeps serving until it is checked again.",
          "A tenant release holds only that tenant's rules. A platform rule built from tenant signals needs at least "
            + "3 tenants. Every platform-scope rule, in a release or through Activate (L0), needs the program owner's "
            + "OK first (ADR 0010 decision 10). The page does not enforce this.",
        ] },
        { kind: "callout", tone: "note",
          text: "Release 0 is an empty release: while it is the active release, the writers' output is exactly the "
            + "same as with no memory." },

        { kind: "subheading", text: "Header buttons" },
        { kind: "paragraph", text: "Refresh only reloads the page data. Every other button queues one background job, "
          + "and a message says 'Queued (job …); see Platform › Jobs'. The result numbers are on Platform › Jobs, not "
          + "on this page." },
        { kind: "control", label: "Backfill (dry run)",
          does: "Counts what Backfill signals would copy, per source and per tenant. The four sources: old admin "
            + "sentence edits, tenant T2 edits, failed T9 attempts and A1 brand-audit lessons. It inserts nothing.",
          cost: "$0. Up to about 15 minutes.",
          when: "Before the first real backfill, or to check whether older data is missing." },
        { kind: "control", label: "Backfill signals",
          does: "Copies every signal not yet captured, old or new, from the four sources above. Then it queues the "
            + "fact filter by itself. Safe to run twice: nothing is added twice.",
          cost: "$0. Up to about 15 minutes.",
          when: "Every week, as the first step of the routine. Tenant T2 edits, failed T9 attempts and A1 "
            + "brand-audit lessons reach this page only this way." },
        { kind: "control", label: "Run fact filter",
          does: "Marks pending signals pass, grey or fail (up to 5000 per run). It also checks the automatic mining "
            + "trigger, but starts a paid run only if automatic mining is switched on (it is off by default).",
          cost: "$0. Up to about 10 minutes.",
          when: "Signals are pending and you do not want to wait for the daily run." },
        { kind: "control", label: "R0 baseline",
          does: "The 'R0 sanity check'. It re-runs the gates on stored T9 blog and social texts with no rule, and "
            + "compares the result with what the gates said at write time. It writes nothing. High agreement means "
            + "you can trust R0 results.",
          cost: "$0. Heavy on the worker's CPU, up to about 20 minutes.",
          when: "After a change to the gates, or before you rely on R0 results for the first time.",
          avoid: "A wave is running." },
        { kind: "control", label: "Mine rules",
          does: "First it expires candidates and approved rules older than 60 days (except rules that a draft, "
            + "evaluated or approved release holds). Then it builds the clusters and makes at most one model call per "
            + "cluster marked 'next run drafts it'. It skips a cluster whose last draft the lint rejected while its "
            + "signals are unchanged, even though the badge still says 'next run drafts it'. Each call ends one of "
            + "three ways: a candidate, a draft rejected by the lint, or nothing stored (the model answered NONE, "
            + "its reply was unusable, or the database refused the row). A call that stored nothing is paid for, and "
            + "the next run pays for that cluster again. A confirm dialog shows this month's spend first.",
          cost: "Paid. At most 50 calls and $1 per run, and $2 per day for mining itself. The $25 month cap covers "
            + "the whole learning loop (mining and the evaluation runs). The real cost is about $0.001 per call, so "
            + "a full run is about $0.05. Up to about 30 minutes. A failed run is never retried by itself.",
          when: "The Clusters table shows at least one 'next run drafts it' whose last draft was not rejected by the "
            + "lint on the same signals (check Candidates with the status filter on rejected).",
          avoid: "No cluster says 'next run drafts it', or each one waits on a lint-rejected draft with no new "
            + "signal since (the run would draft nothing and still use one of the 3 runs of the month), or the same "
            + "clusters came back with nothing stored on the last run (the model found no rule: the next run will "
            + "most likely pay for the same answer), or a wave is running." },
        { kind: "callout", tone: "warning", title: "3 mining runs per month",
          text: "Months count in UTC. Every started run counts, even one that drafts nothing. A 4th run is refused. "
            + "When the month is over its spend cap, the run still starts, stops before its first paid call, and "
            + "counts as one of the 3." },
        { kind: "callout", tone: "warning", title: "Waves and deploys",
          text: "Nothing in the code blocks Mine rules, R0 or R0 baseline during a wave. Not running them then is our "
            + "own rule, because they use the worker's CPU. Also, any queued or running job holds back a backend "
            + "deploy, so start long jobs when no deploy is planned." },
        { kind: "callout", tone: "note",
          text: "R0 baseline and the per-rule Run R0 use the same job kind, so the last-job line may show either one." },
      ],
    },
    {
      id: "labels",
      heading: "Reading the statuses and badges",
      blocks: [
        { kind: "subheading", text: "Facts (the fact status of a signal)" },
        { kind: "terms", uiLabels: true, items: [
          { term: "pending", text: "Not checked yet. The daily fact filter will check it." },
          { term: "grey", text: "The edit added a name the source does not have. It waits for your Pass or Fail." },
          { term: "pass", text: "Usable for learning. Cuts, style edits and reason-only signals are pass." },
          { term: "fail", text: "The edit added a number, time, price or promise word ('included', 'free', "
            + "'pick-up'…) the source does not have, or its reason code is fact_not_in_source. Never learned from." },
        ] },
        { kind: "subheading", text: "State (a cluster)" },
        { kind: "terms", uiLabels: true, items: [
          { term: "has a rule", text: "An open rule (candidate, approved or active) already exists for it." },
          { term: "next run drafts it", text: "It has at least 3 signals and no open rule. Usually the next Mine "
            + "rules run makes one paid call for it. Two exceptions the badge does not show: if the lint rejected "
            + "its last draft and its signals have not changed since, the run skips it; and if the last paid call "
            + "stored nothing (NONE, an unusable reply, or a row the database refused), the badge stays and the next "
            + "run pays again." },
          { term: "needs 3 signals", text: "It is over its threshold but has only 2 signals. It waits, at no cost. "
            + "The drafter sees 1 signal, and 2 are kept aside to test the draft." },
        ] },
        { kind: "paragraph", text: "A cluster whose draft the lint rejected during a mining run is skipped until its "
          + "set of signals changes: a new signal joins, or an old one leaves the 90-day window. A cluster is drafted "
          + "again on the next run, with a new paid call, when an admin rejected or rolled back its rule, when its rule "
          + "failed the lint after an admin edit, or when its last call stored nothing." },
        { kind: "subheading", text: "Thresholds (when a cluster is over threshold)" },
        { kind: "paragraph", text: "Only pass signals of the last 90 days count. Human signals are edits and also "
          + "reason-only signals: a reject, dismiss or regenerate with a reason chip. Every cluster, whatever its "
          + "basis, needs at least 2 signals on at least 2 pieces. On top of that:" },
        { kind: "list", items: [
          "Tenant rule: at least 2 human signals (admin or tenant) on at least 2 pieces of that tenant.",
          "Platform rule from tenant signals: the same problem in at least 3 different tenants.",
          "Platform rule from AA's own human signals (A1, and the AA master tenant's own T2 and T9 pieces): at least "
            + "2 different reviewers.",
          "Platform rule from A1 gate failures: on at least 2 different pieces, or on 5% of the A1 pieces of the "
            + "last 30 days (rounded up), whichever is more.",
        ] },
        { kind: "callout", tone: "warning", title: "Code and ADR differ here",
          text: "ADR 0010 names A1 only for the reviewers threshold. The code counts every human signal on AA's own "
            + "pieces, whatever the writer, so AA's own T2 and T9 edits can also build a platform rule." },
        { kind: "subheading", text: "Status (a rule)" },
        { kind: "terms", uiLabels: true, items: [
          { term: "candidate", text: "Drafted, lint passed, waiting for a human. Expires after 60 days." },
          { term: "approved", text: "Approved for a release. No button sets this yet." },
          { term: "active", text: "In use by a writer. Never expires." },
          { term: "rejected", text: "Rejected by the lint or by an admin, or rolled back. The drawer shows why." },
          { term: "expired", text: "A candidate or approved rule left for 60 days. Checked when a mining run starts." },
        ] },
        { kind: "subheading", text: "Status (a release)" },
        { kind: "terms", uiLabels: true, items: [
          { term: "draft", text: "Created; its evidence runs are not all attached." },
          { term: "evaluated", text: "Every evidence run it needs is attached. Waits for one admin's Approve." },
          { term: "approved", text: "Approved by a signed-in admin. Activate puts it live." },
          { term: "active", text: "The release the writer reads (when its memory is on). One per writer and owner." },
          { term: "retired", text: "Replaced by a newer release, or rolled back. A rolled-back one never returns." },
          { term: "rejected", text: "Stopped before it went live, with a reason." },
          { term: "reverify", text: "Its golden set, a model route or the gates changed since its evidence. It keeps "
            + "serving until it is checked again." },
        ] },
        { kind: "subheading", text: "Kind (a rule)" },
        { kind: "terms", items: [
          { term: "lexicon", text: "Ban a phrase, or replace it with neutral wording. The only kind that can go live "
            + "today (T9, through L0)." },
          { term: "writer_instruction", text: "A short instruction to the writer. Needs a release." },
          { term: "repair_hint", text: "A hint for the repair step. Needs a release." },
          { term: "preference_summary", text: "A tenant's taste in at most 5 sentences. Planned; the miner does not "
            + "draft it yet." },
        ] },
        { kind: "subheading", text: "Lint badges (green = passed, red = failed; hover for the detail)" },
        { kind: "terms", uiLabels: true, items: [
          { term: "Size", text: "Within the word, character and phrase limits." },
          { term: "Only constrains", text: "The rule only holds the writer back. It fails if it says add, mention, "
            + "include, promise… without a 'not'." },
          { term: "Replacement adds nothing", text: "A replacement adds no number, time, price, name or promise word. "
            + "'lunch' → 'lunch included' fails." },
          { term: "No tenant text", text: "A platform rule copies no 5-word run of tenant text and names no tenant "
            + "brand or tenant forbidden word. A tenant rule names no other tenant's brand or forbidden word. If the "
            + "names or text it needs cannot be loaded, it fails, to be safe." },
          { term: "Explains held-out edits", text: "The rule's phrases explain at least 2 of the edits kept aside." },
        ] },
        { kind: "paragraph", text: "A rule is a candidate only when every badge is green." },
        { kind: "subheading", text: "Drawer badges" },
        { kind: "list", items: [
          "The status, the size in tokens, the expiry date, and the model that drafted it (its model id, not a "
            + "person).",
          "The approved and active rules of one writer and one owner may hold at most 400 tokens for the platform "
            + "and 250 per tenant. The Activate button does not check this: over the cap, the database refuses the "
            + "click and an error message says token_cap. Any other refusal by a database check at that moment is "
            + "also reported as token_cap.",
        ] },
        { kind: "subheading", text: "R0 panel" },
        { kind: "terms", uiLabels: true, items: [
          { term: "L0 passed / L0 failed", text: "What the replay proved: a valid phrase, a replacement that adds "
            + "nothing, the phrase found in stored text, gone after the replacement, and no new hard stop. The red "
            + "line under it says why not. 'L0 passed' alone does not enable Activate (L0): the lint must also pass "
            + "and the replacement must use no word the writer is told never to use." },
          { term: "recurs (R2 eligible) / under k", text: "Whether the problem still repeats often enough in recent "
            + "signals (k is the mining threshold)." },
          { term: "Phrase in stored text", text: "How often the phrase appears in stored texts now, and after the "
            + "rule." },
          { term: "new hard stops", text: "Gate failures the rule would newly cause. Should be none." },
          { term: "Texts it would newly flag", text: "How many stored texts (final text, discarded attempts, "
            + "before-edit texts) the rule would newly flag, out of the texts replayed from the pieces loaded. One "
            + "piece gives up to 6 texts, so this count can be larger than the number of pieces. Always 0 for A1 and "
            + "T2 rules: no piece is replayed for them." },
          { term: "Past failures it would have prevented", text: "Earlier gate failures the rule would have avoided." },
          { term: "Signals matched", text: "The signals the rule explains." },
          { term: "Baseline", text: "How well the replay agrees with the stored gate results, with the rule off." },
          { term: "stale: the rule changed after this run", text: "The rule was edited after R0. Run R0 again." },
        ] },
        { kind: "subheading", text: "'Not activatable yet: …'" },
        { kind: "paragraph", text: "The message names the first thing missing. All of these must hold:" },
        { kind: "list", items: [
          "a lexicon rule, for a T9 writer (t9_blog or t9_social);",
          "status candidate or approved;",
          "the lint passed;",
          "a valid phrase: the Rule text itself (not the match phrases), with real words, at most 6 words and at "
            + "least 3 characters, not only small words like 'the' or 'a', and no masked token such as [NAME];",
          "a replacement that adds nothing;",
          "R0 was run on the rule's current text, and L0 passed;",
          "a replacement that uses no word the writer is told never to use (code replacement_forbidden).",
        ] },
        { kind: "paragraph", text: "Why L0 fails most often:" },
        { kind: "list", items: [
          "'the phrase appears in no stored text of this writer and owner: nothing to prove'. Common for a new "
            + "phrase: R0 can only prove a rule on text that has the phrase. Wait until stored pieces contain it, then "
            + "run R0 again.",
          "'the phrase is still there … after the replacement': the replacement keeps the phrase (for example it "
            + "contains it). Change the replacement.",
          "'new hard stops: …': the replaced text would fail a blocking gate it passed before. Change the "
            + "replacement, or reject the rule.",
        ] },
        { kind: "paragraph", text: "Room under the token cap is not part of this check. The button stays enabled, and "
          + "over the cap the click fails with an error message (token_cap)." },
      ],
    },
    {
      id: "routine",
      heading: "Weekly routine",
      blocks: [
        { kind: "list", ordered: true, items: [
          "Click Backfill signals. It picks up the tenant T2 edits, failed T9 attempts and A1 brand-audit lessons "
            + "since the last run (they are never captured live), then queues the fact filter. Wait until both jobs "
            + "end on Platform › Jobs, then click Refresh.",
          "Open the Signals tab and look at the Signals tile: how many are pending and grey.",
          "Signals still pending after the fact filter job ended? Click Run fact filter.",
          "Set the filter to grey. For each row, decide whether the added name is correct and not an invented fact, "
            + "then click Pass or Fail.",
          "Look at Clusters over threshold. Is there a 'next run drafts it'? In Candidates (status rejected), check "
            + "that its last draft was not rejected by the lint on the same signals: such a cluster is skipped. If "
            + "the same cluster already showed 'next run drafts it' after the last run, that call stored nothing: "
            + "another run will likely pay for the same result. Check that no wave is running and that the month has "
            + "a run left (3 at most). Then click Mine rules and confirm.",
          "When the mining job has finished (Platform › Jobs), open Candidates. For each rule, read it and its "
            + "Provenance. Edit it if the wording is off. Reject it, with a reason, if it is wrong.",
          "For a T9 lexicon candidate you agree with: Run R0 (outside a wave), reopen the drawer, and check the R0 "
            + "panel. If its Scope is Platform, get the program owner's OK first: every platform rule needs it, and "
            + "the page does not enforce this. If 'Not activatable yet' is gone and the rule would not fire in the "
            + "wrong places, click Activate (L0).",
          "Set the status filter to active. Check that the live rules still make sense. Roll back any that hurt.",
          "Glance at the spend tile.",
        ] },
        { kind: "subheading", text: "Now and then" },
        { kind: "list", items: [
          "After a change to the gates: R0 baseline.",
          "To see what the backfill would copy before it runs (per source and tenant): Backfill (dry run).",
          "Candidates expire after 60 days, so review them before that.",
          "After a model route or golden set change: Reverify scan on the Releases tab, then look for 'reverify' "
            + "badges.",
          "At the end of a month, if no more regular release should go live for it: Close month for each writer.",
        ] },
      ],
    },
    {
      id: "not-in-ui",
      heading: "Not on this page yet",
      blocks: [
        { kind: "paragraph", text: "Some steps have no button yet. On the program owner's request, an agent does them "
          + "through the API or a command-line tool." },
        { kind: "subheading", text: "Done by an agent today" },
        { kind: "list", items: [
          "Release 0 replay and approval, the rollback drill and the golden set import (command-line tools).",
          "The R2 evaluation run and the targeted shadow run (command-line tools that queue a paid background job, "
            + "with a hard cost cap). Their run is then attached as the release's evidence on the Releases tab.",
        ] },
        { kind: "subheading", text: "Not built anywhere yet" },
        { kind: "list", items: [
          "Approving a candidate (candidate → approved). So rules other than T9 lexicon rules cannot move forward "
            + "yet.",
          "Writers reading rules from a release. Today only empty releases can be active.",
          "Exemplars and Metrics tabs.",
          "Tenant names, the numbers of a dry run or baseline (Platform › Jobs only), and more than 200 signals in "
            + "the list.",
        ] },
        { kind: "callout", tone: "warning", title: "The memory switch does not stop L0 rules",
          text: "T9 reads active lexicon rules directly. To stop one, roll it back in its drawer." },
      ],
    },
  ],
  glossary: [
    { term: "Signal", text: "One saved piece of evidence about a writer's output: a human edit, a reject or "
      + "regenerate with a reason, or a gate failure." },
    { term: "Fact filter", text: "The free daily check that marks each signal pass, grey or fail." },
    { term: "Cluster", text: "A group of pass signals that point at the same problem." },
    { term: "Threshold", text: "How often a problem must repeat before a rule is drafted (see 'Reading the statuses "
      + "and badges')." },
    { term: "Rule", text: "One short instruction (at most 25 words) for one writer." },
    { term: "Candidate", text: "A drafted rule that passed the lint and waits for a human." },
    { term: "Lint", text: "The automatic check on every drafted or edited rule (the five badges)." },
    { term: "R0", text: "A free replay of a rule over stored texts (T9) and signals. No model call." },
    { term: "R0 baseline", text: "R0 with no rule: checks that the replay agrees with the stored gate results." },
    { term: "L0", text: "The fast path: a T9 lexicon rule proven by R0 goes live in one click." },
    { term: "R2 / shadow", text: "Evaluation runs that a release with other rules needs. R2 runs the golden set "
      + "(at most $10 a run); shadow writes recent real inputs that nobody sees (20, or 50 for T2 and Facebook; "
      + "a social release for every channel: 50 Facebook + 20 others). Only runs of that release count as its "
      + "evidence." },
    { term: "Release", text: "A versioned bundle of rules for one writer and one owner. It needs one approval "
      + "before it goes live." },
    { term: "Golden set", text: "A fixed set of test inputs (tours, blog and social posts, plus traps) used to "
      + "evaluate a release." },
    { term: "Memory switch", text: "Per writer: on means the writer reads its active release. Off means it writes as "
      + "if there were no memory. It does not affect L0 rules." },
    { term: "Scope", text: "Who a signal or rule belongs to: Platform (AA, every tenant) or one tenant." },
    { term: "Provenance", text: "The signals a rule was drafted from." },
    { term: "Wave", text: "A planned rerun of the content pipeline. By our rule, start Mine rules, R0 and R0 baseline "
      + "only outside a wave; nothing in the code makes them wait." },
    { term: "Learning-loop spend", text: "Money spent on mining and the evaluation runs, capped at $25 a month. Mining "
      + "alone is also capped at $1 per run and $2 per day." },
  ],
};
