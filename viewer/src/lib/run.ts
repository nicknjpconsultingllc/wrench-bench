/**
 * Pure run-directory assembly and step annotation. Shared between the browser
 * loader (`loader.ts`) and the Node smoke test.
 */
import type { LedgerEvent, RunData, TrajectoryStep } from "../types";
import { ParseError, parseLedger, parseMeta, parseSamples, parseTrajectory } from "./parse";

/** A file's name and text content, however it was obtained. */
export interface NamedText {
  name: string;
  text: string;
}

/**
 * Assemble a RunData from a bag of files. Only `trajectory.jsonl` is required;
 * meta, samples, and the ledger degrade gracefully when absent.
 */
export function assembleRun(name: string, files: NamedText[]): RunData {
  const byName = new Map(files.map((f) => [f.name, f]));

  const trajectoryFile = byName.get("trajectory.jsonl");
  if (!trajectoryFile) {
    throw new ParseError(
      "trajectory.jsonl not found — select or drop a WRENCH run directory " +
        "(it should contain trajectory.jsonl and, optionally, the ledger, " +
        "trajectory.meta.json, and samples.json)",
    );
  }
  const trajectory = parseTrajectory(trajectoryFile.text);

  const metaFile = byName.get("trajectory.meta.json");
  const meta = metaFile ? parseMeta(metaFile.text) : null;

  const samplesFile = byName.get("samples.json");
  const samples = samplesFile ? parseSamples(samplesFile.text) : null;

  // The ledger is named after the task key. Prefer meta's task; otherwise any
  // remaining .jsonl in the directory is the best candidate.
  const ledgerCandidates = files.filter(
    (f) => f.name.endsWith(".jsonl") && f.name !== "trajectory.jsonl",
  );
  const ledgerFile = meta
    ? (ledgerCandidates.find((f) => f.name === `${meta.task}.jsonl`) ?? ledgerCandidates[0])
    : ledgerCandidates[0];
  const ledger = ledgerFile ? parseLedger(ledgerFile.text, ledgerFile.name) : [];

  const taskKey =
    meta?.task ?? (ledgerFile ? ledgerFile.name.replace(/\.jsonl$/, "") : null);

  return { name, taskKey, trajectory, ledger, meta, samples };
}

/** Ledger events attributed to one step's execution interval. */
export interface StepEvents {
  fired: LedgerEvent[];
  faults: LedgerEvent[];
}

/**
 * Attribute fired / report_fault events to steps. A step's interval is
 * `(previous step's game_tick, this step's game_tick]`; events after the last
 * recorded tick attach to the final step.
 */
export function annotateSteps(
  steps: TrajectoryStep[],
  ledger: LedgerEvent[],
): StepEvents[] {
  return steps.map((step, i) => {
    const lower = i > 0 ? (steps[i - 1]?.game_tick ?? -Infinity) : -Infinity;
    const upper = i === steps.length - 1 ? Infinity : step.game_tick;
    const within = (e: LedgerEvent) => e.tick > lower && e.tick <= upper;
    return {
      fired: ledger.filter((e) => e.event === "fired" && within(e)),
      faults: ledger.filter((e) => e.event === "report_fault" && within(e)),
    };
  });
}
