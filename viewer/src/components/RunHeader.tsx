/** Summary stat tiles for the loaded run. */
import type { RunData } from "../types";
import { fmtPct, humanizeKey, ticksToMinutes } from "../lib/format";

interface Stat {
  label: string;
  value: string;
  title?: string;
}

export function RunHeader({ run }: { run: RunData }) {
  const { meta } = run;
  const steps = meta?.steps ?? run.trajectory.length;
  const fires = meta?.fires ?? run.ledger.filter((e) => e.event === "fired").length;
  const detection = meta?.detection ?? null;

  const meanLatency =
    detection && detection.latencies.length > 0
      ? detection.latencies.reduce((a, b) => a + b, 0) / detection.latencies.length
      : null;

  const stats: Stat[] = [
    { label: "Task", value: run.taskKey ? humanizeKey(run.taskKey) : "unknown" },
    { label: "Model", value: meta?.model ?? "unknown" },
    { label: "Steps", value: String(steps) },
    { label: "Fires", value: String(fires) },
    {
      // strict (3-tile) is the headline; loose radius shown on hover
      label:
        detection?.precision_strict !== undefined
          ? "Detection precision (strict)"
          : "Detection precision",
      value: detection
        ? fmtPct(detection.precision_strict ?? detection.precision)
        : "—",
      title:
        detection?.precision_strict !== undefined
          ? `loose 10-tile radius: ${fmtPct(detection.precision)}`
          : undefined,
    },
    {
      label: "Detection recall",
      value: detection ? fmtPct(detection.recall) : "—",
    },
    {
      label: "Detection latency",
      value: meanLatency !== null ? `${ticksToMinutes(meanLatency).toFixed(1)} min` : "—",
      title:
        meanLatency !== null
          ? `mean of ${detection?.latencies.length ?? 0} latency sample(s), in game time`
          : "no fires detected in this run",
    },
  ];

  return (
    <section className="stat-row" aria-label="Run summary">
      {stats.map((s) => (
        <div className="stat-tile" key={s.label} title={s.title}>
          <div className="stat-label">{s.label}</div>
          <div className="stat-value">{s.value}</div>
        </div>
      ))}
    </section>
  );
}
