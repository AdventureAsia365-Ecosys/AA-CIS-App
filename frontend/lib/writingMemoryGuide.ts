// lib/writingMemoryGuide.ts — AA-919: the Guide tab of Tenant output › Writing memory (/admin/writing-memory).
//
// Plain-words guide for Nghiệp and admin staff (not engineers): what the page is for, the loop, every tab and button
// (cost, when to use it, when not), the labels as the page prints them, a weekly routine, what has no UI yet, and a
// glossary. Two locales with the same sections and the same block shapes (role guides are EN–VI, root
// .kiro/steering/language-convention.md); English is the default. lib/writingMemoryGuide.test.mjs keeps the two
// locales in step.
//
// Data only (no JSX, type-level TS only) so the page renders it with the kit and node --test can import it. Facts
// come from the code at the time of writing: frontend/app/admin/writing-memory/WritingMemory.tsx,
// services/writing_memory/* (config, mining, lint, l0, replay, releases, insertion) and app ADR 0010. Public repo:
// no ids, ARNs or tenant names here.

export type GuideLocale = "en" | "vi";

export const GUIDE_LOCALES: readonly GuideLocale[] = ["en", "vi"];
export const DEFAULT_GUIDE_LOCALE: GuideLocale = "en";

/** One term and its meaning (glossary entries, label meanings). */
export interface GuideTerm { term: string; text: string }

export type GuideBlock =
  | { type: "p"; text: string }
  | { type: "h3"; text: string }
  | { type: "list"; ordered?: boolean; items: string[] }
  | { type: "note"; tone: "info" | "warning"; title?: string; text: string }
  | { type: "terms"; items: GuideTerm[] }
  /** A header button: its exact label on the page, then what it does, its cost and when (not) to use it. */
  | { type: "action"; label: string; does: string; cost: string; when: string; avoid?: string };

export interface GuideSection { id: string; heading: string; blocks: GuideBlock[] }

export interface GuideContent {
  title: string;
  intro: string;
  /** Field captions of an `action` block, in this locale. */
  actionLabels: { does: string; cost: string; when: string; avoid: string };
  sections: GuideSection[];
}

export const GUIDE_LOCALE_LABEL: Record<GuideLocale, string> = { en: "English", vi: "Tiếng Việt" };

const en: GuideContent = {
  title: "How to use Writing memory",
  intro: "Read this once from top to bottom. After that, the weekly routine (section 5) is all you need.",
  actionLabels: { does: "What it does", cost: "Cost and time", when: "Use it when", avoid: "Do not use it when" },
  sections: [
    {
      id: "purpose",
      heading: "1. What this page is for",
      blocks: [
        { type: "p", text: "Our writers (A1 tours, T2 tenant tours, T9 blog and social posts) make the same mistakes "
          + "again and again, and people fix them by hand. This page collects those fixes, finds the ones that repeat, "
          + "and turns them into short rules the writers follow next time." },
        { type: "p", text: "Nothing here changes a writer on its own. A rule reaches a writer only after a check and "
          + "a human click. Tenants never see this page or its rules. They only get text that needs less editing." },
        { type: "note", tone: "info", title: "The one live path today",
          text: "Today only one path really changes what a writer writes: a lexicon rule (ban or replace a phrase) "
            + "for T9 blog or social, through Run R0 and then Activate (L0). Everything else on the page gathers "
            + "evidence for later." },
      ],
    },
    {
      id: "loop",
      heading: "2. How it works, in 6 steps",
      blocks: [
        { type: "list", ordered: true, items: [
          "Capture. A person edits writer output: an admin cuts or rewrites a sentence, edits an A1 field, or "
            + "rejects, dismisses or regenerates a review row; a tenant edits a piece in the portal. Each action is "
            + "saved quietly as a signal. Older edits and past gate failures come in through the backfill.",
          "Fact filter. Once a day, or when you click Run fact filter, each new signal is marked pass, grey or fail. "
            + "Fail: the edit added a fact the source does not have, so it is never learned from. Grey: it added a "
            + "name the source does not have, so an admin decides. Pass: it can be learned from.",
          "Cluster. Pass signals from the last 90 days that say the same thing are grouped into a cluster. A "
            + "cluster that repeats often enough is 'over threshold'.",
          "Draft (Mine rules, paid). One model call per ready cluster drafts one short rule. An automatic check, "
            + "the lint, tests it. A rule that passes becomes a candidate. One that fails is kept as rejected by lint.",
          "Check (free). An admin reads the candidate, then edits or rejects it, and runs R0: a replay over stored "
            + "texts that shows where the rule would fire and what it would have prevented. No model call.",
          "Go live. A T9 lexicon rule whose R0 passed: click Activate (L0), and the next T9 write uses it. Every "
            + "other rule must go into a release: evaluation, then one human approval, then activation. The release "
            + "path has no buttons yet (section 6).",
        ] },
        { type: "p", text: "In one line:" },
        { type: "list", items: [
          "human edits + gate failures → signals → fact filter → clusters → candidate rules → lint + R0 → "
            + "Activate (L0), or a release (evaluate → approve → activate) → the writers use it",
        ] },
        { type: "h3", text: "Undo" },
        { type: "list", items: [
          "An L0 rule: open it in Candidates (status filter: active), type a reason, click Roll back. The next write "
            + "no longer gets it.",
          "A release: rolling it back makes the previous release active again, with no deploy. Today an agent does "
            + "this through the API.",
        ] },
      ],
    },
    {
      id: "tabs",
      heading: "3. Tabs and buttons",
      blocks: [
        { type: "h3", text: "The tiles at the top" },
        { type: "list", items: [
          "Signals: the total, then how many are pass, grey, fail and pending.",
          "Candidates: rules waiting for a human, and how many were rejected.",
          "Learning-loop spend this month: what mining and the other learning runs spent, against the $25 month "
            + "cap and the $1 cap per run.",
          "Next automatic run: off, waiting or due. Automatic mining is off by default, so 'off' is normal.",
          "The line under the tiles: the status of the last job of each kind. Status only, no numbers: open "
            + "Platform › Jobs for the numbers.",
        ] },
        { type: "note", tone: "warning", title: "The 'Next automatic run' tile can be wrong",
          text: "It reads the switch on the API service, but the jobs run on the worker service. If the switch were "
            + "set only on the worker, the tile would still say 'off'." },

        { type: "h3", text: "Signals tab" },
        { type: "list", items: [
          "Clusters over threshold: What repeats (hover for its label), Support (signals, pieces, tenants, "
            + "reviewers), Basis, State and one masked Sample. Looking costs nothing.",
          "Signals: the newest 200 signals, with a filter on fact status. Each row shows When, Scope, Writer, Action "
            + "(what was done, by whom, the reason chip, the gate), Before → after, and Facts.",
          "A grey signal has Pass and Fail buttons in the Facts column. Click Pass when the added name is right and "
            + "the source supports it. Click Fail when it is not. A signal tagged 'Fact not in source' can never be "
            + "set to pass.",
        ] },
        { type: "note", tone: "info",
          text: "Scope shows 'Platform' (the AA master tenant) or 'Tenant' plus the first 8 characters of its id. "
            + "The page never shows tenant names." },

        { type: "h3", text: "Candidates tab" },
        { type: "p", text: "The rules the miner drafted. The status filter starts at candidate. Switch it to active "
          + "to see the live rules, or to rejected to see what failed. Columns: Drafted, Scope, Writer, Kind, Rule "
          + "(→ replacement), Support, Lint, Status. Click a row to open its drawer." },
        { type: "p", text: "In the drawer:" },
        { type: "list", items: [
          "Edit (candidates only): change the rule (≤ 25 words), the replacement (lexicon rules only; empty means "
            + "ban the phrase) and the match phrases (one per line, ≤ 5). 'Save and lint' runs the lint again. If it "
            + "fails, the rule becomes rejected. If the edit contains tenant text or is too long, nothing is saved.",
          "Reject: type a reason (at least 3 characters), then click Reject.",
          "Run R0: free, no model call. It replays the rule over up to 300 stored pieces on the worker and takes "
            + "minutes. When the job ends, close and reopen the drawer to see the result.",
          "Activate (L0): only for a lexicon rule of t9_blog or t9_social. It stays greyed out, with 'Not "
            + "activatable yet: …', until the lint passed and R0 passed on the rule's current text. After the click, "
            + "the next T9 write gets the rule.",
          "Roll back (active rules only): type a reason, then click. The rule becomes rejected, and the next write "
            + "no longer gets it.",
          "Provenance: every signal behind the rule, with its source and its before/after. Read it before you "
            + "activate anything.",
        ] },
        { type: "note", tone: "info",
          text: "Any edit makes the old R0 result stale. Run R0 again before Activate (L0)." },

        { type: "h3", text: "What an active L0 rule does" },
        { type: "list", items: [
          "The phrase joins the brand's 'Never use' words for that owner's T9 writes.",
          "With a replacement, the writer is also told 'Write X instead of Y'.",
          "If the phrase still appears, the lexicon gate flags it as blocking, and the repair step can fix it.",
          "It is skipped when the phrase is one of the tenant's signature words or part of the CTA, the brand name "
            + "or the trip name. A tenant's own rule wins over a platform rule.",
          "It guides the writer before the gates. It never swaps words in the text afterwards.",
        ] },

        { type: "h3", text: "Header buttons" },
        { type: "p", text: "Each button except Refresh queues one background job. The message says 'Queued (job …); "
          + "see Platform › Jobs'. The result numbers are on Platform › Jobs, not on this page." },
        { type: "action", label: "Backfill (dry run)",
          does: "Counts what Backfill signals would copy, per source and per tenant. The sources: old admin "
            + "sentence edits, tenant T2 edits, failed T9 attempts and A1 brand-audit lessons. It inserts nothing.",
          cost: "$0. Up to about 15 minutes.",
          when: "Before the first real backfill, or to check whether older data is missing." },
        { type: "action", label: "Backfill signals",
          does: "Copies the missing older signals, then queues the fact filter. Safe to run twice: nothing is added "
            + "twice.",
          cost: "$0. Up to about 15 minutes.",
          when: "Once after setup, or when the dry run shows missing rows.",
          avoid: "Just out of habit. New edits are captured live." },
        { type: "action", label: "Run fact filter",
          does: "Marks pending signals pass, grey or fail (up to 5000 per run). It also checks the automatic mining "
            + "trigger, but starts a paid run only if automatic mining is switched on (it is off by default).",
          cost: "$0. Up to about 10 minutes.",
          when: "Signals are pending and you do not want to wait for the daily run." },
        { type: "action", label: "R0 baseline",
          does: "The 'R0 sanity check'. It re-runs the gates on stored T9 blog and social texts with no rule, and "
            + "compares the result with what the gates said at write time. It writes nothing. High agreement means "
            + "you can trust R0 results.",
          cost: "$0. Heavy on the worker's CPU, up to about 20 minutes.",
          when: "After a change to the gates, or before you rely on R0 results for the first time.",
          avoid: "A wave is running." },
        { type: "action", label: "Mine rules",
          does: "Expires old candidates, builds the clusters, then makes at most one model call per cluster marked "
            + "'next run drafts it'. It runs the lint and stores each draft as a candidate or as rejected by lint. A "
            + "confirm dialog shows this month's spend first.",
          cost: "Paid. At most 50 calls and $1 per run, $2 per day and $25 per month for the whole learning loop. "
            + "The real cost is about $0.001 per call, so a full run is about $0.05. Up to about 30 minutes. A "
            + "failed run is never retried on its own.",
          when: "The Clusters table shows at least one 'next run drafts it'.",
          avoid: "No cluster says 'next run drafts it' (the run would still use one of the 3 runs of the month), or a "
            + "wave is running." },
        { type: "note", tone: "warning", title: "3 mining runs per month",
          text: "Months count in UTC. Every started run counts, even one that drafts nothing. The confirm dialog says "
            + "a run is refused when the month is over the cap. In fact the run starts, stops before its first paid "
            + "call, and still counts as one of the 3." },
        { type: "note", tone: "warning", title: "Waves and deploys",
          text: "Nothing blocks Mine rules, R0 or R0 baseline during a wave: not running them then is our own rule, "
            + "because they use the worker's CPU. Also, any queued or running job holds back a backend deploy, so "
            + "start long jobs when no deploy is planned." },
        { type: "note", tone: "info",
          text: "R0 baseline and the per-rule Run R0 use the same job kind, so the last-job line may show either one." },
      ],
    },
    {
      id: "labels",
      heading: "4. Reading the labels",
      blocks: [
        { type: "h3", text: "Facts (fact status of a signal)" },
        { type: "terms", items: [
          { term: "pending", text: "Not checked yet. The daily fact filter will check it." },
          { term: "grey", text: "The edit added a name the source does not have. It waits for your Pass or Fail." },
          { term: "pass", text: "Usable for learning. Cuts, style edits and reason-only signals are pass." },
          { term: "fail", text: "The edit added a number, time, price or promise word ('included', 'free', "
            + "'pick-up'…) the source does not have, or it was tagged 'Fact not in source'. Never learned from." },
        ] },
        { type: "h3", text: "State (a cluster)" },
        { type: "terms", items: [
          { term: "has a rule", text: "An open rule (candidate, approved or active) already exists for it." },
          { term: "next run drafts it", text: "It has at least 3 signals and no open rule. The next Mine rules run "
            + "makes one paid call for it." },
          { term: "needs 3 signals", text: "It is over its threshold but has only 2 signals. It waits, at no cost. "
            + "The drafter sees 1 signal and 2 are kept aside to test the draft." },
        ] },
        { type: "p", text: "A cluster whose draft the lint rejected is not drafted again until new signals join it. "
          + "A cluster whose rule an admin rejected or rolled back is drafted again on the next run, with a new paid "
          + "call." },
        { type: "h3", text: "Thresholds (when a cluster is over threshold)" },
        { type: "list", items: [
          "Tenant rule: at least 2 human edits on at least 2 pieces of that tenant.",
          "Platform rule from tenant edits: the same thing in at least 3 different tenants.",
          "Platform rule from A1 human edits: at least 2 pieces, by at least 2 different reviewers.",
          "Platform rule from A1 gate failures: on at least 5% of the A1 pieces of the last 30 days.",
        ] },
        { type: "h3", text: "Status (a rule)" },
        { type: "terms", items: [
          { term: "candidate", text: "Drafted, lint passed, waiting for a human. Expires after 60 days." },
          { term: "approved", text: "Approved for a release. No button sets this yet." },
          { term: "active", text: "In use by a writer. Never expires." },
          { term: "rejected", text: "Rejected by the lint or by an admin, or rolled back. The drawer shows why." },
          { term: "expired", text: "A candidate or approved rule left for 60 days. Checked when a mining run starts." },
        ] },
        { type: "h3", text: "Kind (a rule)" },
        { type: "terms", items: [
          { term: "lexicon", text: "Ban a phrase, or replace it with neutral wording. The only kind that can go live "
            + "today (T9, through L0)." },
          { term: "writer_instruction", text: "A short instruction to the writer. Needs a release." },
          { term: "repair_hint", text: "A hint for the repair step. Needs a release." },
          { term: "preference_summary", text: "A tenant's taste in at most 5 sentences. Planned; the miner does not "
            + "draft it yet." },
        ] },
        { type: "h3", text: "Lint badges (green = passed, red = failed; hover for the detail)" },
        { type: "terms", items: [
          { term: "Size", text: "Within the word, character and phrase limits." },
          { term: "Only constrains", text: "The rule only holds the writer back. It fails if it says add, mention, "
            + "include, promise… without a 'not'." },
          { term: "Replacement adds nothing", text: "A replacement adds no number, time, price, name or promise word. "
            + "'lunch' → 'lunch included' fails." },
          { term: "No tenant text", text: "A platform rule copies no 5-word run of tenant text and names no tenant "
            + "brand. If the tenant text cannot be loaded, it fails to be safe." },
          { term: "Explains held-out edits", text: "The rule's phrases explain at least 2 of the edits kept aside." },
        ] },
        { type: "p", text: "A rule is a candidate only when every badge is green." },
        { type: "h3", text: "Drawer badges" },
        { type: "list", items: [
          "The status, the size in tokens, the expiry date and who drafted it.",
          "Active rules of one writer may use at most 400 tokens for the platform and 250 per tenant. Over that, "
            + "Activate is refused.",
        ] },
        { type: "h3", text: "R0 panel" },
        { type: "terms", items: [
          { term: "L0 passed / L0 failed", text: "Whether the rule may go live through L0. The red line under it "
            + "says why not." },
          { term: "recurs (R2 eligible) / under k", text: "Whether the problem still repeats often enough in recent "
            + "signals (k is the mining threshold)." },
          { term: "Phrase in stored text: before → after", text: "How often the phrase appears in stored texts now, "
            + "and after the rule." },
          { term: "new hard stops", text: "Gate failures the rule would newly cause. Should be none." },
          { term: "Would fire on X of Y pieces", text: "How many stored pieces the rule would touch." },
          { term: "Past failures it would have prevented", text: "Earlier gate failures the rule would have avoided." },
          { term: "Signals matched", text: "The signals the rule explains." },
          { term: "Baseline", text: "How well the replay agrees with the stored gate results, with the rule off." },
          { term: "stale: the rule changed after this run", text: "The rule was edited after R0. Run R0 again." },
        ] },
        { type: "h3", text: "'Not activatable yet: …'" },
        { type: "p", text: "The message names what is missing. All of these must hold: a lexicon rule, a T9 writer, "
          + "status candidate or approved, lint passed, a valid phrase (real words, at most 6, not only small words "
          + "like 'the' or 'a'), a replacement that adds nothing, R0 run on the current text and passed, and room "
          + "under the token cap." },
      ],
    },
    {
      id: "routine",
      heading: "5. Weekly routine",
      blocks: [
        { type: "list", ordered: true, items: [
          "Open the Signals tab and look at the Signals tile: how many are pending and grey.",
          "Signals pending for more than a day? Click Run fact filter.",
          "Set the filter to grey. For each row, compare before and after with the source, then click Pass or Fail.",
          "Look at Clusters over threshold. Is there a 'next run drafts it'? Check that no wave is running and that "
            + "the month has a run left (3 at most). Then click Mine rules and confirm.",
          "When the mining job has finished (Platform › Jobs), open Candidates. For each rule, read it and its "
            + "Provenance. Edit it if the wording is off. Reject it, with a reason, if it is wrong.",
          "For a T9 lexicon candidate you agree with: Run R0 (outside a wave), reopen the drawer, and check the R0 "
            + "panel. If L0 passed and the rule would not fire in the wrong places, click Activate (L0).",
          "Set the status filter to active. Check that the live rules still make sense. Roll back any that hurt.",
          "Glance at the spend tile.",
        ] },
        { type: "h3", text: "Now and then" },
        { type: "list", items: [
          "After a change to the gates: R0 baseline.",
          "After a new kind of edit is captured, or an import of old data: Backfill (dry run), then Backfill signals.",
          "Candidates expire after 60 days, so review them before that.",
        ] },
      ],
    },
    {
      id: "not-in-ui",
      heading: "6. Not on this page yet",
      blocks: [
        { type: "p", text: "Some steps have no button yet. On Nghiệp's request, an agent does them through the API "
          + "or a command-line tool." },
        { type: "h3", text: "Done by an agent today" },
        { type: "list", items: [
          "Releases: create, attach evidence, approve, activate, roll back, reject. A Releases tab is planned.",
          "The per-writer memory switch, and closing a month's release train.",
          "Release 0 replay and approval, the rollback drill and the golden set import (command-line tools).",
        ] },
        { type: "h3", text: "Not built anywhere yet" },
        { type: "list", items: [
          "Approving a candidate (candidate → approved). So rules other than T9 lexicon rules cannot move forward "
            + "yet.",
          "The R2 and shadow evaluation runs. So a release with rules cannot be evaluated yet.",
          "Writers reading rules from a release. Today only empty releases can be active.",
          "Exemplars and Metrics tabs.",
          "Tenant names, the numbers of a dry run or baseline (Platform › Jobs only), and more than 200 signals in "
            + "the list.",
        ] },
        { type: "note", tone: "warning", title: "The memory switch does not stop L0 rules",
          text: "T9 reads active lexicon rules directly. To stop one, roll it back in its drawer." },
        { type: "h3", text: "How a release works (for when the tab arrives)" },
        { type: "list", items: [
          "A release is one versioned bundle of rules for one writer and one owner (the platform, or one tenant).",
          "It goes draft → evaluated → approved → active. Activating it retires the release before it. It can be "
            + "rejected at any step before active.",
          "Evaluated means its evidence is attached. Empty release: an R0 replay with 0 differences over at least "
            + "20 inputs, plus the baseline evaluation. Lexicon-only release: R0. Release with other rules: R2 and "
            + "shadow runs.",
          "Every release, tenant releases too, needs one approval by a signed-in admin. None goes live on its own.",
          "One regular release per writer and owner per month (the monthly train). Lexicon rules, emergencies and "
            + "rollbacks do not wait for it.",
          "A live release is flagged 'reverify' when the golden set, a model route or the gates changed since its "
            + "evidence. It keeps serving until it is re-checked.",
          "A tenant release holds only that tenant's rules. A platform rule built from tenant edits needs at least 3 "
            + "tenants and Nghiệp's approval.",
        ] },
        { type: "h3", text: "State on 27/10/2026" },
        { type: "list", items: [
          "Release 0 (empty) is active on the platform for all four writers (a1, t2, t9_blog, t9_social), with the "
            + "memory switch on. Nghiệp approved it.",
          "Its prompts are byte-for-byte the same as without memory, so the writers' output has not changed.",
          "The rollback drill passed on one test tenant.",
          "No active L0 rule on record; few or no clusters so far.",
        ] },
        { type: "note", tone: "info",
          text: "This comes from the release 0 record, not a live read. The tiles and tables above are live." },
      ],
    },
    {
      id: "glossary",
      heading: "7. Glossary",
      blocks: [
        { type: "terms", items: [
          { term: "Signal", text: "One saved piece of evidence about a writer's output: a human edit, a reject or "
            + "regenerate, or a gate failure." },
          { term: "Fact filter", text: "The free daily check that marks each signal pass, grey or fail." },
          { term: "Cluster", text: "A group of pass signals that point at the same problem." },
          { term: "Threshold", text: "How often a problem must repeat before a rule is drafted (section 4)." },
          { term: "Rule", text: "One short instruction (at most 25 words) for one writer." },
          { term: "Candidate", text: "A drafted rule that passed the lint and waits for a human." },
          { term: "Lint", text: "The automatic check on every drafted or edited rule (the five badges)." },
          { term: "R0", text: "A free replay of a rule over stored texts. No model call." },
          { term: "R0 baseline", text: "R0 with no rule: checks that the replay agrees with the stored gate results." },
          { term: "L0", text: "The fast path: a T9 lexicon rule proven by R0 goes live in one click." },
          { term: "R2 / shadow", text: "Evaluation runs a release with other rules will need. Not built yet." },
          { term: "Release", text: "A versioned, approved bundle of rules for one writer and one owner." },
          { term: "Golden set", text: "A fixed set of test inputs (tours, blog and social posts, plus traps) used to "
            + "evaluate a release." },
          { term: "Memory switch", text: "Per writer: on means the writer reads its active release. Off means it "
            + "writes as if there were no memory. It does not affect L0 rules." },
          { term: "Scope", text: "Who a signal or rule belongs to: Platform (AA, every tenant) or one tenant." },
          { term: "Provenance", text: "The signals a rule was drafted from." },
          { term: "Wave", text: "A planned rerun of the content pipeline. Heavy jobs wait until it ends." },
          { term: "Learning-loop spend", text: "Money spent on mining and other learning runs, capped at $25 a month." },
        ] },
      ],
    },
  ],
};

const vi: GuideContent = {
  title: "Cách dùng Writing memory",
  intro: "Đọc một lần từ đầu đến cuối. Sau đó chỉ cần làm theo việc hằng tuần (mục 5).",
  actionLabels: { does: "Làm gì", cost: "Chi phí và thời gian", when: "Dùng khi", avoid: "Không dùng khi" },
  sections: [
    {
      id: "purpose",
      heading: "1. Trang này để làm gì",
      blocks: [
        { type: "p", text: "Các writer (tour A1, tour tenant T2, bài blog và social T9) lặp lại cùng một lỗi, và "
          + "người phải sửa tay. Trang này gom các lần sửa đó, tìm lỗi lặp lại, rồi biến chúng thành những rule ngắn "
          + "để writer làm theo ở lần viết sau." },
        { type: "p", text: "Không có gì ở đây tự đổi writer. Một rule chỉ đến được writer sau một bước kiểm và một "
          + "cú click của người. Tenant không bao giờ thấy trang này hay các rule của nó. Họ chỉ nhận được bài cần sửa "
          + "ít hơn." },
        { type: "note", tone: "info", title: "Con đường duy nhất đang chạy thật",
          text: "Hôm nay chỉ một con đường thật sự đổi được cách writer viết: rule lexicon (cấm hoặc thay một cụm từ) "
            + "cho T9 blog hoặc social, qua Run R0 rồi Activate (L0). Mọi thứ khác trên trang là gom bằng chứng cho "
            + "sau này." },
      ],
    },
    {
      id: "loop",
      heading: "2. Cách hoạt động, 6 bước",
      blocks: [
        { type: "list", ordered: true, items: [
          "Ghi nhận. Một người sửa bài writer viết: admin cắt hoặc viết lại một câu, sửa một trường A1, hoặc "
            + "reject, dismiss, regenerate một dòng review; tenant sửa một bài trong portal. Mỗi hành động được lưu "
            + "lặng lẽ thành một signal. Các lần sửa cũ và lỗi gate trước đây vào qua backfill.",
          "Lọc fact. Mỗi ngày một lần, hoặc khi bạn bấm Run fact filter, mỗi signal mới được đánh pass, grey hoặc "
            + "fail. Fail: lần sửa thêm một fact nguồn không có, nên không bao giờ được học. Grey: thêm một tên nguồn "
            + "không có, nên admin quyết định. Pass: học được.",
          "Gom cụm. Các signal pass trong 90 ngày gần nhất nói cùng một điều được gom thành một cluster. Cluster lặp "
            + "đủ nhiều thì 'vượt ngưỡng' (over threshold).",
          "Soạn rule (Mine rules, tốn tiền). Mỗi cluster sẵn sàng được một lần gọi model soạn một rule ngắn. Một "
            + "bước kiểm tự động, gọi là lint, kiểm rule đó. Rule đạt thành candidate. Rule không đạt được lưu là "
            + "rejected bởi lint.",
          "Kiểm (miễn phí). Admin đọc candidate, sửa hoặc reject, rồi chạy R0: chạy lại rule trên các bài đã lưu để "
            + "xem rule sẽ bắt ở đâu và lẽ ra đã chặn được gì. Không gọi model.",
          "Đưa vào dùng. Rule lexicon T9 có R0 đạt: bấm Activate (L0), lần viết T9 kế tiếp sẽ dùng nó. Mọi rule khác "
            + "phải vào một release: đánh giá, rồi một người duyệt, rồi kích hoạt. Đường release chưa có nút bấm "
            + "(mục 6).",
        ] },
        { type: "p", text: "Gói trong một dòng:" },
        { type: "list", items: [
          "người sửa + lỗi gate → signal → lọc fact → cluster → candidate rule → lint + R0 → "
            + "Activate (L0), hoặc release (đánh giá → duyệt → kích hoạt) → writer dùng",
        ] },
        { type: "h3", text: "Hoàn tác" },
        { type: "list", items: [
          "Rule L0: mở nó trong Candidates (lọc status: active), gõ lý do, bấm Roll back. Lần viết kế tiếp không còn "
            + "nhận rule đó.",
          "Release: roll back thì release trước đó active trở lại, không cần deploy. Hiện agent làm việc này qua API.",
        ] },
      ],
    },
    {
      id: "tabs",
      heading: "3. Các tab và nút",
      blocks: [
        { type: "h3", text: "Các ô số ở đầu trang" },
        { type: "list", items: [
          "Signals: tổng số, rồi số pass, grey, fail và pending.",
          "Candidates: rule đang chờ người xem, và số đã bị reject.",
          "Learning-loop spend this month: tiền mining và các lần chạy học khác đã tiêu, so với trần $25 mỗi tháng "
            + "và trần $1 mỗi lần chạy.",
          "Next automatic run: off, waiting hoặc due. Mining tự động mặc định tắt, nên 'off' là bình thường.",
          "Dòng dưới các ô: trạng thái job gần nhất của từng loại. Chỉ có trạng thái, không có số: mở "
            + "Platform › Jobs để xem số.",
        ] },
        { type: "note", tone: "warning", title: "Ô 'Next automatic run' có thể sai",
          text: "Ô này đọc công tắc trên service API, nhưng job chạy trên service worker. Nếu công tắc chỉ bật ở "
            + "worker, ô vẫn ghi 'off'." },

        { type: "h3", text: "Tab Signals" },
        { type: "list", items: [
          "Clusters over threshold: What repeats (rê chuột để xem nhãn), Support (số signal, bài, tenant, reviewer), "
            + "Basis, State và một Sample đã che dữ liệu. Xem thì không tốn gì.",
          "Signals: 200 signal mới nhất, có bộ lọc theo fact status. Mỗi dòng có When, Scope, Writer, Action (làm "
            + "gì, ai làm, chip lý do, gate), Before → after, và Facts.",
          "Signal grey có nút Pass và Fail ở cột Facts. Bấm Pass khi tên được thêm vào là đúng và nguồn có. Bấm Fail "
            + "khi không. Signal gắn chip 'Fact not in source' không bao giờ đặt được thành pass.",
        ] },
        { type: "note", tone: "info",
          text: "Scope ghi 'Platform' (tenant gốc của AA) hoặc 'Tenant' kèm 8 ký tự đầu của id. Trang không bao giờ "
            + "hiện tên tenant." },

        { type: "h3", text: "Tab Candidates" },
        { type: "p", text: "Các rule mà miner đã soạn. Bộ lọc status mặc định là candidate. Đổi sang active để xem "
          + "rule đang chạy, hoặc rejected để xem rule đã hỏng. Các cột: Drafted, Scope, Writer, Kind, Rule "
          + "(→ replacement), Support, Lint, Status. Bấm một dòng để mở drawer." },
        { type: "p", text: "Trong drawer:" },
        { type: "list", items: [
          "Edit (chỉ candidate): sửa rule (≤ 25 từ), replacement (chỉ rule lexicon; để trống nghĩa là cấm cụm từ) và "
            + "match phrases (mỗi dòng một cụm, ≤ 5). 'Save and lint' chạy lại lint. Không đạt thì rule thành "
            + "rejected. Nếu bản sửa chứa chữ của tenant hoặc quá dài thì không lưu gì.",
          "Reject: gõ lý do (ít nhất 3 ký tự), rồi bấm Reject.",
          "Run R0: miễn phí, không gọi model. Chạy lại rule trên tối đa 300 bài đã lưu, trên worker, mất vài phút. "
            + "Khi job xong, đóng rồi mở lại drawer để xem kết quả.",
          "Activate (L0): chỉ cho rule lexicon của t9_blog hoặc t9_social. Nút bị mờ, kèm 'Not activatable yet: …', "
            + "cho tới khi lint đạt và R0 đạt trên đúng nội dung hiện tại của rule. Sau khi bấm, lần viết T9 kế tiếp "
            + "nhận rule.",
          "Roll back (chỉ rule active): gõ lý do, rồi bấm. Rule thành rejected, lần viết kế tiếp không còn nhận nó.",
          "Provenance: mọi signal làm nên rule, kèm nguồn và before/after. Đọc phần này trước khi kích hoạt gì.",
        ] },
        { type: "note", tone: "info",
          text: "Mọi lần sửa đều làm kết quả R0 cũ hết hiệu lực (stale). Chạy lại R0 trước khi Activate (L0)." },

        { type: "h3", text: "Rule L0 đang active làm gì" },
        { type: "list", items: [
          "Cụm từ được thêm vào danh sách 'Never use' của brand cho các lần viết T9 của chủ rule đó.",
          "Nếu có replacement, writer còn được dặn 'Write X instead of Y' (viết X thay cho Y).",
          "Nếu cụm từ vẫn xuất hiện, gate lexicon gắn cờ blocking, và bước repair sửa được.",
          "Rule bị bỏ qua khi cụm từ là từ đặc trưng của tenant, hoặc nằm trong CTA, tên brand hay tên trip. Rule "
            + "riêng của tenant thắng rule platform.",
          "Rule hướng dẫn writer trước các gate. Nó không bao giờ tự thay chữ trong bài sau đó.",
        ] },

        { type: "h3", text: "Các nút trên đầu trang" },
        { type: "p", text: "Mỗi nút, trừ Refresh, xếp một job chạy nền. Thông báo ghi 'Queued (job …); see "
          + "Platform › Jobs'. Số liệu kết quả nằm ở Platform › Jobs, không ở trang này." },
        { type: "action", label: "Backfill (dry run)",
          does: "Đếm những gì Backfill signals sẽ chép, theo từng nguồn và từng tenant. Các nguồn: các lần admin sửa "
            + "câu trước đây, các lần tenant sửa T2, các lần T9 viết hỏng, và bài học từ audit brand A1. Không chèn gì.",
          cost: "$0. Tối đa khoảng 15 phút.",
          when: "Trước lần backfill thật đầu tiên, hoặc để xem dữ liệu cũ có còn thiếu không." },
        { type: "action", label: "Backfill signals",
          does: "Chép các signal cũ còn thiếu, rồi xếp job lọc fact. Chạy hai lần vẫn an toàn: không thêm trùng.",
          cost: "$0. Tối đa khoảng 15 phút.",
          when: "Một lần sau khi thiết lập, hoặc khi bản dry run cho thấy còn thiếu dòng.",
          avoid: "Chỉ vì thói quen. Các lần sửa mới đã được ghi nhận ngay." },
        { type: "action", label: "Run fact filter",
          does: "Đánh pass, grey hoặc fail cho signal đang pending (tối đa 5000 mỗi lần). Nó cũng kiểm điều kiện "
            + "mining tự động, nhưng chỉ mở lần chạy tốn tiền khi mining tự động được bật (mặc định tắt).",
          cost: "$0. Tối đa khoảng 10 phút.",
          when: "Có signal pending và bạn không muốn chờ lần chạy hằng ngày." },
        { type: "action", label: "R0 baseline",
          does: "Chính là 'R0 sanity check'. Chạy lại các gate trên bài blog và social T9 đã lưu, không có rule nào, "
            + "rồi so với kết quả gate lúc viết. Không ghi gì. Tỷ lệ khớp cao nghĩa là tin được kết quả R0.",
          cost: "$0. Nặng CPU worker, tối đa khoảng 20 phút.",
          when: "Sau khi gate thay đổi, hoặc trước lần đầu dựa vào kết quả R0.",
          avoid: "Đang có wave chạy." },
        { type: "action", label: "Mine rules",
          does: "Cho hết hạn các candidate cũ, dựng cluster, rồi gọi model tối đa một lần cho mỗi cluster ghi 'next "
            + "run drafts it'. Chạy lint và lưu mỗi bản nháp thành candidate hoặc rejected bởi lint. Một hộp xác nhận "
            + "hiện số tiền đã tiêu trong tháng trước.",
          cost: "Tốn tiền. Tối đa 50 lần gọi và $1 mỗi lần chạy, $2 mỗi ngày và $25 mỗi tháng cho cả vòng học. Chi "
            + "phí thật khoảng $0.001 mỗi lần gọi, nên một lần chạy đầy đủ khoảng $0.05. Tối đa khoảng 30 phút. Lần "
            + "chạy hỏng không bao giờ tự chạy lại.",
          when: "Bảng Clusters có ít nhất một dòng 'next run drafts it'.",
          avoid: "Không cluster nào ghi 'next run drafts it' (lần chạy vẫn tính vào 3 lần của tháng), hoặc đang có "
            + "wave chạy." },
        { type: "note", tone: "warning", title: "3 lần mining mỗi tháng",
          text: "Tháng tính theo giờ UTC. Mọi lần đã bắt đầu đều tính, kể cả lần không soạn được gì. Hộp xác nhận "
            + "ghi rằng lần chạy bị từ chối khi tháng đã vượt trần. Thật ra lần chạy vẫn bắt đầu, dừng trước lần gọi "
            + "tốn tiền đầu tiên, và vẫn tính là một trong 3 lần." },
        { type: "note", tone: "warning", title: "Wave và deploy",
          text: "Không có gì chặn Mine rules, R0 hay R0 baseline trong lúc có wave: không chạy chúng lúc đó là quy "
            + "ước của mình, vì chúng dùng CPU của worker. Ngoài ra, mọi job đang chờ hoặc đang chạy đều giữ lại "
            + "deploy backend, nên hãy chạy job dài khi không định deploy." },
        { type: "note", tone: "info",
          text: "R0 baseline và Run R0 trên từng rule dùng chung một loại job, nên dòng job gần nhất có thể là một "
            + "trong hai." },
      ],
    },
    {
      id: "labels",
      heading: "4. Đọc các nhãn",
      blocks: [
        { type: "h3", text: "Facts (fact status của signal)" },
        { type: "terms", items: [
          { term: "pending", text: "Chưa kiểm. Lần lọc fact hằng ngày sẽ kiểm." },
          { term: "grey", text: "Lần sửa thêm một tên mà nguồn không có. Chờ bạn bấm Pass hoặc Fail." },
          { term: "pass", text: "Học được. Cắt câu, sửa văn phong và signal chỉ có lý do đều là pass." },
          { term: "fail", text: "Lần sửa thêm một con số, giờ, giá hoặc từ hứa hẹn ('included', 'free', "
            + "'pick-up'…) mà nguồn không có, hoặc được gắn 'Fact not in source'. Không bao giờ được học." },
        ] },
        { type: "h3", text: "State (của cluster)" },
        { type: "terms", items: [
          { term: "has a rule", text: "Đã có một rule đang mở (candidate, approved hoặc active) cho nó." },
          { term: "next run drafts it", text: "Có ít nhất 3 signal và chưa có rule mở. Lần Mine rules kế tiếp sẽ "
            + "gọi model một lần (tốn tiền) cho nó." },
          { term: "needs 3 signals", text: "Đã vượt ngưỡng nhưng mới có 2 signal. Nó chờ, không tốn gì. Người soạn "
            + "thấy 1 signal, còn 2 signal được giữ riêng để kiểm bản nháp." },
        ] },
        { type: "p", text: "Cluster có bản nháp bị lint reject sẽ không được soạn lại cho tới khi có signal mới. "
          + "Cluster có rule bị admin reject hoặc roll back sẽ được soạn lại ở lần chạy kế tiếp, với một lần gọi "
          + "tốn tiền mới." },
        { type: "h3", text: "Ngưỡng (khi nào cluster vượt ngưỡng)" },
        { type: "list", items: [
          "Rule tenant: ít nhất 2 lần người sửa trên ít nhất 2 bài của tenant đó.",
          "Rule platform từ các lần tenant sửa: cùng một điều ở ít nhất 3 tenant khác nhau.",
          "Rule platform từ các lần người sửa A1: ít nhất 2 bài, bởi ít nhất 2 reviewer khác nhau.",
          "Rule platform từ lỗi gate A1: trên ít nhất 5% số bài A1 trong 30 ngày gần nhất.",
        ] },
        { type: "h3", text: "Status (của rule)" },
        { type: "terms", items: [
          { term: "candidate", text: "Đã soạn, lint đạt, chờ người xem. Hết hạn sau 60 ngày." },
          { term: "approved", text: "Đã duyệt để vào release. Chưa có nút nào đặt trạng thái này." },
          { term: "active", text: "Writer đang dùng. Không bao giờ hết hạn." },
          { term: "rejected", text: "Bị lint hoặc admin reject, hoặc đã roll back. Drawer ghi lý do." },
          { term: "expired", text: "Candidate hoặc approved để quá 60 ngày. Được kiểm khi một lần mining bắt đầu." },
        ] },
        { type: "h3", text: "Kind (loại rule)" },
        { type: "terms", items: [
          { term: "lexicon", text: "Cấm một cụm từ, hoặc thay bằng cách nói trung tính. Loại duy nhất đưa vào dùng "
            + "được hôm nay (T9, qua L0)." },
          { term: "writer_instruction", text: "Một chỉ dẫn ngắn cho writer. Cần release." },
          { term: "repair_hint", text: "Một gợi ý cho bước repair. Cần release." },
          { term: "preference_summary", text: "Gu viết của một tenant, tối đa 5 câu. Đã lên kế hoạch; miner chưa "
            + "soạn loại này." },
        ] },
        { type: "h3", text: "Huy hiệu lint (xanh = đạt, đỏ = không đạt; rê chuột để xem chi tiết)" },
        { type: "terms", items: [
          { term: "Size", text: "Trong giới hạn số từ, số ký tự và số cụm từ." },
          { term: "Only constrains", text: "Rule chỉ được kìm writer lại. Không đạt nếu rule bảo thêm, nhắc tới, "
            + "bao gồm, hứa… mà không có chữ 'không'." },
          { term: "Replacement adds nothing", text: "Replacement không thêm con số, giờ, giá, tên hay từ hứa hẹn nào. "
            + "'lunch' → 'lunch included' là không đạt." },
          { term: "No tenant text", text: "Rule platform không chép chuỗi 5 từ liền nào của tenant và không nêu tên "
            + "brand tenant nào. Nếu không tải được chữ của tenant thì tính là không đạt cho an toàn." },
          { term: "Explains held-out edits", text: "Các cụm từ của rule giải thích được ít nhất 2 lần sửa được giữ "
            + "riêng." },
        ] },
        { type: "p", text: "Rule chỉ thành candidate khi mọi huy hiệu đều xanh." },
        { type: "h3", text: "Huy hiệu trong drawer" },
        { type: "list", items: [
          "Trạng thái, kích thước tính bằng token, ngày hết hạn và ai soạn.",
          "Các rule active của một writer được dùng tối đa 400 token cho platform và 250 cho mỗi tenant. Vượt mức "
            + "đó thì Activate bị từ chối.",
        ] },
        { type: "h3", text: "Khung R0" },
        { type: "terms", items: [
          { term: "L0 passed / L0 failed", text: "Rule có được đưa vào dùng qua L0 hay không. Dòng đỏ bên dưới ghi "
            + "lý do nếu không." },
          { term: "recurs (R2 eligible) / under k", text: "Lỗi còn lặp đủ nhiều trong các signal gần đây hay không "
            + "(k là ngưỡng mining)." },
          { term: "Phrase in stored text: before → after", text: "Cụm từ xuất hiện bao nhiêu lần trong bài đã lưu "
            + "bây giờ, và sau khi có rule." },
          { term: "new hard stops", text: "Các lỗi gate mới mà rule sẽ gây ra. Phải là không có." },
          { term: "Would fire on X of Y pieces", text: "Rule sẽ chạm vào bao nhiêu bài đã lưu." },
          { term: "Past failures it would have prevented", text: "Các lỗi gate trước đây mà rule lẽ ra đã tránh được." },
          { term: "Signals matched", text: "Các signal mà rule giải thích được." },
          { term: "Baseline", text: "Mức khớp giữa lần chạy lại và kết quả gate đã lưu, khi tắt rule." },
          { term: "stale: the rule changed after this run", text: "Rule đã bị sửa sau lần R0. Chạy lại R0." },
        ] },
        { type: "h3", text: "'Not activatable yet: …'" },
        { type: "p", text: "Thông báo nêu điều còn thiếu. Phải đủ tất cả: rule lexicon, writer T9, status candidate "
          + "hoặc approved, lint đạt, cụm từ hợp lệ (từ thật, tối đa 6 từ, không chỉ toàn từ nhỏ như 'the' hay 'a'), "
          + "replacement không thêm gì, R0 đã chạy trên nội dung hiện tại và đạt, và còn chỗ trong trần token." },
      ],
    },
    {
      id: "routine",
      heading: "5. Việc hằng tuần",
      blocks: [
        { type: "list", ordered: true, items: [
          "Mở tab Signals và xem ô Signals: bao nhiêu pending và grey.",
          "Có signal pending quá một ngày? Bấm Run fact filter.",
          "Đặt bộ lọc là grey. Với mỗi dòng, so before và after với nguồn, rồi bấm Pass hoặc Fail.",
          "Xem Clusters over threshold. Có dòng 'next run drafts it' không? Kiểm không có wave đang chạy và tháng "
            + "này còn lượt (tối đa 3). Rồi bấm Mine rules và xác nhận.",
          "Khi job mining xong (Platform › Jobs), mở Candidates. Với mỗi rule, đọc rule và Provenance của nó. Sửa "
            + "nếu câu chữ chưa ổn. Reject, kèm lý do, nếu rule sai.",
          "Với candidate lexicon T9 mà bạn đồng ý: Run R0 (ngoài wave), mở lại drawer, xem khung R0. Nếu L0 passed "
            + "và rule không bắt nhầm chỗ, bấm Activate (L0).",
          "Đặt bộ lọc status là active. Kiểm các rule đang chạy vẫn hợp lý. Roll back rule nào gây hại.",
          "Liếc ô chi phí.",
        ] },
        { type: "h3", text: "Thỉnh thoảng" },
        { type: "list", items: [
          "Sau khi gate thay đổi: R0 baseline.",
          "Sau khi bắt đầu ghi nhận một kiểu sửa mới, hoặc nhập dữ liệu cũ: Backfill (dry run), rồi Backfill signals.",
          "Candidate hết hạn sau 60 ngày, nên xem trước khi tới hạn.",
        ] },
      ],
    },
    {
      id: "not-in-ui",
      heading: "6. Chưa có trên trang này",
      blocks: [
        { type: "p", text: "Một số bước chưa có nút bấm. Khi Nghiệp yêu cầu, agent làm qua API hoặc công cụ dòng "
          + "lệnh." },
        { type: "h3", text: "Hiện do agent làm" },
        { type: "list", items: [
          "Release: tạo, gắn bằng chứng, duyệt, kích hoạt, roll back, reject. Tab Releases đã lên kế hoạch.",
          "Công tắc memory theo từng writer, và việc chốt chuyến release của tháng.",
          "Chạy lại và duyệt release 0, diễn tập rollback, và nhập golden set (công cụ dòng lệnh).",
        ] },
        { type: "h3", text: "Chưa xây ở đâu cả" },
        { type: "list", items: [
          "Duyệt một candidate (candidate → approved). Vì vậy các rule không phải lexicon T9 chưa đi tiếp được.",
          "Các lần đánh giá R2 và shadow. Vì vậy release có rule chưa đánh giá được.",
          "Writer đọc rule từ release. Hôm nay chỉ release rỗng được active.",
          "Tab Exemplars và Metrics.",
          "Tên tenant, số liệu của dry run hay baseline (chỉ ở Platform › Jobs), và hơn 200 signal trong danh sách.",
        ] },
        { type: "note", tone: "warning", title: "Công tắc memory không dừng rule L0",
          text: "T9 đọc thẳng các rule lexicon đang active. Muốn dừng một rule, roll back nó trong drawer." },
        { type: "h3", text: "Release hoạt động thế nào (để dùng khi có tab)" },
        { type: "list", items: [
          "Release là một gói rule có đánh số phiên bản, cho một writer và một chủ (platform, hoặc một tenant).",
          "Nó đi draft → evaluated → approved → active. Kích hoạt release mới thì release trước đó nghỉ. Có thể "
            + "reject ở bất kỳ bước nào trước active.",
          "Evaluated nghĩa là đã gắn bằng chứng. Release rỗng: một lần R0 không lệch byte nào trên ít nhất 20 đầu "
            + "vào, cộng bản đánh giá baseline. Release chỉ có lexicon: R0. Release có rule khác: R2 và shadow.",
          "Mọi release, kể cả release của tenant, cần một admin đã đăng nhập duyệt. Không release nào tự chạy.",
          "Mỗi writer và mỗi chủ có một release thường mỗi tháng (chuyến tàu hằng tháng). Rule lexicon, việc khẩn và "
            + "rollback không phải chờ.",
          "Release đang chạy bị gắn cờ 'reverify' khi golden set, route model hoặc gate thay đổi kể từ lúc lấy bằng "
            + "chứng. Nó vẫn phục vụ cho tới khi được kiểm lại.",
          "Release của tenant chỉ chứa rule của chính tenant đó. Rule platform dựng từ các lần tenant sửa cần ít nhất "
            + "3 tenant và Nghiệp duyệt.",
        ] },
        { type: "h3", text: "Tình trạng ngày 27/10/2026" },
        { type: "list", items: [
          "Release 0 (rỗng) đang active trên platform cho cả bốn writer (a1, t2, t9_blog, t9_social), công tắc "
            + "memory đang bật. Nghiệp đã duyệt.",
          "Prompt của nó giống hệt từng byte so với khi không có memory, nên bài writer viết chưa đổi gì.",
          "Diễn tập rollback đã đạt trên một tenant thử nghiệm.",
          "Chưa có rule L0 nào active trong hồ sơ; hiện có ít hoặc chưa có cluster.",
        ] },
        { type: "note", tone: "info",
          text: "Phần này lấy từ hồ sơ release 0, không đọc trực tiếp. Các ô số và bảng ở trên mới là số thật." },
      ],
    },
    {
      id: "glossary",
      heading: "7. Thuật ngữ",
      blocks: [
        { type: "terms", items: [
          { term: "Signal", text: "Một bằng chứng đã lưu về bài writer viết: một lần người sửa, một lần reject hay "
            + "regenerate, hoặc một lỗi gate." },
          { term: "Fact filter", text: "Bước kiểm miễn phí hằng ngày, đánh mỗi signal là pass, grey hoặc fail." },
          { term: "Cluster", text: "Một nhóm signal pass cùng chỉ về một lỗi." },
          { term: "Threshold", text: "Ngưỡng: lỗi phải lặp bao nhiêu lần thì mới soạn rule (mục 4)." },
          { term: "Rule", text: "Một chỉ dẫn ngắn (tối đa 25 từ) cho một writer." },
          { term: "Candidate", text: "Rule đã soạn, đạt lint, đang chờ người xem." },
          { term: "Lint", text: "Bước kiểm tự động cho mọi rule vừa soạn hoặc vừa sửa (năm huy hiệu)." },
          { term: "R0", text: "Chạy lại miễn phí một rule trên các bài đã lưu. Không gọi model." },
          { term: "R0 baseline", text: "R0 khi không có rule: kiểm lần chạy lại khớp với kết quả gate đã lưu." },
          { term: "L0", text: "Đường nhanh: rule lexicon T9 đã qua R0 được đưa vào dùng bằng một cú click." },
          { term: "R2 / shadow", text: "Các lần đánh giá mà release có rule khác sẽ cần. Chưa xây." },
          { term: "Release", text: "Gói rule có phiên bản, đã được duyệt, cho một writer và một chủ." },
          { term: "Golden set", text: "Bộ đầu vào thử cố định (tour, bài blog và social, cộng các bẫy) dùng để đánh "
            + "giá release." },
          { term: "Memory switch", text: "Công tắc theo từng writer: bật thì writer đọc release đang active của nó. "
            + "Tắt thì writer viết như chưa có memory. Không ảnh hưởng rule L0." },
          { term: "Scope", text: "Signal hay rule thuộc về ai: Platform (AA, áp cho mọi tenant) hoặc một tenant." },
          { term: "Provenance", text: "Các signal mà rule được soạn ra từ đó." },
          { term: "Wave", text: "Một đợt chạy lại pipeline nội dung đã lên lịch. Job nặng chờ wave xong." },
          { term: "Learning-loop spend", text: "Tiền chi cho mining và các lần chạy học khác, trần $25 mỗi tháng." },
        ] },
      ],
    },
  ],
};

export const WRITING_MEMORY_GUIDE: Record<GuideLocale, GuideContent> = { en, vi };

export function guideFor(locale: string | null | undefined): GuideContent {
  return locale === "vi" ? WRITING_MEMORY_GUIDE.vi : WRITING_MEMORY_GUIDE.en;
}
