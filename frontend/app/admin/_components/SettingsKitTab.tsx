"use client";
// app/admin/_components/SettingsKitTab.tsx
// AA-662 — the UI-kit "living reference" rendered as a tab inside admin Settings (not a standalone
// nav page). Shows every kit primitive with static data so the design system can be reviewed in
// the running admin shell without hitting the backend; one section proves the app-wide
// QueryClientProvider via a fake async query.

import { useQuery } from "@tanstack/react-query";
import { Trash2, Wand2 } from "lucide-react";
import { useState } from "react";
import { A, sans, Card, SLabel } from "./adminUi";
import {
  Badge,
  ConfirmModal,
  type ColumnDef,
  DataTable,
  Drawer,
  EmptyState,
  ErrorState,
  Modal,
  SkeletonBar,
  SkeletonStyle,
  StatusBadge,
  Tabs,
  TONE,
  type Tone,
  ToastProvider,
  useToast,
} from "../../_kit";

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

const COLUMNS: ColumnDef<DemoTour, unknown>[] = [
  { accessorKey: "name", header: "Tour", cell: (c) => <strong>{c.getValue<string>()}</strong> },
  { accessorKey: "country", header: "Country" },
  { accessorKey: "status", header: "Status", cell: (c) => <StatusBadge status={c.getValue<string>()} /> },
  {
    accessorKey: "quality",
    header: "Quality",
    cell: (c) => {
      const q = c.getValue<number>();
      return q > 0 ? q.toFixed(1) : "—";
    },
  },
  { accessorKey: "atoms", header: "Atoms" },
];

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <Card style={{ marginBottom: 20 }}>
      <SLabel>{title}</SLabel>
      <div style={{ marginTop: 10 }}>{children}</div>
    </Card>
  );
}

function fakeFetchCount(): Promise<number> {
  return new Promise((resolve) => setTimeout(() => resolve(DEMO_ROWS.length), 600));
}

function ReactQueryDemo() {
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["kit-demo-count"],
    queryFn: fakeFetchCount,
  });
  if (isLoading) return <SkeletonBar w={120} h={16} />;
  if (isError) return <ErrorState message="demo query failed" onRetry={() => refetch()} />;
  return (
    <span style={{ fontSize: 13, color: A.ink }}>
      react-query resolved: <strong>{data}</strong> demo rows (via app-wide QueryClientProvider)
    </span>
  );
}

function KitTabInner() {
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
            { key: "primitives", label: "Primitives" },
            { key: "table", label: "DataTable" },
            { key: "overlays", label: "Overlays" },
          ]}
          active={tab}
          onChange={setTab}
        />
      </div>

      {tab === "primitives" && (
        <>
          <Section title="Badges (tones)">
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              {tones.map((t) => (
                <Badge key={t} tone={t}>{t}</Badge>
              ))}
            </div>
          </Section>

          <Section title="StatusBadge (job status driven)">
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              {["queued", "running", "completed", "failed", "held", "approved", "rejected", "regenerating"].map((s) => (
                <StatusBadge key={s} status={s} />
              ))}
            </div>
          </Section>

          <Section title="react-query">
            <ReactQueryDemo />
          </Section>

          <Section title="Toasts">
            <div style={{ display: "flex", gap: 8 }}>
              <KitBtn onClick={() => toast.success("Saved successfully")}>Success</KitBtn>
              <KitBtn onClick={() => toast.error("Something went wrong")}>Error</KitBtn>
              <KitBtn onClick={() => toast.info("Just so you know")}>Info</KitBtn>
            </div>
          </Section>

          <Section title="Skeleton">
            <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
              <KitBtn onClick={() => setShowSkeleton((v) => !v)}>Toggle</KitBtn>
              {showSkeleton && (
                <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 8 }}>
                  <SkeletonBar w="60%" />
                  <SkeletonBar w="40%" />
                  <SkeletonBar w="80%" />
                </div>
              )}
            </div>
          </Section>

          <Section title="EmptyState">
            <EmptyState
              title="No tours in the queue"
              description="When a rewrite fails a gate it lands here for review."
              action={<KitBtn onClick={() => toast.info("refresh")}>Refresh</KitBtn>}
            />
          </Section>

          <Section title="ErrorState">
            <ErrorState message="Could not reach the backend (503)." onRetry={() => toast.info("retrying")} />
          </Section>
        </>
      )}

      {tab === "table" && (
        <Section title="DataTable — filter, multi-sort, columns, pagination, selection, saved views, CSV">
          <DataTable<DemoTour>
            data={DEMO_ROWS}
            columns={COLUMNS}
            tableId="kit-demo"
            getRowId={(r) => r.id}
            userId="demo"
            searchable
            enableSelection
            enableSavedViews
            enableCsv
            csvFilename="kit-demo-tours"
            pageSize={10}
            emptyTitle="No tours"
            bulkActions={({ selectedRows, clearSelection }) => (
              <>
                <KitBtn onClick={() => { toast.success(`Regenerating ${selectedRows.length} tour(s)`); clearSelection(); }}>
                  <Wand2 size={13} /> Regenerate selected
                </KitBtn>
                <KitBtn onClick={() => { toast.error(`Deleting ${selectedRows.length}`); clearSelection(); }}>
                  <Trash2 size={13} /> Delete
                </KitBtn>
              </>
            )}
          />
        </Section>
      )}

      {tab === "overlays" && (
        <Section title="Modal · ConfirmModal · Drawer">
          <div style={{ display: "flex", gap: 8 }}>
            <KitBtn onClick={() => setModal(true)}>Open Modal</KitBtn>
            <KitBtn onClick={() => setConfirm(true)}>Confirm (destructive)</KitBtn>
            <KitBtn onClick={() => setDrawer(true)}>Open Drawer</KitBtn>
          </div>

          <Modal open={modal} onClose={() => setModal(false)} title="Example modal" footer={<KitBtn onClick={() => setModal(false)}>Close</KitBtn>}>
            A centered modal for forms and confirmations. Escape or backdrop closes it.
          </Modal>

          <ConfirmModal
            open={confirm}
            onClose={() => setConfirm(false)}
            onConfirm={() => { setConfirm(false); toast.success("Deleted"); }}
            title="Delete this tour?"
            body="This cannot be undone. The tour and its atoms will be removed."
            confirmLabel="Delete"
            destructive
          />

          <Drawer open={drawer} onClose={() => setDrawer(false)} title="Tour detail" footer={<KitBtn onClick={() => setDrawer(false)}>Close</KitBtn>}>
            <p>Right-side slide-over for detail / edit views.</p>
            <p style={{ color: A.muted }}>Nakasendo Way: Kiso Valley — Japan — 24 atoms.</p>
          </Drawer>
        </Section>
      )}
    </div>
  );
}

function KitBtn({ children, onClick }: { children: React.ReactNode; onClick?: () => void }) {
  return (
    <button
      onClick={onClick}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        padding: "7px 14px",
        borderRadius: 999,
        border: `1px solid ${A.line}`,
        background: A.card,
        color: A.ink3,
        fontSize: 13,
        fontWeight: 600,
        cursor: "pointer",
        fontFamily: sans,
      }}
    >
      {children}
    </button>
  );
}

export default function SettingsKitTab() {
  return (
    <ToastProvider>
      <KitTabInner />
    </ToastProvider>
  );
}
