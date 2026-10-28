// frontend/lib/proxyResponse.ts
// S241 — the BFF proxies (/api/admin, /api/tenant, /api/pipeline) hand the upstream body straight
// through as a stream instead of buffering it with `await res.arrayBuffer()` first (Vercel Fluid
// Active CPU — S240 audit; the Jobs list alone was up to ~766 MB/day of outgoing transfer).
//
// Headers: only Content-Type is forwarded, exactly as before (plus, for an SVG only, the sandbox headers of
// lib/upstreamHeaders.ts, AA-697). Node's fetch (undici) has already
// DECODED a gzip/br upstream body, so Content-Encoding and Content-Length must never be copied —
// they describe the encoded bytes and would corrupt the decoded stream. Set-Cookie was never
// forwarded by these proxies and still is not.
//
// Behaviour differences vs. the buffered version (both edge cases, documented on purpose):
//   - An upstream that fails MID-body now gives the client a truncated/aborted response with the
//     upstream status, not a clean 502 (the status line is already sent). Connection errors before
//     the response headers still go to the caller's catch → 502, unchanged.
//   - A bodyless upstream status (204/304) now passes through. Before, `new NextResponse(emptyBuf,
//     {status: 204})` threw inside the try and the client got 502. No backend route returns 204 today.
import { NextResponse } from "next/server";
import { upstreamHeaders } from "./upstreamHeaders";

export function streamUpstream(res: Response): NextResponse {
  return new NextResponse(res.body, {
    status: res.status,
    headers: upstreamHeaders(res.headers.get("content-type")),
  });
}
