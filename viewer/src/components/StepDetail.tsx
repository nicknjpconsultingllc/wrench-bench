/** Two-pane detail for a selected step: agent code and its observation. */
import type { TrajectoryStep } from "../types";
import type { StepEvents } from "../lib/run";
import { fmtElapsed, fmtInt } from "../lib/format";
import { CodeBlock } from "./CodeBlock";

interface StepDetailProps {
  step: TrajectoryStep;
  events: StepEvents | undefined;
  startTick: number;
}

export function StepDetail({ step, events, startTick }: StepDetailProps) {
  const produced = Object.entries(step.produced_counts);
  return (
    <div className="step-detail">
      <div className="step-detail-head">
        <h3>Step {step.step_index}</h3>
        <span className="step-meta">
          tick {fmtInt(step.game_tick)} · {fmtElapsed(step.game_tick - startTick)}
        </span>
        {events?.fired.map((e, i) => (
          <span key={`f${i}`} className="pill pill-fired">
            {e.kind ?? "disruption"} fired
          </span>
        ))}
        {events?.faults.map((_, i) => (
          <span key={`r${i}`} className="pill pill-fault">
            fault reported
          </span>
        ))}
      </div>
      {produced.length > 0 && (
        <div className="produced-row">
          {produced.map(([item, count]) => (
            <span key={item} className="pill">
              {item}: {fmtInt(count)}
            </span>
          ))}
        </div>
      )}
      <div className="panes">
        <section className="pane">
          <h4>Agent code</h4>
          <CodeBlock code={step.code} />
        </section>
        <section className="pane">
          <h4>Observation</h4>
          <pre className="response-block">{step.response}</pre>
        </section>
      </div>
    </div>
  );
}
