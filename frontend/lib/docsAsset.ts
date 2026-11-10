// frontend/lib/docsAsset.ts — AA-1064: the BFF side of GET /api/admin/docs/asset (docs reader images).
//
// The REST API Gateway in front of the CIS API has no binaryMediaTypes, so it corrupts a binary body on the way here
// (the brand-file upload broke the same way, S229). The BFF therefore reads the image as JSON (backend
// GET /admin/docs/asset?encoding=base64 -> {repo, path, type, bytes, sha256, data}) and sends the decoded bytes to the
// browser itself, with the image headers: the declared type (SVG, PNG or WebP only), a 5-minute private cache, nosniff,
// inline, and the sandbox CSP of lib/upstreamHeaders.ts (an image opened on its own can run or fetch nothing).
// The backend re-checks every image on read; this decoder still refuses a type it does not know, a size over the
// type's cap, a length or sha256 that does not match, and a PNG / WebP without its signature (502 instead).
// Pure apart from node:crypto, so node --test can load it (lib/docsAsset.test.mjs).

import { createHash } from "node:crypto";
import { SVG_HEADERS } from "./upstreamHeaders.ts";

/** The BFF path (under /api/admin/) of the docs reader's image route. */
export const DOCS_ASSET_PATH = "docs/asset";

/** Image types the docs reader serves, with the largest body the backend may send for each. */
export const DOC_IMAGE_MAX_BYTES: Readonly<Record<string, number>> = {
  "image/svg+xml": 2 * 1024 * 1024, // the backend's SVG read cap (the builder skips SVGs over 1 MB)
  "image/png": 300 * 1024, // docs/STYLE.md screenshot cap (root build_docs_reader.py rule 10)
  "image/webp": 300 * 1024,
};

const BASE64 = /^[A-Za-z0-9+/]*={0,2}$/;
const PNG_SIGNATURE = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];

/** True for the request the BFF answers with decoded image bytes instead of streaming the upstream body. */
export function isDocsAssetRead(method: string, path: string): boolean {
  return method === "GET" && path === DOCS_ASSET_PATH;
}

/** The query string sent upstream: the caller's parameters with encoding=base64 (any caller value replaced). */
export function base64Search(search: string): string {
  const params = new URLSearchParams(search);
  params.set("encoding", "base64");
  return `?${params.toString()}`;
}

/** The headers of a docs image response. */
export function imageHeaders(type: string): Record<string, string> {
  return {
    "Content-Type": type,
    "Cache-Control": "private, max-age=300",
    "Content-Disposition": "inline",
    ...SVG_HEADERS,
  };
}

function hasSignature(type: string, body: Uint8Array): boolean {
  if (type === "image/png") return PNG_SIGNATURE.every((b, i) => body[i] === b);
  if (type === "image/webp") {
    const tag = (from: number) => String.fromCharCode(...body.subarray(from, from + 4));
    return body.length >= 12 && tag(0) === "RIFF" && tag(8) === "WEBP";
  }
  return true; // SVG: text, checked by the backend
}

export type DecodedAsset = { body: Uint8Array<ArrayBuffer>; headers: Record<string, string> };

/** The image bytes and headers from the backend's base64 JSON, or null when the answer is not a usable image. */
export function decodeDocsAsset(json: unknown): DecodedAsset | null {
  if (!json || typeof json !== "object") return null;
  const { type, data, bytes, sha256 } = json as Record<string, unknown>;
  if (typeof type !== "string" || !(type in DOC_IMAGE_MAX_BYTES)) return null;
  if (typeof data !== "string" || data.length % 4 !== 0 || !BASE64.test(data)) return null;
  const body = Uint8Array.from(Buffer.from(data, "base64"));
  if (body.length > DOC_IMAGE_MAX_BYTES[type]) return null;
  if (typeof bytes === "number" && bytes !== body.length) return null;
  if (typeof sha256 === "string" && createHash("sha256").update(body).digest("hex") !== sha256) return null;
  if (!hasSignature(type, body)) return null;
  return { body, headers: imageHeaders(type) };
}
