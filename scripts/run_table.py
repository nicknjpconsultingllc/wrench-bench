"""Build the WRENCH comparison table: N models x sentinel tasks x seeds.

Runs one Inspect episode per (model, task, seed) through the WRENCH eval
path (fle.eval.inspect.wrench) and writes a results table (JSON + markdown)
to <outdir>/<timestamp>/.

Per-seed variation adds the seed offset (0..seeds-1) to every
DisruptionSpec seed in a task's config (DisruptionRecoveryTask.with_seed_offset);
seed 0 is the canonical registry configuration.

Aggregation uses the pooled-ratio rule (see fle/disruptions/scoring.py):
per-(model, task) metrics are sums of raw numerators over sums of raw
denominators across seeds and fires -- never means of per-episode ratios.

Usage:
    python scripts/run_table.py \
        --models anthropic/claude-sonnet-4-5,openai/gpt-5-mini \
        --seeds 3 \
        [--limit-tasks iron_plate_sentinel] \
        [--trajectory-length 32] \
        [--max-connections 4]

Requires provider API keys in the environment / .env
(ANTHROPIC_API_KEY, OPENAI_API_KEY, ...). Use model "mockllm/model" for a
plumbing smoke test without API keys.
"""

import argparse
import json
import math
import sys
import time
from collections import defaultdict
from pathlib import Path

# Ensure the repo root is importable when run as `python scripts/run_table.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fle.disruptions.scoring import winsorize_tr  # noqa: E402
from fle.eval.tasks.task_definitions.disruption.sentinel_tasks import (  # noqa: E402
    DISRUPTION_TASKS,
)

# Sourced from the registry so this can't go stale as task families are added
# (this list predated the observability/adaptive/scarcity families and had to
# be caught and fixed before the first real published-table run).
TASK_KEYS = list(DISRUPTION_TASKS.keys())

TR_SCORER = "throughput_retained_scorer"
RECOVERY_SCORER = "recovery_scorer"
DETECTION_SCORER = "detection_scorer"


def _num(value):
    """NaN-safe float -> float | None (NaN is 'not scoreable')."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _fmt(value, digits=3):
    return "-" if value is None else f"{value:.{digits}f}"


def _score_meta(sample, scorer_name):
    score = (sample.scores or {}).get(scorer_name)
    if score is None:
        return None, {}
    return _num(score.value), (score.metadata or {})


def collect_episode_rows(logs):
    """One row per (model, task, seed) episode from the eval logs."""
    from inspect_ai.log import read_eval_log

    rows = []
    for log in logs:
        if log.samples is None:
            log = read_eval_log(log.location)
        model = str(log.eval.model)
        task_name = log.eval.task
        if log.status != "success":
            rows.append(
                {
                    "model": model,
                    "task": task_name,
                    "seed": None,
                    "status": log.status,
                    "error": str(log.error.message) if log.error else None,
                }
            )
            continue
        for sample in log.samples or []:
            meta = sample.metadata or {}
            # wrench_solver's own try/except always returns `state` normally
            # rather than re-raising -- so a WRENCH-internal failure (a
            # server-pool timeout, an instance-creation error, anything
            # caught by solve()'s outer except) never sets sample.error, the
            # only thing this function used to check. It DOES set
            # WrenchData.error (wrench.py, store_as(WrenchData)), which
            # lands in the serialized sample.store under "WrenchData:error".
            # Without checking it too, an infra-level failure is
            # indistinguishable from a genuine 0-fire success -- exactly how
            # the pool-exhaustion bug went unnoticed in an earlier run: every
            # failed episode still said "success" here. Check both.
            store = getattr(sample, "store", None) or {}
            wrench_error = (
                store.get("WrenchData:error") if hasattr(store, "get") else None
            )
            episode_error = sample.error or wrench_error
            tr_value, tr_meta = _score_meta(sample, TR_SCORER)
            rec_value, rec_meta = _score_meta(sample, RECOVERY_SCORER)
            det_value, det_meta = _score_meta(sample, DETECTION_SCORER)
            latencies = det_meta.get("latencies") or []
            ep_tr_num = tr_meta.get("pooled_numerator", 0.0)
            ep_tr_den = tr_meta.get("pooled_denominator", 0.0)
            ep_floor_num = tr_meta.get("floor_adjusted_pooled_numerator", 0.0)
            ep_floor_den = tr_meta.get("floor_adjusted_pooled_denominator", 0.0)
            rows.append(
                {
                    "model": model,
                    "task": meta.get("task_key", task_name),
                    "seed": meta.get("seed_offset"),
                    "status": "error" if episode_error else "success",
                    "error": str(episode_error) if episode_error else None,
                    "fires": tr_meta.get("num_fires", 0),
                    "throughput_retained": tr_value,
                    # Raw (un-winsorized) pooled TR ratio for this episode:
                    # num/den before winsorize_tr() clamps it to
                    # [-0.5, 1.5]. See fle/disruptions/scoring.py
                    # throughput_retained_parts docstring -- kept unclamped
                    # so dramatic overbuild (TR > 1.5) stays visible instead
                    # of pegging at the winsorize cap like the headline TR
                    # does (docs/failure_taxonomy.md finding V4). None when
                    # the episode isn't scoreable (no fires with a valid
                    # frozen baseline), matching throughput_retained's None
                    # policy.
                    "throughput_retained_raw": (
                        ep_tr_num / ep_tr_den if ep_tr_den > 0 else None
                    ),
                    "tr_numerator": ep_tr_num,
                    "tr_denominator": ep_tr_den,
                    # Redundancy-floor-adjusted TR (see
                    # fle/disruptions/scoring.py
                    # floor_adjusted_throughput_retained_parts): additive
                    # alongside TR/TR (raw) above, never replacing them.
                    # Only defined for entity_destruction fires carrying a
                    # same_type_total redundancy count -- None when the
                    # episode has no such fire (e.g. nothing armed, or only
                    # other disruption kinds), matching the None policy the
                    # plain TR columns already use.
                    "throughput_retained_floor_adj": (
                        winsorize_tr(ep_floor_num / ep_floor_den)
                        if ep_floor_den > 0
                        else None
                    ),
                    "tr_floor_numerator": ep_floor_num,
                    "tr_floor_denominator": ep_floor_den,
                    "recovery_rate": rec_value,
                    "recovered": rec_meta.get("recovered", 0),
                    "scoreable_fires": rec_meta.get("scoreable_fires", 0),
                    "detection_recall": _num(det_meta.get("recall")),
                    "detection_precision": _num(det_meta.get("precision")),
                    "detection_precision_strict": _num(
                        det_meta.get("precision_strict")
                    ),
                    "detection_latencies": latencies,
                    "matched_reports": det_meta.get("matched_reports", 0),
                    "matched_reports_strict": det_meta.get("matched_reports_strict", 0),
                    "num_reports": det_meta.get("num_reports", 0),
                    "matched_fires": det_meta.get("matched_fires", 0),
                    "num_fires": det_meta.get("num_fires", 0),
                }
            )
    return rows


def aggregate_rows(rows):
    """Pooled per-(model, task) aggregates: sum numerators / sum denominators."""
    groups = defaultdict(list)
    for row in rows:
        if row.get("seed") is None and row.get("status") != "success":
            continue  # whole-log failure; surfaced in the episode table
        groups[(row["model"], row["task"])].append(row)

    aggregates = []
    for (model, task_name), episodes in sorted(groups.items()):
        ok = [e for e in episodes if e["status"] == "success"]
        tr_num = sum(e["tr_numerator"] for e in ok)
        tr_den = sum(e["tr_denominator"] for e in ok)
        tr_floor_num = sum(e["tr_floor_numerator"] for e in ok)
        tr_floor_den = sum(e["tr_floor_denominator"] for e in ok)
        recovered = sum(e["recovered"] for e in ok)
        scoreable = sum(e["scoreable_fires"] for e in ok)
        matched_reports = sum(e["matched_reports"] for e in ok)
        matched_strict = sum(e["matched_reports_strict"] for e in ok)
        num_reports = sum(e["num_reports"] for e in ok)
        matched_fires = sum(e["matched_fires"] for e in ok)
        num_fires = sum(e["num_fires"] for e in ok)
        latencies = [lat for e in ok for lat in e["detection_latencies"]]
        aggregates.append(
            {
                "model": model,
                "task": task_name,
                "episodes": len(episodes),
                "episodes_ok": len(ok),
                "fires": num_fires,
                "throughput_retained": (
                    winsorize_tr(tr_num / tr_den) if tr_den > 0 else None
                ),
                # Raw (un-winsorized) pooled TR ratio across all episodes in
                # this (model, task) group -- see the per-episode comment in
                # collect_episode_rows for why this is worth surfacing
                # alongside the winsorized headline TR.
                "throughput_retained_raw": (tr_num / tr_den if tr_den > 0 else None),
                "tr_numerator": tr_num,
                "tr_denominator": tr_den,
                # Redundancy-floor-adjusted TR, pooled across episodes the
                # same sum-numerator/sum-denominator way as TR/TR (raw)
                # above -- see the per-episode comment in
                # collect_episode_rows and
                # fle/disruptions/scoring.py:floor_adjusted_throughput_retained_parts.
                "throughput_retained_floor_adj": (
                    winsorize_tr(tr_floor_num / tr_floor_den)
                    if tr_floor_den > 0
                    else None
                ),
                "tr_floor_numerator": tr_floor_num,
                "tr_floor_denominator": tr_floor_den,
                "recovery_rate": recovered / scoreable if scoreable else None,
                "recovered": recovered,
                "scoreable_fires": scoreable,
                "detection_recall": (matched_fires / num_fires if num_fires else 1.0),
                "detection_precision": (
                    matched_reports / num_reports if num_reports else 1.0
                ),
                "detection_precision_strict": (
                    matched_strict / num_reports if num_reports else 1.0
                ),
                "mean_detection_latency_ticks": (
                    sum(latencies) / len(latencies) if latencies else None
                ),
            }
        )
    return aggregates


def write_markdown(path: Path, aggregates, rows, args):
    lines = [
        "# WRENCH comparison table",
        "",
        f"- models: {', '.join(args.models)}",
        f"- tasks: {', '.join(args.task_keys)}",
        f"- seeds per (model, task): {args.seeds}",
        f"- generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "Aggregates use the pooled-ratio rule: sum of raw numerators over sum",
        "of raw denominators across seeds/fires (never mean-of-ratios).",
        "`-` = not scoreable (no fires with a valid frozen baseline).",
        "TR is winsorized to [-0.5, 1.5] (see fle/disruptions/scoring.py) and",
        "is the headline metric. TR (raw) is the same pooled ratio before",
        "that clamp -- it can exceed 1.5 when recovery dramatically",
        "overbuilds past the pre-disruption baseline, which TR alone cannot",
        "show once it pegs at the cap (docs/failure_taxonomy.md finding V4).",
        "TR (floor-adj) subtracts a passive-redundancy floor (fixed at fire",
        "time, non-manipulable -- see",
        "fle/disruptions/scoring.py:floor_adjusted_throughput_retained_parts)",
        "from both TR's numerator and denominator, isolating the agent's own",
        "recovery contribution. Only defined for entity_destruction fires; `-`",
        "elsewhere (e.g. no entity_destruction fire this episode/group).",
        "",
        "## Per-(model, task) aggregates",
        "",
        "| Model | Task | Episodes | Fires | TR | TR (raw) | TR (floor-adj) | Recovery | Det. recall "
        "| Det. precision (strict, 3-tile) | Det. precision (loose, 10-tile) | Det. latency (ticks) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for a in aggregates:
        lines.append(
            f"| {a['model']} | {a['task']} | {a['episodes_ok']}/{a['episodes']} "
            f"| {a['fires']} | {_fmt(a['throughput_retained'])} "
            f"| {_fmt(a['throughput_retained_raw'])} "
            f"| {_fmt(a['throughput_retained_floor_adj'])} "
            f"| {_fmt(a['recovery_rate'], 2)} | {_fmt(a['detection_recall'], 2)} "
            f"| {_fmt(a['detection_precision_strict'], 2)} "
            f"| {_fmt(a['detection_precision'], 2)} "
            f"| {_fmt(a['mean_detection_latency_ticks'], 0)} |"
        )
    lines += [
        "",
        "## Per-episode results",
        "",
        "| Model | Task | Seed | Status | Fires | TR | TR (raw) | TR (floor-adj) | Recovery | Det. recall |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['model']} | {r['task']} | {r.get('seed', '-')} "
            f"| {r['status']} | {r.get('fires', '-')} "
            f"| {_fmt(r.get('throughput_retained'))} "
            f"| {_fmt(r.get('throughput_retained_raw'))} "
            f"| {_fmt(r.get('throughput_retained_floor_adj'))} "
            f"| {_fmt(r.get('recovery_rate'), 2)} "
            f"| {_fmt(r.get('detection_recall'), 2)} |"
        )
    path.write_text("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(
        description="Run the WRENCH model-comparison table via Inspect."
    )
    ap.add_argument(
        "--models",
        required=True,
        help="Comma-separated Inspect model names "
        "(e.g. anthropic/claude-sonnet-4-5,openai/gpt-5-mini)",
    )
    ap.add_argument("--seeds", type=int, default=3, help="Seeds per (model, task)")
    ap.add_argument(
        "--limit-tasks",
        default=None,
        help=f"Comma-separated subset of {TASK_KEYS}",
    )
    ap.add_argument(
        "--trajectory-length",
        type=int,
        default=None,
        help="Override episode length in steps (default: task config)",
    )
    ap.add_argument("--outdir", default="table_runs")
    ap.add_argument(
        "--resume",
        default=None,
        help="Path to an existing run dir (e.g. table_runs/20260809T160623) to "
        "resume into instead of starting a fresh timestamped dir. Every "
        "invocation without this flag mints a brand-new log_dir, and "
        "eval_set's resume/retry scan only kicks in when log_dir is reused "
        "across runs -- so a crash/restart with the plain command silently "
        "re-runs (and re-pays for) every episode, completed or not. Pass "
        "the run_dir printed by the interrupted run to pick up only the "
        "unfinished (model, task, seed) combinations.",
    )
    ap.add_argument(
        "--max-connections", type=int, default=4, help="Max concurrent model calls"
    )
    ap.add_argument(
        "--max-samples",
        type=int,
        default=None,
        help="Max concurrent episodes (bound by available Factorio containers)",
    )
    args = ap.parse_args()

    args.models = [m.strip() for m in args.models.split(",") if m.strip()]
    if args.limit_tasks:
        args.task_keys = [t.strip() for t in args.limit_tasks.split(",") if t.strip()]
        unknown = set(args.task_keys) - set(TASK_KEYS)
        if unknown:
            ap.error(f"Unknown task(s): {sorted(unknown)}. Choose from {TASK_KEYS}")
    else:
        args.task_keys = list(TASK_KEYS)

    # Imports deferred so --help stays fast and argparse errors don't need FLE.
    from inspect_ai import eval_set

    from fle.eval.inspect.wrench import create_wrench_task

    if args.resume:
        run_dir = Path(args.resume)
        if not run_dir.is_dir():
            ap.error(f"--resume path does not exist or is not a directory: {run_dir}")
        resuming = True
    else:
        run_dir = Path(args.outdir) / time.strftime("%Y%m%dT%H%M%S")
        resuming = False
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "ledgers").mkdir(exist_ok=True)
    import os

    os.environ.setdefault("WRENCH_LEDGER_DIR", str(run_dir / "ledgers"))

    tasks = [
        create_wrench_task(
            tk,
            seeds=args.seeds,
            trajectory_length=args.trajectory_length,
            name=tk,
        )
        for tk in args.task_keys
    ]

    print(
        f"{'Resuming' if resuming else 'Running'} {len(args.models)} model(s) "
        f"x {len(args.task_keys)} task(s) x {args.seeds} seed(s) -> {run_dir}"
    )
    if resuming:
        print(
            "Resume mode: eval_set will scan the existing log_dir and skip "
            "(model, task, seed) combinations that already have a completed "
            "log there -- only unfinished ones will make new model calls."
        )
    eval_kwargs = dict(
        tasks=tasks,
        model=args.models,
        log_dir=str(run_dir / "logs"),
        max_connections=args.max_connections,
        fail_on_error=False,
    )
    if args.max_samples is not None:
        eval_kwargs["max_samples"] = args.max_samples
    success, logs = eval_set(**eval_kwargs)
    if not success:
        print(
            "WARNING: some evals did not complete successfully; "
            "partial results follow (re-run to retry)."
        )

    rows = collect_episode_rows(logs)
    aggregates = aggregate_rows(rows)

    results = {
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "models": args.models,
        "tasks": args.task_keys,
        "seeds": args.seeds,
        "trajectory_length": args.trajectory_length,
        "log_dir": str(run_dir / "logs"),
        "pooling": "sum of raw numerators / sum of raw denominators "
        "across seeds and fires (see fle/disruptions/scoring.py)",
        "episodes": rows,
        "aggregates": aggregates,
    }
    json_path = run_dir / "results.json"
    json_path.write_text(json.dumps(results, indent=2, default=str))
    md_path = run_dir / "results.md"
    write_markdown(md_path, aggregates, rows, args)

    print(f"\nWrote {json_path}\nWrote {md_path}\n")
    print(md_path.read_text())


if __name__ == "__main__":
    main()
