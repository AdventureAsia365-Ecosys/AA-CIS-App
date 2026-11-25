"use client";
// app/admin/overview/_status/words.ts — AA-1140 (S289 live check): the page's words for the structured texts the API
// sends (lib/opsStatusText.ts builds the phrase, this file supplies the language). One hook, so every section says an
// app, a tile, a level or a service reason the same way in EN and VI.

import { useMemo } from "react";
import { useFormat } from "../../../_kit";
import { APP_LABEL, type Msg, type ResourceTile, type RuntimeRow } from "@/lib/opsStatus";
import { phrase, type Words } from "@/lib/opsStatusText";
import { useST, type ST, type SKey } from "./ui";

const LEVEL_KEY: Readonly<Record<string, SKey>> = {
  warn: "lvWarn", crit: "lvCrit", down: "lvDown", degraded: "lvDegraded",
};
const TILE_KEY: Readonly<Record<string, SKey>> = {
  cpu: "resCpu", connections: "resConnections", free_storage: "resFree", role: "resRole", xid: "resXid", ecs: "resEcs",
};
const UNIT_KEY: Readonly<Record<string, SKey>> = { connections: "unitOfMax", role: "unitOfLimit", ecs: "unitTasks" };
const SOURCE_KEY: Readonly<Record<string, SKey>> = {
  "CloudWatch alarm": "srcAlarm", ECS: "srcEcs", "Ops check": "srcCheck", "health check": "srcHealth",
  job: "srcJobs", "external checks": "srcExternal",
};

type Say = (key: string, ...args: string[]) => string;

function why(say: Say, code: string, running: number | null, desired: number | null): string | null {
  const r = String(running ?? "—");
  const d = String(desired ?? "—");
  const byCode: Record<string, () => string> = {
    scaled_to_zero: () => say("whyScaledToZero"),
    none_running: () => say("whyNoneRunning", d),
    rollout_failed: () => say("whyRolloutFailed"),
    below_desired: () => say("whyBelowDesired", r, d),
    rollout_in_progress: () => say("whyRolloutInProgress"),
    running: () => say("whyRunning", r, d),
  };
  return byCode[code]?.() ?? null;
}

function makeWords(t: ST, time?: (iso: string) => string): { words: Words; say: Say } {
  const say = t as unknown as Say;   // keys come from lib/opsStatusText.ts phraseKeys(), checked in its test
  const app = (id: string) => {
    if (id === "crm") return t("appCrm");
    return id === "shared" ? t("appShared") : APP_LABEL[id] ?? id;
  };
  const words: Words = {
    app,
    level: (lv) => (LEVEL_KEY[lv] ? t(LEVEL_KEY[lv]) : lv),
    tile: (id) => (TILE_KEY[id] ? t(TILE_KEY[id]) : id),
    unit: (tile, unit) => (UNIT_KEY[tile] ? t(UNIT_KEY[tile]) : unit),
    why: (code, running, desired) => why(say, code, running, desired),
    time,
  };
  return { words, say };
}

export interface StatusWords {
  /** The text of a structured message in the page language, or the API's English fallback. */
  text: (m: Msg | null | undefined, fallback: string) => string;
  app: (id: string) => string;
  tile: (t: ResourceTile) => string;
  unit: (t: ResourceTile) => string;
  reason: (r: RuntimeRow) => string | null;
  source: (source: string) => string;
}

export function useWords(): StatusWords {
  const t = useST();
  const { formatDateTime } = useFormat();
  return useMemo(() => {
    const { words, say } = makeWords(t, (iso) => formatDateTime(iso));
    return {
      text: (m, fallback) => {
        const p = phrase(m, words);
        return p ? say(p.key, ...p.args) : fallback;
      },
      app: words.app,
      tile: (tile) => (TILE_KEY[tile.id] ? t(TILE_KEY[tile.id]) : tile.label),
      unit: (tile) => words.unit(tile.id, tile.unit ?? ""),
      reason: (r) => (r.reason_code ? words.why(r.reason_code, r.running ?? null, r.desired ?? null) : null)
        ?? r.reason ?? null,
      source: (source) => t("srcLabel", SOURCE_KEY[source] ? t(SOURCE_KEY[source]) : t("srcOther")),
    };
  }, [t, formatDateTime]);
}
