"use client";
// app/kit-gallery/GalleryView.tsx — the client part of the gated kit gallery route (page.tsx): the admin language
// provider (the gallery has EN and VI demo text), the kit PageFrame and the gallery. `plant` is the visual job's
// self-test: a strip 2400px wide in the document ("overflow") or inside the frame ("frame").
import { I18nProvider, KitGallery, PageFrame } from "../_kit";
import { langStore } from "../_kit/i18n";

const PLANTED = { width: 2400, height: 8 } as const;

export function GalleryView({ plant }: { plant: "overflow" | "frame" | null }) {
  return (
    <I18nProvider store={langStore}>
      <PageFrame width="wide" title="UI kit gallery" description="Every kit core component in its states (static demo data).">
        <KitGallery />
        {plant === "frame" && <div data-testid="planted-overflow" style={PLANTED} aria-hidden="true" />}
      </PageFrame>
      {plant === "overflow" && <div data-testid="planted-overflow" style={PLANTED} aria-hidden="true" />}
    </I18nProvider>
  );
}
