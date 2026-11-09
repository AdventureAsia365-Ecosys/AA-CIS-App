// app/kit-gallery/page.tsx
// AA-1008 (S1d, root ADR 0016 decision 9) — the gated kit gallery route of the visual job: every kit core component in
// its states (core/KitGallery.tsx) on a public page, so the per-PR visual run (`next build && next start` in the
// runner, .github/workflows/visual.yml) can capture it without a session or a backend. It exists only in a build made
// with KIT_GALLERY=1; every deployed build answers 404 (notFound) here.
// `?plant=overflow` (gallery builds only) adds an element wider than the viewport to the document, `?plant=frame` one
// inside the page frame (the frame's own scroll region scrolls sideways): the visual job's self-test opens both and
// must fail on each, which proves both overflow assertions work.
import { notFound } from "next/navigation";
import { GalleryView } from "./GalleryView";

export const dynamic = "force-dynamic";

export default async function KitGalleryPage({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  if (process.env.KIT_GALLERY !== "1") notFound();
  const { plant } = await searchParams;
  return <GalleryView plant={plant === "overflow" || plant === "frame" ? plant : null} />;
}
