/**
 * Browser-side run loading: from picked/dropped files and from a `?run=` URL
 * served next to the app (e.g. `viewer/public/runs/<run_dir>` in dev).
 */
import type { RunData } from "../types";
import { parseMeta } from "./parse";
import { assembleRun, type NamedText } from "./run";

/** Build a run from File objects (directory picker or drag-and-drop). */
export async function runFromFiles(
  files: File[],
  dirName: string | null,
): Promise<RunData> {
  const named: NamedText[] = await Promise.all(
    files.map(async (f) => ({ name: f.name, text: await f.text() })),
  );
  const name = dirName ?? inferDirName(files) ?? "local run";
  return assembleRun(name, named);
}

/** First path segment of webkitRelativePath, when the directory picker set it. */
function inferDirName(files: File[]): string | null {
  for (const f of files) {
    const rel = f.webkitRelativePath;
    if (rel) {
      const dir = rel.split("/")[0];
      if (dir) return dir;
    }
  }
  return null;
}

/**
 * Extract Files from a drop. Directories are walked one level deep via the
 * webkitGetAsEntry API; plain file drops pass through unchanged.
 */
export async function filesFromDrop(
  dt: DataTransfer,
): Promise<{ files: File[]; dirName: string | null }> {
  const entries = Array.from(dt.items)
    .map((item) => item.webkitGetAsEntry?.())
    .filter((e): e is FileSystemEntry => e != null);

  if (entries.length === 0) {
    return { files: Array.from(dt.files), dirName: null };
  }

  const files: File[] = [];
  let dirName: string | null = null;
  for (const entry of entries) {
    if (entry.isDirectory) {
      dirName ??= entry.name;
      for (const child of await readAllEntries(entry as FileSystemDirectoryEntry)) {
        if (child.isFile) files.push(await entryFile(child as FileSystemFileEntry));
      }
    } else if (entry.isFile) {
      files.push(await entryFile(entry as FileSystemFileEntry));
    }
  }
  return { files, dirName };
}

function readAllEntries(dir: FileSystemDirectoryEntry): Promise<FileSystemEntry[]> {
  const reader = dir.createReader();
  return new Promise((resolve, reject) => {
    const all: FileSystemEntry[] = [];
    const readBatch = () => {
      reader.readEntries((batch) => {
        if (batch.length === 0) resolve(all);
        else {
          all.push(...batch);
          readBatch(); // readEntries returns results in batches; drain them all
        }
      }, reject);
    };
    readBatch();
  });
}

function entryFile(entry: FileSystemFileEntry): Promise<File> {
  return new Promise((resolve, reject) => entry.file(resolve, reject));
}

/** Fetch a text file; treat 404s and SPA index.html fallbacks as absent. */
async function tryFetch(url: string): Promise<string | null> {
  try {
    const res = await fetch(url);
    if (!res.ok) return null;
    const text = await res.text();
    // Dev servers answer missing paths with index.html — not run data.
    if (text.trimStart().startsWith("<")) return null;
    return text;
  } catch {
    return null;
  }
}

/**
 * Load a run from a path served next to the app, e.g.
 * `?run=runs/iron_plate_sentinel_sonnet_1786200726`.
 */
export async function fetchRun(path: string): Promise<RunData> {
  const base = path.replace(/\/+$/, "");
  const name = base.split("/").pop() ?? base;
  const named: NamedText[] = [];

  const trajectory = await tryFetch(`${base}/trajectory.jsonl`);
  if (trajectory === null) {
    throw new Error(
      `Could not fetch ${base}/trajectory.jsonl — serve the run directory next to ` +
        "the app (in dev, copy or symlink it under viewer/public/).",
    );
  }
  named.push({ name: "trajectory.jsonl", text: trajectory });

  const metaText = await tryFetch(`${base}/trajectory.meta.json`);
  if (metaText !== null) named.push({ name: "trajectory.meta.json", text: metaText });

  const samplesText = await tryFetch(`${base}/samples.json`);
  if (samplesText !== null) named.push({ name: "samples.json", text: samplesText });

  // The ledger file is named after the task key, which meta tells us.
  if (metaText !== null) {
    let task: string | null = null;
    try {
      task = parseMeta(metaText).task;
    } catch {
      task = null;
    }
    if (task) {
      const ledgerText = await tryFetch(`${base}/${task}.jsonl`);
      if (ledgerText !== null) named.push({ name: `${task}.jsonl`, text: ledgerText });
    }
  }

  return assembleRun(name, named);
}
