// portal/app/api/playground/proxy/route.ts (AA-885: moved from the admin app)
import { NextRequest, NextResponse } from "next/server";
import { API_URL, requireTenant } from "@/lib/auth-server";

// Only these exact paths are reachable through the playground — must match
// endpoints-config.ts's own displayed endpoint list 1:1 (AA-582: the previous
// "/v1/tours" prefix + /^\/v1\/tours\/[\w-]+$/ regex were broader than the UI,
// letting a logged-in tenant reach /v1/tours/:id and .../full — neither ever
// shown in the Playground UI — directly via devtools/curl).
const ALLOWED_PATHS: Array<string | RegExp> = [
  "/v1/tours/my-versions",                    // list catalog
  /^\/v1\/tours\/versions\/[\w-]+$/,          // get tour detail (version)
  "/v1/webhooks",                             // webhook setup
];

function isAllowed(endpoint: string): boolean {
  return ALLOWED_PATHS.some(p =>
    typeof p === "string" ? endpoint.startsWith(p) : p.test(endpoint)
  );
}

export async function POST(req: NextRequest) {
  // Auth — reuse the same cookie-based pattern as /api/tenant/[...path]
  // AA-656: verify the tenant JWT here too (was: token presence only), like every other BFF proxy.
  const auth = await requireTenant(req);
  if (!auth.ok) return auth.response;
  const token = auth.token; // the token that was just verified

  let body: { endpoint: string; params?: Record<string, string>; method?: string };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "Invalid request body" }, { status: 400 });
  }

  const { endpoint, params, method = "GET" } = body;

  if (!endpoint || !isAllowed(endpoint)) {
    return NextResponse.json({ error: "Endpoint not in playground scope" }, { status: 403 });
  }

  // Build upstream URL — append query params for GET requests
  const url = new URL(`${API_URL}${endpoint}`);
  if (method === "GET" && params) {
    Object.entries(params).forEach(([k, v]) => {
      if (v && k !== "id") url.searchParams.set(k, v);
    });
  }

  const startTime = Date.now();
  try {
    const apiRes = await fetch(url.toString(), {
      method,
      headers: {
        "Authorization": `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      // POST/PATCH body (e.g. webhook setup)
      ...(method !== "GET" && method !== "HEAD" && params
        ? { body: JSON.stringify(params) }
        : {}),
    });

    let data: unknown;
    try { data = await apiRes.json(); } catch { data = {}; }

    return NextResponse.json({
      status: apiRes.status,
      statusText: apiRes.statusText,
      responseTime: Date.now() - startTime,
      data,
    });
  } catch {
    return NextResponse.json({
      status: 0,
      statusText: "Network Error",
      responseTime: Date.now() - startTime,
      data: { error: "Could not reach API" },
    });
  }
}
