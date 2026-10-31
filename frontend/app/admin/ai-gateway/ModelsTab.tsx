"use client";
// app/admin/ai-gateway/ModelsTab.tsx — AA-804 UI part 2 / AA-1017: the Models & trials tab of AI providers.
//
// Two kit Sections: the model catalog (CatalogTable: prices view-only, on/off only) and the shadow comparisons
// (ShadowReport: primary vs shadow on real calls). Both moved here from Settings › LLM Models. C4 (AA-900 UI): the
// read-only Experiments section (ExperimentsSection: prompt versions, evaluation runs). The trial ladder of AA-900
// (quick test, judge check, compare) joins this tab when it ships.

import { K, Section, useT } from "../../_kit";
import { CatalogTable } from "./CatalogTable";
import { ExperimentsSection } from "./ExperimentsSection";
import { MODELS } from "./models.messages";
import { ShadowReport } from "./ShadowReport";

export default function ModelsTab() {
  const t = useT(MODELS);
  return (
    <div data-testid="ai-gateway-models-ready"
      style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 16 }}>
      <p style={{ fontSize: 12.5, color: K.muted, margin: 0 }}>{t("modelsIntro")}</p>
      <Section title={t("catalogTitle")} description={<>{t("catalogIntro")} {t("pricesReadOnly")}</>}>
        <CatalogTable />
      </Section>
      <Section title={t("shadowTitle")} description={t("shadowIntro")}>
        <ShadowReport />
      </Section>
      <ExperimentsSection />
    </div>
  );
}
