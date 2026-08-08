import { useEffect, useMemo, useState } from "react";
import type { RunData } from "./types";
import { deriveRates } from "./lib/rates";
import { annotateSteps } from "./lib/run";
import { fetchRun, runFromFiles } from "./lib/loader";
import { DropZone } from "./components/DropZone";
import { RunIndex } from "./components/RunIndex";
import { RunHeader } from "./components/RunHeader";
import { ProductionChart } from "./components/ProductionChart";
import { StepTimeline } from "./components/StepTimeline";
import { StepDetail } from "./components/StepDetail";

type LoadState =
  | { kind: "idle"; error: string | null }
  | { kind: "loading" }
  | { kind: "loaded"; run: RunData };

export default function App() {
  const [state, setState] = useState<LoadState>({ kind: "idle", error: null });

  const load = (promise: Promise<RunData>) => {
    setState({ kind: "loading" });
    promise
      .then((run) => setState({ kind: "loaded", run }))
      .catch((err: unknown) =>
        setState({
          kind: "idle",
          error: err instanceof Error ? err.message : String(err),
        }),
      );
  };

  // ?run=runs/<run_dir> fetches files served next to the app. Trailing
  // punctuation is stripped: URLs pasted from prose often pick up a period.
  useEffect(() => {
    const raw = new URLSearchParams(window.location.search).get("run");
    const runPath = raw?.replace(/[.,/\s]+$/, "");
    if (runPath) load(fetchRun(runPath));
  }, []);

  const pickRun = (dir: string) => {
    const url = new URL(window.location.href);
    url.searchParams.set("run", `runs/${dir}`);
    window.history.replaceState(null, "", url);
    load(fetchRun(`runs/${dir}`));
  };

  return (
    <div className="app">
      <header className="topbar">
        <h1>
          WRENCH <span className="topbar-sub">trajectory viewer</span>
        </h1>
        {state.kind === "loaded" && (
          <div className="topbar-right">
            <span className="run-name">{state.run.name}</span>
            <button
              type="button"
              className="ghost-button"
              onClick={() => {
                const url = new URL(window.location.href);
                url.searchParams.delete("run");
                window.history.replaceState(null, "", url);
                setState({ kind: "idle", error: null });
              }}
            >
              Load another run
            </button>
          </div>
        )}
      </header>

      {state.kind === "loaded" ? (
        <RunView key={state.run.name} run={state.run} />
      ) : (
        <main className="landing">
          <p className="landing-blurb">
            WRENCH is a disruption-recovery benchmark for LLM agents playing Factorio.
            This viewer replays a benchmark run: production over game time, when the
            harness sabotaged the factory, when the agent noticed, and what code it
            wrote at every step.
          </p>
          <RunIndex onPick={pickRun} />
          <DropZone
            onFiles={(files, dirName) => load(runFromFiles(files, dirName))}
            error={state.kind === "idle" ? state.error : null}
            loading={state.kind === "loading"}
          />
        </main>
      )}
    </div>
  );
}

function RunView({ run }: { run: RunData }) {
  const derivation = useMemo(() => deriveRates(run), [run]);
  const annotations = useMemo(
    () => annotateSteps(run.trajectory, run.ledger),
    [run],
  );
  const [selectedItem, setSelectedItem] = useState<string | null>(null);
  const [selectedStep, setSelectedStep] = useState(0);

  const startTick = useMemo(() => {
    const candidates = [
      run.trajectory[0]?.game_tick,
      run.samples?.[0]?.tick,
      derivation.series[0]?.points[0]?.tick,
    ].filter((t): t is number => t !== undefined);
    return candidates.length > 0 ? Math.min(...candidates) : 0;
  }, [run, derivation]);

  const step = run.trajectory[selectedStep];

  return (
    <main>
      <RunHeader run={run} />
      <ProductionChart
        derivation={derivation}
        selectedItem={selectedItem}
        onSelectItem={setSelectedItem}
        events={run.ledger}
        startTick={startTick}
      />
      <section className="card">
        <div className="card-head">
          <div>
            <h2>Step timeline</h2>
            <p className="card-sub">
              {run.trajectory.length} agent steps — select one to inspect its code and
              observation
            </p>
          </div>
        </div>
        <StepTimeline
          steps={run.trajectory}
          annotations={annotations}
          selected={selectedStep}
          onSelect={setSelectedStep}
        />
        {step && (
          <StepDetail
            step={step}
            events={annotations[selectedStep]}
            startTick={startTick}
          />
        )}
      </section>
    </main>
  );
}
