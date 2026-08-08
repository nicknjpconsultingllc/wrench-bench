/**
 * Runtime parsers/validators for the four run-directory file shapes.
 * Pure string -> typed-object functions: no DOM or Node APIs, so they are
 * shared by the browser loader and the Node smoke test.
 */
import type {
  AffectedEntity,
  DetectionSummary,
  FireMetrics,
  LedgerEvent,
  LedgerEventDetail,
  LedgerEventName,
  Sample,
  TrajectoryMeta,
  TrajectoryStep,
} from "../types";
import { LEDGER_EVENT_NAMES } from "../types";

export class ParseError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ParseError";
  }
}

function fail(ctx: string, message: string): never {
  throw new ParseError(`${ctx}: ${message}`);
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function asRecord(v: unknown, ctx: string): Record<string, unknown> {
  if (!isRecord(v)) fail(ctx, `expected an object, got ${describe(v)}`);
  return v;
}

function asNumber(v: unknown, ctx: string): number {
  if (typeof v !== "number" || Number.isNaN(v)) {
    fail(ctx, `expected a number, got ${describe(v)}`);
  }
  return v;
}

function asString(v: unknown, ctx: string): string {
  if (typeof v !== "string") fail(ctx, `expected a string, got ${describe(v)}`);
  return v;
}

function asCounts(v: unknown, ctx: string): Record<string, number> {
  const rec = asRecord(v, ctx);
  const out: Record<string, number> = {};
  for (const [key, value] of Object.entries(rec)) {
    out[key] = asNumber(value, `${ctx}.${key}`);
  }
  return out;
}

function describe(v: unknown): string {
  if (v === null) return "null";
  if (Array.isArray(v)) return "an array";
  return typeof v;
}

/** Parse a .jsonl file, applying `row` to each non-empty line. */
function parseJsonl<T>(
  text: string,
  fileName: string,
  row: (value: unknown, ctx: string) => T,
): T[] {
  const out: T[] = [];
  const lines = text.split(/\r?\n/);
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (!line || !line.trim()) continue;
    const ctx = `${fileName}:${i + 1}`;
    let value: unknown;
    try {
      value = JSON.parse(line);
    } catch (err) {
      fail(ctx, `invalid JSON (${err instanceof Error ? err.message : String(err)})`);
    }
    out.push(row(value, ctx));
  }
  return out;
}

// ---------------------------------------------------------------- trajectory

export function parseTrajectory(text: string): TrajectoryStep[] {
  return parseJsonl(text, "trajectory.jsonl", (value, ctx) => {
    const o = asRecord(value, ctx);
    return {
      step_index: asNumber(o.step_index, `${ctx}.step_index`),
      code: asString(o.code, `${ctx}.code`),
      response: asString(o.response, `${ctx}.response`),
      game_tick: asNumber(o.game_tick, `${ctx}.game_tick`),
      produced_counts:
        o.produced_counts === undefined
          ? {}
          : asCounts(o.produced_counts, `${ctx}.produced_counts`),
    } satisfies TrajectoryStep;
  });
}

// -------------------------------------------------------------------- ledger

const EVENT_NAMES: ReadonlySet<string> = new Set(LEDGER_EVENT_NAMES);

function asEventName(v: unknown, ctx: string): LedgerEventName {
  const s = asString(v, ctx);
  if (!EVENT_NAMES.has(s)) {
    fail(ctx, `unknown ledger event "${s}" (expected ${[...EVENT_NAMES].join(" | ")})`);
  }
  return s as LedgerEventName;
}

function asAffected(v: unknown, ctx: string): AffectedEntity[] {
  if (!Array.isArray(v)) fail(ctx, `expected an array, got ${describe(v)}`);
  return v.map((entry, i) => {
    const o = asRecord(entry, `${ctx}[${i}]`);
    return {
      name: asString(o.name, `${ctx}[${i}].name`),
      x: asNumber(o.x, `${ctx}[${i}].x`),
      y: asNumber(o.y, `${ctx}[${i}].y`),
    };
  });
}

export function parseLedger(text: string, fileName = "ledger.jsonl"): LedgerEvent[] {
  return parseJsonl(text, fileName, (value, ctx) => {
    const o = asRecord(value, ctx);
    const event: LedgerEvent = {
      tick: asNumber(o.tick, `${ctx}.tick`),
      event: asEventName(o.event, `${ctx}.event`),
    };
    if (o.kind !== undefined) event.kind = asString(o.kind, `${ctx}.kind`);
    if (o.seed !== undefined) event.seed = asNumber(o.seed, `${ctx}.seed`);
    if (o.affected !== undefined) event.affected = asAffected(o.affected, `${ctx}.affected`);
    if (o.detail !== undefined) {
      event.detail = asRecord(o.detail, `${ctx}.detail`) as LedgerEventDetail;
    }
    return event;
  });
}

// ---------------------------------------------------------------------- meta

function asNumberOrNull(v: unknown, ctx: string): number | null {
  return v === null ? null : asNumber(v, ctx);
}

function asFireMetrics(v: unknown, ctx: string): FireMetrics {
  const o = asRecord(v, ctx);
  const recovered = o.recovered;
  if (
    recovered !== null &&
    recovered !== undefined &&
    typeof recovered !== "boolean" &&
    typeof recovered !== "number"
  ) {
    fail(`${ctx}.recovered`, `expected boolean | number | null, got ${describe(recovered)}`);
  }
  return {
    kind: asString(o.kind, `${ctx}.kind`),
    tick: asNumber(o.tick, `${ctx}.tick`),
    baseline: asNumberOrNull(o.baseline, `${ctx}.baseline`),
    TR: asNumberOrNull(o.TR, `${ctx}.TR`),
    recovered: (recovered ?? null) as boolean | number | null,
  };
}

function asDetection(v: unknown, ctx: string): DetectionSummary {
  const o = asRecord(v, ctx);
  const rawLatencies = o.latencies;
  if (!Array.isArray(rawLatencies)) {
    fail(`${ctx}.latencies`, `expected an array, got ${describe(rawLatencies)}`);
  }
  return {
    latencies: rawLatencies.map((n, i) => asNumber(n, `${ctx}.latencies[${i}]`)),
    precision: asNumber(o.precision, `${ctx}.precision`),
    ...(o.precision_strict !== undefined && {
      precision_strict: asNumber(o.precision_strict, `${ctx}.precision_strict`),
    }),
    recall: asNumber(o.recall, `${ctx}.recall`),
  };
}

export function parseMeta(text: string): TrajectoryMeta {
  const ctx = "trajectory.meta.json";
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch (err) {
    fail(ctx, `invalid JSON (${err instanceof Error ? err.message : String(err)})`);
  }
  const o = asRecord(value, ctx);
  const meta: TrajectoryMeta = {
    task: asString(o.task, `${ctx}.task`),
    model: asString(o.model, `${ctx}.model`),
    steps: asNumber(o.steps, `${ctx}.steps`),
    fires: asNumber(o.fires, `${ctx}.fires`),
  };
  if (o.detection !== undefined) {
    meta.detection = asDetection(o.detection, `${ctx}.detection`);
  }
  for (let i = 0; i < meta.fires; i++) {
    const raw = o[`fire_${i}`];
    if (raw !== undefined) {
      meta[`fire_${i}`] = asFireMetrics(raw, `${ctx}.fire_${i}`);
    }
  }
  return meta;
}

// ------------------------------------------------------------------- samples

export function parseSamples(text: string): Sample[] {
  const ctx = "samples.json";
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch (err) {
    fail(ctx, `invalid JSON (${err instanceof Error ? err.message : String(err)})`);
  }
  if (!Array.isArray(value)) fail(ctx, `expected an array, got ${describe(value)}`);
  return value.map((entry, i) => {
    const o = asRecord(entry, `${ctx}[${i}]`);
    return {
      tick: asNumber(o.tick, `${ctx}[${i}].tick`),
      counts: asCounts(o.counts, `${ctx}[${i}].counts`),
    };
  });
}
