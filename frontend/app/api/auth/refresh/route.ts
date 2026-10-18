// frontend/app/api/auth/refresh/route.ts
// AA-816 (C4) — the one place a Cognito staff session is refreshed (refresh-token rotation is on,
// so refreshing from every proxy would race; plan risk 8).
//   GET  ?next=/admin/...  middleware.ts sends a page navigation here when the access token is
//                          expired; rotates the tokens and redirects back (or to /login).
//   POST                   the admin shell's keep-alive; returns { mode, expiresAt } as JSON.
import type { NextRequest } from "next/server";
import { handleRefreshKeepAlive, handleRefreshNavigation } from "@/lib/cognitoRoutes";

export function GET(request: NextRequest) {
  return handleRefreshNavigation(request);
}

export function POST(request: NextRequest) {
  return handleRefreshKeepAlive(request);
}
