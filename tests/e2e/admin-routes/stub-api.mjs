// tests/e2e/admin-routes/stub-api.mjs
// S292 follow-up of the S291 hotfix (the Ecosystem status page lost its ToastProvider and only the administrator saw
// it): the CIS API stand-in of the admin route check (.github/workflows/admin-routes.yml). TEST CODE ONLY: the check
// builds its own throwaway `next build` with NEXT_PUBLIC_API_URL / API_URL pointed here; the production image and its
// sign-in, proxy.ts gate and BFF are unchanged and never talk to this file.
//
// Every request it gets is server-side: proxy.ts verifies the session cookie with POST /auth/verify-admin, and the BFF
// routes (/api/admin/*, /api/pipeline/*, /api/tenant/*...) forward the page's reads to the API. It answers:
//   POST /auth/verify-admin  -> the administrator ("admin" role, every admin capability), for ANY token (a stub: the
//                               real API checks the signature; the check sets an unsigned stand-in cookie)
//   GET  /admin/session      -> a writable session (read_only false), so the pages render their write controls too
//   GET  /health             -> 200 (the workflow waits on it)
//   anything else            -> 503 {"detail": "admin_route_check_stub"}: every page renders its "API unavailable"
//                               state, which must not crash it (the S291 crash was in a control that renders always)
// Each request is appended to the log file (argv[3]) as "<status> <METHOD> <path>" for the job summary.
// Listens on 127.0.0.1 only. Run: node tests/e2e/admin-routes/stub-api.mjs [port] [log file]

import { appendFileSync } from "node:fs";
import http from "node:http";

export const STUB_DETAIL = "admin_route_check_stub";

// The administrator's verify-admin answer (lib/auth-server.ts verifiedStaffOf reads these keys). The built-in "admin"
// role takes its capabilities from lib/rbac.ts ROLE_PERMS, so the list here is informative only.
export const ADMIN_VERIFY = {
  admin_id: "route-check-admin",
  role: "admin",
  display_name: "Route Check Admin",
  capabilities: [],
  custom_role_label: null,
};

// GET /admin/session (lib/sessionAccess.ts SessionHint): writable, so WriteOnly / useReadOnly controls render.
export const ADMIN_SESSION = { role: "admin", read_only: false, reason: null };

/** The stub's answer to one request: [status, JSON body]. Pure, so the unit test checks it without a socket. */
export function answer(method, url) {
  const path = (url || "").split("?")[0];
  if (method === "POST" && path === "/auth/verify-admin") return [200, ADMIN_VERIFY];
  if (method === "GET" && path === "/admin/session") return [200, ADMIN_SESSION];
  if (path === "/health") return [200, { status: "ok" }];
  return [503, { detail: STUB_DETAIL, message: `The admin route check answers ${path} as unavailable` }];
}

function main() {
  const port = Number(process.argv[2] || process.env.ADMIN_ROUTES_STUB_PORT || 55491);
  const log = process.argv[3] || "";
  http
    .createServer((req, res) => {
      // Drain the body (POST verify-admin, BFF writes) before answering.
      req.on("data", () => {});
      req.on("end", () => {
        const [status, body] = answer(req.method || "GET", req.url || "/");
        if (log) {
          try {
            appendFileSync(log, `${status} ${req.method} ${(req.url || "").split("?")[0]}\n`);
          } catch {
            // the log is a convenience for the summary; never fail a request on it
          }
        }
        res.writeHead(status, { "content-type": "application/json" });
        res.end(JSON.stringify(body));
      });
    })
    .listen(port, "127.0.0.1", () => process.stdout.write(`admin route check stub on http://127.0.0.1:${port}\n`));
}

if (import.meta.url === `file://${process.argv[1]}`) main();
