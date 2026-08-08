/** Number and tick formatting helpers. */
import { TICKS_PER_MINUTE } from "../types";

export function fmtInt(n: number): string {
  return n.toLocaleString("en-US");
}

/** A per-minute rate: one decimal under 100, whole numbers above. */
export function fmtRate(n: number): string {
  if (n >= 100) return Math.round(n).toLocaleString("en-US");
  const s = n.toFixed(1);
  return s.endsWith(".0") ? s.slice(0, -2) : s;
}

export function ticksToMinutes(dtTicks: number): number {
  return dtTicks / TICKS_PER_MINUTE;
}

/** Elapsed game time, e.g. "+23.3 min". */
export function fmtElapsed(dtTicks: number): string {
  const min = ticksToMinutes(dtTicks);
  const value = min >= 100 ? Math.round(min).toString() : min.toFixed(1);
  return `+${value} min`;
}

export function fmtPct(x: number): string {
  const pct = x * 100;
  return `${Number.isInteger(pct) ? pct : pct.toFixed(1)}%`;
}

/** "iron_plate_sentinel" -> "iron plate sentinel". */
export function humanizeKey(key: string): string {
  return key.replace(/[_-]+/g, " ");
}

/** Round a positive value up to a "nice" 1 / 2 / 2.5 / 5 x 10^k. */
export function niceCeil(v: number): number {
  if (!(v > 0)) return 1;
  const pow = 10 ** Math.floor(Math.log10(v));
  const f = v / pow;
  const nf = f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10;
  return nf * pow;
}

/** Evenly spaced ticks from 0 to max (inclusive) at a nice step. */
export function niceTicks(max: number, targetCount = 4): number[] {
  if (!(max > 0)) return [0, 1];
  const rawStep = max / targetCount;
  const pow = 10 ** Math.floor(Math.log10(rawStep));
  const f = rawStep / pow;
  const step = (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * pow;
  const ticks: number[] = [];
  for (let v = 0; v <= max + step * 1e-9; v += step) ticks.push(Number(v.toFixed(10)));
  return ticks;
}
