"""
The end-to-end client pipeline: build geometry for every point in a
parameter sweep, queue each one for analysis, wait for and monitor the
worker's progress (printing to the screen as each finishes), then plot
the combined results -- all resumable from wherever it left off. Runs
standalone from the command line, or driven by the web UI (webapp.py),
which calls `run_pipeline()` directly in a background thread.

    python -m microwave_package.pipeline --sweep pedestal_depth=0:10:2
    python -m microwave_package.pipeline --resume run_20260819_140102

A run solves either for the cavity's eigenmodes or for the package's
S-parameters, and the same stages carry both -- see hfss/registry.py.

The local half of a run -- Altium, SolidWorks, AEDT -- is three queues
draining at the same time rather than one loop (see workflow/stages.py);
this module is what feeds them, hands each finished model to the worker,
and does everything after that. Ctrl-C (or the web UI's Stop button)
stops a run between combinations, leaving it resumable.
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from typing import Callable, Dict, List, Optional

from ansys_analyze_common import queue_common

from . import paths
from .design.sweep import SweepParameter, expand_values, split_swept_and_fixed
from .hfss import model_builder, registry
from .hfss.driven import ports
from .workflow import plotting, postprocess, resilient_io, results, stages
from .workflow.monitor import monitor_run
from .workflow import run_manager
from .workflow.run_manager import Run

logger = logging.getLogger("microwave_package")


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


def _stopped(stop_event) -> bool:
    return stop_event is not None and stop_event.is_set()


def run_pipeline(run: Run, queue: "queue_common.QueuePaths", log=logger.info, stop_event=None) -> None:
    """Runs whatever `run` hasn't already finished. Safe to call again on
    a run that was interrupted or stopped part-way -- every stage skips
    what it already did, per combination and per program.

    `stop_event` is how a run is stopped from outside (the web UI's Stop
    button, Ctrl-C on the command line). Nothing is abandoned when it is
    set: each stage finishes the combination it is in the middle of --
    none of SolidWorks, Altium or AEDT can be interrupted mid-call
    anyway -- records it, and the run ends where it stood, resumable.
    """
    try:
        _run_stages(run, queue, log=log, stop_event=stop_event)
    except resilient_io.Stopped as stopped:
        # Somewhere that was waiting for the share gave up because this
        # run was stopped. That is the stop working, not a failure, so it
        # does not become the run's error.
        logger.info("Run %s: %s", run.run_id, stopped)
    finally:
        # Whatever happened, nothing is working on this run any more --
        # so a combination still marked as being drawn, built or read is
        # a leftover, and saying so on the page would be a lie. (A
        # process that was killed outright cannot do this, which is why
        # the stages clear it on the way in as well.)
        stages.clear_stale_progress(run)
        if _stopped(stop_event) and run.state.get("stage") != "complete":
            run.set_stage("stopped")
            log(f"Run {run.run_id} stopped. Nothing already built is lost -- "
                "resume it to carry on from where it stopped.")


def _run_stages(run: Run, queue: "queue_common.QueuePaths", log=logger.info, stop_event=None) -> None:
    analysis_settings = run.config["analysis_settings"]
    parameter_defaults = run.config.get("parameter_defaults", {})

    # Combinations whose geometry is built but which could not be handed
    # over, because the queue was unreachable at the time.
    deferred: List[str] = []

    # Start checking whether the share is reachable straight away, on its
    # own thread, so the first STEP files and AEDT models start building
    # now instead of after the ~20s a disconnected mapped drive costs to
    # answer. `handoff` consults the result when a model is actually
    # ready to hand over -- by then the answer is almost always already in.
    log(f"Checking the queue at {queue.root} in the background "
        "(a disconnected network drive can take ~20s to answer); "
        "geometry building starts now.")
    queue_probe = _QueueProbe(queue, log=log).start()

    def handoff(combo_id: str, object_names: Dict[str, str]) -> None:
        """Copy one built model into the queue, if the queue is there.

        The AEDT stage builds and this hands over, and the two are kept
        apart deliberately: building is local work that needs AEDT and no
        network, so it happens either way, and only the copy needs the
        share. That is what makes a run preparable offline -- everything
        computable here is computed, and all that is left for later is
        the handoff.

        Consults the background probe rather than checking the share
        here: by now it has almost always answered, so this does not
        block. If it somehow hasn't (a very slow share), waiting for it
        beats deferring a handoff that would have gone through.
        """
        if not queue_probe.wait():
            # Offline. The model is built and saved; resuming once the
            # share is back picks up exactly here.
            if combo_id not in deferred:
                deferred.append(combo_id)
                run.update_combo(combo_id, handoff_status="deferred")
            return

        resilient_io.ensure_queue_ready(
            queue, stop_event=stop_event,
            on_wait=lambda p: log(f"Queue path {p} unreachable -- waiting for it to come back..."),
        )
        resilient_io.call_with_retry(
            model_builder.queue_model,
            combo_id, run.state["combos"][combo_id]["step_path"], run, queue, object_names,
            queue_root=queue.root, log=log, stop_event=stop_event,
        )

    # Altium, SolidWorks and AEDT each drain their own queue of
    # combinations at the same time (workflow/stages.py), and a monitor
    # thread watches the analysis queue throughout -- so the combos that
    # are already analysing report progress to the UI while the remaining
    # geometry is still being drawn and built. The queue's reachability
    # is being probed in parallel too (see above); nothing waits on it
    # until there is a model to hand over.
    log("Preparing models: Altium draws each combination's boards, SolidWorks "
        "rebuilds its solid, and AEDT turns the two into a model and queues it.")
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
            stages.run_local_stages, run,
            equations_path=str(paths.DEFAULT_EQUATIONS_FILE),
            assembly_path=str(paths.DEFAULT_ASSEMBLY_FILE),
            analysis_settings=analysis_settings,
            parameter_defaults=parameter_defaults,
            handoff=handoff, log=log, stop_event=stop_event,
        )
    finally:
        queuing_finished.set()

    if _stopped(stop_event):
        monitor_thread.join(timeout=30)
        return

    # If nothing was handed over (every combo was already queued on an
    # earlier run) the probe was never consulted; settle it now so the
    # deferred / geometry-ready decision below rests on a real answer.
    queue_probe.wait()

    if deferred:
        # The geometry is built and saved; only the handoff is missing.
        # Resuming this run once the share is back picks up exactly here,
        # because every finished stage is skipped and every combination
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
        log("Waiting for the worker, and reading each analysis back as it finishes...")
        run.set_stage("postprocessing")
    else:
        log("Waiting for the remaining analyses to finish...")
        run.set_stage("monitoring")

    while True:
        if _stopped(stop_event):
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

    if _stopped(stop_event):
        return
    log("Copying the analysed projects back and reading their results...")

    run.set_stage("postprocessing")
    resilient_io.call_with_retry(
        postprocess.postprocess_run, run, queue, analysis_settings,
        queue_root=queue.root, log=log, stop_event=stop_event,
    )
    if _stopped(stop_event):
        return

    log("Building combined results and plotting...")
    run.set_stage("plotting")
    df = resilient_io.call_with_retry(results.build_combined_results, run, queue,
                                      queue_root=queue.root, log=log, stop_event=stop_event)
    plotting.plot_results(df, run, run.sweep_param_names, log=log)

    run.set_stage("complete")
    log(f"Run {run.run_id} complete. Plots saved to {run.plots_dir}")


# What a brand-new run uses when there is no earlier run to copy from.
# Kept here so the web form and the CLI start from the same numbers.
#
# The per-solution-type numbers come from the solvers rather than being
# repeated here: both types' defaults are always present, and the run's
# `solution_type` decides which half is ever read. See that module for
# why the unused half is kept rather than stripped.
DEFAULT_ANALYSIS_SETTINGS = {
    "solution_type": registry.default_solution_type(),
    **registry.all_defaults(),
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
        "--solution-type", choices=[t["value"] for t in registry.solution_types()],
        default=registry.default_solution_type(),
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
        "--reference-plane", choices=[p["value"] for p in ports.REFERENCE_PLANES],
        default=ports.DEFAULT_REFERENCE_PLANE,
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


def stop_on_interrupt(stop_event: threading.Event) -> None:
    """Make Ctrl-C ask the run to stop rather than kill it mid-call.

    The first Ctrl-C sets the stop event: every stage finishes the
    combination it is in the middle of -- none of the three programs can
    be interrupted mid-call anyway -- records it, and the run ends
    resumable, exactly as the web UI's Stop button leaves it. The
    handler then puts Python's own back, so a second Ctrl-C quits
    immediately for when waiting is not what you want.
    """
    def handler(signum, frame):
        signal.signal(signal.SIGINT, signal.default_int_handler)
        stop_event.set()
        logger.warning(
            "Stopping this run -- each program will finish the combination it is on. "
            "Press Ctrl-C again to quit without waiting (the run stays resumable either way)."
        )

    signal.signal(signal.SIGINT, handler)


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
        complaint = registry.get(analysis_settings["solution_type"]).validate(
            run_manager.parameter_values_for("port_count", sweep_params, parameter_defaults)
        )
        if complaint:
            raise SystemExit(complaint)

        run = Run.create(sweep_params, analysis_settings, parameter_defaults)
        logger.info("Created run %s with %d combination(s)", run.run_id, len(run.state["combos"]))

    stop_event = threading.Event()
    stop_on_interrupt(stop_event)
    run_pipeline(run, queue, stop_event=stop_event)


if __name__ == "__main__":
    main()
