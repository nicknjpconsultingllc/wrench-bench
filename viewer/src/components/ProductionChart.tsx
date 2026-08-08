/**
 * Production-rate line chart, hand-rolled SVG.
 *
 * One series (items/min) with an area wash, hairline grid, crosshair tooltip,
 * and vertical event markers: "fired" disruptions (solid, critical red,
 * triangle glyph) and "report_fault" agent reports (dashed, violet, diamond
 * glyph). Marker identity is carried by shape + dash as well as color, and
 * every plotted value is also reachable through the data-table toggle.
 */
import { useLayoutEffect, useMemo, useRef, useState } from "react";
import type { LedgerEvent } from "../types";
import { TICKS_PER_MINUTE } from "../types";
import type { RateDerivation, RatePoint } from "../lib/rates";
import { fmtElapsed, fmtInt, fmtRate, niceCeil, niceTicks } from "../lib/format";

interface ProductionChartProps {
  derivation: RateDerivation;
  selectedItem: string | null;
  onSelectItem: (item: string) => void;
  /** Full ledger; the chart overlays fired + report_fault. */
  events: LedgerEvent[];
  /** Tick treated as t=0 for elapsed-time labeling. */
  startTick: number;
}

const MARGIN = { top: 34, right: 68, bottom: 44, left: 52 };
const PLOT_HEIGHT = 260;
const MINUTE_STEPS = [1, 2, 5, 10, 15, 20, 30, 60, 90, 120, 180, 240, 360];

function useMeasuredWidth(): [React.MutableRefObject<HTMLDivElement | null>, number] {
  const ref = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(720);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) setWidth(entry.contentRect.width);
    });
    observer.observe(el);
    setWidth(el.clientWidth);
    return () => observer.disconnect();
  }, []);
  return [ref, width];
}

function nearestPoint(points: RatePoint[], tick: number): RatePoint | null {
  if (points.length === 0) return null;
  let lo = 0;
  let hi = points.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if ((points[mid]?.tick ?? Infinity) < tick) lo = mid + 1;
    else hi = mid;
  }
  const after = points[lo];
  const before = points[lo - 1];
  if (!before) return after ?? null;
  if (!after) return before;
  return tick - before.tick <= after.tick - tick ? before : after;
}

function eventTitle(event: LedgerEvent): string {
  return event.event === "fired" ? "Disruption fired" : "Fault reported";
}

function eventCause(event: LedgerEvent): string | null {
  return event.detail?.cause ?? event.detail?.error ?? null;
}

function affectedSummary(event: LedgerEvent): string | null {
  if (!event.affected || event.affected.length === 0) return null;
  return event.affected.map((a) => `${a.name} (${a.x}, ${a.y})`).join(", ");
}

export function ProductionChart({
  derivation,
  selectedItem,
  onSelectItem,
  events,
  startTick,
}: ProductionChartProps) {
  const [wrapRef, width] = useMeasuredWidth();
  const [hover, setHover] = useState<RatePoint | null>(null);
  const [markerHover, setMarkerHover] = useState<number | null>(null);
  const [showTable, setShowTable] = useState(false);

  const series =
    derivation.series.find((s) => s.item === selectedItem) ?? derivation.series[0] ?? null;

  const markers = useMemo(
    () =>
      events
        .filter((e) => e.event === "fired" || e.event === "report_fault")
        .sort((a, b) => a.tick - b.tick),
    [events],
  );

  // Decimate very dense sample series for drawing; hover snaps to drawn points.
  const points = useMemo(() => {
    const all = series?.points ?? [];
    const stride = Math.max(1, Math.ceil(all.length / 1500));
    if (stride === 1) return all;
    const out = all.filter((_, i) => i % stride === 0);
    const last = all[all.length - 1];
    if (last && out[out.length - 1] !== last) out.push(last);
    return out;
  }, [series]);

  const hasChart = points.length >= 2;

  const first = points[0];
  const last = points[points.length - 1];
  const tickLo = Math.min(first?.tick ?? Infinity, markers[0]?.tick ?? Infinity);
  const tickHi = Math.max(
    last?.tick ?? -Infinity,
    markers[markers.length - 1]?.tick ?? -Infinity,
  );
  const pad = Math.max((tickHi - tickLo) * 0.02, 1);
  const x0 = tickLo - pad;
  const x1 = tickHi + pad;

  const w = Math.max(width, 360);
  const innerW = w - MARGIN.left - MARGIN.right;
  const innerH = PLOT_HEIGHT;
  const height = PLOT_HEIGHT + MARGIN.top + MARGIN.bottom;
  const yMax = niceCeil(Math.max(1, ...points.map((p) => p.rate)) * 1.05);

  const xs = (tick: number) => MARGIN.left + ((tick - x0) / (x1 - x0)) * innerW;
  const ys = (rate: number) => MARGIN.top + innerH - (rate / yMax) * innerH;
  const baselineY = MARGIN.top + innerH;

  const yTicks = niceTicks(yMax, 4);
  const xTicks = useMemo(() => {
    const spanMin = (x1 - x0) / TICKS_PER_MINUTE;
    const step = MINUTE_STEPS.find((s) => spanMin / s <= 6) ?? 480;
    const lo = (x0 - startTick) / TICKS_PER_MINUTE;
    const hi = (x1 - startTick) / TICKS_PER_MINUTE;
    const ticks: { tick: number; label: string }[] = [];
    for (let k = Math.ceil(lo / step); k * step <= hi; k++) {
      ticks.push({
        tick: startTick + k * step * TICKS_PER_MINUTE,
        label: String(k * step),
      });
    }
    return ticks;
  }, [x0, x1, startTick]);

  const linePath = useMemo(
    () =>
      points
        .map((p, i) => `${i === 0 ? "M" : "L"}${xs(p.tick).toFixed(2)} ${ys(p.rate).toFixed(2)}`)
        .join(""),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [points, x0, x1, yMax, w],
  );
  const areaPath = hasChart && first && last
    ? `${linePath}L${xs(last.tick).toFixed(2)} ${baselineY}L${xs(first.tick).toFixed(2)} ${baselineY}Z`
    : "";

  const handleMove = (e: React.PointerEvent<SVGRectElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const frac = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
    setHover(nearestPoint(points, x0 + frac * (x1 - x0)));
  };

  const activeMarker = markerHover !== null ? (markers[markerHover] ?? null) : null;
  const showCrosshair = hover !== null && activeMarker === null;

  const tooltip = (() => {
    if (activeMarker) {
      const cause = eventCause(activeMarker);
      const affected = affectedSummary(activeMarker);
      return {
        x: xs(activeMarker.tick),
        body: (
          <>
            <div className={`tooltip-title ${activeMarker.event === "fired" ? "is-fired" : "is-fault"}`}>
              {eventTitle(activeMarker)}
            </div>
            {activeMarker.kind && <div className="tooltip-row">kind: {activeMarker.kind}</div>}
            {affected && <div className="tooltip-row">affected: {affected}</div>}
            {cause && <div className="tooltip-row">{cause}</div>}
            <div className="tooltip-sub">
              tick {fmtInt(activeMarker.tick)} · {fmtElapsed(activeMarker.tick - startTick)}
            </div>
          </>
        ),
      };
    }
    if (showCrosshair && hover) {
      return {
        x: xs(hover.tick),
        body: (
          <>
            <div className="tooltip-value">
              {fmtRate(hover.rate)} <span className="tooltip-unit">/min</span>
            </div>
            <div className="tooltip-sub">
              tick {fmtInt(hover.tick)} · {fmtElapsed(hover.tick - startTick)}
            </div>
          </>
        ),
      };
    }
    return null;
  })();

  return (
    <section className="card">
      <div className="card-head">
        <div>
          <h2>Production rate</h2>
          <p className="card-sub">{derivation.note}</p>
        </div>
        <div className="card-controls">
          {derivation.series.length > 1 && series && (
            <label className="select-label">
              item
              <select value={series.item} onChange={(e) => onSelectItem(e.target.value)}>
                {derivation.series.map((s) => (
                  <option key={s.item} value={s.item}>
                    {s.item}
                  </option>
                ))}
              </select>
            </label>
          )}
          {hasChart && (
            <button
              type="button"
              className="ghost-button"
              aria-pressed={showTable}
              onClick={() => setShowTable((v) => !v)}
            >
              {showTable ? "Hide data table" : "Data table"}
            </button>
          )}
        </div>
      </div>

      {!hasChart ? (
        <p className="chart-empty">{derivation.note}</p>
      ) : (
        <>
          <div className="legend" aria-hidden="true">
            <span className="key">
              <svg width="18" height="8">
                <line x1="0" y1="4" x2="18" y2="4" className="key-line" />
              </svg>
              {series?.item} per minute
            </span>
            {markers.some((m) => m.event === "fired") && (
              <span className="key">
                <span className="key-mark key-fired" />
                disruption fired
              </span>
            )}
            {markers.some((m) => m.event === "report_fault") && (
              <span className="key">
                <span className="key-mark key-fault" />
                fault reported
              </span>
            )}
          </div>

          <div className="chart-wrap" ref={wrapRef}>
            <svg
              width={w}
              height={height}
              role="img"
              aria-label={`Production rate of ${series?.item ?? "items"} per minute over game time, with disruption and fault markers`}
            >
              {/* grid + y axis */}
              {yTicks.map((t) => (
                <g key={t}>
                  <line
                    x1={MARGIN.left}
                    x2={MARGIN.left + innerW}
                    y1={ys(t)}
                    y2={ys(t)}
                    className={t === 0 ? "axis-line" : "grid-line"}
                  />
                  <text x={MARGIN.left - 8} y={ys(t)} className="tick-label tick-y">
                    {fmtInt(t)}
                  </text>
                </g>
              ))}
              {/* x axis */}
              {xTicks.map((t) => (
                <g key={t.tick}>
                  <line x1={xs(t.tick)} x2={xs(t.tick)} y1={baselineY} y2={baselineY + 5} className="axis-line" />
                  <text x={xs(t.tick)} y={baselineY + 18} className="tick-label tick-x">
                    {t.label}
                  </text>
                </g>
              ))}
              <text x={MARGIN.left + innerW / 2} y={height - 6} className="axis-title">
                game time — minutes since run start (tick {fmtInt(startTick)})
              </text>

              {/* series */}
              <path d={areaPath} className="series-area" />
              <path d={linePath} className="series-line" />
              {last && (
                <g>
                  <circle cx={xs(last.tick)} cy={ys(last.rate)} r={4.5} className="series-dot" />
                  <text x={xs(last.tick) + 10} y={ys(last.rate)} className="end-label">
                    {fmtRate(last.rate)}/min
                  </text>
                </g>
              )}

              {/* crosshair */}
              {showCrosshair && hover && (
                <g className="crosshair">
                  <line x1={xs(hover.tick)} x2={xs(hover.tick)} y1={MARGIN.top} y2={baselineY} />
                  <circle cx={xs(hover.tick)} cy={ys(hover.rate)} r={4.5} className="series-dot" />
                </g>
              )}

              {/* pointer capture for the crosshair */}
              <rect
                x={MARGIN.left}
                y={MARGIN.top}
                width={innerW}
                height={innerH}
                fill="transparent"
                onPointerMove={handleMove}
                onPointerLeave={() => setHover(null)}
              />

              {/* event markers (drawn above the capture rect: own hit targets) */}
              {markers.map((event, i) => {
                const x = xs(event.tick);
                const fired = event.event === "fired";
                return (
                  <g key={i} className={fired ? "marker marker-fired" : "marker marker-fault"}>
                    <line x1={x} x2={x} y1={MARGIN.top - 2} y2={baselineY} className="marker-line" />
                    {fired ? (
                      <path
                        d={`M${x - 5} ${MARGIN.top - 13}L${x + 5} ${MARGIN.top - 13}L${x} ${MARGIN.top - 4}Z`}
                        className="marker-glyph"
                      />
                    ) : (
                      <path
                        d={`M${x} ${MARGIN.top - 14}L${x + 5} ${MARGIN.top - 9}L${x} ${MARGIN.top - 4}L${x - 5} ${MARGIN.top - 9}Z`}
                        className="marker-glyph"
                      />
                    )}
                    <rect
                      x={x - 12}
                      y={MARGIN.top - 16}
                      width={24}
                      height={innerH + 16}
                      fill="transparent"
                      onPointerEnter={() => setMarkerHover(i)}
                      onPointerLeave={() => setMarkerHover(null)}
                    >
                      <title>{`${eventTitle(event)}${event.kind ? ` — ${event.kind}` : ""}`}</title>
                    </rect>
                  </g>
                );
              })}
            </svg>

            {tooltip && (
              <div
                className="tooltip"
                style={
                  tooltip.x > MARGIN.left + innerW * 0.58
                    ? { left: tooltip.x - 12, transform: "translateX(-100%)", top: MARGIN.top + 6 }
                    : { left: tooltip.x + 12, top: MARGIN.top + 6 }
                }
              >
                {tooltip.body}
              </div>
            )}
          </div>

          {showTable && series && (
            <div className="table-grid">
              <RateTable points={series.points} startTick={startTick} item={series.item} />
              <EventTable events={events} startTick={startTick} />
            </div>
          )}
        </>
      )}
    </section>
  );
}

function RateTable({
  points,
  startTick,
  item,
}: {
  points: RatePoint[];
  startTick: number;
  item: string;
}) {
  const stride = Math.max(1, Math.ceil(points.length / 300));
  const rows = points.filter((_, i) => i % stride === 0);
  return (
    <div className="table-block">
      <h3>
        Rate samples{stride > 1 ? ` (every ${stride}th of ${fmtInt(points.length)})` : ""}
      </h3>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>tick</th>
              <th>elapsed</th>
              <th>{item}/min</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((p) => (
              <tr key={p.tick}>
                <td>{fmtInt(p.tick)}</td>
                <td>{fmtElapsed(p.tick - startTick)}</td>
                <td>{fmtRate(p.rate)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function EventTable({ events, startTick }: { events: LedgerEvent[]; startTick: number }) {
  if (events.length === 0) {
    return (
      <div className="table-block">
        <h3>Ledger events</h3>
        <p className="card-sub">No ledger file in this run.</p>
      </div>
    );
  }
  return (
    <div className="table-block">
      <h3>Ledger events</h3>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>tick</th>
              <th>elapsed</th>
              <th>event</th>
              <th>kind</th>
              <th>detail</th>
            </tr>
          </thead>
          <tbody>
            {events.map((e, i) => {
              const parts = [affectedSummary(e), eventCause(e)].filter(Boolean);
              return (
                <tr key={i}>
                  <td>{fmtInt(e.tick)}</td>
                  <td>{fmtElapsed(e.tick - startTick)}</td>
                  <td>{e.event}</td>
                  <td>{e.kind ?? "—"}</td>
                  <td className="detail-cell">{parts.length > 0 ? parts.join(" · ") : "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
