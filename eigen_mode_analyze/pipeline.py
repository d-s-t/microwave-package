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
import threading
from typing import Optional

from ansys_analyze_common import queue_common

from . import equations as eq
from . import model_builder, paths, plotting, postprocess, resilient_io, results, step_generation
from .monitor import monitor_run
from .run_manager import Run
from .sweep import SweepParameter, expand_values, split_swept_and_fixed

logger = logging.getLogger("eigen_mode_analyze")


def run_pipeline(run: Run, queue: "queue_common.QueuePaths", log=logger.info, stop_event=None) -> None:
    """Runs whichever stages `run` hasn't already completed. Safe to call
    again on a `run` that was interrupted mid-stage -- every stage skips
    combos it already finished."""
    equations_path = str(paths.DEFAULT_EQUATIONS_FILE)
    assembly_path = str(paths.DEFAULT_ASSEMBLY_FILE)
    analysis_settings = run.config["analysis_settings"]
    parameter_defaults = run.config.get("parameter_defaults", {})
    combos = {cid: c["params"] for cid, c in run.state["combos"].items()}
    aedt_version = analysis_settings.get("aedt_version")

    def queue_combo(combo_id: str) -> None:
        """Build the AEDT model for one finished STEP and queue it."""
        combo = run.state["combos"][combo_id]
        if combo.get("task_id") or combo.get("step_status") != "done":
            return
        resilient_io.wait_until_available(
            queue.root, on_wait=lambda p: log(f"Queue path {p} unreachable -- waiting for it to come back...")
        )
        dimensions_txt_content = eq.read_file(f"{run.combo_dir(combo_id)}/dimensions.txt")
        resilient_io.call_with_retry(
            model_builder.build_and_queue,
            combo_id, combo["step_path"], dimensions_txt_content, run, queue, analysis_settings,
            aedt_version, analysis_settings.get("student_version", True), analysis_settings.get("graphical", False),
            queue_root=queue.root, log=log,
        )

    # Each model is queued the moment its STEP file exists, and a monitor
    # thread watches the queue throughout -- so the combos that are
    # already analysing report progress to the UI while the remaining
    # geometry is still being built. The STEP files themselves are still
    # produced one at a time: they share a single SolidWorks document.
    log("Stage 1/4: generating STEP files, building AEDT models, and queuing analysis tasks...")
    run.set_stage("generating_and_queuing")

    queuing_finished = threading.Event()
    monitor_thread = threading.Thread(
        target=monitor_run,
        args=(run, queue),
        kwargs={
            "poll_seconds": analysis_settings.get("poll_seconds", 10.0),
            "log": log,
            "stop_event": stop_event,
            "more_expected": lambda: not queuing_finished.is_set(),
        },
        daemon=True,
        name=f"monitor-{run.run_id}",
    )
    monitor_thread.start()

    try:
        resilient_io.call_with_retry(
            step_generation.generate_step_files, combos, equations_path, assembly_path, run,
            log=log, parameter_defaults=parameter_defaults, on_step_done=queue_combo,
            stop_event=stop_event,
        )
        if stop_event is None or not stop_event.is_set():
            # Anything whose STEP was already on disk from an earlier
            # attempt never went through on_step_done, so it is queued here.
            for combo_id, combo in run.state["combos"].items():
                if stop_event is not None and stop_event.is_set():
                    break
                if combo.get("task_id"):
                    continue
                if combo.get("step_status") != "done":
                    log(f"Skipping {combo_id} -- STEP generation did not succeed for it.")
                    continue
                queue_combo(combo_id)
    finally:
        queuing_finished.set()

    if stop_event is not None and stop_event.is_set():
        monitor_thread.join(timeout=30)
        return

    # Post-processing drives a local AEDT session. When the worker has its
    # own machine (which is what "student version" means here -- see the
    # note on the setting in the web UI) nothing else is using AEDT on
    # this one, so each combo is post-processed the moment its analysis
    # lands rather than after the whole sweep. With the worker on this
    # machine the two would be competing for the same AEDT, so it waits.
    worker_is_remote = analysis_settings.get("student_version", True)
    poll_seconds = analysis_settings.get("poll_seconds", 10.0)

    if worker_is_remote:
        log("Stage 2/4: post-processing each analysis as it finishes...")
        run.set_stage("postprocessing")
        while monitor_thread.is_alive():
            if stop_event is not None and stop_event.is_set():
                return
            resilient_io.call_with_retry(
                postprocess.postprocess_run, run, queue, analysis_settings,
                queue_root=queue.root, log=log, stop_event=stop_event, announce_idle=False,
            )
            monitor_thread.join(timeout=poll_seconds)
        if stop_event is not None and stop_event.is_set():
            return
        log("Stage 3/4: post-processing whatever finished last...")
    else:
        log("Stage 2/4: waiting for the remaining analyses to finish...")
        run.set_stage("monitoring")
        monitor_thread.join()
        if stop_event is not None and stop_event.is_set():
            return
        log("Stage 3/4: post-processing analyzed projects...")

    run.set_stage("postprocessing")
    resilient_io.call_with_retry(
        postprocess.postprocess_run, run, queue, analysis_settings,
        queue_root=queue.root, log=log, stop_event=stop_event,
    )
    if stop_event is not None and stop_event.is_set():
        return

    log("Stage 4/4: building combined results and plotting...")
    run.set_stage("plotting")
    df = resilient_io.call_with_retry(results.build_combined_results, run, queue, queue_root=queue.root, log=log)
    plotting.plot_results(df, run, run.sweep_param_names, log=log)

    run.set_stage("complete")
    log(f"Run {run.run_id} complete. Plots saved to {run.plots_dir}")


# What a brand-new run uses when there is no earlier run to copy from.
# Kept here so the web form and the CLI start from the same numbers.
DEFAULT_ANALYSIS_SETTINGS = {
    "modes": 20,
    "max_passes": 99,
    "min_passes": 5,
    "min_converged": 3,
    "max_delta_f": 1.0,
    "min_freq_safety_factor": 0.4,
    "aedt_version": None,
    "student_version": True,
    "graphical": False,
    "poll_seconds": 10.0,
    "materials": {
        "mat_box": "aluminum",
        "mat_pcb": "Rogers TMM 10i (tm)",
        "mat_chip": "silicon",
        "mat_vacuum": "vacuum",
        "perfect_e_box": True,
    },
}


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
        requested = []
        for item in args.sweep:
            if "=" not in item:
                raise SystemExit(f"Invalid --sweep {item!r} -- expected NAME=SPEC")
            name, spec = item.split("=", 1)
            requested.append(SweepParameter(name=name, values=expand_values(spec)))
        # A --sweep given a single value pins that value for the run
        # instead of becoming a (one-point) sweep dimension.
        sweep_params, parameter_defaults = split_swept_and_fixed(requested)
        if parameter_defaults:
            logger.info(
                "Pinned for this run: %s",
                ", ".join(f"{n}={v:g}" for n, v in parameter_defaults.items()),
            )
        run = Run.create(sweep_params, build_analysis_settings(args), parameter_defaults)
        logger.info("Created run %s with %d combination(s)", run.run_id, len(run.state["combos"]))

    run_pipeline(run, queue)


if __name__ == "__main__":
    main()
