/**
 * Horizontal strip of step chips. Steps during which a disruption fired carry
 * a red badge; steps where the agent reported a fault carry a violet badge.
 */
import type { TrajectoryStep } from "../types";
import type { StepEvents } from "../lib/run";

interface StepTimelineProps {
  steps: TrajectoryStep[];
  annotations: StepEvents[];
  selected: number;
  onSelect: (index: number) => void;
}

export function StepTimeline({ steps, annotations, selected, onSelect }: StepTimelineProps) {
  return (
    <div className="timeline" role="listbox" aria-label="Agent steps">
      {steps.map((step, i) => {
        const ann = annotations[i];
        const fired = (ann?.fired.length ?? 0) > 0;
        const fault = (ann?.faults.length ?? 0) > 0;
        const labels = [
          `Step ${step.step_index}`,
          fired ? "disruption fired during this step" : null,
          fault ? "agent reported a fault" : null,
        ].filter(Boolean);
        return (
          <button
            key={step.step_index}
            type="button"
            role="option"
            aria-selected={i === selected}
            className={`step-chip${i === selected ? " is-selected" : ""}`}
            title={labels.join(" · ")}
            onClick={() => onSelect(i)}
          >
            {step.step_index}
            {fired && <span className="badge badge-fired" aria-hidden="true" />}
            {fault && <span className="badge badge-fault" aria-hidden="true" />}
          </button>
        );
      })}
    </div>
  );
}
