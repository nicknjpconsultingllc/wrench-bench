/**
 * Node-side smoke test: parse a real run directory with the viewer's typed
 * parsers and report what they saw. Proves src/types.ts matches reality.
 *
 *   npm run smoke                      # default: the sonnet pilot run
 *   npm run smoke -- <run_dir> [...]   # any run directories
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { basename, join, resolve } from "node:path";
import { assembleRun, annotateSteps, type NamedText } from "../src/lib/run";
import { deriveRates } from "../src/lib/rates";
import { fireMetricsOf } from "../src/types";

const DEFAULT_RUN = resolve(
  import.meta.dirname,
  "../../pilot_runs/iron_plate_sentinel_sonnet_1786200726",
);

function loadDir(dir: string): NamedText[] {
  return readdirSync(dir)
    .filter((name) => {
      const path = join(dir, name);
      return statSync(path).isFile() && /\.(jsonl?|json)$/.test(name);
    })
    .map((name) => ({ name, text: readFileSync(join(dir, name), "utf8") }));
}

function checkRun(dir: string): void {
  const name = basename(dir);
  console.log(`\n=== ${name} ===`);
  const run = assembleRun(name, loadDir(dir));

  console.log(`  trajectory : ${run.trajectory.length} steps`);
  const firstStep = run.trajectory[0];
  const lastStep = run.trajectory[run.trajectory.length - 1];
  if (firstStep && lastStep) {
    console.log(
      `               ticks ${firstStep.game_tick} -> ${lastStep.game_tick}, ` +
        `step 0 code ${firstStep.code.length} chars, response ${firstStep.response.length} chars`,
    );
  }

  const eventCounts = new Map<string, number>();
  for (const e of run.ledger) {
    eventCounts.set(e.event, (eventCounts.get(e.event) ?? 0) + 1);
  }
  console.log(
    `  ledger     : ${run.ledger.length} events` +
      (run.ledger.length > 0
        ? ` (${[...eventCounts.entries()].map(([k, v]) => `${k}=${v}`).join(", ")})`
        : " (no ledger file)"),
  );

  if (run.meta) {
    const fires = fireMetricsOf(run.meta);
    console.log(
      `  meta       : task=${run.meta.task} model=${run.meta.model} ` +
        `steps=${run.meta.steps} fires=${run.meta.fires}` +
        (run.meta.detection
          ? ` precision=${run.meta.detection.precision} recall=${run.meta.detection.recall}` +
            ` latencies=[${run.meta.detection.latencies.join(", ")}]`
          : " (no detection block)"),
    );
    for (const [i, fire] of fires.entries()) {
      console.log(
        `               fire_${i}: kind=${fire.kind} tick=${fire.tick} ` +
          `baseline=${fire.baseline} TR=${fire.TR} recovered=${fire.recovered}`,
      );
    }
  } else {
    console.log("  meta       : absent");
  }

  console.log(
    run.samples
      ? `  samples    : ${run.samples.length} rows`
      : "  samples    : absent",
  );

  const rates = deriveRates(run);
  console.log(
    `  rates      : source=${rates.source} series=[${rates.series
      .map((s) => `${s.item} x${s.points.length}`)
      .join(", ")}]`,
  );
  const primary = rates.series[0];
  if (primary) {
    const peak = Math.max(...primary.points.map((p) => p.rate));
    const sample = primary.points
      .slice(0, 3)
      .map((p) => `${p.rate}@${p.tick}`)
      .join(", ");
    console.log(`               ${primary.item}: peak ${peak}/min, first points: ${sample}`);
  }

  const annotations = annotateSteps(run.trajectory, run.ledger);
  const flagged = annotations
    .map((a, i) => ({ i, a }))
    .filter(({ a }) => a.fired.length > 0 || a.faults.length > 0)
    .map(({ i, a }) => `step ${i} (fired=${a.fired.length}, faults=${a.faults.length})`);
  console.log(
    `  steps flagged: ${flagged.length > 0 ? flagged.join("; ") : "none"}`,
  );
}

const dirs = process.argv.slice(2).map((d) => resolve(d));
if (dirs.length === 0) dirs.push(DEFAULT_RUN);

let failures = 0;
for (const dir of dirs) {
  try {
    checkRun(dir);
  } catch (err) {
    failures++;
    console.error(`\n=== ${basename(dir)} ===`);
    console.error(`  FAILED: ${err instanceof Error ? err.message : String(err)}`);
  }
}

console.log(
  failures === 0
    ? `\nSmoke test passed for ${dirs.length} run(s).`
    : `\nSmoke test FAILED for ${failures} of ${dirs.length} run(s).`,
);
process.exit(failures === 0 ? 0 : 1);
