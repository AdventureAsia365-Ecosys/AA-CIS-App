// app/trip/[tourId]/page.tsx — AA-482, ADR-2026-030 D5 ("AA hosts the white-label tour page").
//
// Public, no-auth. Real per-tour landing page — the trip_url gate F6 (services/acp_produce/
// gates.py) checks acp_deliver.tenant_tour_pages.url_alive for. Server component: fetches
// directly from the backend's new public GET /v1/trip/{tourId} (api/routers/v1_trip_page.py),
// no proxy route needed since there's no secret/auth to attach for public data.
//
// v1 scope (see api/routers/v1_trip_page.py's own module docstring for the full architecture
// decision record): serves aa_internal's admin-canonical published tours — the shared catalog
// ADR-2026-030's own v_trip_registry view was designed around. Real per-tenant rewritten
// versions are a separate, real follow-up, not built here.
import type { Metadata } from "next";
import { notFound, permanentRedirect } from "next/navigation";

const API_URL = process.env.API_URL ?? "https://api-cis.lumiguides.it.com";

const T = {
  gold: "#DB9628", ink: "#1F2933", ink2: "#2A333E", ink3: "#3A4453",
  paper: "#FBF9F6", line: "#E7E1D6", muted: "#6B7280",
};
const serif = "'Georgia', 'Iowan Old Style', serif";
const sans = "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";

interface TripPageData {
  tour_id: string;
  country: string | null;
  duration: string | null;
  name: string | null;
  subtitle: string | null;
  summary: string | null;
  itineraries: string | null;
  highlights: string[];
  seo_title: string | null;
  seo_meta: string | null;
}

// AA-986 L4 (root ADR 0015 Q5): the API answers 301 for an older version of a tour (same catalog key) and 410 for a
// tour Adventure Asia no longer offers. The redirect is followed here (permanentRedirect: the browser and search
// engines move to the current version's URL) instead of fetch silently rendering the new tour under the old URL.
// A 410 renders the not-found page (Next.js has no 410 from a page); search engines drop it from the sitemap side.
const UUID_RE = /\/v1\/trip\/([0-9a-fA-F-]{36})$/;

async function fetchTrip(tourId: string): Promise<TripPageData | { movedTo: string } | null> {
  try {
    const res = await fetch(`${API_URL}/v1/trip/${tourId}`, {
      // Real per-tour content changes rarely — cache at the edge, revalidate hourly.
      next: { revalidate: 3600 },
      redirect: "manual",
    });
    if (res.status === 301 || res.status === 308) {
      const m = (res.headers.get("location") ?? "").match(UUID_RE);
      return m ? { movedTo: m[1] } : null;
    }
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

export async function generateMetadata(
  { params }: { params: Promise<{ tourId: string }> },
): Promise<Metadata> {
  const { tourId } = await params;
  const trip = await fetchTrip(tourId);
  if (!trip || "movedTo" in trip) return { title: "Trip Not Found — Adventure Asia" };
  return {
    title: trip.seo_title || trip.name || "Adventure Asia",
    description: trip.seo_meta || trip.summary || undefined,
  };
}

function renderItineraryParagraphs(text: string) {
  return text.split(/\n{2,}/).filter(Boolean).map((block, i) => (
    <p key={i} style={{ margin: "0 0 18px", lineHeight: 1.75, color: T.ink2, fontSize: 15.5 }}>
      {block}
    </p>
  ));
}

export default async function TripPage({ params }: { params: Promise<{ tourId: string }> }) {
  const { tourId } = await params;
  const found = await fetchTrip(tourId);
  if (!found) notFound();
  if ("movedTo" in found) permanentRedirect(`/trip/${found.movedTo}`);
  const trip = found;

  return (
    <main style={{ background: T.paper, minHeight: "100vh", color: T.ink }}>
      <header style={{
        borderBottom: `1px solid ${T.line}`, padding: "20px 24px",
        display: "flex", justifyContent: "space-between", alignItems: "center",
      }}>
        <span style={{ fontFamily: serif, fontWeight: 700, fontSize: 18, letterSpacing: "0.02em" }}>
          Adventure Asia
        </span>
        <span style={{ fontFamily: sans, fontSize: 11, textTransform: "uppercase",
          letterSpacing: "0.14em", color: T.gold, fontWeight: 700 }}>
          Discreet Executive Adventure
        </span>
      </header>

      <section style={{ maxWidth: 760, margin: "0 auto", padding: "56px 24px 80px" }}>
        {trip.country && (
          <div style={{ fontFamily: sans, fontSize: 12, textTransform: "uppercase",
            letterSpacing: "0.14em", color: T.muted, marginBottom: 14 }}>
            {trip.country}{trip.duration ? ` · ${trip.duration}` : ""}
          </div>
        )}
        <h1 style={{ fontFamily: serif, fontSize: 38, lineHeight: 1.2, margin: "0 0 12px",
          fontWeight: 700 }}>
          {trip.name}
        </h1>
        {trip.subtitle && (
          <p style={{ fontFamily: serif, fontSize: 18, color: T.ink3, fontStyle: "italic",
            margin: "0 0 32px", lineHeight: 1.5 }}>
            {trip.subtitle}
          </p>
        )}
        {trip.summary && (
          <p style={{ fontSize: 16.5, lineHeight: 1.8, color: T.ink2, margin: "0 0 36px" }}>
            {trip.summary}
          </p>
        )}

        {trip.highlights?.length > 0 && (
          <div style={{ margin: "0 0 40px", padding: "24px 28px", background: "#fff",
            border: `1px solid ${T.line}`, borderRadius: 6 }}>
            <div style={{ fontFamily: sans, fontSize: 11, textTransform: "uppercase",
              letterSpacing: "0.12em", color: T.gold, fontWeight: 700, marginBottom: 14 }}>
              Journey Highlights
            </div>
            <ul style={{ margin: 0, padding: 0, listStyle: "none" }}>
              {trip.highlights.map((h, i) => (
                <li key={i} style={{ display: "flex", gap: 10, marginBottom: 10,
                  fontSize: 15, lineHeight: 1.6, color: T.ink2 }}>
                  <span style={{ color: T.gold, flexShrink: 0 }}>—</span>
                  <span>{h}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {trip.itineraries && (
          <div style={{ marginBottom: 40 }}>
            <div style={{ fontFamily: sans, fontSize: 11, textTransform: "uppercase",
              letterSpacing: "0.12em", color: T.gold, fontWeight: 700, marginBottom: 18 }}>
              The Itinerary
            </div>
            {renderItineraryParagraphs(trip.itineraries)}
          </div>
        )}

        {/* No price shown — ADR-2026-030 §8.4: never guess/display price without a real
            source; this v1 has none wired in yet, so it's omitted rather than fabricated. */}
        <div style={{ textAlign: "center", padding: "36px 0 0", borderTop: `1px solid ${T.line}` }}>
          <a
            href={`mailto:admin@adventureasia.com?subject=${encodeURIComponent(
              `Design This Journey — ${trip.name ?? trip.tour_id}`,
            )}`}
            style={{
              display: "inline-block", background: T.gold, color: "#fff",
              fontFamily: sans, fontWeight: 700, fontSize: 14, letterSpacing: "0.02em",
              padding: "14px 32px", borderRadius: 4, textDecoration: "none",
            }}
          >
            Design This Journey
          </a>
        </div>
      </section>
    </main>
  );
}
