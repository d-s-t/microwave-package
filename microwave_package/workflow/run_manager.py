"""
Manages one run's on-disk state: its folder layout, its resumable
progress record (state.json), and the sweep configuration that produced
it (run_config.json). A "run" is one full pass of the pipeline over one
parameter sweep -- possibly spanning multiple pipeline invocations if
it's interrupted (share down, process killed, machine rebooted, ...) and
resumed later. Every stage records its progress per combination as it
goes, so resuming just skips whatever's already done.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import socket
import threading
import time
from typing import Dict, List, Optional

import psutil

from ..design import constraints
from ..design import equations as eq
from ..design import parameter_meta
from .. import paths
from ..design.sweep import SweepParameter, combinations, combo_id_for

logger = logging.getLogger("microwave_package")


# What happens to a combination, in the order it happens, and which of
# the combination's status fields records each one. `states` maps that
# field's values into the one vocabulary everything downstream paints:
# pending / running / waiting / done / failed / skipped ("waiting" being
# real progress that nothing on this machine is working on -- a project
# sitting in the queue, or one built while the share was unreachable).
#
# The first two run AT THE SAME TIME, on different combinations (see
# workflow/stages.py); everything from the model onwards is strictly in
# order. One list, used by the progress counts, the run page's
# per-combination bar and the live status feed, so all three agree on
# what a run is doing -- which matters more now that "what stage is the
# run in" has three answers at once.
PHASES = (
    {"key": "layout", "label": "Altium", "what": "the boards, drawn in Altium",
     "field": "altium_status", "parallel": True,
     "states": {"running": "running", "done": "done", "failed": "failed"}},
    {"key": "geometry", "label": "SolidWorks", "what": "the solid, rebuilt and exported as STEP",
     "field": "step_status", "parallel": True,
     "states": {"running": "running", "done": "done", "failed": "failed",
                "skipped": "skipped"}},
    {"key": "model", "label": "AEDT model", "what": "the HFSS model, built on this machine",
     "field": "model_status",
     "states": {"building": "running", "built": "done", "failed": "failed"}},
    {"key": "upload", "label": "Copy to queue", "what": "the project, copied to the shared queue",
     "field": "handoff_status", "waiting_label": "Waiting for the queue",
     "states": {"running": "running", "done": "done", "deferred": "waiting",
                "failed": "failed"}},
    {"key": "analysis", "label": "Analysis", "what": "the solve, run by the worker",
     "field": "queue_status", "waiting_label": "Queued",
     # "held" is a task the user has paused out of the queue (see
     # workflow/queue_hold.py): waiting for the worker in every way that
     # counts, and shown as held on its own where that matters.
     "states": {"pending": "waiting", "held": "waiting", "in_progress": "running",
                "done": "done", "failed": "failed"}},
    {"key": "download", "label": "Copy back", "what": "the analysed files, copied off the queue",
     "field": "fetch_status",
     "states": {"running": "running", "done": "done", "failed": "failed"}},
    {"key": "results", "label": "Results", "what": "the results, read out of the solved project",
     "field": "post_status",
     "states": {"running": "running", "done": "done", "failed": "failed"}},
)
PHASE_KEYS = [phase["key"] for phase in PHASES]

# The first phase that can only happen after every earlier one has --
# see `phase_states` for what that is used for.
_STRICTLY_ORDERED_FROM = PHASE_KEYS.index("model")


def phase_states(combo: dict, use_altium: bool = True) -> Dict[str, str]:
    """What each of a combination's phases is doing, in one vocabulary.

    A run made before a phase was recorded separately still reads right:
    a combination only reaches AEDT with both of its halves in hand, and
    only reaches the queue with a model, so anything from the model
    onwards means everything before it landed, whether or not there is a
    field saying so. Nothing is inferred BETWEEN the layout and the
    geometry -- those two run at the same time, in either order, and one
    being finished says nothing about the other.
    """
    if combo.get("skip_reason"):
        return {key: "skipped" for key in PHASE_KEYS}

    states = {}
    for phase in PHASES:
        value = combo.get(phase["field"])
        states[phase["key"]] = phase["states"].get(value, "pending")
    if not use_altium:
        states["layout"] = "skipped"
    # A run prepared before the handoff had a field of its own still has
    # the one thing that proves it happened.
    if states["upload"] == "pending" and combo.get("task_id"):
        states["upload"] = "done"

    # Read backwards: anything a combination has reached is proof of
    # everything that had to happen first. Only the phases that cannot
    # happen out of order count as that proof, which is why the layout
    # and the geometry are evidence about nothing -- they run at the
    # same time.
    later = False
    for key in reversed(PHASE_KEYS):
        if later and states[key] == "pending":
            states[key] = "done"
        if PHASE_KEYS.index(key) >= _STRICTLY_ORDERED_FROM and states[key] != "pending":
            later = True
    return states


def combo_state(states: Dict[str, str]) -> str:
    """One word for a whole combination: what the run page's filter chips
    count, and what colours its row."""
    values = list(states.values())
    if "failed" in values:
        return "failed"
    if all(value == "skipped" for value in values):
        return "skipped"
    if all(value in ("done", "skipped") for value in values):
        return "done"
    if any(value in ("running", "waiting", "done") for value in values):
        return "running"
    return "pending"


# The one failure Resume does not retry: the worker's own -- a failed task
# stays failed on the queue. Everything before it is redone for any
# combination not yet done (see stages.run_local_stages), and everything
# after it -- copying the files back, reading them -- is retried from the
# queue or the local copy (see postprocess.postprocess_run). Any failure
# used to count as a combination's end, so a run whose last copy back the
# laptop's standby cut off (it drops the network) lost its Resume button.
FINAL_FAILURES = ("analysis",)


def _finished(states: Dict[str, str]) -> bool:
    """Whether a combination is at its end: done, skipped, or failed in a
    way Resume does not retry."""
    state = combo_state(states)
    if state == "failed":
        return any(states[key] == "failed" for key in FINAL_FAILURES)
    return state in ("done", "skipped")


def progress_of(state: dict, analysis_settings: Optional[dict] = None) -> Dict[str, object]:
    """How far a run has got, counted from its combinations.

    One definition, used by both the run list and the run page, so the
    numbers agree: a combination is *done* once every phase of it is,
    *failed* as soon as any phase of it failed, and otherwise still on
    its way.

    Skipped combinations (their geometry never fitted, so nothing is
    built for them) are *not* progress -- a run whose every buildable
    combination is done is finished, however many were skipped. `total`
    is therefore the buildable count; `skipped` and `grand_total` are
    reported alongside for context.

    `phases` counts the same combinations again, once per phase. That is
    what makes a run readable while three programs are working on three
    different combinations at once: "18 drawn, 17 built, 14 modelled, 9
    analysed" says what a single overall bar cannot.
    """
    combos = state.get("combos", {})
    use_altium = (analysis_settings or {}).get("use_altium", True)
    counted = {key: {"done": 0, "running": 0, "waiting": 0, "failed": 0} for key in PHASE_KEYS}
    done = failed = skipped = 0

    for combo in combos.values():
        states = phase_states(combo, use_altium)
        overall = combo_state(states)
        if overall == "skipped":
            skipped += 1
            continue
        for key, value in states.items():
            if value in counted[key]:
                counted[key][value] += 1
        if overall == "failed":
            failed += 1
        elif overall == "done":
            done += 1

    grand_total = len(combos)
    total = grand_total - skipped
    phases = [
        {
            "key": phase["key"], "label": phase["label"], "what": phase["what"],
            "parallel": bool(phase.get("parallel")), "total": total,
            **counted[phase["key"]],
            "pending": total - sum(counted[phase["key"]].values()),
        }
        for phase in PHASES
        if use_altium or phase["key"] != "layout"
    ]
    return {
        "total": total,
        "done": done,
        "failed": failed,
        "skipped": skipped,
        "pending": total - done - failed,
        "grand_total": grand_total,
        "phases": phases,
    }


# -- the hidden parts of a run ----------------------------------------------
# A run can have hidden parts (see companion.py): its eigenmode run, and the
# refined solves made from it. They are listed under their parent's row, not
# as runs of their own. A part's state is read from its combinations' own
# fields -- the queue's and the results' -- not from its stage name, which
# reads "postprocessing" for as long as the worker is still solving the part.

def part_kind(config: dict) -> str:
    """What a hidden part is: "eigen" for a driven run's eigenmode run,
    "refinement" for a refined solve, "field" for a field-image part, and
    "part" for anything else."""
    # An eigenmode part of a driven run (companion.create_eigenmode_part): terminated
    # like the driven run, and not the hidden pair below.
    if config.get("eigen_of"):
        return "eigenmodes"
    if config.get("part_role") == "eigen" or config.get("driven_run_id"):
        return "eigen"
    # A field-image part is named by its frequency: it has no range to refine.
    if (config.get("analysis_settings") or {}).get("field_frequency_ghz") is not None:
        return "field"
    if config.get("refines_run_id"):
        return "refinement"
    return "part"


# The stages in which a run's pipeline is doing work. Any other stage says
# nothing is being done: "complete", "stopped", "error" or "created".
ACTIVE_STAGES = frozenset({"eigenmodes", "generating_and_queuing", "geometry_ready",
                           "monitoring", "postprocessing", "plotting"})

# A remote-worker run stays in stage "postprocessing" for its whole wait, while
# its analyses are still on the worker (the pipeline's waiting loop). What the
# page calls that wait is worked out from the combinations, by displayed_stage.
WAITING_ON_WORKER = "waiting_for_worker"
SOLVING_ON_WORKER = "solving_on_worker"


def displayed_stage(stage: Optional[str], combos: Optional[dict]) -> Optional[str]:
    """The stage the page shows for a run. Only "postprocessing" is reworded, and
    only while some of its analyses are still on the worker: "solving_on_worker"
    while one of them is in progress there, "waiting_for_worker" while the rest
    are pending or held. Otherwise the stage as stored is shown -- a run whose
    analyses are all back is really being read and processed.

    `stage` stays as stored: this is the words only, so ACTIVE_STAGES and resume
    are unaffected, and runs saved before this was written need no migration.
    """
    if stage != "postprocessing":
        return stage
    unfinished = [
        combo.get("queue_status") for combo in (combos or {}).values()
        if isinstance(combo, dict) and combo.get("task_id")
        and combo.get("queue_status") not in ("done", "failed")
    ]
    if "in_progress" in unfinished:
        return SOLVING_ON_WORKER
    if any(status in ("pending", "held", None) for status in unfinished):
        return WAITING_ON_WORKER
    return stage


# The part states that are not still going, and not a failure to act on.
DONE_PART_STATES = frozenset({"complete", "failed", "stopped"})


def part_state(state: dict, use_altium: bool = True) -> str:
    """Where a hidden part has got to, in one word. This is the one place that
    decides it; the words the page shows for each are the webapp's.

    The part's own stage decides first. A stage that is "complete" has no work
    left. A "stopped" or "error" stage is not solving anything, whatever its
    combinations' queue fields still say: a stopped run can leave a task
    reading "in_progress". "solving" needs a live stage as well as a task on
    the worker. Then the combinations say what is left:

    "complete" -- finished: its stage is complete, or every combination is;
    "stopped" -- its stage is stopped;
    "failed" -- its stage is error, or a combination failed and can be retried;
    "solving" -- a combination is running on the worker, in a live stage;
    "paused" -- queued, and the queue is paused for it;
    "queued" -- waiting for the worker to take it;
    "reading" -- the worker is done with a combination, and its results are
        being copied back and read;
    "waiting" -- its files cannot reach the queue yet;
    "preparing" -- its geometry and model are still being built here;
    "starting" -- nothing has been done yet, or a task is left "in progress"
        in a stage that is not live.
    """
    stage = state.get("stage")
    combos = [combo for combo in (state.get("combos") or {}).values() if isinstance(combo, dict)]
    unfinished = []
    for combo in combos:
        phases = phase_states(combo, use_altium)
        if not _finished(phases):
            unfinished.append((combo, phases))
    if stage == "complete" or (combos and not unfinished):
        return "complete"
    if stage == "stopped":
        return "stopped"
    if stage == "error":
        return "failed"
    live = stage in ACTIVE_STAGES

    seen = set()
    for combo, phases in unfinished:
        if phases["analysis"] == "running":
            seen.add("solving" if live else "starting")
        elif phases["analysis"] == "waiting" and combo.get("queue_status") == "held":
            seen.add("paused")
        elif phases["analysis"] == "waiting" or (phases["analysis"] == "pending" and phases["upload"] == "done"):
            seen.add("queued")
        elif combo_state(phases) == "failed":
            seen.add("failed")
        elif phases["analysis"] == "done":
            seen.add("reading")
        elif phases["upload"] == "waiting":
            seen.add("waiting")
        else:
            seen.add("preparing")

    for word in ("solving", "paused", "queued", "reading", "waiting"):
        if word in seen:
            return word
    if "failed" in seen:
        return "failed"
    if "preparing" in seen:
        return "preparing"
    return "starting"


def part_record(run_id: str, config: dict, state: dict, updated: float = 0.0) -> dict:
    """One hidden part as its parent's row needs it: its kind, what it is doing
    (`part_state`), the fine windows it solves (a refined solve only), how far its
    combinations have got, and when its state was last written (`updated`, a
    modification time, so a recent failure can be told from an old one). Made from
    the state and config the run list has already read for the part; the time is
    a stat of the same file."""
    analysis = config.get("analysis_settings") or {}
    counts = progress_of(state, analysis)
    kind = part_kind(config)
    return {
        "run_id": run_id,
        "kind": kind,
        "state": part_state(state, analysis.get("use_altium", True)),
        "windows": (analysis.get("fine_windows_manual") or "") if kind == "refinement" else "",
        "frequency_ghz": analysis.get("field_frequency_ghz") if kind == "field" else None,
        "done": counts["done"],
        "total": counts["total"],
        "failed": counts["failed"],
        "updated": updated,
    }


def _modified(path: str) -> float:
    """When a file was last written, in seconds since the epoch; 0 if it cannot be read."""
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _equation_file_values() -> Dict[str, float]:
    """The equations file's own numeric values, as the base every
    combination is layered on top of. An unreadable file is not fatal
    here -- it only means the constraint check has nothing to work with,
    and STEP generation will fail with a better message than this could.
    """
    try:
        return eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))
    except OSError:
        logger.warning("Could not read %s to check geometry constraints.", paths.DEFAULT_EQUATIONS_FILE)
        return {}


def _without_retired_config(config: dict) -> dict:
    """An old run's config with the retired parameters (parameter_meta.RETIRED_PARAMETERS)
    left out. Done in memory on load only: the run's file on disk is not rewritten."""
    config = dict(config)
    if "parameter_defaults" in config:
        config["parameter_defaults"] = {
            name: value for name, value in config["parameter_defaults"].items()
            if not parameter_meta.is_retired(name)}
    if "sweep_params" in config:
        config["sweep_params"] = [
            spec for spec in config["sweep_params"] if not parameter_meta.is_retired(spec.get("name", ""))]
    return config


def _without_retired_state(state: dict) -> dict:
    """An old run's state with the retired parameters left out of each combination's values."""
    combos = state.get("combos")
    if not isinstance(combos, dict):
        return state

    def strip(combo):
        if not isinstance(combo, dict) or not isinstance(combo.get("params"), dict):
            return combo
        return {**combo, "params": {name: value for name, value in combo["params"].items()
                                    if not parameter_meta.is_retired(name)}}

    return {**state, "combos": {cid: strip(combo) for cid, combo in combos.items()}}


def _skip_reason(values: Dict[str, float], use_altium: bool) -> Optional[str]:
    """Why a combination will not be built, decided when its run is
    created, or None when it will be.

    The via grid is only checked when the run draws a layout. Without
    Altium there are no vias to place, so the via parameters cannot make
    a combination unbuildable.
    """
    if use_altium:
        return constraints.check_via_grid(values)
    return None


def geometry_refusal(sweep_params, parameter_defaults=None, use_altium: bool = True) -> Optional[str]:
    """Why a new run is refused, when a combination of it cannot be built with the
    current model builder, or None.

    Two cases: vents, which every run is refused (see constraints.check_vents), and,
    when Altium draws the copper, a pedestal recess the copper cannot close (see
    constraints.check_recess_copper). Every combination is checked with the layering
    `Run.create` applies, so the answer is for the geometry that would be built. The
    whole run is refused, and the message says how many combinations are, and the first
    few. Refinement and field parts are checked by companion, against their parent's own
    values, not here.
    """
    base = _equation_file_values()
    combos = combinations(sweep_params)
    refused = []
    for combo in combos:
        values = {**base, **(parameter_defaults or {}), **combo}
        reason = constraints.check_vents(values)
        if reason is None and use_altium:
            reason = constraints.check_recess_copper(values)
        if reason:
            refused.append((combo, reason))
    if not refused:
        return None
    first = refused[0][1]
    if len(combos) == 1:
        return f"Run not created: {first}"
    named = "; ".join(_combo_label(combo) for combo, _ in refused[:3])
    more = f" and {len(refused) - 3} more" if len(refused) > 3 else ""
    return (f"Run not created: {len(refused)} of {len(combos)} combinations are refused. "
            f"First: {first} Refused: {named}{more}.")


def _combo_label(combo: Dict[str, float]) -> str:
    """A combination's swept values, as the message names it."""
    if not combo:
        return "(no swept values)"
    return ", ".join(f"{name} = {value:g}" for name, value in combo.items())


def equation_file_values() -> Dict[str, float]:
    """The equations file's own numbers (what a run layers its values over)."""
    return _equation_file_values()


def parameter_values_for(name: str, sweep_params, parameter_defaults) -> List[float]:
    """Every value `name` takes across the combinations a run would build.

    The same layering `Run.create` applies to each combination -- the
    equations file underneath, the run's pinned values over it, the swept
    values on top -- so a check made against this answers for the geometry
    that will actually be built.

    That last layer is the one easy to forget: a parameter nobody typed
    into the form is not absent from the run, it just takes the value the
    equations file gives it. Treating it as absent is how a default
    `port_count` of 2 came to be reported as 0 ports.

    An empty list means the parameter is not in the equations file at all
    and the run has not pinned it, so there is genuinely nothing to check
    against -- better to let the build say so than to invent a value here.
    """
    for param in sweep_params:
        if param.name == name:
            return list(param.values)
    if name in (parameter_defaults or {}):
        return [parameter_defaults[name]]
    base = _equation_file_values()
    return [base[name]] if name in base else []


def held_count(state: dict) -> int:
    """How many of a run's combinations have their task held out of the
    worker queue (see workflow/queue_hold.py)."""
    return sum(1 for combo in state.get("combos", {}).values()
               if combo.get("queue_status") == "held")


def facet_inputs(config: dict) -> dict:
    """What the run list and the run page need to say what a run was built
    with (see workflow/run_facets.py), taken out of its config.

    Kept small and here, so `list_runs` can hand it on from the config it
    has already read -- no second read of the run's files per poll, and no
    import of the solver registry into this module.
    """
    analysis = config.get("analysis_settings") or {}
    materials = analysis.get("materials") or {}
    return {
        "parameter_defaults": dict(config.get("parameter_defaults") or {}),
        "sweep_params": list(config.get("sweep_params") or []),
        "solution_type": analysis.get("solution_type"),
        "mat_pcb": materials.get("mat_pcb"),
        # A driven run that also solved its eigenmodes: companion.EIGEN_LINK.
        "paired": bool(config.get("eigen_run_id")),
    }


def parent_of(config: dict) -> Optional[str]:
    """The run a run's config says it belongs to, or None.

    `part_of` is what runs made now carry. Runs made before a pair was one
    run have only the links they used to be joined by -- an eigenmode run
    that names its driven run, a refinement that names the run it refines
    -- and are the same thing: parts of that run, with their data on its
    page. They stay on disk exactly as they were.
    """
    return config.get("part_of") or config.get("refines_run_id") or config.get("driven_run_id")


class Run:
    # When the atomic replace is being blocked by something outside this
    # process, this holds the time until which to assume it still is.
    _replace_blocked_until = 0.0

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.run_dir = os.path.join(str(paths.RUNS_ROOT), run_id)
        self.plots_dir = os.path.join(self.run_dir, "plots")
        self.config_path = os.path.join(self.run_dir, "run_config.json")
        self.state_path = os.path.join(self.run_dir, "state.json")
        self.config: dict = {}
        self.state: dict = {"stage": "created", "combos": {}}
        # STEP/AEDT work and queue monitoring now run at the same time
        # (see pipeline.run_pipeline), and both record progress here, so
        # every read-modify-write of the state file is serialised.
        self._lock = threading.RLock()
        # Held by whatever moves this run's tasks in or out of the worker
        # queue: queuing a task, the monitor reading a task's status, and a
        # pause or resume (see workflow/queue_hold.py). Taken before
        # `_lock`, never after it.
        self._queue_lock = threading.RLock()

    # -- creation / loading -------------------------------------------------
    @classmethod
    def create(
        cls,
        sweep_params: List[SweepParameter],
        analysis_settings: dict,
        parameter_defaults: Optional[Dict[str, float]] = None,
        *,
        part_of: Optional["Run"] = None,
        role: Optional[str] = None,
    ) -> "Run":
        """`part_of` makes a hidden part of another run (see companion.py):
        it is stored like any run but is not listed on its own, and its id
        carries `role` so the folder says what it is."""
        run_id = _dt.datetime.now().strftime("run_%Y%m%d_%H%M%S")
        if part_of is not None:
            run_id = f"{part_of.run_id}_{role or 'part'}"
        # Two runs made in the same second must not share a folder.
        taken, suffix = run_id, 1
        while os.path.exists(os.path.join(str(paths.RUNS_ROOT), run_id)):
            suffix += 1
            run_id = f"{taken}_{suffix}"
        run = cls(run_id)
        os.makedirs(run.run_dir, exist_ok=True)
        os.makedirs(run.plots_dir, exist_ok=True)

        run.config = {
            "sweep_params": [{"name": p.name, "values": p.values} for p in sweep_params],
            # Parameters pinned to one value for this run: applied to every
            # combination before its geometry is built, and reloaded into
            # the form when this run is used as the basis for a new one.
            "parameter_defaults": dict(parameter_defaults or {}),
            "analysis_settings": analysis_settings,
            "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
        }
        if part_of is not None:
            run.config["part_of"] = part_of.run_id
            run.config["part_role"] = role
        combos = combinations(sweep_params)

        # Each combination's full geometry -- the equations file's values
        # with the run's pinned values and then the combo's own on top --
        # is checked before anything is built, so a combination that
        # cannot physically fit is marked here and skipped later rather
        # than failing somewhere inside SolidWorks.
        base_values = _equation_file_values()
        use_altium = (analysis_settings or {}).get("use_altium", True)
        run.state = {
            "stage": "created",
            "combos": {
                combo_id_for(combo): {
                    "params": combo,
                    "step_status": None,
                    "step_paths": None,
                    "step_error": None,
                    # Why this combination will not be built, or None when
                    # it is buildable (see constraints.py).
                    "skip_reason": _skip_reason(
                        {**base_values, **(parameter_defaults or {}), **combo}, use_altium
                    ),
                    "task_id": None,
                    "queue_status": None,
                    "post_status": None,
                    "post_error": None,
                }
                for combo in combos
            },
        }
        run.save()
        return run

    @staticmethod
    def _read_json(path: str, attempts: int = 20) -> dict:
        """Read one of the run's files, retrying while it is being replaced.

        The pipeline rewrites these as it works, and on Windows the file
        is briefly unopenable during the replace. The web UI polls them
        every few seconds, so without this it would occasionally fail to
        load a run for no reason the user could act on.
        """
        last_error = None
        for attempt in range(attempts):
            try:
                with open(path) as f:
                    return json.load(f)
            except (PermissionError, json.JSONDecodeError) as exc:
                last_error = exc
                time.sleep(0.02 * (attempt + 1))
        raise last_error

    @classmethod
    def load(cls, run_id: str) -> "Run":
        run = cls(run_id)
        run.config = _without_retired_config(cls._read_json(run.config_path))
        run.state = _without_retired_state(cls._read_json(run.state_path))
        return run

    @classmethod
    def list_runs(cls) -> List[dict]:
        """Summaries of every run under RUNS_ROOT, newest first, for the web UI's run list / --resume."""
        runs_root = str(paths.RUNS_ROOT)
        if not os.path.isdir(runs_root):
            return []
        summaries = []
        # Held tasks of a part belong to the run it is part of: counted there.
        parts_held: Dict[str, int] = {}
        # Each part's record (part_record), listed under the run it is part of.
        parts_found: Dict[str, List[dict]] = {}
        for run_id in sorted(os.listdir(runs_root), reverse=True):
            run_dir = os.path.join(runs_root, run_id)
            state_path = os.path.join(run_dir, "state.json")
            if not os.path.isfile(state_path):
                continue
            try:
                state = cls._read_json(state_path)
            except (OSError, json.JSONDecodeError):
                continue
            try:
                config = cls._read_json(os.path.join(run_dir, "run_config.json"))
            except (OSError, json.JSONDecodeError):
                config = {}
            if parent_of(config):
                # a part of another run: shown on that run's page, not on its own
                owner = parent_of(config)
                parts_held[owner] = parts_held.get(owner, 0) + held_count(state)
                parts_found.setdefault(owner, []).append(
                    part_record(run_id, config, state, _modified(state_path)))
                continue
            swept = [
                {"label": parameter_meta.label_for(p["name"]), "n": len(p.get("values", []))}
                for p in config.get("sweep_params", [])
                if not parameter_meta.is_retired(p["name"])
            ]
            progress_state, shown_stage = state, state.get("stage")
            if shown_stage == "eigenmodes" and config.get("eigen_run_id"):
                # Its eigenmodes are being solved first: what the list shows
                # is that part's progress, since this run has none yet.
                try:
                    progress_state = cls._read_json(
                        os.path.join(runs_root, config["eigen_run_id"], "state.json"))
                except (OSError, json.JSONDecodeError):
                    pass
            summaries.append({
                "run_id": run_id,
                "label": (config.get("label") or "").strip(),
                "created_at": config.get("created_at"),
                "stage": shown_stage,
                "stage_shown": displayed_stage(shown_stage, progress_state.get("combos")),
                "swept": swept,
                "facet_inputs": facet_inputs(config),
                "held": held_count(state),
                "queue_paused": bool(state.get("queue_paused")),
                **progress_of(progress_state, config.get("analysis_settings")),
            })
        for summary in summaries:
            summary["held"] += parts_held.get(summary["run_id"], 0)
            # Listed newest first, as the folders are read; the eigenmode part
            # then the refined solves, oldest first, reads in the order made.
            summary["parts"] = list(reversed(parts_found.get(summary["run_id"], [])))
        return summaries

    # -- persistence ----------------------------------------------------------
    @staticmethod
    def _write_json(path: str, payload: dict, attempts: int = 20) -> bool:
        """Write via a temp file and replace, so a reader (the web UI polls
        these constantly) never catches a half-written file.

        On Windows the replace fails with PermissionError while any other
        handle is open on the destination -- which the web UI's polling
        opens every few seconds. Those collisions clear in milliseconds,
        so the replace is retried; if it still will not go through, the
        write is skipped rather than raised. Whoever called this is
        recording progress, and losing one intermediate write (the next
        update rewrites the whole file anyway) is far better than killing
        the thread that was reporting it.
        """
        # Once the replace has proved to be persistently blocked, stop
        # spending the full retry window on every subsequent write -- go
        # almost straight to writing in place, and re-probe periodically
        # in case whatever held the file has let go.
        if time.time() < Run._replace_blocked_until:
            attempts = 2

        temporary = f"{path}.{os.getpid()}.tmp"
        try:
            with open(temporary, "w") as f:
                json.dump(payload, f, indent=2, default=str)
            for attempt in range(attempts):
                try:
                    os.replace(temporary, path)
                    Run._replace_blocked_until = 0.0
                    return True
                except PermissionError:
                    time.sleep(0.02 * (attempt + 1))
            Run._replace_blocked_until = time.time() + 60.0

            # Something is holding the file open for longer than a passing
            # read -- an indexer, a virus scanner, another copy of this
            # app. Writing in place is not atomic, but a reader that
            # catches it mid-write retries (see _read_json), and losing
            # the run's progress entirely is the worse outcome.
            logger.warning(
                "%s could not be replaced after %d attempts -- something is holding it open. "
                "Writing in place instead.", path, attempts,
            )
            with open(path, "w") as f:
                json.dump(payload, f, indent=2, default=str)
            return True
        except OSError as exc:
            logger.warning("Could not update %s (%s); skipping this write.", path, exc)
        finally:
            if os.path.exists(temporary):
                try:
                    os.remove(temporary)
                except OSError:
                    pass
        return False

    def save(self) -> None:
        with self._lock:
            os.makedirs(self.run_dir, exist_ok=True)
            self._write_json(self.config_path, self.config)
            self._write_json(self.state_path, self.state)

    def save_state(self) -> None:
        """Just the progress file. The configuration is fixed when the run
        is created, so rewriting it on every status update is pure churn
        -- and one more file to collide with a reader over."""
        with self._lock:
            os.makedirs(self.run_dir, exist_ok=True)
            self._write_json(self.state_path, self.state)

    def save_config(self) -> None:
        """Just the configuration file -- for the rare edits after creation
        (naming a run), so state.json's mtime, which the UI reads as "last
        activity", is left alone."""
        with self._lock:
            os.makedirs(self.run_dir, exist_ok=True)
            self._write_json(self.config_path, self.config)

    def set_stage(self, stage: str) -> None:
        with self._lock:
            self.state["stage"] = stage
            self.save_state()

    def update_combo(self, combo_id: str, **fields) -> None:
        with self._lock:
            self.state["combos"][combo_id].update(fields)
            self.save_state()

    def update_queue_state(self, *, paused: Optional[bool] = None,
                           combos: Optional[Dict[str, dict]] = None) -> None:
        """A pause or a resume's writes, saved once: the run's pause flag,
        and the queue fields of the combinations it changed (see
        workflow/queue_hold.py)."""
        with self._lock:
            if paused is not None:
                self.state["queue_paused"] = bool(paused)
            for combo_id, fields in (combos or {}).items():
                self.state["combos"][combo_id].update(fields)
            self.save_state()

    @property
    def queue_lock(self):
        """The lock that serialises this run's moves in and out of the worker queue."""
        return self._queue_lock

    @property
    def queue_paused(self) -> bool:
        """Whether tasks queued for this run are held: set by a pause, cleared by a resume."""
        return bool(self.state.get("queue_paused"))

    # -- which process is driving it ----------------------------------------
    # The web app knows which runs its own threads are driving, and nothing
    # more: a run resumed from the command line looked idle there, and its
    # Resume button would have had two processes driving one run -- both
    # waiting on the same tasks and reading the same results back. So the
    # process driving a run says so in the run's folder.
    @property
    def driver_path(self) -> str:
        return os.path.join(self.run_dir, "driver.json")

    def claim_driver(self) -> None:
        """Record this process as the one driving the run."""
        me = psutil.Process()
        self._write_json(self.driver_path, {
            "pid": me.pid, "started": me.create_time(), "host": socket.gethostname(),
        })

    def release_driver(self) -> None:
        """Take back claim_driver -- unless another process has claimed it since."""
        try:
            with open(self.driver_path) as f:
                ours = json.load(f).get("pid") == os.getpid()
            if ours:
                os.remove(self.driver_path)
        except (OSError, ValueError, AttributeError):
            pass

    def driven_elsewhere(self) -> Optional[int]:
        """The pid of another live process on this machine that is driving
        this run, or None.

        A process killed outright leaves its claim behind, so a claim only
        counts while its process is still alive -- the same process, not a
        later one that was handed its pid: the start time has to match too.
        A claim from another machine cannot be checked from here, so it is
        not counted (runs are kept per machine; see paths.RUNS_ROOT).
        """
        try:
            with open(self.driver_path) as f:
                driver = json.load(f)
            if driver["host"] != socket.gethostname() or driver["pid"] == os.getpid():
                return None
            if abs(psutil.Process(driver["pid"]).create_time() - driver["started"]) > 1.0:
                return None
        except (OSError, ValueError, KeyError, TypeError, psutil.Error):
            return None
        return driver["pid"]

    def combo_path(self, combo_id: str) -> str:
        """Where a combination's files live, without creating anything.

        `combo_dir` makes the folder, which is what the pipeline wants but
        not what a reader does -- the web UI looking over a few thousand
        combinations should not leave a few thousand empty folders behind.
        """
        return os.path.join(self.run_dir, "models", combo_id)

    def combo_dir(self, combo_id: str) -> str:
        d = self.combo_path(combo_id)
        os.makedirs(d, exist_ok=True)
        return d

    @property
    def sweep_param_names(self) -> List[str]:
        return [p["name"] for p in self.config.get("sweep_params", [])]

    @property
    def parent_id(self) -> Optional[str]:
        """The run this one is a hidden part of (its eigenmodes, or a
        refinement of it), or None for a run in its own right."""
        return parent_of(self.config)

    @property
    def label(self) -> str:
        """The name the user gave this run, or "" if they haven't."""
        return (self.config.get("label") or "").strip()

    @property
    def created_at(self) -> Optional[str]:
        return self.config.get("created_at")

    @property
    def parameter_defaults(self) -> Dict[str, float]:
        return self.config.get("parameter_defaults", {})

    def starting_values(self) -> Dict[str, float]:
        """What to prefill a new run's form with, based on this run: the
        values it pinned, plus the low end of anything it swept (a sweep's
        starting point is the most useful single value to carry over)."""
        values = dict(self.parameter_defaults)
        for param in self.config.get("sweep_params", []):
            if param.get("values"):
                values[param["name"]] = min(param["values"])
        return values

    @property
    def analysis_settings(self) -> dict:
        return self.config.get("analysis_settings", {})

    @property
    def progress(self) -> Dict[str, object]:
        """This run's progress, counted the one way (see `progress_of`).
        A method on the run rather than a bare call, so nobody has to
        remember to pass the settings that say which phases apply."""
        return progress_of(self.state, self.analysis_settings)

    def phase_states(self, combo_id: str) -> Dict[str, str]:
        return phase_states(
            self.state["combos"][combo_id],
            self.analysis_settings.get("use_altium", True),
        )

    @property
    def is_complete(self) -> bool:
        """Nothing left that this pipeline could still do for any
        combination -- every one of them finished, failed for good or
        skipped. See FINAL_FAILURES for the only failure that is for
        good."""
        combos = self.state["combos"]
        use_altium = self.analysis_settings.get("use_altium", True)
        return bool(combos) and all(
            _finished(phase_states(combo, use_altium)) for combo in combos.values()
        )
