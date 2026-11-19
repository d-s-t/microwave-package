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
import time
from typing import Callable, Dict, List, Optional

from ansys_analyze_common import queue_common

from . import paths, solver_presets
from .design.sweep import (
    SweepParameter, expand_values, pin_bare_board_pedestal, pin_switches, split_swept_and_fixed,
)
from .hfss import model_builder, registry
from .hfss.driven import DRIVEN_MODAL, ports
from .hfss.eigenmode import EIGENMODE
from .workflow import companion, field_images, plotting, postprocess, resilient_io, results, stages
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


# The runs this process is driving right now. Two threads driving one run
# would build and queue every combination twice -- and a Driven run that
# started while its Eigenmode run was being driven by a thread of its own
# would be built without that run's modes, so it waits for them instead.
_DRIVING: set = set()
_DRIVING_LOCK = threading.Lock()


def _begin_driving(run_id: str) -> bool:
    with _DRIVING_LOCK:
        if run_id in _DRIVING:
            return False
        _DRIVING.add(run_id)
        return True


def _end_driving(run_id: str) -> None:
    with _DRIVING_LOCK:
        _DRIVING.discard(run_id)


def _being_driven(run_id: str) -> bool:
    with _DRIVING_LOCK:
        return run_id in _DRIVING


# The Run object each run this process is driving is being driven through.
_LIVE_RUNS: Dict[str, Run] = {}


def live_run(run_id: str) -> Optional[Run]:
    """The Run this process is driving right now for `run_id`, or None.

    A pause or a resume for a run is applied to this object, because its
    state is the one the driver writes: a second copy would be overwritten by
    the next write from here (see workflow/queue_hold.py).
    """
    with _DRIVING_LOCK:
        return _LIVE_RUNS.get(run_id)


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
    if not _begin_driving(run.run_id):
        log(f"Run {run.run_id} is already being driven by another thread in this process "
            "-- not starting a second driver on it.")
        return
    with _DRIVING_LOCK:
        _LIVE_RUNS[run.run_id] = run
    run.claim_driver()
    try:
        if not _finish_prerequisite(run, queue, log=log, stop_event=stop_event):
            return
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
        with _DRIVING_LOCK:
            if _LIVE_RUNS.get(run.run_id) is run:
                del _LIVE_RUNS[run.run_id]
        _end_driving(run.run_id)
        stages.clear_stale_progress(run)
        if _stopped(stop_event) and run.state.get("stage") != "complete":
            run.set_stage("stopped")
            log(f"Run {run.run_id} stopped. Nothing already built is lost -- "
                "resume it to carry on from where it stopped.")
        run.release_driver()


def _finish_prerequisite(run: Run, queue, log=logger.info, stop_event=None) -> bool:
    """For a Driven run paired with an Eigenmode run: finish that one first.

    The driven run's fine windows are centred on the eigenmodes, so they
    have to exist before its models are built, and its geometry is the
    Eigenmode run's (see workflow/companion.py). Resumable like anything
    else here: an Eigenmode run that is already complete costs nothing.

    Returns False when the run was stopped meanwhile, which leaves this one
    stopped too. An Eigenmode run that failed outright does not take this
    one down with it -- the driven run goes ahead with the coarse sweep,
    and says so.
    """
    parent = (companion.refined_parent(run) or companion.field_parent(run)
              or companion.eigenmode_parent(run))
    if parent is not None:
        # A refinement, a field-image part or an eigenmode part, starts from its parent's finished geometry.
        companion.seed_geometry(run, parent, log=log)
        return True
    eigen = companion.prerequisite_of(run)
    if eigen is None:
        return True
    if _being_driven(eigen.run_id):
        # Something else in this process is already finishing it. Wait for
        # that rather than starting a second driver on the same run.
        run.set_stage("eigenmodes")
        log(f"Eigenmode run {eigen.run_id} is being driven by another thread -- waiting for it "
            "to finish before building this run's models.")
        while _being_driven(eigen.run_id):
            if _stopped(stop_event):
                run.set_stage("stopped")
                return False
            time.sleep(1.0)
        eigen = Run.load(eigen.run_id)
    if eigen.state.get("stage") != "complete":
        log(f"This run's eigenmodes ({eigen.run_id}) are solved first: its fine frequency "
            "windows are centred on them.")
        run.set_stage("eigenmodes")
        try:
            run_pipeline(eigen, queue, log=log, stop_event=stop_event)
        except Exception as exc:
            logger.exception("The paired Eigenmode run %s failed", eigen.run_id)
            eigen.state["stage"] = "error"
            eigen.state["error"] = repr(exc)
            eigen.save_state()
            log(f"The Eigenmode run {eigen.run_id} failed ({exc}); continuing with the driven "
                "run, which will have no automatic fine windows.")
        eigen = Run.load(eigen.run_id)
        if _stopped(stop_event):
            run.set_stage("stopped")
            return False
        if eigen.state.get("stage") != "complete":
            log(f"WARNING: Eigenmode run {eigen.run_id} did not complete "
                f"(stage {eigen.state.get('stage')}); combinations without its modes get no "
                "automatic fine windows.")
    companion.seed_geometry(run, eigen, log=log)
    return True


def _finish_field_part(run: Run, log=logger.info) -> None:
    """The end of a field image part's run. It has no sweep to plot: its pictures
    are drawn from the fields the worker exported, and it fails only when none came out."""
    outcome = field_images.draw_run(run, log=log)
    if outcome["drawn"]:
        run.set_stage("complete")
        log(f"Run {run.run_id} complete. Its field images are in {field_images.images_dir(run.run_dir)}")
    else:
        run.state["error"] = outcome["first_error"] or "The worker exported no field."
        run.set_stage("error")
        log(f"Run {run.run_id} failed: {run.state['error']}")


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
            combo_id, run.state["combos"][combo_id].get("step_paths") or {}, run, queue, object_names,
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
            part_paths={name: str(p) for name, p in paths.SOLIDWORKS_PART_FILES.items()},
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

    # Results that failed are tried again on the first pass after the run
    # (re)starts, and at the end -- not on every poll: see postprocess_run.
    retry_results = True
    while True:
        if _stopped(stop_event):
            return
        if worker_is_remote:
            # results are pulled in as they land rather than at the end
            resilient_io.call_with_retry(
                postprocess.postprocess_run, run, queue, analysis_settings,
                queue_root=queue.root, log=log, stop_event=stop_event, announce_idle=False,
                retry_results=retry_results,
            )
            retry_results = False
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

    if field_images.is_field_part(analysis_settings):
        _finish_field_part(run, log=log)
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
    # Solve Eigenmode AND Driven Modal for every combination, as a pair of
    # runs (see workflow/companion.py). Which solution type the form or
    # command line picked only matters with this off.
    "run_both": True,
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
        # Used only with perfect_e_box off: the box and lid_inside then get
        # a finite-conductivity boundary of this value (see
        # model_builder.DEFAULT_BOX_CONDUCTIVITY).
        "box_conductivity": model_builder.DEFAULT_BOX_CONDUCTIVITY,
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
        #
        # See model_builder.MATERIAL_FALLBACKS for why aluminum is the
        # wirebond default. Turning include_wirebonds off skips only the
        # bond wires themselves -- see build_layout's docstring.
        "mat_wirebond": model_builder.MATERIAL_FALLBACKS["mat_wirebond"],
        "include_wirebonds": True,
    },
}


def create_run(sweep_params, analysis_settings: dict, parameter_defaults=None) -> Run:
    """Create what a new run asks for, and return the run to start.

    With `run_both` that is a pair -- an Eigenmode run and a Driven run on
    the same sweep (see workflow/companion.py) -- and the one returned is
    the Driven run, which finishes the Eigenmode run first when started.
    Otherwise it is one run of the chosen solution type.

    Every solution type the run will use is validated against the geometry
    before anything is created, not per combination: a driven run needs
    two real ports to have an S21, and finding that out one model at a
    time would mean a sweep's worth of geometry built for projects that
    can never be given ports. Raises ValueError with the reason.
    """
    # Every on/off switch is pinned explicitly before anything reads it, so no
    # run's geometry depends on the shared equations file's current state.
    parameter_defaults = pin_switches(
        sweep_params, parameter_defaults, run_manager.equation_file_values())
    # A bare board has no pedestal unless one was asked for -- the same rule
    # for the command line and the web form, which both come through here.
    parameter_defaults = pin_bare_board_pedestal(
        sweep_params, parameter_defaults, run_manager.equation_file_values())
    # A solver that draws its own copper never starts Altium, whatever the form says.
    if not registry.for_settings(analysis_settings).needs_altium:
        analysis_settings = {**analysis_settings, "use_altium": False}
    # A line-impedance check is its own question, not a half of a pair.
    both = (analysis_settings.get("run_both")
            and registry.solution_type_of(analysis_settings) not in registry.standalone_types())
    types = [EIGENMODE, DRIVEN_MODAL] if both else [registry.solution_type_of(analysis_settings)]
    port_counts = run_manager.parameter_values_for("port_count", sweep_params, parameter_defaults)
    for solution_type in types:
        complaint = registry.get(solution_type).validate(port_counts)
        if complaint:
            raise ValueError(complaint)
    # The whole run is refused here, before anything is made, when a combination cannot be
    # built: vents in every run (constraints.check_vents), and a pedestal recess the copper
    # cannot close (constraints.check_recess_copper). The recess check is gated on Altium, as
    # the via grid is: without copper nothing is sealed.
    reason = run_manager.geometry_refusal(
        sweep_params, parameter_defaults, use_altium=analysis_settings.get("use_altium", True))
    if reason:
        raise ValueError(reason)
    if both:
        return companion.create_pair(sweep_params, analysis_settings, parameter_defaults)[1]
    return Run.create(sweep_params, analysis_settings, parameter_defaults)


def apply_setting_overrides(settings: dict, items) -> dict:
    """`--setting NAME=VALUE` items laid over the settings, each coerced to the
    type of the default it replaces. Only a setting some solver declares can be
    named, so a typo is an error, not a silently ignored flag."""
    defaults = registry.all_defaults()
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"Invalid --setting {item!r} -- expected NAME=VALUE")
        name, raw = item.split("=", 1)
        name = name.strip().replace("-", "_")
        if name not in defaults:
            raise SystemExit(f"--setting {name!r} is not a setting of any solution type; "
                             f"known: {', '.join(sorted(defaults))}")
        default = defaults[name]
        try:
            if isinstance(default, bool):
                value = raw.strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(default, int):
                value = int(float(raw))
            elif isinstance(default, float):
                value = float(raw)
            else:
                value = raw
        except ValueError:
            raise SystemExit(f"--setting {name}: {raw!r} is not a {type(default).__name__}")
        settings[name] = value
    return settings


def build_analysis_settings(args: argparse.Namespace) -> dict:
    # The five solver settings: a flag given explicitly wins, then the
    # preset's value, then the solver's own default (see solver_presets).
    # Without --preset they are the solver defaults, as they always were.
    preset = getattr(args, "preset", None)
    solver, overrides = solver_presets.resolve(
        {key: getattr(args, key, None) for key in solver_presets.SOLVER_KEYS},
        preset, registry.all_defaults())
    # --setting can name a solver key too, and that is an override as well.
    for item in getattr(args, "setting", None) or []:
        name = item.split("=", 1)[0].strip().replace("-", "_")
        if preset is not None and name in solver_presets.SOLVER_KEYS and name not in overrides:
            overrides.append(name)

    # Every solver's defaults underneath, so a setting the command line has
    # no flag for (the fine-window ones, mostly) still takes its default.
    settings = apply_setting_overrides({
        **registry.all_defaults(),
        **solver,
        "run_both": getattr(args, "run_both", False),
        "fine_windows": getattr(args, "fine_windows", "auto"),
        "fine_windows_manual": getattr(args, "fine_windows_manual", ""),
        "adapt_at": getattr(args, "adapt_at", "f_stop"),
        "solution_type": args.solution_type,
        "modes": args.modes,
        "min_freq_safety_factor": args.min_freq_safety_factor,
        "f_start_ghz": args.f_start_ghz,
        "f_stop_ghz": args.f_stop_ghz,
        "sweep_points": args.sweep_points,
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
            "box_conductivity": getattr(
                args, "box_conductivity", model_builder.DEFAULT_BOX_CONDUCTIVITY),
            "mat_conductor_pcb": args.mat_conductor_pcb,
            "mat_conductor_chip": args.mat_conductor_chip,
            "perfect_e_conductors": args.perfect_e_conductors,
            "mat_wirebond": args.mat_wirebond,
            "include_wirebonds": args.include_wirebonds,
        },
    }, getattr(args, "setting", None))
    # Recorded with the run, so a run says which preset it was made with and
    # which of the five settings it then changed. Web-made runs have neither.
    settings["solver_preset"] = preset
    settings["solver_overrides"] = [key for key in solver_presets.SOLVER_KEYS if key in overrides]
    return settings


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the analysis client pipeline end to end")
    parser.add_argument(
        "--sweep", action="append", default=[], metavar="NAME=SPEC",
        help='A parameter to sweep, e.g. --sweep pedestal_depth=0:10:2 (repeatable for '
             "multiple parameters -- combinations are the cartesian product across all of them)",
    )
    parser.add_argument(
        "--setting", action="append", default=[], metavar="NAME=VALUE",
        help="Set any analysis setting by name, e.g. --setting which_line=chip or "
             "--setting line_tolerance_ohm=1.5 (repeatable). For the settings that have no "
             "flag of their own, such as the line-impedance check's.",
    )
    parser.add_argument("--resume", type=str, default=None, help="Resume an existing run by its run_id instead of starting a new one")
    parser.add_argument(
        "--preset", choices=solver_presets.PRESET_NAMES, default=None,
        help="Solver settings for a new run as one set: quick (about 20 min a solve, not "
             "converged) or converged (4.5 to 10 h a solve). The five settings below "
             "(--max-passes, --min-passes, --min-converged, --max-delta-s, --max-delta-f) "
             "take the preset's values unless given explicitly. Without a preset they are "
             "the solver defaults, which are the converged values.",
    )
    parser.add_argument(
        "--solution-type", choices=[t["value"] for t in registry.solution_types()],
        default=registry.default_solution_type(),
        help="Eigenmode reports resonant frequencies and Q; DrivenModal drives the "
             "two ports and reports S11/S21. The settings below are grouped by which "
             "one they apply to -- the other group is ignored.",
    )
    # Eigenmode settings.
    parser.add_argument("--modes", type=int, default=20)
    parser.add_argument("--max-delta-f", type=float, default=None, help="Maximum Delta Frequency Per Pass (%%); default from --preset, else 3.0")
    parser.add_argument("--min-freq-safety-factor", type=float, default=0.1)
    # DrivenModal settings.
    parser.add_argument("--f-start-ghz", type=float, default=1.0, help="Frequency sweep start")
    parser.add_argument("--f-stop-ghz", type=float, default=20.0, help="Frequency sweep stop")
    parser.add_argument("--sweep-points", type=int, default=401, help="Frequency points reported by the sweep")
    parser.add_argument("--max-delta-s", type=float, default=None, help="Maximum Delta S Per Pass; default from --preset, else 0.02")
    parser.add_argument("--port-impedance-ohm", type=float, default=50.0)
    parser.add_argument(
        "--reference-plane", choices=[p["value"] for p in ports.REFERENCE_PLANES],
        default=ports.DEFAULT_REFERENCE_PLANE,
        help="Where the driven run's ports go: 'connector' puts a wave port on the "
             "SMP's air bore just above its bead (connector transition included); 'launch' "
             "puts a lumped port under the CPW trace just inboard of the land pattern "
             "(board only); 'bonds' puts one on the PCB just outboard of the wire bonds; "
             "'chip' puts one on the die's CPW just inboard of where the bonds land.",
    )
    parser.add_argument(
        "--fine-windows", choices=["auto", "off"], default="auto",
        help="Driven sweep: add a narrow, densely sampled window around each eigenmode "
             "(needs the paired Eigenmode run) beside the coarse sweep.")
    parser.add_argument(
        "--fine-windows-manual", default="", metavar="LO:HI:STEP[;...]",
        help="Driven sweep: extra fine windows in GHz, e.g. '12.1:12.4:0.002;15:15.2:0.001'.")
    parser.add_argument(
        "--adapt-at", choices=["f_stop", "highest_mode"], default="f_stop",
        help="Driven sweep: refine the mesh at the top of the sweep, or just above the "
             "highest eigenmode in it.")
    parser.add_argument(
        "--no-pair", dest="run_both", action="store_false", default=True,
        help="Solve only --solution-type. By default every combination is solved twice, "
             "as an Eigenmode run and then a Driven Modal run on the same geometry.")
    # Shared.
    # The five solver settings a preset sets (the two delta ones are below):
    # None means "not given", so the preset or the solver default fills them in.
    parser.add_argument("--max-passes", type=int, default=None, help="Default from --preset, else 99")
    parser.add_argument("--min-passes", type=int, default=None, help="Default from --preset, else 3")
    parser.add_argument("--min-converged", type=int, default=None, help="Default from --preset, else 2")
    parser.add_argument("--aedt-version", type=str, default=None)
    parser.add_argument("--no-student-version", dest="student_version", action="store_false", default=True)
    parser.add_argument("--graphical", action="store_true", help="Show the AEDT UI while building")
    parser.add_argument("--poll-seconds", type=float, default=10.0, help="Queue status poll interval")
    parser.add_argument("--mat-box", type=str, default="aluminum")
    parser.add_argument("--mat-pcb", type=str, default="Rogers TMM 10i (tm)")
    parser.add_argument("--mat-chip", type=str, default="silicon")
    parser.add_argument("--mat-vacuum", type=str, default="vacuum")
    parser.add_argument("--no-perfect-e-box", dest="perfect_e_box", action="store_false", default=True)
    parser.add_argument(
        "--box-conductivity", type=float, default=model_builder.DEFAULT_BOX_CONDUCTIVITY,
        help="With --no-perfect-e-box: the box and lid's conductivity in S/m (a finite-"
             "conductivity boundary). Perfect E puts mode and dip POSITIONS in the right "
             "place but exaggerates dip depth and Q.")
    parser.add_argument("--no-altium", dest="use_altium", action="store_false", default=True,
                        help="Skip layout generation and build the SolidWorks-only geometry")
    parser.add_argument("--mat-conductor-pcb", type=str, default="copper")
    parser.add_argument("--mat-conductor-chip", type=str, default="niobium")
    parser.add_argument("--no-perfect-e-conductors", dest="perfect_e_conductors",
                        action="store_false", default=True,
                        help="Use the named conductor materials instead of perfect E. "
                             "Only meaningful with a proper cryogenic surface impedance.")
    parser.add_argument("--mat-wirebond", type=str, choices=model_builder.WIREBOND_MATERIALS,
                        default=model_builder.MATERIAL_FALLBACKS["mat_wirebond"])
    parser.add_argument("--no-wirebonds", dest="include_wirebonds", action="store_false",
                        default=True,
                        help="Build the model with no wire bonds -- the die is left "
                             "unbonded. Everything else (including the bond pads) is "
                             "unchanged.")
    args = parser.parse_args(argv)
    if args.resume and args.preset:
        parser.error("--preset sets the solver settings of a new run; a resumed run keeps its own")
    return args


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
        driver = run.driven_elsewhere()
        if driver:
            raise SystemExit(
                f"Run {run.run_id} is already being driven by process {driver} "
                "(the web app, or another command line). Stop it there first."
            )
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
        try:
            run = create_run(sweep_params, build_analysis_settings(args), parameter_defaults)
        except ValueError as exc:
            raise SystemExit(f"{exc} (--no-pair solves just the one --solution-type.)")
        logger.info("Created run %s with %d combination(s)", run.run_id, len(run.state["combos"]))
        solver = run.analysis_settings
        logger.info(
            "Solver settings: preset %s%s -- %s",
            solver.get("solver_preset") or "none",
            f" (overridden: {', '.join(solver['solver_overrides'])})" if solver.get("solver_overrides") else "",
            ", ".join(f"{key}={solver.get(key)}" for key in solver_presets.SOLVER_KEYS),
        )

    stop_event = threading.Event()
    stop_on_interrupt(stop_event)
    # The web app stops a run it cannot reach in-process by writing the stop
    # file into the run's folder (see pipeline_child).
    from . import pipeline_child
    pipeline_child.clear_stop_request(run)
    threading.Thread(target=pipeline_child.watch_for_stop, args=(run, stop_event), daemon=True).start()
    # Likewise a pause or resume the web app asks for (see workflow/queue_hold.py).
    threading.Thread(target=pipeline_child.watch_for_queue_requests,
                     args=(run, queue, stop_event), daemon=True).start()
    run_pipeline(run, queue, stop_event=stop_event)
    try:
        pipeline_child.apply_queue_request(run, queue)
    except Exception:
        logger.exception("Could not apply a pause or resume for run %s", run.run_id)


if __name__ == "__main__":
    main()
