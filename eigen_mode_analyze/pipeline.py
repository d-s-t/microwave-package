"""
The end-to-end client pipeline: build geometry for every point in a
parameter sweep, queue each one for analysis, wait for and monitor the
worker's progress (printing to the screen as each finishes), then plot
the combined results -- all resumable from wherever it left off. Runs
standalone from the command line, or driven by the web UI (webapp.py),
which calls `run_pipeline()` directly in a background thread.

    python -m eigen_mode_analyze.pipeline --sweep pedestal_depth=0:10:2
    python -m eigen_mode_analyze.pipeline --resume run_20260819_140102
"""
from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

from ansys_analyze_common import queue_common

from . import equations as eq
from . import model_builder, paths, plotting, postprocess, resilient_io, results, step_generation
from .monitor import monitor_run
from .run_manager import Run
from .sweep import SweepParameter, expand_values

logger = logging.getLogger("eigen_mode_analyze")


def run_pipeline(run: Run, queue: "queue_common.QueuePaths", log=logger.info, stop_event=None) -> None:
    """Runs whichever stages `run` hasn't already completed. Safe to call
    again on a `run` that was interrupted mid-stage -- every stage skips
    combos it already finished."""
    equations_path = str(paths.DEFAULT_EQUATIONS_FILE)
    assembly_path = str(paths.DEFAULT_ASSEMBLY_FILE)
    analysis_settings = run.config["analysis_settings"]
    combos = {cid: c["params"] for cid, c in run.state["combos"].items()}

    log("Stage 1/5: generating STEP files...")
    run.set_stage("generating_steps")
    resilient_io.call_with_retry(
        step_generation.generate_step_files, combos, equations_path, assembly_path, run, log=log,
    )
    if stop_event is not None and stop_event.is_set():
        return

    log("Stage 2/5: building AEDT models, creating setups, and queuing analysis tasks...")
    run.set_stage("building_models")
    resilient_io.wait_until_available(
        queue.root, on_wait=lambda p: log(f"Queue path {p} unreachable -- waiting for it to come back...")
    )
    aedt_version = analysis_settings.get("aedt_version")
    for combo_id, combo in run.state["combos"].items():
        if stop_event is not None and stop_event.is_set():
            return
        if combo.get("task_id"):
            continue
        if combo.get("step_status") != "done":
            log(f"Skipping {combo_id} -- STEP generation did not succeed for it.")
            continue

        dimensions_txt_content = eq.read_file(f"{run.combo_dir(combo_id)}/dimensions.txt")
        resilient_io.call_with_retry(
            model_builder.build_and_queue,
            combo_id, combo["step_path"], dimensions_txt_content, run, queue, analysis_settings,
            aedt_version, analysis_settings.get("student_version", True), analysis_settings.get("graphical", False),
            queue_root=queue.root, log=log,
        )

    log("Stage 3/5: monitoring analysis progress...")
    run.set_stage("monitoring")
    monitor_run(run, queue, poll_seconds=analysis_settings.get("poll_seconds", 10.0), log=log, stop_event=stop_event)
    if stop_event is not None and stop_event.is_set():
        return

    log("Stage 4/5: post-processing analyzed projects (one at a time)...")
    run.set_stage("postprocessing")
    resilient_io.call_with_retry(
        postprocess.postprocess_run, run, queue, analysis_settings,
        queue_root=queue.root, log=log, stop_event=stop_event,
    )
    if stop_event is not None and stop_event.is_set():
        return

    log("Stage 5/5: building combined results and plotting...")
    run.set_stage("plotting")
    df = resilient_io.call_with_retry(results.build_combined_results, run, queue, queue_root=queue.root, log=log)
    plotting.plot_results(df, run, run.sweep_param_names, log=log)

    run.set_stage("complete")
    log(f"Run {run.run_id} complete. Plots saved to {run.plots_dir}")


def build_analysis_settings(args: argparse.Namespace) -> dict:
    return {
        "modes": args.modes,
        "max_passes": args.max_passes,
        "min_passes": args.min_passes,
        "min_converged": args.min_converged,
        "max_delta_f": args.max_delta_f,
        "min_freq_safety_factor": args.min_freq_safety_factor,
        "aedt_version": args.aedt_version,
        "student_version": args.student_version,
        "graphical": args.graphical,
        "poll_seconds": args.poll_seconds,
        "materials": {
            "mat_box": args.mat_box,
            "mat_pcb": args.mat_pcb,
            "mat_chip": args.mat_chip,
            "mat_vacuum": args.mat_vacuum,
            "perfect_e_box": args.perfect_e_box,
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the eigenmode-analysis client pipeline end to end")
    parser.add_argument(
        "--sweep", action="append", default=[], metavar="NAME=SPEC",
        help='A parameter to sweep, e.g. --sweep pedestal_depth=0:10:2 (repeatable for '
             "multiple parameters -- combinations are the cartesian product across all of them)",
    )
    parser.add_argument("--resume", type=str, default=None, help="Resume an existing run by its run_id instead of starting a new one")
    parser.add_argument("--modes", type=int, default=20)
    parser.add_argument("--max-passes", type=int, default=99)
    parser.add_argument("--min-passes", type=int, default=5)
    parser.add_argument("--min-converged", type=int, default=3)
    parser.add_argument("--max-delta-f", type=float, default=1.0, help="Maximum Delta Frequency Per Pass (%%)")
    parser.add_argument("--min-freq-safety-factor", type=float, default=0.4)
    parser.add_argument("--aedt-version", type=str, default=None)
    parser.add_argument("--no-student-version", dest="student_version", action="store_false", default=True)
    parser.add_argument("--graphical", action="store_true", help="Show the AEDT UI while building")
    parser.add_argument("--poll-seconds", type=float, default=10.0, help="Queue status poll interval")
    parser.add_argument("--mat-box", type=str, default="aluminum")
    parser.add_argument("--mat-pcb", type=str, default="Rogers TMM 10i (tm)")
    parser.add_argument("--mat-chip", type=str, default="silicon")
    parser.add_argument("--mat-vacuum", type=str, default="vacuum")
    parser.add_argument("--no-perfect-e-box", dest="perfect_e_box", action="store_false", default=True)
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s", stream=sys.stdout)
    args = parse_args()
    queue = queue_common.QueuePaths.from_env("ANSYS_ANALYZE_QUEUE_PATH")

    if args.resume:
        run = Run.load(args.resume)
        logger.info("Resuming run %s (stage=%s)", run.run_id, run.state.get("stage"))
    else:
        if not args.sweep:
            raise SystemExit("Provide at least one --sweep NAME=SPEC, or --resume RUN_ID")
        sweep_params = []
        for item in args.sweep:
            if "=" not in item:
                raise SystemExit(f"Invalid --sweep {item!r} -- expected NAME=SPEC")
            name, spec = item.split("=", 1)
            sweep_params.append(SweepParameter(name=name, values=expand_values(spec)))
        run = Run.create(sweep_params, build_analysis_settings(args))
        logger.info("Created run %s with %d combination(s)", run.run_id, len(run.state["combos"]))

    run_pipeline(run, queue)


if __name__ == "__main__":
    main()
