"use client";
// app/admin/_components/SettingsKitTab.tsx
// AA-662 — the UI-kit "living reference" rendered as a tab inside admin Settings (not a standalone
// nav page). Shows every kit primitive with static data so the design system can be reviewed in
// the running admin shell without hitting the backend; one section proves the app-wide
// QueryClientProvider via a fake async query. AA-994 S4a: the "Gallery" tab renders the shared kit gallery
// (core/KitGallery.tsx: Section, KpiTile, FilterBar, RowMenu, ConfirmModal, Money, DateText), the same in every app.

import { useQuery } from "@tanstack/react-query";
import { Trash2, Wand2 } from "lucide-react";
import { useMemo, useState } from "react";
import {
  Badge,
  ConfirmModal,
  type ColumnDef,
  DataTable,
  Drawer,
  EmptyState,
  ErrorState,
  GuidePanel,
  InfoTip,
  KitGallery,
  Modal,
  SkeletonBar,
  SkeletonStyle,
  StatusBadge,
  Tabs,
  TONE,
  type Tone,
  ToastProvider,
  useLocalized,
  useT,
  useToast,
  type Translator,
  Eyebrow,
  K,
  Section as KitSection,
  Button,
  sans,
} from "../../_kit";
import { M } from "./SettingsKitTab.messages";
import { DEMO_GUIDE } from "./kitDemoGuide";
import { DEMO_GUIDE_VI } from "./kitDemoGuide.vi";

type DemoTour = {
  id: string;
  name: string;
  country: string;
  status: string;
  quality: number;
  atoms: number;
};

const DEMO_ROWS: DemoTour[] = [
  { id: "t-001", name: "Nakasendo Way: Kiso Valley", country: "Japan", status: "completed", quality: 7.9, atoms: 24 },
  { id: "t-002", name: "Shoguns & Shrines of Kyoto", country: "Japan", status: "running", quality: 0, atoms: 0 },
  { id: "t-003", name: "Taj & Rajasthan Forts", country: "India", status: "held", quality: 6.8, atoms: 18 },
  { id: "t-004", name: "Highlands of Sapa", country: "Vietnam", status: "failed", quality: 0, atoms: 0 },
  { id: "t-005", name: "Angkor at Dawn", country: "Cambodia", status: "completed", quality: 8.2, atoms: 31 },
  { id: "t-006", name: "Gobi Desert Crossing", country: "Mongolia", status: "queued", quality: 0, atoms: 0 },
  { id: "t-007", name: "Bhutan Druk Path Trek", country: "Bhutan", status: "approved", quality: 8.0, atoms: 22 },
  { id: "t-008", name: "Great Wall Wild Sections", country: "China", status: "regenerating", quality: 0, atoms: 0 },
];

function demoColumns(t: Translator<typeof M.en>): ColumnDef<DemoTour, unknown>[] {
  return [
    { accessorKey: "name", header: t("colTour"), cell: (c) => <strong>{c.getValue<string>()}</strong> },
    { accessorKey: "country", header: t("colCountry") },
    { accessorKey: "status", header: t("colStatus"), cell: (c) => <StatusBadge status={c.getValue<string>()} /> },
    {
      accessorKey: "quality",
      header: t("colQuality"),
      cell: (c) => {
        const q = c.getValue<number>();
        return q > 0 ? q.toFixed(1) : "—";
      },
    },
    { accessorKey: "atoms", header: t("colAtoms") },
  ];
}

// AA-919 — a static guide showing every GuideContent block; real guides live next to their page (guide.ts).
function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <KitSection style={{ marginBottom: 20 }}>
      <Eyebrow>{title}</Eyebrow>
      <div style={{ marginTop: 10 }}>{children}</div>
    </KitSection>
  );
}

function fakeFetchCount(): Promise<number> {
  return new Promise((resolve) => setTimeout(() => resolve(DEMO_ROWS.length), 600));
}

function ReactQueryDemo() {
  const t = useT(M);
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["kit-demo-count"],
    queryFn: fakeFetchCount,
  });
  if (isLoading) return <SkeletonBar w={120} h={16} />;
  if (isError) return <ErrorState message={t("demoQueryFailed")} onRetry={() => refetch()} />;
  return (
    <span style={{ fontSize: 13, color: K.ink }}>
      {t("rqResolved")}<strong>{data}</strong>{t("rqRows")}
    </span>
  );
}

function KitTabInner() {
  const t = useT(M);
  const guide = useLocalized({ en: DEMO_GUIDE, vi: DEMO_GUIDE_VI });
  const columns = useMemo(() => demoColumns(t), [t]);
  const toast = useToast();
  const [tab, setTab] = useState("primitives");
  const [modal, setModal] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [drawer, setDrawer] = useState(false);
  const [showSkeleton, setShowSkeleton] = useState(false);
  const tones: Tone[] = Object.keys(TONE) as Tone[];

  return (
    <div>
      <SkeletonStyle />
      <div style={{ marginBottom: 20 }}>
        <Tabs
          tabs={[
            { key: "primitives", label: t("tabPrimitives") },
            { key: "table", label: t("tabTable") },
            { key: "overlays", label: t("tabOverlays") },
            { key: "guide", label: t("tabGuide") },
            { key: "gallery", label: t("tabGallery") },
          ]}
          active={tab}
          onChange={setTab}
        />
      </div>

      {tab === "gallery" && <KitGallery />}

      {tab === "guide" && (
        <Section title={t("secGuide")}>
          <GuidePanel content={guide} testId="kit-guide-demo" />
        </Section>
      )}

      {tab === "primitives" && (
        <>
          <Section title={t("secBadges")}>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              {tones.map((tone) => (
                <Badge key={tone} tone={tone}>{tone}</Badge>
              ))}
            </div>
          </Section>

          <Section title={t("secStatus")}>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              {["queued", "running", "completed", "failed", "held", "approved", "rejected", "regenerating"].map((s) => (
                <StatusBadge key={s} status={s} />
              ))}
            </div>
          </Section>

          <Section title={t("secRq")}>
            <ReactQueryDemo />
          </Section>

          <Section title={t("secToasts")}>
            <div style={{ display: "flex", gap: 8 }}>
              <KitBtn onClick={() => toast.success(t("toastSaved"))}>{t("success")}</KitBtn>
              <KitBtn onClick={() => toast.error(t("toastError"))}>{t("error")}</KitBtn>
              <KitBtn onClick={() => toast.info(t("toastInfo"))}>{t("info")}</KitBtn>
            </div>
          </Section>

          <Section title={t("secSkeleton")}>
            <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
              <KitBtn onClick={() => setShowSkeleton((v) => !v)}>{t("toggle")}</KitBtn>
              {showSkeleton && (
                <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 8 }}>
                  <SkeletonBar w="60%" />
                  <SkeletonBar w="40%" />
                  <SkeletonBar w="80%" />
                </div>
              )}
            </div>
          </Section>

          <Section title={t("secEmpty")}>
            <EmptyState
              title={t("emptyTitle")}
              description={t("emptyBody")}
              action={<KitBtn onClick={() => toast.info(t("refreshing"))}>{t("refresh")}</KitBtn>}
            />
          </Section>

          <Section title={t("secError")}>
            <ErrorState message={t("errorBody")} onRetry={() => toast.info(t("retrying"))} />
          </Section>

          <Section title={t("secInfoTip")}>
            <div style={{ fontSize: 13, color: K.body, fontFamily: sans }}>
              {t("toursThisWeek")}<b>12</b>
              <InfoTip
                text={t("infoTipText")}
                label={t("infoTipLabel")}
              />
            </div>
          </Section>
        </>
      )}

      {tab === "table" && (
        <Section title={t("secTable")}>
          <DataTable<DemoTour>
            data={DEMO_ROWS}
            columns={columns}
            tableId="kit-demo"
            getRowId={(r) => r.id}
            userId="demo"
            searchable
            enableSelection
            enableSavedViews
            enableCsv
            csvFilename="kit-demo-tours"
            pageSize={10}
            emptyTitle={t("noTours")}
            bulkActions={({ selectedRows, clearSelection }) => (
              <>
                <KitBtn onClick={() => { toast.success(t("regenerating", selectedRows.length)); clearSelection(); }}>
                  <Wand2 size={13} /> {t("regenerateSelected")}
                </KitBtn>
                <KitBtn onClick={() => { toast.error(t("deleting", selectedRows.length)); clearSelection(); }}>
                  <Trash2 size={13} /> {t("delete")}
                </KitBtn>
              </>
            )}
          />
        </Section>
      )}

      {tab === "overlays" && (
        <Section title={t("secOverlays")}>
          <div style={{ display: "flex", gap: 8 }}>
            <KitBtn onClick={() => setModal(true)}>{t("openModal")}</KitBtn>
            <KitBtn onClick={() => setConfirm(true)}>{t("confirmDestructive")}</KitBtn>
            <KitBtn onClick={() => setDrawer(true)}>{t("openDrawer")}</KitBtn>
          </div>

          <Modal open={modal} onClose={() => setModal(false)} title={t("exampleModal")} footer={<KitBtn onClick={() => setModal(false)}>{t("close")}</KitBtn>}>
            {t("modalBody")}
          </Modal>

          <ConfirmModal
            open={confirm}
            onClose={() => setConfirm(false)}
            onConfirm={() => { setConfirm(false); toast.success(t("deleted")); }}
            title={t("deleteTitle")}
            body={t("deleteBody")}
            confirmLabel={t("delete")}
            destructive
          />

          <Drawer open={drawer} onClose={() => setDrawer(false)} title={t("tourDetail")} footer={<KitBtn onClick={() => setDrawer(false)}>{t("close")}</KitBtn>}>
            <p>{t("drawerBody")}</p>
            <p style={{ color: K.muted }}>{t("drawerExample")}</p>
          </Drawer>
        </Section>
      )}
    </div>
  );
}

/** The demo buttons of this gallery: the kit ghost button. */
function KitBtn({ children, onClick }: { children: React.ReactNode; onClick?: () => void }) {
  return <Button variant="ghost" onClick={onClick}>{children}</Button>;
}

export default function SettingsKitTab() {
  return (
    <ToastProvider>
      <KitTabInner />
    </ToastProvider>
  );
}
