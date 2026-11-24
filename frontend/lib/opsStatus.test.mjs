// AA-1140 (S289): the pure helpers of the Ecosystem status page (lib/opsStatus.ts): URL view state, filters, sorts,
// chart rows, headroom and the "not measured yet is never green" rule. Fixtures are invented. Run: npm run test:unit.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  RESET_VIEW, chartRows, customProblem, eventKind, filterAttention, filterEvents, headroom, inApps, isoToLocal,
  latest, levelTone, localToIso, nextSort, onlyHidden, parseHidden, parseSort, parseView, peak, queueSeries,
  runtimeMatches, sectionDown, shortSha, shownSeries, signalSeries, sortRows, statusQuery, toggleHidden,
  unmeasuredApps, viewParams, yTop, fmtMs, fmtPct, ageMinutes, tableSorting, sortingParam, withSort,
  previousRequests, deltaPct, verdictHeadline, nextViewQuery, resetViewQuery, flipChip, tightestHeadroom,
} from "./opsStatus.ts";

const FRONTEND = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (p) => readFileSync(join(FRONTEND, p), "utf8");
const getter = (q) => { const p = new URLSearchParams(q); return (k) => p.get(k) ?? ""; };

test("view state: defaults, parsing, unknown values dropped", () => {
  const v = parseView(getter(""));
  assert.equal(v.range, "24h");
  assert.equal(v.compare, false);
  assert.deepEqual([v.apps, v.sev, v.st, v.ev], [[], [], [], []]);
  assert.equal(v.svc, "all");
  const w = parseView(getter("range=7d&compare=1&apps=cis,zzz,crm&sev=sev1,sevX&st=acked&svc=attention"
    + "&ev=pass&hide=traffic:cis,bogus:x,errors:booking&sort=att:since:asc,svc:uptime:sideways"));
  assert.equal(w.range, "7d");
  assert.equal(w.compare, true);
  assert.deepEqual(w.apps, ["cis", "crm"]);
  assert.deepEqual(w.sev, ["sev1"]);
  assert.deepEqual(w.st, ["acked"]);
  assert.equal(w.svc, "attention");
  assert.deepEqual(w.hide, { traffic: ["cis"], errors: ["booking"] });
  assert.deepEqual(w.sort, { att: "since:asc" });
  assert.equal(parseView(getter("range=30d")).range, "24h");
  assert.deepEqual(parseHidden("queue:shared,queue:crm,:x"), { queue: ["shared", "crm"] });
});

test("view params: defaults leave the URL, a round trip keeps the view", () => {
  assert.deepEqual(viewParams({ range: "24h", compare: false, apps: [], svc: "all" }),
    { range: null, compare: null, apps: null, svc: null });
  const p = viewParams({ range: "custom", from: "2026-11-20T00:00:00.000Z", to: "2026-11-21T00:00:00.000Z",
    compare: true, apps: ["booking"], hide: { latency: ["cis", "crm"] }, sort: { svc: "uptime:asc" } });
  const q = new URLSearchParams(Object.entries(p).filter(([, v]) => v));
  const back = parseView((k) => q.get(k) ?? "");
  assert.equal(back.range, "custom");
  assert.equal(back.compare, true);
  assert.deepEqual(back.apps, ["booking"]);
  assert.deepEqual(back.hide, { latency: ["cis", "crm"] });
  assert.deepEqual(back.sort, { svc: "uptime:asc" });
  for (const k of ["range", "apps", "hide", "sort", "compare"]) assert.ok(k in RESET_VIEW);
});

test("API query: preset, custom and compare", () => {
  assert.equal(statusQuery({ range: "1h", from: "", to: "", compare: false }), "range=1h");
  const q = new URLSearchParams(statusQuery({ range: "custom", from: "a", to: "b", compare: true }));
  assert.deepEqual([q.get("range"), q.get("from"), q.get("to"), q.get("compare")], ["custom", "a", "b", "true"]);
});

test("custom range problems follow the history setting", () => {
  const now = Date.parse("2026-11-24T02:00:00Z");
  assert.equal(customProblem("", "x", 14, now), "missing");
  assert.equal(customProblem("2026-11-23T02:00:00Z", "2026-11-23T01:00:00Z", 14, now), "order");
  assert.equal(customProblem("2026-11-23T02:00:00Z", "2026-11-25T01:00:00Z", 14, now), "future");
  assert.equal(customProblem("2026-11-01T00:00:00Z", "2026-11-02T00:00:00Z", 14, now), "tooOld");
  assert.equal(customProblem("2026-11-01T00:00:00Z", "2026-11-02T00:00:00Z", 30, now), null);
  assert.equal(customProblem("2026-11-23T02:00:00Z", "2026-11-23T02:05:00Z", 14, now), "tooShort");
  assert.equal(localToIso(""), "");
  assert.equal(isoToLocal(localToIso("2026-11-20T09:05")), "2026-11-20T09:05");
});

const ITEMS = [
  { id: "a", severity: "sev1", app: "cis", status: "open" },
  { id: "b", severity: "sev3", app: "booking", status: "acked" },
  { id: "c", severity: "sev2", app: "shared", status: "open" },
];

test("app filter scopes everything but shared rows; severity and status filters", () => {
  assert.ok(inApps("crm", []) && inApps("shared", ["cis"]) && !inApps("crm", ["cis"]));
  const ids = (v) => filterAttention(ITEMS, v).map((i) => i.id);
  assert.deepEqual(ids({ apps: [], sev: [], st: [] }), ["a", "b", "c"]);
  assert.deepEqual(ids({ apps: ["booking"], sev: [], st: [] }), ["b", "c"]);
  assert.deepEqual(ids({ apps: [], sev: ["sev1", "sev2"], st: ["open"] }), ["a", "c"]);
});

test("services filter: attention includes a runtime below its objective; not measured is not healthy", () => {
  const ok = { state: "ok" };
  const slow = { state: "ok", below_slo: true };
  const nm = { state: "not_measured" };
  assert.ok(runtimeMatches(ok, "healthy") && !runtimeMatches(slow, "healthy") && runtimeMatches(slow, "attention"));
  assert.ok(!runtimeMatches(nm, "healthy") && runtimeMatches(nm, "no_sample") && runtimeMatches(nm, "all"));
});

test("events: severity chips, app filter and search", () => {
  const ev = [
    { id: "1", severity: "sev2", app: "cis", text: "Alarm one", detail: "zz", open: true },
    { id: "2", severity: "info", app: "booking", text: "Deploy two", open: false },
    { id: "3", severity: "odd", app: "shared", text: "Other", open: false },
  ];
  assert.equal(eventKind(ev[2]), "info");
  assert.deepEqual(filterEvents(ev, { apps: [], ev: ["info"] }, "").map((e) => e.id), ["2", "3"]);
  assert.deepEqual(filterEvents(ev, { apps: ["cis"], ev: [] }, "").map((e) => e.id), ["1", "3"]);
  assert.deepEqual(filterEvents(ev, { apps: [], ev: [] }, " ZZ ").map((e) => e.id), ["1"]);
});

test("sorting: stable, missing values last, click flips", () => {
  const rows = [{ v: 2 }, { v: null }, { v: 9 }, { v: 2 }];
  assert.deepEqual(sortRows(rows, (r) => r.v, "desc").map((r) => r.v), [9, 2, 2, null]);
  assert.deepEqual(sortRows(rows, (r) => r.v, "asc").map((r) => r.v), [2, 2, 9, null]);
  assert.deepEqual(parseSort(undefined, "since:desc"), { col: "since", dir: "desc" });
  assert.equal(nextSort({ col: "since", dir: "desc" }, "since"), "since:asc");
  assert.equal(nextSort({ col: "since", dir: "asc" }, "app"), "app:desc");
  assert.deepEqual(tableSorting("uptime:asc"), [{ id: "uptime", desc: false }]);
  assert.deepEqual(tableSorting(""), []);
  assert.equal(sortingParam([{ id: "t", desc: true }]), "t:desc");
  assert.equal(sortingParam([]), "");
  assert.deepEqual(withSort({ a: "x:asc" }, "b", "y:desc"), { a: "x:asc", b: "y:desc" });
  assert.deepEqual(withSort({ a: "x:asc" }, "a", ""), {});
});

test("not measured, unknown and stale are grey, never green", () => {
  for (const s of ["none", "not_measured", "no_sample", "unknown", "stale", "few", undefined, null]) {
    assert.equal(levelTone(s), "neutral", String(s));
  }
  assert.equal(levelTone("ok"), "success");
  assert.equal(levelTone("crit"), "danger");
  assert.ok(sectionDown({ status: "not_measured" }) && sectionDown(undefined) && !sectionDown({ status: "ok" }));
});

test("charts: rows on one axis with the previous period, latest, peak, headroom, axis top", () => {
  const rows = chartRows(["2026-11-24T00:00:00Z", "2026-11-24T00:05:00Z"],
    [{ id: "cis", label: "CIS", values: [1, null], previous: [3] }]);
  assert.deepEqual(rows[0], { t: Date.parse("2026-11-24T00:00:00Z"), cis: 1, cis__prev: 3 });
  assert.deepEqual(rows[1].cis__prev, null);
  assert.equal(latest([1, 4, null]), 4);
  assert.equal(peak([null, 2, 7]), 7);
  assert.equal(peak([null]), null);
  assert.equal(headroom(30, 80), 50);
  assert.equal(headroom(30, 10, "below"), 20);
  assert.equal(headroom(null, 10), null);
  assert.ok(Math.abs(yTop([1, 2], [5, null]) - 5.5) < 1e-9);
  assert.equal(yTop([], []), 1.1);
});

test("legends: toggle, only this one, only again resets", () => {
  const all = ["cis", "booking", "tripplanner"];
  assert.deepEqual(toggleHidden([], "cis"), ["cis"]);
  assert.deepEqual(toggleHidden(["cis"], "cis"), []);
  const only = onlyHidden(all, [], "booking");
  assert.deepEqual(only, ["cis", "tripplanner"]);
  assert.deepEqual(onlyHidden(all, only, "booking"), []);
  assert.deepEqual(shownSeries([{ id: "cis" }, { id: "booking" }], ["cis"]).map((s) => s.id), ["booking"]);
});

test("signal series skip unmeasured apps and carry the previous period", () => {
  const sig = { status: "ok", apps: {
    cis: { measured: true, rate_per_min: [1, 2] }, crm: { measured: false },
    booking: { measured: true, rate_per_min: [3, 4] } },
    previous: { status: "ok", range: {}, apps: { cis: { measured: true, rate_per_min: [5, 6] } } } };
  const s = signalSeries(sig, "rate_per_min", []);
  assert.deepEqual(s.map((x) => x.id), ["cis", "booking"]);
  assert.deepEqual(s[0].previous, [5, 6]);
  assert.equal(s[1].previous, undefined);
  assert.deepEqual(unmeasuredApps(sig, []), ["tripplanner", "crm"]);
  assert.deepEqual(signalSeries(sig, "rate_per_min", ["booking"]).map((x) => x.id), ["booking"]);
  const jobs = { queues: [{ id: "shared", name: "shared.job", app: "cis", series_wait_min: [1] },
    { id: "booking", name: "booking.job", app: "booking", status: "not_measured" }] };
  assert.deepEqual(queueSeries(jobs, []).map((q) => q.id), ["shared"]);
});

test("tile deltas against the previous period", () => {
  const sig = { status: "ok", previous: { status: "ok", range: {}, apps: { cis: { measured: true, requests: [3, null, 7] },
    crm: { measured: false } } } };
  assert.equal(previousRequests(sig, "cis"), 10);
  assert.equal(previousRequests(sig, "crm"), null);
  assert.equal(previousRequests({ status: "ok" }, "cis"), null);
  assert.equal(deltaPct(15, 10), 50);
  assert.equal(deltaPct(5, 0), null);
});

test("numbers", () => {
  assert.equal(fmtMs(850.4), "850 ms");
  assert.equal(fmtMs(1500), "1.50 s");
  assert.equal(fmtMs(null), "—");
  assert.equal(fmtPct(1.234), "1.2 %");
  assert.equal(shortSha("dev-1a2b3c4d5e"), "1a2b3c4");
  assert.equal(ageMinutes("2026-11-24T00:00:00Z", Date.parse("2026-11-24T00:10:00Z")), 10);
  assert.equal(ageMinutes(null, 0), null);
});

test("page wiring: one status endpoint, kit only, no hard-coded limit, every string in EN and VI", () => {
  const page = read("app/admin/overview/page.tsx");
  assert.match(page, /\/api\/admin\/overview\/status\?/);
  assert.doesNotMatch(page, /"\/api\/admin\/overview"[,)]/, "the old overview read moved to Data coverage / Tenants");
  const charts = read("app/admin/overview/_status/Signals.tsx");
  assert.match(charts, /thresholds\.error_rate_pct/);
  assert.match(charts, /thresholds\.p95_ms/);
  assert.match(charts, /thresholds\.queue_wait_min/);
  assert.doesNotMatch(charts, /\b(1\.0|5\.0|1500|3000|15|60)\s*[,}]\s*\/\/\s*limit/);
  const msgs = read("app/admin/overview/status.messages.ts");
  assert.match(msgs, /notMeasured: "Not measured yet"/);
  assert.match(msgs, /notMeasured: "Chưa đo"/);
});

test("the old overview content lives on its pages: Data coverage (pipeline, content inventory), Tenants (quotas)", () => {
  const dc = read("app/admin/data-coverage/page.tsx");
  assert.match(dc, /<ContentPipeline \/>/);
  assert.match(read("app/admin/data-coverage/ContentPipeline.tsx"), /\/api\/admin\/overview"/);
  const tn = read("app/admin/tenants/page.tsx");
  assert.match(tn, /<TenantActivity \/>/);
  assert.match(read("app/admin/tenants/TenantActivity.tsx"), /quota_outliers/);
});

test("verdict headline: green only when ok and nothing missing; partial and unknown are neutral", () => {
  assert.deepEqual(verdictHeadline({ status: "ok", unknown: [] }), { key: "allNormal", tone: "success" });
  assert.deepEqual(verdictHeadline({ status: "partial", unknown: ["signals"] }), { key: "verdictPartial", tone: "neutral" });
  assert.deepEqual(verdictHeadline({ status: "ok", unknown: ["runtimes"] }), { key: "verdictPartial", tone: "neutral" });
  assert.deepEqual(verdictHeadline({ status: "unknown", unknown: ["signals", "runtimes"] }),
    { key: "verdictUnknown", tone: "neutral" });
  assert.deepEqual(verdictHeadline({ status: "attention", unknown: ["ai"] }), { key: "needYou", tone: "danger" });
});

test("view writes start from the URL as it is now: two quick chip clicks both count (S289 F2)", () => {
  // The page writes nextViewQuery(location.search, change) synchronously; a change built from the view of the last
  // render (the old bug) dropped the first of two quick clicks.
  let url = "range=6h&other=keep";
  const click = (app) => { url = nextViewQuery(url, (v) => ({ apps: flipChip(v.apps, app) })); };
  click("cis");
  click("booking"); // no re-render in between
  assert.equal(new URLSearchParams(url).get("apps"), "cis,booking");
  click("cis");
  assert.equal(new URLSearchParams(url).get("apps"), "booking");
  assert.equal(new URLSearchParams(url).get("range"), "6h");
  assert.equal(new URLSearchParams(url).get("other"), "keep", "keys the page does not own are kept");
  // legends and sorts of different tables accumulate the same way
  url = nextViewQuery(url, (v) => ({ hide: { ...v.hide, latency: ["tripplanner"] } }));
  url = nextViewQuery(url, (v) => ({ hide: { ...v.hide, errors: ["cis"] } }));
  assert.equal(new URLSearchParams(url).get("hide"), "latency:tripplanner,errors:cis");
  url = nextViewQuery(url, (v) => ({ sort: withSort(v.sort, "svc", "uptime:desc") }));
  url = nextViewQuery(url, (v) => ({ sort: withSort(v.sort, "ev", "t:asc") }));
  assert.equal(new URLSearchParams(url).get("sort"), "svc:uptime:desc,ev:t:asc");
  // a plain object change still works; a default value removes its key
  assert.equal(nextViewQuery("range=6h&page=3", { range: "24h" }), "");
  assert.equal(resetViewQuery("range=6h&apps=cis&other=keep"), "other=keep");
  assert.deepEqual(flipChip(["a"], "b"), ["a", "b"]);
  assert.deepEqual(flipChip(["a", "b"], "a"), ["b"]);
});

test("headroom per series against its own limit, the tightest one wins (S289 F1)", () => {
  const api = { warn: 1500, crit: 3000 };
  const lambda = { warn: 6000 };
  const series = [
    { id: "cis", label: "CIS", values: [300, 409, null] },
    { id: "tripplanner", label: "TripPlanner", values: [2167, 900] },
    { id: "booking", label: "Booking", values: [120, 200] },
  ];
  const limitFor = (id) => (id === "tripplanner" ? lambda : api);
  const h = tightestHeadroom(series, limitFor);
  assert.equal(h.id, "cis");
  assert.equal(h.worst, 409);
  assert.equal(h.latest, 409);
  assert.equal(h.room, 1091, "never TripPlanner's 2.17 s against the 1.5 s API limit (-668 ms)");
  // one limit for all series: the old reading, kept when every series shares the limit
  assert.equal(tightestHeadroom(series, () => api).room, -667);
  // a series without a limit is left out; no limit at all -> null (requests per minute)
  assert.equal(tightestHeadroom(series, (id) => (id === "tripplanner" ? null : api)).id, "cis");
  assert.equal(tightestHeadroom(series, () => null), null);
  assert.equal(tightestHeadroom([{ id: "x", label: "X", values: [null] }], () => api), null);
  // a floor: the lowest value counts
  assert.equal(tightestHeadroom([{ id: "f", label: "F", values: [40, 25] }], () => ({ warn: 20, dir: "below" })).room, 5);
});

// ── AA-1141 (P1) ───────────────────────────────────────────────────────────────────────────────────────────────────
import {
  filterChanges, fmtDays, isStale, muteProblem, reasonOk, refreshPause, savedViewQuery, shownAnnotations,
  snoozeOptions, snoozeRequest, snoozeUntil, viewQueryOf, zoomRange,
} from "./opsStatus.ts";

test("AA-1141 view keys: hidden annotation types and the What changed filter live in the URL", () => {
  const v = parseView(getter("ann=setting,bogus,route&chg=deploy"));
  assert.deepEqual(v.ann, ["setting", "route"]);
  assert.deepEqual(v.chg, ["deploy"]);
  assert.equal(nextViewQuery("", { ann: ["wave"] }), "ann=wave");
  assert.ok("ann" in RESET_VIEW && "chg" in RESET_VIEW);
});

test("AA-1141 annotations and changes follow the app filter; types hide and filter", () => {
  const rows = [{ id: "a", t: "2026-11-25T01:00:00Z", type: "deploy", app: "cis", text: "x" },
    { id: "b", t: "2026-11-25T02:00:00Z", type: "setting", app: "booking", text: "y" },
    { id: "c", t: "2026-11-25T03:00:00Z", type: "route", app: "shared", text: "z" }];
  assert.deepEqual(shownAnnotations(rows, { apps: ["cis"], ann: [] }).map((r) => r.id), ["a", "c"]);
  assert.deepEqual(shownAnnotations(rows, { apps: [], ann: ["deploy"] }).map((r) => r.id), ["b", "c"]);
  const changes = rows.map((r) => ({ ...r, title: r.text, t2: null, by: null, ref: null, href: null }));
  assert.deepEqual(filterChanges(changes, { apps: [], chg: ["setting"] }).map((r) => r.id), ["b"]);
  assert.deepEqual(filterChanges(changes, { apps: ["booking"], chg: [] }).map((r) => r.id), ["b", "c"]);
});

test("AA-1141 snooze ends 1 h, 4 h or at the morning hour of the next day on the viewer's clock", () => {
  const now = new Date(2026, 10, 25, 22, 30).getTime();   // local 22:30
  assert.equal(Date.parse(snoozeUntil("1h", now, 8)) - now, 3_600_000);
  assert.equal(Date.parse(snoozeUntil("4h", now, 8)) - now, 4 * 3_600_000);
  const t = new Date(snoozeUntil("tomorrow", now, 8));
  assert.deepEqual([t.getDate(), t.getHours(), t.getMinutes()], [26, 8, 0]);
});

test("AA-1141 the snooze picker follows the setting; a length goes to the API as hours, tomorrow as an end time", () => {
  assert.deepEqual(snoozeOptions([4, 1, 4, 12]), ["1h", "4h", "12h", "tomorrow"]);
  assert.deepEqual(snoozeOptions([]), ["tomorrow"]);
  assert.deepEqual(snoozeOptions(undefined), ["tomorrow"]);
  const now = new Date(2026, 10, 25, 22, 30).getTime();
  assert.equal(Date.parse(snoozeUntil("12h", now, 8)) - now, 12 * 3_600_000);
  assert.deepEqual(snoozeRequest("12h", now, 8), { snooze_hours: 12 });
  const r = snoozeRequest("tomorrow", now, 8);
  assert.ok("until" in r && new Date(r.until).getHours() === 8);
});

test("AA-1141 mute end time and reason rules", () => {
  const now = Date.parse("2026-11-25T00:00:00Z");
  assert.equal(muteProblem("", now, 7), "missing");
  assert.equal(muteProblem(isoToLocal("2026-11-24T00:00:00Z"), now, 7), "past");
  assert.equal(muteProblem(isoToLocal("2026-12-03T00:00:00Z"), now, 7), "tooFar");
  assert.equal(muteProblem(isoToLocal("2026-11-27T00:00:00Z"), now, 7), null);
  assert.equal(reasonOk("   short   ", 20), false);
  assert.equal(reasonOk("an invented reason, long enough", 20), true);
});

test("AA-1141 a section is stale after factor x its refresh time; unknown is never judged", () => {
  const now = Date.parse("2026-11-25T01:00:00Z");
  assert.equal(isStale("2026-11-25T00:40:00Z", 300, 3, now), true);    // 20 min > 15 min
  assert.equal(isStale("2026-11-25T00:50:00Z", 300, 3, now), false);
  assert.equal(isStale(null, 300, 3, now), false);
  assert.equal(isStale("2026-11-25T00:00:00Z", 300, null, now), false);
});

test("AA-1141 auto refresh pauses on a custom range, a hidden tab or by hand", () => {
  assert.equal(refreshPause({ range: "24h" }, false, false), null);
  assert.equal(refreshPause({ range: "24h" }, true, false), "hidden");
  assert.equal(refreshPause({ range: "24h" }, false, true), "paused");
  assert.equal(refreshPause({ range: "custom" }, true, true), "custom");
});

test("AA-1141 saved views carry only the page's keys and restore them", () => {
  const q = viewQueryOf("range=6h&apps=cis&other=keep&ann=wave");
  assert.deepEqual(q, { range: "6h", apps: "cis", ann: "wave" });
  assert.equal(savedViewQuery("range=1h&sev=sev1&other=keep", { range: "7d", bogus: "x" }), "range=7d&other=keep");
});

test("AA-1141 a drag on a chart becomes a custom range of at least 10 minutes", () => {
  const a = Date.parse("2026-11-25T01:00:00Z");
  assert.deepEqual(zoomRange(a + 3_600_000, a), { from: "2026-11-25T01:00:00.000Z", to: "2026-11-25T02:00:00.000Z" });
  assert.equal(zoomRange(a, a + 60_000), null);
  assert.equal(zoomRange(null, a), null);
  assert.equal(fmtDays(4.25), "4.3");
  assert.equal(fmtDays(42.4), "42");
  assert.equal(fmtDays(null), "—");
});

test("AA-1141 the page reads limits from the API and says levels in words, not colour alone", () => {
  const ui = read("app/admin/overview/_status/ui.tsx");
  assert.match(ui, /export function LevelGlyph/);
  for (const f of ["Panels.tsx", "Apps.tsx", "Services.tsx", "Capacity.tsx"]) {
    assert.match(read(`app/admin/overview/_status/${f}`), /LevelGlyph/, f);
  }
  assert.match(read("app/admin/overview/_status/Verdict.tsx"), /aria-live="polite"/);
  assert.match(read("app/admin/overview/_status/Ack.tsx"), /rules\.reason_min_chars/);
  assert.doesNotMatch(read("app/admin/overview/_status/Ack.tsx"), /reason_min_chars\s*[:=]\s*\d/);
});
