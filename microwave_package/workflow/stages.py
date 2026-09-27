"""
The local half of a run -- Altium, SolidWorks and AEDT -- as three
queues that drain at the same time.

Each of the three programs is single-instance and drives one shared
document, so what a run does *inside* one of them is strictly
sequential: one combination at a time, in order. Between them it is not.
A combination's layout and its solid are worked out from the same
numbers and neither reads the other's output, so Altium can be drawing
combination 5's boards while SolidWorks rebuilds combination 4's
assembly and AEDT imports combination 3's STEP. Only the third depends
on the other two -- a model needs both the copper and the solid -- so a
combination is handed to AEDT when, and only when, both of its halves
have landed (see `_Gate`).

That is the whole idea: three queues with one worker each, rather than
one loop doing everything for a combination before starting the next. A
sweep whose Altium half and SolidWorks half each take a minute now costs
about as long as the slower of the two rather than their sum.

Each stage records what it is doing on the run as it goes
(`altium_status`, `step_status`, `model_status`), which is what the run
page's per-combination bar is made of. With three programs working on
three different combinations at once, "what stage is this run in" stops
having a single answer -- the per-combination record is the answer.

Stopping: every lane checks between combinations, so a stop is acted on
within one combination rather than at once. None of the three programs
can be interrupted mid-call -- a SolidWorks rebuild or an HFSS import
finishes whatever it is doing -- so the run stops as each program comes
back, with everything finished up to that point recorded and nothing to
rebuild on resume.
"""
from __future__ import annotations

import contextlib
import logging
import os
import queue as queue_module
import threading
from typing import Callable, Dict, List, Optional

from ..design import altium_generation, equations as eq, step_generation
from ..hfss import model_builder
from . import resilient_io

logger = logging.getLogger("microwave_package")

# How long a lane waits on an empty queue before looking up to see
# whether the run has been stopped, or its input sealed. Short enough
# that a stop is acted on promptly, long enough to cost nothing.
_TICK = 0.25


@contextlib.contextmanager
def _com_on_this_thread():
    """COM, initialised for the thread that is about to drive a program.

    Windows requires EVERY thread that uses COM to call CoInitialize()
    itself. The thread the pipeline runs on does that for itself (see
    webapp.py), but each lane is its own thread and two of the three
    programs are driven over COM from it -- SolidWorks through win32com,
    AEDT through pyaedt. Without this, the first call fails with
    "CoInitialize has not been called", which reads like SolidWorks not
    being installed rather than like a threading mistake.

    pywin32 is a Windows-only dependency; where it is missing, nothing
    on this thread could be driving COM anyway.
    """
    try:
        import pythoncom
    except ImportError:
        yield
        return
    pythoncom.CoInitialize()
    try:
        yield
    finally:
        pythoncom.CoUninitialize()


def _prefixed(log: Callable[[str], None], program: str) -> Callable[[str], None]:
    """Three programs log into the same stream at the same time, so every
    line says which one it came from."""
    return lambda message: log(f"{program}: {message}")


class _Lane:
    """One program's queue, drained in order on its own thread.

    `work(combo_id)` returns whether that combination came out usable,
    and `on_finished(combo_id, ok)` is how the next lane hears about it.

    An exception out of `work` is treated as fatal rather than as one
    combination's failure: every stage reports a combination it could
    not build by returning False, so an exception means the *program*
    could not be used at all (SolidWorks not installed, AEDT refusing to
    start). The next combination will not do any better, so it stops the
    run instead of being repeated once per combination. The exception is
    kept for the coordinator to re-raise on the thread that started the
    run, and `abort` tells the other lanes to stop too.
    """

    def __init__(
        self,
        program: str,
        work: Callable[[str], bool],
        close: Optional[Callable[[], None]] = None,
        on_finished: Optional[Callable[[str, bool], None]] = None,
        stop_event=None,
        abort: Optional[threading.Event] = None,
    ) -> None:
        self.program = program
        self.error: Optional[BaseException] = None
        self.on_finished = on_finished
        self._work = work
        self._close = close
        self._stop_event = stop_event
        self._abort = abort if abort is not None else threading.Event()
        self._queue: "queue_module.Queue" = queue_module.Queue()
        self._sealed = threading.Event()
        self.thread = threading.Thread(target=self._drain, name=f"stage-{program}", daemon=True)

    # -- the coordinator's side ----------------------------------------
    def submit(self, combo_id: str) -> None:
        self._queue.put(combo_id)

    def seal(self) -> None:
        """Nothing more will be submitted: finish what is queued, then stop."""
        self._sealed.set()

    def start(self) -> "_Lane":
        self.thread.start()
        return self

    @property
    def stopping(self) -> bool:
        stopped = self._stop_event is not None and self._stop_event.is_set()
        return bool(stopped) or self._abort.is_set()

    # -- the lane's own thread -----------------------------------------
    def _drain(self) -> None:
        with _com_on_this_thread():
            self._work_through_the_queue()

    def _work_through_the_queue(self) -> None:
        try:
            while not self.stopping:
                try:
                    combo_id = self._queue.get(timeout=_TICK)
                except queue_module.Empty:
                    if self._sealed.is_set():
                        return
                    continue
                try:
                    ok = bool(self._work(combo_id))
                except resilient_io.Stopped:
                    # Something this combination was waiting for gave up
                    # because the run was stopped. That is the stop
                    # arriving, not this program failing.
                    return
                except Exception as exc:
                    self.error = exc
                    self._abort.set()
                    logger.exception("The %s stage could not continue", self.program)
                    return
                if self.on_finished is not None:
                    self.on_finished(combo_id, ok)
        finally:
            if self._close is not None:
                try:
                    self._close()
                except Exception:
                    logger.exception("Closing the %s stage failed", self.program)


class _Gate:
    """Holds a combination back until every stage it depends on has landed.

    A model needs both the copper and the solid, and the two arrive in
    whatever order their programs happen to finish them, so something
    has to count. A combination whose layout or geometry failed is
    dropped here rather than passed on: there is nothing to build a
    model from, and the stage that failed has already recorded why.
    """

    def __init__(self, depends_on: int, on_ready: Callable[[str], None]) -> None:
        self._depends_on = depends_on
        self._on_ready = on_ready
        self._left: Dict[str, int] = {}
        self._lock = threading.Lock()

    def report(self, combo_id: str, ok: bool) -> None:
        with self._lock:
            left = self._left.get(combo_id, self._depends_on)
            if left <= 0:
                return  # already released, or already dropped
            left = left - 1 if ok else 0
            self._left[combo_id] = left
            ready = ok and left == 0
        if ready:
            self._on_ready(combo_id)


class _AltiumStage:
    """Regenerates one combination's boards and exports their copper.

    Reads nothing SolidWorks writes: its numbers come from the run's own
    parameters laid over the equations file *in memory*, never from the
    shared file on disk -- which the SolidWorks stage is rewriting for a
    different combination at the same time.
    """

    def __init__(self, run, original_content, parameter_defaults, materials, log) -> None:
        self._run = run
        self._original = original_content
        self._defaults = parameter_defaults or {}
        self._materials = materials
        self._log = log

    def build(self, combo_id: str) -> bool:
        run = self._run
        combo = run.state["combos"][combo_id]
        # A resumed run does not redraw a layout it already has. The file
        # is checked as well as the record, because the copper is what
        # the model is actually built from.
        if combo.get("altium_status") == "done" and os.path.isfile(
            os.path.join(run.combo_path(combo_id), "copper.txt")
        ):
            return True

        run.update_combo(combo_id, altium_status="running")
        values = eq.get_numeric_parameters(
            eq.with_values(self._original, self._defaults, combo["params"])
        )
        ok, reason = altium_generation.generate_for_combo(
            run.combo_dir(combo_id), values, materials=self._materials, log=self._log
        )
        run.update_combo(
            combo_id,
            altium_status="done" if ok else "failed",
            altium_error=None if ok else reason,
        )
        if not ok:
            # One unbuildable layout is recorded and skipped, the same way
            # a failed STEP is. It must not take the rest of the sweep
            # with it.
            self._log(f"{combo_id}: {reason} -- no model will be built for it.")
        return ok


def _needs_new_step_files(run, combo_id: str) -> bool:
    """Whether a combination still has a model to build from STEP files.

    Asks the AEDT stage's own question (model_builder.reusable_model): a
    model it will reuse never reads the STEP files again, so a run from
    before they were split resumes without re-exporting geometry nothing
    will look at -- and one it will rebuild gets files it can use.
    """
    return model_builder.reusable_model(run, combo_id, log=lambda _m: None) is None


class _SolidWorksStage:
    """Rebuilds box.SLDPRT and lid_inside.SLDPRT for one combination and
    exports each to its own STEP.

    The session is opened on the first combination that actually needs
    it, so resuming a run whose geometry is already on disk never starts
    SolidWorks at all.
    """

    def __init__(self, run, original_content, parameter_defaults,
                 equations_path, part_paths, log) -> None:
        self._run = run
        self._original = original_content
        self._defaults = parameter_defaults or {}
        self._equations_path = equations_path
        self._part_paths = part_paths
        self._log = log
        self._session: Optional[step_generation.Session] = None
        self._needed = False

    def build(self, combo_id: str) -> bool:
        run = self._run
        combo = run.state["combos"][combo_id]
        if combo.get("step_status") == "done":
            if combo.get("step_paths") or not _needs_new_step_files(run, combo_id):
                return True
            # Exported before box and lid_inside became separate files:
            # one assembly STEP, which the model builder can no longer use.
            self._log(f"{combo_id}: geometry predates the separate box/lid_inside "
                      "STEP files -- rebuilding it.")

        self._needed = True
        run.update_combo(combo_id, step_status="running")
        content = eq.with_values(self._original, self._defaults, combo["params"])
        self._log(f"Building geometry for {combo_id} ({combo['params']})...")
        step_paths, reason = self._session_now().build(
            combo_id, content, run.combo_dir(combo_id), log=self._log
        )
        if step_paths is None:
            self._log(f"FAILED to save STEP for {combo_id}: {reason}")
            run.update_combo(combo_id, step_status="failed", step_error=reason)
            return False
        run.update_combo(combo_id, step_paths=step_paths, step_status="done", step_error=None)
        return True

    def _session_now(self) -> "step_generation.Session":
        if self._session is None:
            self._session = step_generation.Session.open(
                self._equations_path, self._part_paths, log=self._log
            )
        return self._session

    def close(self) -> None:
        if self._session is not None:
            self._session.close(log=self._log)
            self._session = None
        elif not self._needed:
            # Said only when there was genuinely nothing to do. A session
            # that failed to open is not that, and has already said so.
            self._log("Every STEP file was already on disk -- SolidWorks was never started.")
        step_generation.write_common_dimensions(self._run, log=self._log)


class _AedtStage:
    """Builds one combination's HFSS model, then hands it over.

    The handoff is the caller's, not this stage's: building is local work
    that needs AEDT and no network, and handing over is network work that
    needs no AEDT (see `model_builder`). Keeping the network out of here
    is what lets a whole run be prepared with the queue disconnected.
    """

    def __init__(self, run, analysis_settings, handoff, log) -> None:
        self._run = run
        self._settings = analysis_settings
        self._handoff = handoff
        self._log = log

    def build(self, combo_id: str) -> bool:
        run = self._run
        combo = run.state["combos"][combo_id]
        if combo.get("task_id"):
            self._log(f"{combo_id} was already handed over (task {combo['task_id']}).")
            return True
        if combo.get("model_status") != "built":
            run.update_combo(combo_id, model_status="building")

        settings = self._settings
        dimensions = eq.read_file(os.path.join(run.combo_dir(combo_id), "dimensions.txt"))
        object_names = model_builder.build_model(
            combo_id, combo.get("step_paths") or {}, dimensions, run, settings,
            settings.get("aedt_version"), settings.get("student_version", True),
            settings.get("graphical", False), log=self._log,
        )
        if object_names is None:
            # build_model has already said which of its reasons it was.
            run.update_combo(
                combo_id, model_status="failed",
                model_error="AEDT could not make this model ready -- see the run log.",
            )
            return False

        self._handoff(combo_id, object_names)
        return True


def run_local_stages(
    run,
    *,
    equations_path: str,
    part_paths: Dict[str, str],
    analysis_settings: dict,
    handoff: Callable[[str, Dict[str, str]], None],
    parameter_defaults: Optional[Dict[str, float]] = None,
    log: Callable[[str], None] = logger.info,
    stop_event=None,
) -> None:
    """Draw, build and model every combination of `run`, three programs
    at a time, handing each finished model over as it lands.

    Resumable at the granularity of one program and one combination:
    each stage skips what it has already done, so a run picked back up
    redraws no layout, rebuilds no solid and rebuilds no model it does
    not have to.

    Raises whatever stopped a program that could not be used at all --
    that is the run's error, and it is raised on the caller's thread
    rather than being swallowed on a worker's.
    """
    combos = run.state["combos"]
    for combo_id, combo in combos.items():
        if not combo.get("skip_reason"):
            continue
        # Never handed to any of the three: the geometry cannot fit, which
        # was decided when the run was created (see constraints.py).
        if combo.get("step_status") != "skipped":
            run.update_combo(combo_id, step_status="skipped")
        log(f"Skipping {combo_id} -- {combo['skip_reason']}")

    buildable = [cid for cid, combo in combos.items() if not combo.get("skip_reason")]
    if not buildable:
        log("Nothing to build -- every combination in this run was skipped.")
        return

    clear_stale_progress(run, buildable)

    original_content = eq.read_file(equations_path)
    abort = threading.Event()

    upstream: List[_Lane] = []
    if analysis_settings.get("use_altium", True):
        altium = _AltiumStage(
            run, original_content, parameter_defaults,
            analysis_settings.get("materials"), _prefixed(log, "Altium"),
        )
        upstream.append(_Lane("Altium", altium.build, stop_event=stop_event, abort=abort))
    else:
        log("Altium is off for this run -- building the SolidWorks-only geometry, "
            "with no copper.")

    solidworks = _SolidWorksStage(
        run, original_content, parameter_defaults, equations_path, part_paths,
        _prefixed(log, "SolidWorks"),
    )
    upstream.append(_Lane(
        "SolidWorks", solidworks.build, close=solidworks.close,
        stop_event=stop_event, abort=abort,
    ))

    aedt = _AedtStage(run, analysis_settings, handoff, _prefixed(log, "AEDT"))
    aedt_lane = _Lane("AEDT", aedt.build, stop_event=stop_event, abort=abort)
    gate = _Gate(len(upstream), aedt_lane.submit)
    for lane in upstream:
        lane.on_finished = gate.report

    lanes = [*upstream, aedt_lane]
    programs = [lane.program for lane in lanes]
    log(f"{len(buildable)} combination(s) to prepare. "
        f"{', '.join(programs[:-1])} and {programs[-1]} work at the same time, "
        "each on its own combination.")

    for lane in lanes:
        lane.start()
    try:
        for combo_id in buildable:
            for lane in upstream:
                lane.submit(combo_id)
        for lane in upstream:
            lane.seal()
        for lane in upstream:
            lane.thread.join()
        # Nothing can reach the gate now that both of its feeders have
        # finished, so what AEDT has been given is all it will get.
        aedt_lane.seal()
        aedt_lane.thread.join()
    finally:
        # However this ends -- finished, stopped, or one program taking
        # the run down -- no lane is left waiting for work that will
        # never come.
        for lane in lanes:
            lane.seal()

    for lane in lanes:
        if lane.error is not None:
            raise lane.error


# The per-combination fields that mean "a program is working on this
# right now", and what they say when it is.
BUSY_FIELDS = {
    "altium_status": "running",
    "step_status": "running",
    "model_status": "building",
    "post_status": "running",
}


def clear_stale_progress(run, combo_ids=None) -> None:
    """Forget work that was in flight when the run last stopped.

    A run that was stopped (or killed) while a program was in the middle
    of a combination left that combination marked as in progress.
    Nothing is running now, so clearing those before the queues are
    seeded is what keeps the run page from showing work nobody is doing.
    """
    for combo_id in (combo_ids if combo_ids is not None else run.state["combos"]):
        combo = run.state["combos"][combo_id]
        stale = {field: None for field, busy in BUSY_FIELDS.items() if combo.get(field) == busy}
        if stale:
            run.update_combo(combo_id, **stale)
