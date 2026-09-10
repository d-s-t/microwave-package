"""
The end-to-end client pipeline: build geometry for every point in a
parameter sweep, queue each one for analysis, wait for and monitor the
worker's progress (printing to the screen as each finishes), then plot
the combined results -- all resumable from wherever it left off. Runs
standalone from the command line, or driven by the web UI (webapp.py),
which calls `run_pipeline()` directly in a background thread.

    python -m eigen_mode_analyze.pipeline --sweep pedestal_depth=0:10:2
    python -m eigen_mode_analyze.pipeline --resume run_20260819_140102

A run solves either for the cavity's eigenmodes or for the package's
S-parameters, and the same stages carry both -- see solve_types.py.
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
from typing import Callable, List, Optional

from ansys_analyze_common import queue_common

from . import equations as eq
from . import model_builder, paths, plotting, postprocess, resilient_io, results, solve_types, step_generation
from .monitor import monitor_run
from .run_manager import Run
from .sweep import SweepParameter, expand_values, split_swept_and_fixed

logger = logging.getLogger("eigen_mode_analyze")


class _QueueProbe:
    """Checks whether the queue share is reachable, on its own thread.

    The check is instant for a normal folder, but a DISCONNECTED MAPPED
    DRIVE takes about twenty seconds to answer the first time in a
    process -- Windows retries the connection before giving up. Running
    it here, alongside the first STEP files and AEDT models, keeps that
    cost off the critical path: by the time a model is built and ready to
    hand over, the answer is almost always already in.

    `wait()` blocks for the result and returns whether the queue is
    reachable. It is meant to be called at the point the handoff would
    happen, where it normally returns at once. On the way it also creates
    the queue's subfolders (the half of `ensure_queue_ready` that needs
    the share), so a reachable queue is ready to be written to.
    """

    def __init__(self, queue, log: Callable[[str], None] = logger.info) -> None:
        self._queue = queue
        self._log = log
        self._done = threading.Event()
        self.reachable = False
        self._thread = threading.Thread(target=self._run, daemon=True, name="queue-probe")

    def start(self) -> "_QueueProbe":
        self._thread.start()
        return self

    def _run(self) -> None:
        try:
            if resilient_io.path_available(self._queue.root):
                self._queue.ensure_exist()
                self.reachable = True
                self._log("Queue is reachable.")
            else:
                self._log(
                    f"Queue at {self._queue.root} is NOT reachable. The geometry will "
                    "still be built on this machine; handing it to the worker is deferred "
                    "until you reconnect and resume this run."
                )
        except OSError as exc:
            # The share answered but a subfolder could not be created
            # (permissions, or it dropped in the gap after the check).
            # Degrade to the offline path rather than taking the run down.
            self._log(
                f"Queue at {self._queue.root} could not be prepared ({exc}); treating it "
                "as unreachable -- the handoff is deferred to a later resume."
            )
        except Exception:
            logger.exception("Unexpected error while probing the queue.")
            self._log(
                f"Queue at {self._queue.root} could not be checked -- treating it as "
                "unreachable; the handoff is deferred to a later resume."
            )
        finally:
            self._done.set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        self._done.wait(timeout)
        return self.reachable


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

    # Combinations whose geometry is built but which could not be handed
    # over, because the queue was unreachable at the time.
    deferred: List[str] = []

    # Start checking whether the share is reachable straight away, on its
    # own thread, so the first STEP files and AEDT models start building
    # now instead of after the ~20s a disconnected mapped drive costs to
    # answer. `queue_combo` consults the result when a model is actually
    # ready to hand over -- by then the answer is almost always already in.
    log(f"Checking the queue at {queue.root} in the background "
        "(a disconnected network drive can take ~20s to answer); "
        "geometry building starts now.")
    queue_probe = _QueueProbe(queue, log=log).start()

    def queue_combo(combo_id: str) -> None:
        """Build the AEDT model for one finished STEP, and hand it over
        if the queue is reachable.

        The two halves are deliberately separate. Building the model is
        local work that needs AEDT and no network, so it happens either
        way; only the copy into the queue needs the share. That is what
        makes a run preparable offline: everything that can be computed
        here is computed, and all that is left for later is the handoff.
        """
        combo = run.state["combos"][combo_id]
        if combo.get("task_id") or combo.get("step_status") != "done":
            return

        dimensions_txt_content = eq.read_file(f"{run.combo_dir(combo_id)}/dimensions.txt")
        object_names = model_builder.build_model(
            combo_id, combo["step_path"], dimensions_txt_content, run, analysis_settings,
            aedt_version, analysis_settings.get("student_version", True),
            analysis_settings.get("graphical", False), log=log,
        )
        if object_names is None:
            # Already queued, or the project could not be made ready --
            # build_model has said which.
            return

        # The model is built and saved; only the handoff is left. Consult
        # the background probe rather than checking the share here: by now
        # it has almost always answered, so this does not block. If it
        # somehow hasn't (a very slow share), waiting for it beats
        # deferring a handoff that would have gone through.
        if not queue_probe.wait():
            # Offline. The model is built and saved; resuming once the
            # share is back picks up exactly here.
            if combo_id not in deferred:
                deferred.append(combo_id)
                run.update_combo(combo_id, queue_status="deferred")
            return

        resilient_io.ensure_queue_ready(
            queue, on_wait=lambda p: log(f"Queue path {p} unreachable -- waiting for it to come back...")
        )
        resilient_io.call_with_retry(
            model_builder.queue_model,
            combo_id, combo["step_path"], run, queue, object_names,
            queue_root=queue.root, log=log,
        )

    # Each model is queued the moment its STEP file exists, and a monitor
    # thread watches the queue throughout -- so the combos that are
    # already analysing report progress to the UI while the remaining
    # geometry is still being built. The STEP files themselves are still
    # produced one at a time: they share a single SolidWorks document.
    # The queue's reachability is being probed in parallel (see above);
    # nothing here waits on it until there is a model to hand over.
    log("Stage 1/4: generating layouts in Altium, STEP files in SolidWorks, "
        "building AEDT models, and queuing analysis tasks...")
    run.set_stage("generating_and_queuing")

    queuing_finished = threading.Event()

    def start_monitor():
        thread = threading.Thread(
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
        thread.start()
        return thread

    monitor_thread = start_monitor()

    try:
        resilient_io.call_with_retry(
            step_generation.generate_step_files, combos, equations_path, assembly_path, run,
            log=log, parameter_defaults=parameter_defaults, on_step_done=queue_combo,
            stop_event=stop_event,
            use_altium=analysis_settings.get("use_altium", True),
            materials=analysis_settings.get("materials"),
        )
        if stop_event is None or not stop_event.is_set():
            # Anything whose STEP was already on disk from an earlier
            # attempt never went through on_step_done, so it is queued here.
            for combo_id, combo in run.state["combos"].items():
                if stop_event is not None and stop_event.is_set():
                    break
                if combo.get("task_id"):
                    continue
                if combo.get("skip_reason"):
                    continue  # already reported by generate_step_files
                if combo.get("step_status") != "done":
                    log(f"Skipping {combo_id} -- STEP generation did not succeed for it.")
                    continue
                queue_combo(combo_id)
    finally:
        queuing_finished.set()

    if stop_event is not None and stop_event.is_set():
        monitor_thread.join(timeout=30)
        return

    # If Stage 1 handed nothing over (every combo was already queued on an
    # earlier run) the probe was never consulted; settle it now so the
    # deferred / geometry-ready decision below rests on a real answer.
    queue_probe.wait()

    if deferred:
        # The geometry is built and saved; only the handoff is missing.
        # Resuming this run once the share is back picks up exactly here,
        # because every finished STEP is skipped and every combination
        # without a task_id gets queued.
        monitor_thread.join(timeout=30)
        run.set_stage("geometry_ready")
        log(
            f"{len(deferred)} combination(s) are fully built -- geometry and AEDT "
            f"project both -- but the queue at {queue.root} is unreachable, so nothing "
            "was handed to the worker. Reconnect and resume this run to hand them over; "
            "nothing will be rebuilt, the projects are just copied across."
        )
        return

    # Post-processing drives a local AEDT session. When the worker has its
    # own machine (which is what "student version" means here -- see the
    # note on the setting in the web UI) nothing else is using AEDT on
    # this one, so each combo is post-processed the moment its analysis
    # lands rather than after the whole sweep. With the worker on this
    # machine the two would be competing for the same AEDT, so it waits.
    worker_is_remote = analysis_settings.get("student_version", True)
    poll_seconds = analysis_settings.get("poll_seconds", 10.0)

    def still_analyzing():
        return [
            combo_id for combo_id, combo in run.state["combos"].items()
            if combo.get("task_id") and combo.get("queue_status") not in ("done", "failed")
        ]

    if worker_is_remote:
        log("Stage 2/4: post-processing each analysis as it finishes...")
        run.set_stage("postprocessing")
    else:
        log("Stage 2/4: waiting for the remaining analyses to finish...")
        run.set_stage("monitoring")

    while True:
        if stop_event is not None and stop_event.is_set():
            return
        if worker_is_remote:
            # results are pulled in as they land rather than at the end
            resilient_io.call_with_retry(
                postprocess.postprocess_run, run, queue, analysis_settings,
                queue_root=queue.root, log=log, stop_event=stop_event, announce_idle=False,
            )
        if not monitor_thread.is_alive():
            outstanding = still_analyzing()
            if not outstanding:
                break
            # Monitoring is the only thing that notices work finishing, so
            # it going quiet while combos are still out means the run would
            # otherwise sit here looking done. Bring it back rather than
            # reporting a run that never finished as complete.
            log(f"Monitoring stopped with {len(outstanding)} combination(s) unfinished -- restarting it.")
            monitor_thread = start_monitor()
        monitor_thread.join(timeout=poll_seconds)

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
#
# The per-solution-type numbers come from solve_types rather than being
# repeated here: both types' defaults are always present, and the run's
# `solution_type` decides which half is ever read. See that module for
# why the unused half is kept rather than stripped.
DEFAULT_ANALYSIS_SETTINGS = {
    "solution_type": solve_types.DEFAULT_SOLUTION_TYPE,
    **solve_types.all_defaults(),
    "aedt_version": None,
    "student_version": True,
    "graphical": False,
    "poll_seconds": 10.0,
    # Regenerate the PCB and chip layouts in Altium for every
    # combination. Turning this off falls back to the SolidWorks-only
    # geometry the pipeline built before Altium existed -- no copper, no
    # vias, and a much lighter mesh.
    "use_altium": True,
    "materials": {
        "mat_box": "aluminum",
        "mat_pcb": "Rogers TMM 10i (tm)",
        "mat_chip": "silicon",
        "mat_vacuum": "vacuum",
        "perfect_e_box": True,
        # Conductors default to perfect E rather than to a named metal.
        # This is not a shortcut: HFSS's stock copper and niobium carry
        # ROOM-TEMPERATURE normal-state conductivity, which at
        # millikelvin is less realistic than PEC, not more. Genuine cryo
        # realism needs a Mattis-Bardeen surface impedance, which is its
        # own project and buys nothing for mode frequencies. PEC also
        # keeps the mesh affordable now that copper exists.
        "mat_conductor_pcb": "copper",
        "mat_conductor_chip": "niobium",
        "perfect_e_conductors": True,
        # The SMP connector's materials (brass conductors, LCP bead) and
        # the fact that it has no perfect-E boundary are fixed in
        # model_builder -- it is vendor geometry, not a run setting.
        # See github.com/d-s-t/wr-smp-connector.
    },
}


def build_analysis_settings(args: argparse.Namespace) -> dict:
    return {
        "solution_type": args.solution_type,
        "modes": args.modes,
        "max_passes": args.max_passes,
        "min_passes": args.min_passes,
        "min_converged": args.min_converged,
        "max_delta_f": args.max_delta_f,
        "min_freq_safety_factor": args.min_freq_safety_factor,
        "f_start_ghz": args.f_start_ghz,
        "f_stop_ghz": args.f_stop_ghz,
        "sweep_points": args.sweep_points,
        "max_delta_s": args.max_delta_s,
        "port_impedance_ohm": args.port_impedance_ohm,
        "reference_plane": args.reference_plane,
        "aedt_version": args.aedt_version,
        "student_version": args.student_version,
        "graphical": args.graphical,
        "poll_seconds": args.poll_seconds,
        "use_altium": args.use_altium,
        "materials": {
            "mat_box": args.mat_box,
            "mat_pcb": args.mat_pcb,
            "mat_chip": args.mat_chip,
            "mat_vacuum": args.mat_vacuum,
            "perfect_e_box": args.perfect_e_box,
            "mat_conductor_pcb": args.mat_conductor_pcb,
            "mat_conductor_chip": args.mat_conductor_chip,
            "perfect_e_conductors": args.perfect_e_conductors,
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the analysis client pipeline end to end")
    parser.add_argument(
        "--sweep", action="append", default=[], metavar="NAME=SPEC",
        help='A parameter to sweep, e.g. --sweep pedestal_depth=0:10:2 (repeatable for '
             "multiple parameters -- combinations are the cartesian product across all of them)",
    )
    parser.add_argument("--resume", type=str, default=None, help="Resume an existing run by its run_id instead of starting a new one")
    parser.add_argument(
        "--solution-type", choices=[t["value"] for t in solve_types.SOLUTION_TYPES],
        default=solve_types.DEFAULT_SOLUTION_TYPE,
        help="Eigenmode reports resonant frequencies and Q; DrivenModal drives the "
             "two ports and reports S11/S21. The settings below are grouped by which "
             "one they apply to -- the other group is ignored.",
    )
    # Eigenmode settings.
    parser.add_argument("--modes", type=int, default=20)
    parser.add_argument("--max-delta-f", type=float, default=1.0, help="Maximum Delta Frequency Per Pass (%%)")
    parser.add_argument("--min-freq-safety-factor", type=float, default=0.4)
    # DrivenModal settings.
    parser.add_argument("--f-start-ghz", type=float, default=1.0, help="Frequency sweep start")
    parser.add_argument("--f-stop-ghz", type=float, default=15.0, help="Frequency sweep stop")
    parser.add_argument("--sweep-points", type=int, default=401, help="Frequency points reported by the sweep")
    parser.add_argument("--max-delta-s", type=float, default=0.02, help="Maximum Delta S Per Pass")
    parser.add_argument("--port-impedance-ohm", type=float, default=50.0)
    parser.add_argument(
        "--reference-plane", choices=[p["value"] for p in solve_types.REFERENCE_PLANES],
        default=solve_types.DEFAULT_REFERENCE_PLANE,
        help="Where the driven run's ports go: 'connector' puts a wave port on the "
             "SMP bead's coax cross-section (connector transition included); 'launch' "
             "puts a lumped port under the CPW trace just inboard of the land pattern "
             "(board only).",
    )
    # Shared.
    parser.add_argument("--max-passes", type=int, default=99)
    parser.add_argument("--min-passes", type=int, default=5)
    parser.add_argument("--min-converged", type=int, default=3)
    parser.add_argument("--aedt-version", type=str, default=None)
    parser.add_argument("--no-student-version", dest="student_version", action="store_false", default=True)
    parser.add_argument("--graphical", action="store_true", help="Show the AEDT UI while building")
    parser.add_argument("--poll-seconds", type=float, default=10.0, help="Queue status poll interval")
    parser.add_argument("--mat-box", type=str, default="aluminum")
    parser.add_argument("--mat-pcb", type=str, default="Rogers TMM 10i (tm)")
    parser.add_argument("--mat-chip", type=str, default="silicon")
    parser.add_argument("--mat-vacuum", type=str, default="vacuum")
    parser.add_argument("--no-perfect-e-box", dest="perfect_e_box", action="store_false", default=True)
    parser.add_argument("--no-altium", dest="use_altium", action="store_false", default=True,
                        help="Skip layout generation and build the SolidWorks-only geometry")
    parser.add_argument("--mat-conductor-pcb", type=str, default="copper")
    parser.add_argument("--mat-conductor-chip", type=str, default="niobium")
    parser.add_argument("--no-perfect-e-conductors", dest="perfect_e_conductors",
                        action="store_false", default=True,
                        help="Use the named conductor materials instead of perfect E. "
                             "Only meaningful with a proper cryogenic surface impedance.")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s", stream=sys.stdout)
    args = parse_args()
    queue = resilient_io.queue_from_env("ANSYS_ANALYZE_QUEUE_PATH")

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
        analysis_settings = build_analysis_settings(args)
        # Checked before anything is built, not per combination: a driven
        # run needs two real ports to have an S21, and finding that out
        # one model at a time would mean a sweep's worth of geometry
        # built for projects that can never be given ports.
        swept_values = {p.name: p.values for p in sweep_params}
        complaint = solve_types.validate(
            analysis_settings["solution_type"],
            swept_values.get("port_count") or [parameter_defaults.get("port_count", 0)],
        )
        if complaint:
            raise SystemExit(complaint)

        run = Run.create(sweep_params, analysis_settings, parameter_defaults)
        logger.info("Created run %s with %d combination(s)", run.run_id, len(run.state["combos"]))

    run_pipeline(run, queue)


if __name__ == "__main__":
    main()
