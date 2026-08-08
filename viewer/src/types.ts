/**
 * Typed shapes for the four files a WRENCH run directory contains.
 *
 * - `trajectory.jsonl`      -> TrajectoryStep (one per line)
 * - `<task_key>.jsonl`      -> LedgerEvent (one per line; the disruption ledger)
 * - `trajectory.meta.json`  -> TrajectoryMeta
 * - `samples.json`          -> Sample[] (cumulative production, 41-tick cadence;
 *                              absent in older runs)
 */

/** One agent step from `trajectory.jsonl`. */
export interface TrajectoryStep {
  step_index: number;
  /** Python policy code the agent submitted for this step. */
  code: string;
  /** Captured stdout / environment observation returned to the agent. */
  response: string;
  /** Game tick at which the step's execution finished. */
  game_tick: number;
  /** Cumulative production snapshot; empty object in current runs. */
  produced_counts: Record<string, number>;
}

export const LEDGER_EVENT_NAMES = [
  "armed",
  "fired",
  "failed",
  // the agent's factory design made the kind inapplicable (e.g. belt_cut
  // on a beltless build) — its own outcome category, not a failure
  "not_applicable",
  "report_fault",
] as const;

export type LedgerEventName = (typeof LEDGER_EVENT_NAMES)[number];

/** An entity touched by a disruption. */
export interface AffectedEntity {
  name: string;
  x: number;
  y: number;
}

/**
 * Free-form detail payload. Observed shapes:
 * - armed / fired:  `{ id }`
 * - failed:         `{ id, error }`
 * - report_fault:   `{ x, y, cause }`
 */
export interface LedgerEventDetail {
  id?: number;
  error?: string;
  x?: number;
  y?: number;
  cause?: string;
  [key: string]: unknown;
}

/** One event from the disruption ledger (`<task_key>.jsonl`). */
export interface LedgerEvent {
  tick: number;
  event: LedgerEventName;
  /** Disruption kind, e.g. "entity_destruction", "belt_cut". Absent on report_fault. */
  kind?: string;
  /** RNG seed for the disruption. Absent on failed / report_fault. */
  seed?: number;
  affected?: AffectedEntity[];
  detail?: LedgerEventDetail;
}

/** Per-fire recovery metrics stored in meta under dynamic `fire_<n>` keys. */
export interface FireMetrics {
  kind: string;
  tick: number;
  baseline: number | null;
  /** Throughput-recovery ratio. */
  TR: number | null;
  recovered: boolean | number | null;
}

export interface DetectionSummary {
  /** Ticks from each fire to the agent's report_fault. */
  latencies: number[];
  precision: number;
  /** Strict 3-tile match radius — the headline precision (absent in
   * runs recorded before it existed). */
  precision_strict?: number;
  recall: number;
}

/** Summary written at finalize (`trajectory.meta.json`). */
export interface TrajectoryMeta {
  task: string;
  model: string;
  steps: number;
  fires: number;
  detection?: DetectionSummary;
  [fire: `fire_${number}`]: FireMetrics;
}

/** One row of `samples.json`: cumulative produced counts at a tick. */
export interface Sample {
  tick: number;
  counts: Record<string, number>;
}

/** A fully parsed run directory. */
export interface RunData {
  /** Directory name (or the `?run=` path's last segment). */
  name: string;
  /** Task key, from meta or the ledger filename; null when neither exists. */
  taskKey: string | null;
  trajectory: TrajectoryStep[];
  /** Empty array when the ledger file is absent. */
  ledger: LedgerEvent[];
  meta: TrajectoryMeta | null;
  samples: Sample[] | null;
}

/** Factorio runs at 60 ticks per second. */
export const TICKS_PER_SECOND = 60;
export const TICKS_PER_MINUTE = TICKS_PER_SECOND * 60;

/** The `fire_<n>` entries of a meta object, in index order. */
export function fireMetricsOf(meta: TrajectoryMeta): FireMetrics[] {
  const fires: FireMetrics[] = [];
  for (let i = 0; i < meta.fires; i++) {
    const entry = meta[`fire_${i}`];
    if (entry) fires.push(entry);
  }
  return fires;
}
