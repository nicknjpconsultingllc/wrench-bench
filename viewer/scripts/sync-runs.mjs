// Sync pilot runs into the viewer's public dir and write an index manifest.
// Dev flow: `npm run sync-runs` (also runs automatically before `npm run dev`).
// Creates public/runs/<run_dir> symlinks and public/runs/index.json, which the
// app's landing page lists (newest first).

import { promises as fs } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const viewerRoot = path.resolve(here, "..");
const runsSource = path.resolve(viewerRoot, "..", "pilot_runs");
const publicRuns = path.join(viewerRoot, "public", "runs");

async function readJson(file) {
  try {
    return JSON.parse(await fs.readFile(file, "utf8"));
  } catch {
    return null;
  }
}

async function main() {
  await fs.mkdir(publicRuns, { recursive: true });
  let sourceDirs = [];
  try {
    sourceDirs = (await fs.readdir(runsSource, { withFileTypes: true }))
      .filter((d) => d.isDirectory())
      .map((d) => d.name);
  } catch {
    console.error(`no run source dir at ${runsSource}`);
  }

  const index = [];
  for (const dir of sourceDirs) {
    const src = path.join(runsSource, dir);
    const trajectory = path.join(src, "trajectory.jsonl");
    try {
      const stat = await fs.stat(trajectory);
      if (stat.size === 0) continue; // empty/crashed runs
      const link = path.join(publicRuns, dir);
      await fs.rm(link, { force: true });
      await fs.symlink(path.relative(publicRuns, src), link);
      const meta = await readJson(path.join(src, "trajectory.meta.json"));
      index.push({
        dir,
        task: meta?.task ?? dir.replace(/_[a-z0-9]+_\d+$/, ""),
        model: meta?.model ?? null,
        steps: meta?.steps ?? null,
        fires: meta?.fires ?? null,
        timelapse: await fs
          .stat(path.join(src, "timelapse.mp4"))
          .then(() => true)
          .catch(() => false),
        // trajectory mtime approximates run completion time
        finishedAt: stat.mtime.toISOString(),
      });
    } catch {
      // no trajectory -> not a run dir
    }
  }

  index.sort((a, b) => (a.finishedAt < b.finishedAt ? 1 : -1));
  await fs.writeFile(
    path.join(publicRuns, "index.json"),
    JSON.stringify(index, null, 2),
  );
  console.log(`indexed ${index.length} runs -> public/runs/index.json`);
}

await main();
