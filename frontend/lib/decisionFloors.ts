// frontend/lib/decisionFloors.ts — S280 (AA-1096): the Jev question editor's floors (Decisions › Questions).
//
// The floors are probabilities from 0 to 1 (api/routers/admin_decisions.py QuestionUpdate). The editor used
// Number(text): "0,95" (a Vietnamese decimal comma) or "0.9x" became NaN, JSON turned NaN into null, and the PUT
// silently removed the floor. These helpers read a floor (a comma is a decimal separator too), refuse anything that
// is not a number from 0 to 1, and check the API's own rules before the request (reject below accept; enforce needs a
// calibration record and at least one floor), so the API's 422 is never the first place the rule shows. Pure.

export type QuestionMode = "off" | "shadow" | "enforce";

export type FloorProblem = "accept" | "reject" | "order" | "enforceCalibration" | "enforceFloor" | "calibrationLong";

/** The API's limits (QuestionUpdate): floors 0..1, calibration_ref at most 300 characters. */
export const FLOOR_MIN = 0;
export const FLOOR_MAX = 1;
export const CALIBRATION_REF_MAX = 300;

/** A floor's text as a number: "" → null (no floor), "0,95" → 0.95; "bad" for anything else or outside 0..1. */
export function parseFloor(text: string): number | null | "bad" {
  const s = text.trim().replace(",", ".");
  if (s === "") return null;
  if (!/^(\d+(\.\d*)?|\.\d+)$/.test(s)) return "bad";
  const n = Number(s);
  return Number.isFinite(n) && n >= FLOOR_MIN && n <= FLOOR_MAX ? n : "bad";
}

export interface QuestionBody {
  mode: QuestionMode;
  accept_floor: number | null;
  reject_ceiling: number | null;
  calibration_ref: string | null;
}

/** The problem of the calibration record and of what enforce needs (a record and at least one floor), else null. */
function enforceProblem(mode: QuestionMode, calibration: string, accept: number | null, reject: number | null):
  FloorProblem | null {
  if (calibration.length > CALIBRATION_REF_MAX) return "calibrationLong";
  if (mode !== "enforce") return null;
  if (!calibration) return "enforceCalibration";
  return accept === null && reject === null ? "enforceFloor" : null;
}

/** The PUT body of the editor's fields, or the first problem (the API's validate_update rules, in its order). */
export function questionBody(d: { mode: QuestionMode; accept: string; reject: string; calibration: string }):
  { body: QuestionBody } | { error: FloorProblem } {
  const accept = parseFloor(d.accept);
  if (accept === "bad") return { error: "accept" };
  const reject = parseFloor(d.reject);
  if (reject === "bad") return { error: "reject" };
  if (accept !== null && reject !== null && reject >= accept) return { error: "order" };
  const calibration = d.calibration.trim();
  const problem = enforceProblem(d.mode, calibration, accept, reject);
  if (problem) return { error: problem };
  return { body: { mode: d.mode, accept_floor: accept, reject_ceiling: reject, calibration_ref: calibration || null } };
}
