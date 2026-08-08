import { useEffect, useState } from "react";

/** One entry in public/runs/index.json (written by scripts/sync-runs.mjs). */
export interface RunIndexEntry {
  dir: string;
  task: string;
  model: string | null;
  steps: number | null;
  fires: number | null;
  timelapse: boolean;
  finishedAt: string;
}

function humanize(key: string): string {
  return key.replace(/_/g, " ");
}

function formatDate(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
}

export function RunIndex({ onPick }: { onPick: (dir: string) => void }) {
  const [entries, setEntries] = useState<RunIndexEntry[] | null | "missing">(
    null,
  );

  useEffect(() => {
    fetch("runs/index.json")
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((data: RunIndexEntry[]) =>
        setEntries(Array.isArray(data) ? data : "missing"),
      )
      .catch(() => setEntries("missing"));
  }, []);

  if (entries === null) return null;
  if (entries === "missing" || entries.length === 0) return null;

  return (
    <section className="run-index">
      <h2 className="run-index-title">Runs</h2>
      <p className="run-index-sub">
        {entries.length} run{entries.length === 1 ? "" : "s"}, newest first —
        select one to inspect
      </p>
      <ul className="run-index-list">
        {entries.map((e) => (
          <li key={e.dir}>
            <button
              type="button"
              className="run-index-row"
              onClick={() => onPick(e.dir)}
            >
              <span className="run-index-task">{humanize(e.task)}</span>
              <span className="run-index-meta">
                {e.model ?? "unknown model"}
                {e.steps !== null && ` · ${e.steps} steps`}
                {e.fires !== null && ` · ${e.fires} fire${e.fires === 1 ? "" : "s"}`}
                {e.timelapse && " · 🎬"}
              </span>
              <span className="run-index-date">{formatDate(e.finishedAt)}</span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
