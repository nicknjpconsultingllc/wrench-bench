# WRENCH trajectory viewer

A single-page viewer for WRENCH benchmark runs — the LLM-agent disruption-recovery
benchmark on Factorio in this repository. Load a run directory (drag-and-drop, the
folder picker, or `?run=` when the run is served next to the app) and it replays the
whole episode client-side: summary tiles from `trajectory.meta.json` (task, model,
steps, fires, detection precision/recall/latency); a hand-rolled SVG production
chart (items per minute, derived from `samples.json` cumulative counts over a
trailing 60 s window, or — when `samples.json` is absent — from the per-step
throughput reports embedded in agent observations) with hoverable vertical markers
for every disruption `fired` and agent `report_fault` event; and a step timeline
whose selected step shows the agent's syntax-highlighted Python next to the
observation it got back. Steps during which a disruption fired are badged. Nothing
is uploaded; all four run files are parsed and validated in the browser against the
typed interfaces in `src/types.ts`.

Built with Vite + React + strict TypeScript, no charting or highlighting
dependencies. Run it with `npm install` then `npm run dev` (production bundle:
`npm run build`, output in `dist/`). For the `?run=` path, copy or symlink a run
directory under `viewer/public/runs/` and open
`http://localhost:5173/?run=runs/<run_dir>` — for example
`ln -s ../../pilot_runs/iron_plate_sentinel_sonnet_1786200726 public/runs/demo`
then `?run=runs/demo`. `npm run smoke -- <run_dir>` parses real run directories
with the same typed parsers the app uses, as a Node-side check that the interfaces
match the data on disk.
