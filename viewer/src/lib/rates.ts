/**
 * Production-rate derivation.
 *
 * Preferred source: `samples.json` cumulative counts, differentiated over a
 * trailing window. Fallback: the "current throughput of your factory is N of
 * <item> created per 60 seconds" lines the environment appends to step
 * responses (already a per-minute rate, stamped at the step's game_tick).
 */
import type { RunData, Sample, TrajectoryStep } from "../types";
import { TICKS_PER_MINUTE } from "../types";

export interface RatePoint {
  tick: number;
  /** Items per minute. */
  rate: number;
}

export interface RateSeries {
  item: string;
  points: RatePoint[];
}

export type RateSource = "samples" | "responses" | "none";

export interface RateDerivation {
  source: RateSource;
  series: RateSeries[];
  note: string;
}

/** Trailing differentiation window: 60 seconds of game time. */
const WINDOW_TICKS = TICKS_PER_MINUTE;

function ratesFromSamples(samples: Sample[]): RateSeries[] {
  const items = new Set<string>();
  for (const sample of samples) {
    for (const key of Object.keys(sample.counts)) items.add(key);
  }

  const series: RateSeries[] = [];
  for (const item of items) {
    const points: RatePoint[] = [];
    let j = 0;
    for (let i = 1; i < samples.length; i++) {
      const cur = samples[i];
      if (!cur) continue;
      // Advance the window start: keep the earliest sample within WINDOW_TICKS.
      while (j < i - 1) {
        const next = samples[j + 1];
        if (next && cur.tick - next.tick >= WINDOW_TICKS) j++;
        else break;
      }
      const base = samples[j];
      if (!base) continue;
      const dt = cur.tick - base.tick;
      if (dt <= 0) continue;
      const dc = (cur.counts[item] ?? 0) - (base.counts[item] ?? 0);
      points.push({ tick: cur.tick, rate: Math.max(0, (dc / dt) * TICKS_PER_MINUTE) });
    }
    if (points.some((p) => p.rate > 0)) series.push({ item, points });
  }
  return sortByProminence(series);
}

const THROUGHPUT_RE =
  /throughput of your factory is ([\d.]+) of ([\w-]+) created per 60 seconds/g;

function ratesFromResponses(steps: TrajectoryStep[]): RateSeries[] {
  const byItem = new Map<string, RatePoint[]>();
  for (const step of steps) {
    THROUGHPUT_RE.lastIndex = 0;
    let match: RegExpExecArray | null;
    while ((match = THROUGHPUT_RE.exec(step.response)) !== null) {
      const rate = Number(match[1]);
      const item = match[2];
      if (!item || Number.isNaN(rate)) continue;
      const points = byItem.get(item) ?? [];
      points.push({ tick: step.game_tick, rate });
      byItem.set(item, points);
    }
  }
  return sortByProminence(
    [...byItem.entries()].map(([item, points]) => ({ item, points })),
  );
}

/** Most-productive items first, so series[0] is the sensible default. */
function sortByProminence(series: RateSeries[]): RateSeries[] {
  const peak = (s: RateSeries) => Math.max(0, ...s.points.map((p) => p.rate));
  return [...series].sort((a, b) => peak(b) - peak(a) || a.item.localeCompare(b.item));
}

export function deriveRates(run: RunData): RateDerivation {
  if (run.samples && run.samples.length >= 2) {
    const series = ratesFromSamples(run.samples);
    if (series.length > 0) {
      return {
        source: "samples",
        series,
        note: "Rate over a trailing 60 s window of samples.json cumulative counts.",
      };
    }
  }
  const series = ratesFromResponses(run.trajectory);
  if (series.length > 0) {
    return {
      source: "responses",
      series,
      note:
        run.samples && run.samples.length >= 2
          ? "samples.json recorded no production; showing per-step throughput reports instead."
          : "samples.json not present in this run; showing per-step throughput reports parsed from agent observations.",
    };
  }
  return {
    source: "none",
    series: [],
    note:
      "No production data available — this run has no samples.json and its step responses contain no throughput reports.",
  };
}
