// frontend/lib/bffPath.ts
// AA-577 follow-up — build the backend path for a BFF proxy from its catch-all segments.
//
// Next.js decodes each [...path] segment, so `/api/admin/a%2F..%2F..%2Fopenapi.json` arrives as ONE
// segment "a/../../openapi.json". Joined as-is and passed to fetch(), the URL parser resolves the dots
// and the request lands on backend `/openapi.json` with the admin secret attached, after the rbac
// path rules (which saw "a/../../openapi.json") had already let it through. The same goes for "\"
// (treated as "/" in http URLs) and for "?" / "#" (which would cut the path short).
//
// So: refuse dot, empty and slash-carrying segments, and percent-encode every other segment. The
// backend decodes the path before routing, so it sees exactly the string the rbac rules checked.
//
// Edge-safe: no Node APIs.

/** The encoded backend sub-path for `segments`, or null when a segment could escape the prefix. */
export function proxyPath(segments: readonly string[]): string | null {
  for (const s of segments) {
    if (s === "" || s === "." || s === ".." || s.includes("/") || s.includes("\\")) return null;
  }
  return segments.map(encodeURIComponent).join("/");
}
