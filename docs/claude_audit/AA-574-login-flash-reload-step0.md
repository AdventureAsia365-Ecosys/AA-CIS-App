# AA-574 STEP0 — Login screen flash-then-reload-to-blank investigation

Status: NOT reproduced. Root cause NOT in this repo's code, with a specific alternative cause identified from the report's own evidence. Not set Done (per issue instruction).

## Reported symptom (distinct from AA-573)

On one specific Chrome profile only: click Login → screen flashes once → back at
`/login` with a blank form. No "Connecting..." shown. No red console errors. No
`POST /api/auth/login` in Network — only a stale `GET /login` (204) and several
unexplained requests to `gc.kis.v2.scr.kaspersky-labs.com`
(`longpooling`/`wsm.session...`/`abn.statInfo...`) despite Nghiệp confirming no
Kaspersky extension is knowingly installed on that profile.

## STEP0: code read (`frontend/app/login/page.tsx`, current — includes AA-573/PR #354)

**Finding #1 — the issue's hypothesis #1 doesn't apply to this file.** There is
no `<form>` element anywhere in `login/page.tsx`. The whole card is a plain
`<div>`; the two `<input>`s and the `<button onClick={login}>` are not wrapped
in a `<form>`, and neither `<input>` has a `form=` attribute.

Per the HTML spec, pressing Enter in a text field only triggers implicit
submission if the field has a form owner. With no `<form>` in the DOM, there is
**no native full-page-reload code path available at all** — not "usually
prevented by `preventDefault()`", but structurally absent. So hypothesis #1
("JS chưa kịp gắn preventDefault, rơi về native submit") cannot be the
mechanism here, independent of timing/hydration — confirmed by reading the
code, not by reproduction. (`onKeyDown={e => e.key === "Enter" && !loading &&
login()}` on each input is the only Enter-handling; it calls `login()`
directly, no form event involved.)

**Finding #2 — checked for other reload triggers, found none in this repo:**
- No service worker registration anywhere (`grep -r serviceWorker app lib` — empty).
- Only `window.location` write in the whole login flow is the intentional
  `window.location.href = ...` on a *successful* login (line 64), which requires
  a 2xx response from `/api/auth/login` first — inconsistent with "no POST seen
  in Network".
- `app/layout.tsx`'s inline `<script>` only touches `localStorage`/theme
  attribute, synchronous, no navigation.
- `middleware.ts`: `/login` is in `PUBLIC_PATHS` (line 49) — middleware
  `return`s immediately for any `/login` request, before any of the
  role/JWT-verification branches run. It cannot be looping/redirecting a
  request that targets `/login` itself.

## Repro attempt (per issue's ask #2)

Ran the app locally (`next dev`) and drove it with real Playwright/Chromium,
throttling via CDP (`Emulation.setCPUThrottlingRate`, `Network.emulateNetworkConditions`
at a rough "slow 3G" profile: 6.4 KB/s down, 800ms latency) — script:
`/tmp/.../scratchpad/repro_aa574.js` (not committed, scratch only). Four
scenarios: normal click, 10x CPU throttle + fast click, slow network + fast
Enter-press, and CPU+network throttle + double-click.

Results:
- Normal / CPU-throttle-only click: `POST /api/auth/login` fires correctly
  (401 on the fake test creds, error shown inline, form retained its values,
  page stayed on `/login` with **no navigation** and **no reload**) — this is
  the expected, already-correct error path.
- Slow-network scenarios (Enter press / double-click issued immediately after
  `domcontentloaded`, before the JS bundle could realistically finish
  downloading at 6.4 KB/s): **no request fired at all, no reload, no error —
  the click/Enter was silently swallowed.** `page.fill()` still worked (it
  writes the DOM value directly), so the inputs looked populated, but nothing
  happened on interaction because React hadn't hydrated yet — there's no
  attached handler AND (per Finding #1) no `<form>` to fall back to natively.

So the one race the ticket asked to check for is real (pre-hydration click is
a genuine no-op under a bad-enough connection) — but its actual symptom is
**silence**, not **flash + reload + blank form**. It doesn't match what's
reported. No scenario produced a reload of any kind. Not reproduced.

## Assessment

Every mechanism inside this codebase that could cause a full-page reload from
`/login` has been read or exercised and ruled out (no `<form>`, no service
worker, no matching `window.location` write, middleware fully bypassed on
`/login`, pre-hydration click races to silence not reload). The reported
behavior — silent interception before any app code runs (no console error, no
network request even attempted) plus unexplained calls to a
`kaspersky-labs.com` telemetry/anti-phishing domain on that one browser
profile — is a textbook signature of a **third-party browser
extension/security-software content script intercepting the click on a
detected login form**, not an app bug. (Several Kaspersky consumer products —
antivirus, VPN, Password Manager — silently install a companion "Kaspersky
Protection" Chrome extension that a user may not associate with "Kaspersky" by
name; its anti-phishing/Safe-Money features are known to hook password-field
submissions.) This is consistent with every observed detail: profile-specific,
zero console errors (isolated extension world), zero POST (intercepted before
`fetch` runs), and the flash+reload (extension-driven navigation).

**Not proven** — this is the most likely explanation given the evidence, not a
confirmed root cause; nobody has captured the actual moment it happens with
diagnostics on. Recommend checking `chrome://extensions` on the affected
profile (not just "did I install Kaspersky" — look for anything from Kaspersky
Lab / any VPN or "protection" extension) as the next concrete step, next time
it's reproducible.

## Proposed temporary logging (not yet added — proposing per issue's ask #3)

To catch this if/when it recurs, without touching the login flow's real logic:
add a `beforeunload`/`pagehide` listener in `login/page.tsx` for the duration
of this investigation that `console.warn`s (and optionally `navigator.sendBeacon`s
to a throwaway logging endpoint) whatever state is available at the moment of
unload — e.g. whether `login()` had been entered, `loading` state, and
`document.activeElement`. A `beforeunload` handler fires even for
extension-triggered navigations, which is exactly the gap the current evidence
can't cross (nothing today runs at the moment of the reload). Left unimplemented
pending Nghiệp's go-ahead, since it needs a small code change and its own
verify pass before shipping to the affected profile.

## Should know
- No code changed by this investigation. No PR opened.
- AA-573 (Done) fixed a different bug (stuck on "Connecting..." forever); this
  file is AA-574 only, do not conflate the two.
- Issue explicitly says not to auto-set Done — left as-is in Linear (Backlog).
