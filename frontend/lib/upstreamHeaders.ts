// frontend/lib/upstreamHeaders.ts — AA-697: the response headers the BFF proxies (lib/proxyResponse.ts) send.
//
// Content-Type is forwarded as is (the proxies never forward anything else from upstream). An SVG (the docs reader's
// diagrams, GET /admin/docs/asset) is a document when someone opens it on its own instead of through <img>, so it
// also gets a sandbox CSP and nosniff: even an SVG that slipped past the backend safety check could not run script or
// fetch anything on this origin. The backend sends the same two headers; the proxy re-adds them because it forwards
// Content-Type only. Pure (no Next import) so node --test can load it.

export const SVG_HEADERS: Readonly<Record<string, string>> = {
  "Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'; sandbox; frame-ancestors 'none'",
  "X-Content-Type-Options": "nosniff",
};

export function upstreamHeaders(contentType: string | null): Record<string, string> {
  const ct = contentType ?? "application/json";
  const headers: Record<string, string> = { "Content-Type": ct };
  if (ct.trim().toLowerCase().startsWith("image/svg+xml")) Object.assign(headers, SVG_HEADERS);
  return headers;
}
