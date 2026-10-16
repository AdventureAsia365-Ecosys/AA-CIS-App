// app/(tenant)/portal/_components/channelLabels.ts — the tenant-facing names of the 8 content
// Channels (the Slate tabs). Shared by the Writer panel header and the wizard's Write card, so the
// tenant never sees a raw key such as "linkedin" (AA-668). Kept out of WriterPanel.tsx because
// AngleGateWizard (which WriterPanel imports) needs it too.

const CHANNEL_LABEL: Record<string, string> = {
  blog: "Blog", linkedin: "LinkedIn", facebook: "Facebook", instagram: "Instagram",
  tiktok: "TikTok", email: "Email", landing_page: "Landing Page", ads: "Ads",
};

/** "linkedin" → "LinkedIn"; an unknown key reads as words ("new_channel" → "New channel"). */
export function channelName(channel: string): string {
  return CHANNEL_LABEL[channel] ?? humanizeKey(channel);
}

/** A snake_case key as words, for a key the UI has no label for yet. */
export function humanizeKey(key: string): string {
  const words = key.replace(/_/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : key;
}
