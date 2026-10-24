// portal/app/terms/page.tsx
// AA-886 — /terms: DRAFT partner terms (was the AA-885 "being finalised" placeholder). Product facts
// only (what the platform does, who publishes, the AI notice, suspension); commercial and legal terms are
// left [to be confirmed] for counsel. AA-820: §3 is the plain-English summary of the AI Schedule
// (.2–.6, root docs/legal/tenant-terms-ai-clause-draft.md v0.2); the full Schedule is attached to the
// partner agreement. Still a draft pending legal review. Not indexed while it is a draft.
import type { Metadata } from "next";
import LegalPage, { A, LegalSection } from "../_legal/LegalPage";
import { SUPPORT_EMAIL } from "../_brand/tokens";
import { PRIVACY_PATH } from "@/lib/hosts";

export const metadata: Metadata = {
  title: "Partner terms (draft) — Adventure Asia Partner Portal",
  robots: { index: false, follow: true },
};

export default function TermsPage() {
  return (
    <LegalPage
      title="Partner terms"
      intro={
        <p style={{ margin: 0 }}>
          These terms apply to partner businesses that use the Adventure Asia Partner Portal and its API. They will form
          part of, or sit next to, the partner agreement you sign with Adventure Asia [relationship to be confirmed].
        </p>
      }
    >
      <LegalSection id="service" title="1. The service">
        <p>
          The portal gives you Adventure Asia catalog tours rewritten in your brand voice, social and blog pieces about
          those tours, and tools to export them or publish them to your own channels. The operating legal entity is
          [to be confirmed].
        </p>
      </LegalSection>

      <LegalSection id="account" title="2. Your account and API key">
        <ul>
          <li>Keep your API key secret. Activity under your key is treated as yours.</li>
          <li>
            Tell us at once at <A href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</A> if you think the key is known to
            someone else, so we can revoke it.
          </li>
        </ul>
      </LegalSection>

      <LegalSection id="ai" title="3. AI-generated content and acceptable use">
        <ul>
          <li>
            Content is generated with AI and can contain mistakes. The portal shows this at review, publish and export:
            &ldquo;This content was generated with AI. Review it before publishing; you are responsible for what you
            publish.&rdquo;
          </li>
          <li>
            The content is written with models from third-party providers, including Anthropic&apos;s Claude through
            Amazon Web Services. You must follow their usage policies, including the{" "}
            <A href="https://www.anthropic.com/legal/aup">Anthropic Usage Policy</A> and the{" "}
            <A href="https://aws.amazon.com/aup/">AWS Acceptable Use Policy</A>, as well as these terms.
          </li>
          <li>
            You must not use the portal or its content for:
            <ul>
              <li>
                fake reviews, ratings or testimonials, or quotes and stories presented as coming from customers who
                did not give them;
              </li>
              <li>invented personas, sockpuppet accounts, or content attributed to a real person who did not write or approve it;</li>
              <li>astroturfing, or networks of websites or accounts that look independent but are not;</li>
              <li>hiding sponsorship or a commercial relationship, or removing a disclosure the law or a channel requires;</li>
              <li>seeding search engines or AI systems with content that misrepresents its origin, authorship or independence;</li>
              <li>spam, or posting meant to get around the publishing limit;</li>
              <li>
                false or misleading information, in particular about visas and entry rules, health, safety, prices or
                availability;
              </li>
              <li>
                advice on insurance, finance, visas or other high-risk matters, unless a qualified person reviews it
                before it is delivered and the person receiving it is told that AI was used;
              </li>
              <li>getting around Adventure Asia&apos;s content checks, limits or other safeguards.</li>
            </ul>
          </li>
          <li>If the law or a channel&apos;s rules require content to be labelled as AI-generated, you add that label.</li>
          <li>
            You review each piece before it is published, and you are its publisher. Telling Adventure Asia to publish a
            piece, in the portal or through the API, confirms that you have reviewed it.
          </li>
          <li>
            Adventure Asia may hold, rewrite or decline to generate or publish content that breaks these rules.
            Unpublishing and suspension are covered in sections 4 and 5.
          </li>
        </ul>
      </LegalSection>

      <LegalSection id="publishing" title="4. Publishing to your channels">
        <ul>
          <li>
            Adventure Asia publishes a piece to your channels only when you tell it to publish that piece, using the
            access you connected. The post appears under your name.
          </li>
          <li>There is a daily limit on the number of posts Adventure Asia publishes for you.</li>
          <li>
            You can unpublish your posts. Adventure Asia can also unpublish a post it published for you, for example when
            it breaks these terms.
          </li>
        </ul>
      </LegalSection>

      <LegalSection id="suspension" title="5. Suspension and ending the service">
        <p>
          Adventure Asia may suspend an account, which stops its API access and publishing at once, for example to
          protect the service or when these terms are broken. Notice periods, termination and what happens to your
          content afterwards are [to be confirmed].
        </p>
      </LegalSection>

      <LegalSection id="ownership" title="6. Content rights">
        <p>Who owns the generated content and the licence to the catalog tours are [to be confirmed].</p>
      </LegalSection>

      <LegalSection id="fees" title="7. Plans and fees">
        <p>Plans, fees and payment terms are [to be confirmed].</p>
      </LegalSection>

      <LegalSection id="availability" title="8. Availability">
        <p>
          The portal is under active development; features can change and the service may be unavailable at times.
          Service levels and support hours are [to be confirmed].
        </p>
      </LegalSection>

      <LegalSection id="liability" title="9. Liability and governing law">
        <p>Limits of liability, the governing law and the competent courts are [to be confirmed].</p>
      </LegalSection>

      <LegalSection id="contact" title="10. Contact">
        <p>
          Questions: <A href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</A>. How we handle personal data is described
          in the <A href={PRIVACY_PATH}>privacy notice</A>.
        </p>
      </LegalSection>
    </LegalPage>
  );
}
