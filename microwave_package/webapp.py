"""
Web UI for the eigenmode-analysis client pipeline: configure a parameter
sweep (reading available parameters straight from the shared SolidWorks
equations file), start or resume a run, and watch its live status --
STEP generation, AEDT build+queue, worker analysis, and the final plot
-- without touching a terminal.

Run with:
    python -m microwave_package.webapp
then open http://127.0.0.1:5000/

Each run's pipeline executes in a background thread inside this process
(see `_start_pipeline_thread`), so the page can poll its live progress;
closing the browser tab does NOT stop it -- only restarting this process
does (an interrupted run resumes from wherever it left off, same as the
CLI's --resume).
"""
from __future__ import annotations

import datetime as _dt
import glob
import itertools
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import traceback
from typing import Dict, Optional

import pandas as pd
from flask import (Flask, Response, jsonify, redirect, render_template, request,
                   send_file, send_from_directory, url_for)
from markupsafe import Markup

from ansys_analyze_common import queue_common

from . import paths, pipeline, pipeline_child
from .design import parameter_meta
from .design import equations as eq
from .design.sweep import SweepParameter, expand_values, split_swept_and_fixed
from .hfss import model_builder, registry
from .hfss.driven import ports
# The chip |E| heatmap is an eigenmode picture end to end -- the export,
# the .fld files and the plot -- so the one route that shows it reaches
# into that solver directly rather than the registry pretending every
# solver has a view like it.
from .hfss.eigenmode import fields as eigenmode_fields
from .workflow import compare, companion, manufacturing, plotting, resilient_io, run_manager, smith
from .workflow.run_manager import Run

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s", stream=sys.stdout)
logger = logging.getLogger("microwave_package")

app = Flask(__name__)


# -- display helpers -----------------------------------------------------
# The pipeline's internal stage names, mapped to something readable and a
# colour kind ("ok"/"busy"/"bad"/"muted") the .pill class understands.
_STAGE_DISPLAY = {
    "created": ("Created", "muted"),
    "eigenmodes": ("Solving eigenmodes first (step 1 of 2)", "busy"),
    "generating_and_queuing": ("Building geometry", "busy"),
    "geometry_ready": ("Waiting for the queue", "busy"),
    "monitoring": ("Analysing", "busy"),
    "postprocessing": ("Post-processing", "busy"),
    "plotting": ("Plotting results", "busy"),
    "complete": ("Complete", "ok"),
    "stopped": ("Stopped", "muted"),
    "error": ("Error", "bad"),
}


def _shown_run(run):
    """The part of a run whose progress the page shows right now.

    A run that asks for eigenmodes AND S-parameters is solved in two
    steps, eigenmodes first, and is one run to the person looking: while
    the first step is going the page shows that step's phases, and then
    its own.
    """
    if run.state.get("stage") == "eigenmodes":
        return companion.prerequisite_of(run) or run
    return run


def _stage_display(stage: Optional[str], failed: int = 0) -> dict:
    label, kind = _STAGE_DISPLAY.get(
        stage, ((stage or "unknown").replace("_", " ").capitalize(), "muted")
    )
    # A finished run that lost some combinations is not a clean "Complete"
    # -- flag it amber so the pill does not read green over an all-red bar.
    if stage == "complete" and failed:
        return {"label": f"Complete · {failed} failed", "kind": "busy"}
    return {"label": label, "kind": kind}


def _parse_dt(value):
    if isinstance(value, _dt.datetime):
        return value
    try:
        return _dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _run_id_dt(run_id: str):
    """The timestamp baked into a `run_YYYYmmdd_HHMMSS` id -- the fallback
    for runs whose config predates the stored `created_at`."""
    try:
        return _dt.datetime.strptime(run_id, "run_%Y%m%d_%H%M%S")
    except (TypeError, ValueError):
        return None


def _fmt_dt(dt) -> str:
    return dt.strftime("%d %b %Y, %H:%M").lstrip("0") if dt else ""


def _fmt_ago(dt) -> str:
    if not dt:
        return ""
    secs = max((_dt.datetime.now() - dt).total_seconds(), 0)
    if secs < 45:
        return "just now"
    if secs < 2700:
        return f"{round(secs / 60)} min ago"
    if secs < 86400:
        return f"{round(secs / 3600)} h ago"
    if secs < 2592000:
        return f"{round(secs / 86400)} d ago"
    return dt.strftime("%d %b %Y")


def _fmt_dur(delta) -> str:
    if not delta:
        return ""
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s"
    mins, secs = divmod(secs, 60)
    if mins < 60:
        return f"{mins}m"
    hrs, mins = divmod(mins, 60)
    if hrs < 24:
        return f"{hrs}h {mins}m"
    days, hrs = divmod(hrs, 24)
    return f"{days}d {hrs}h"


def _run_started(run):
    return _parse_dt(run.created_at) or _run_id_dt(run.run_id)


def _run_name(run) -> str:
    """What a run is called on the page: its label, or when it started."""
    return run.label or _fmt_dt(_run_started(run)) or run.run_id


# In-process registry of running pipeline threads, keyed by run_id -- so
# "resume" can tell whether a run is already active, and so a run
# survives navigating away from its status page.
_active_threads: Dict[str, threading.Thread] = {}
_stop_events: Dict[str, threading.Event] = {}


# The command that drives a run, given its id as the last argument. It runs in
# a process of its own (see pipeline_child.py): the build's native calls hold
# the interpreter lock, which would otherwise stop this web app answering.
PIPELINE_COMMAND = [sys.executable, "-m", "microwave_package.pipeline_child"]
_PACKAGE_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _start_pipeline_thread(run: Run) -> None:
    # Deliberately does not touch the share: a run has to be startable
    # with the queue offline, because the geometry is built here and
    # only the handoff needs the network.
    stop_event = threading.Event()
    _stop_events[run.run_id] = stop_event

    # Clear any error left over from a previous failed attempt at this
    # run, so a stale traceback doesn't linger on the page while this
    # (re)start is in progress.
    run.state.pop("error", None)
    run.save()

    def _target():
        # This thread only waits on the pipeline process. Waiting releases
        # the interpreter lock, so this thread costs the web app nothing.
        pipeline_child.clear_stop_request(run)
        process = subprocess.Popen(
            PIPELINE_COMMAND + [run.run_id], cwd=_PACKAGE_PARENT)
        while True:
            try:
                returncode = process.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if stop_event.is_set():
                    open(os.path.join(run.run_dir, pipeline_child.STOP_FILE), "w").close()
        if returncode != 0:
            # The process records its own crashes; this catches one that
            # got no further than that (killed, or a crash of its own).
            current = Run.load(run.run_id)
            if current.state.get("stage") != "error":
                current.state["stage"] = "error"
                current.state["error"] = (
                    f"The pipeline process exited with code {returncode}. "
                    "Its console output is in the server's console.")
                current.save()

    thread = threading.Thread(target=_target, daemon=True, name=f"pipeline-{run.run_id}")
    _active_threads[run.run_id] = thread
    # A Driven run finishes its paired Eigenmode run first, on this same
    # thread -- so that one is being driven here too, and must say so.
    prerequisite = run.config.get(companion.EIGEN_LINK)
    if prerequisite:
        _active_threads[prerequisite] = thread
        _stop_events[prerequisite] = stop_event
    thread.start()


def _is_running(run_id: str) -> bool:
    thread = _active_threads.get(run_id)
    return thread is not None and thread.is_alive()


def _driven_elsewhere(run_id: str) -> Optional[int]:
    """The pid of another process on this machine that is driving the run
    -- the command line, or another copy of this app -- or None.

    _is_running only knows this process's own threads, so without this a
    run going on elsewhere looked idle here and offered a Resume that
    would have had two processes driving it at once.
    """
    if _is_running(run_id):
        return None
    return Run(run_id).driven_elsewhere()


def _is_stopping(run_id: str) -> bool:
    """Stop asked for, but the run has not got there yet.

    There is always a gap: none of SolidWorks, Altium or AEDT can be
    interrupted mid-call, so a stop lands when each of them next comes
    back. Worth saying on the page rather than leaving the button looking
    like it did nothing.
    """
    stop_event = _stop_events.get(run_id)
    if stop_event is not None and stop_event.is_set() and _is_running(run_id):
        return True
    # A run driven by a process that outlived this app: its stop is the file.
    return bool(_driven_elsewhere(run_id)) and os.path.exists(
        os.path.join(Run(run_id).run_dir, pipeline_child.STOP_FILE))


_XML_DECLARATION = re.compile(r"^\s*<\?xml.*?\?>\s*", re.S)
_ROOT_SIZE_ATTRS = re.compile(r'\s(?:width|height)="[^"]*"')


def _parameter_drawings() -> Dict[str, Markup]:
    """Parameter name -> inline SVG markup for its drawing.

    The SVGs are embedded in the page rather than linked as <img>, because
    they draw themselves in `currentColor`: inlined they follow the page's
    text colour and so stay visible in both the light and dark themes,
    whereas inside an <img> they would render black on any background.

    The fixed millimetre width/height the exporter writes on the root <svg>
    is dropped as well, so CSS decides how big each one is drawn.
    """
    directory = paths.PARAMETER_DRAWINGS_DIR
    if not directory.is_dir():
        return {}

    drawings: Dict[str, Markup] = {}
    for svg in sorted(directory.glob("*.svg")):
        try:
            markup = _XML_DECLARATION.sub("", svg.read_text(encoding="utf-8"))
        except OSError:
            logger.warning("Could not read parameter drawing %s", svg)
            continue
        end_of_root_tag = markup.find(">")
        if end_of_root_tag != -1:
            markup = (
                _ROOT_SIZE_ATTRS.sub("", markup[:end_of_root_tag])
                + markup[end_of_root_tag:]
            )
        drawings[svg.stem] = Markup(markup)
    return drawings


def _latest_run_id() -> Optional[str]:
    runs = Run.list_runs()  # newest first
    return runs[0]["run_id"] if runs else None


def _ticked(name: str, values: dict, parameters: dict) -> bool:
    """Whether a yes/no parameter's tick starts ticked: what the run the
    form starts from pinned for it, else what the equation file says -- so
    the form opens on the same board the equation file would build."""
    pinned = values.get(name)
    if pinned is not None:
        return float(pinned) > 0.5
    if name == parameter_meta.NO_CHIP_PARAM:
        # a run that never pinned it was built with a chip (see new_run)
        return False
    return float(parameters.get(name, 0)) > 0.5


def _form_starting_point(from_run: Optional[str], combo_id: Optional[str]):
    """The analysis settings and per-parameter values to open the form with.

    Opens on the defaults. Settings come from a run only when `from_run`
    names one (the "Load its settings" button); `combo_id` then starts
    from one combination of that run (its own values on top of whatever
    that run had pinned), which is the way to take a single point from a
    sweep and build a new sweep around it. Carrying over the latest run
    silently is what let old settings leak into new runs (issue #98).

    Returns (settings, values, loaded_from_run_id).
    """
    run_id = from_run
    if not run_id:
        return dict(pipeline.DEFAULT_ANALYSIS_SETTINGS), {}, None
    try:
        run = Run.load(run_id)
    except (OSError, ValueError, KeyError):
        logger.warning("Could not load settings from run %s", run_id)
        return dict(pipeline.DEFAULT_ANALYSIS_SETTINGS), {}, None

    settings = {**pipeline.DEFAULT_ANALYSIS_SETTINGS, **run.config.get("analysis_settings", {})}
    values = run.starting_values()
    if combo_id:
        combo = run.state.get("combos", {}).get(combo_id)
        if combo:
            values = {**run.parameter_defaults, **combo.get("params", {})}
    return settings, values, run_id


def _solve_settings_from_form(form) -> dict:
    """The solution type the form picked, and that type's own settings.

    The form renders BOTH types' controls and hides the one that does not
    apply, so a submission always carries values for settings this run
    will never use. Only the chosen type's fields are read back -- the
    others keep whatever the defaults say, which is what makes switching
    an existing run's solution type land on real numbers rather than on
    whatever was last left in a hidden box.

    A field the form did not send, or sent unparseably, falls back to its
    default rather than failing the whole submission: the run is
    recoverable from a wrong number and not from a form that will not
    submit.
    """
    solution_type = form.get("solution_type") or registry.default_solution_type()
    if not registry.is_known(solution_type):
        logger.warning("Unknown solution type %r from the form; using the default.", solution_type)
        solution_type = registry.default_solution_type()

    settings = {"solution_type": solution_type, **registry.all_defaults()}
    # A pair solves both types, so both types' controls count.
    both = "run_both" in form and solution_type not in registry.standalone_types()
    settings["run_both"] = both
    fields = [f for t in registry.solution_types() for f in registry.get(t["value"]).all_fields]         if both else registry.get(solution_type).all_fields
    for field in fields:
        raw = form.get(field["name"])
        if raw is None or raw == "":
            continue
        try:
            settings[field["name"]] = registry.coerce(field, raw)
        except (TypeError, ValueError):
            logger.warning(
                "Ignoring %s=%r from the form: not a valid %s. Using %r.",
                field["name"], raw, field["kind"], field["default"],
            )
    return settings


def _material_choice(form, key: str) -> str:
    """One material dropdown's value off the form.

    The form posts `model_builder.CUSTOM_MATERIAL` for "Custom..." and the
    typed name in `<key>_custom` alongside it -- see the `material_field`
    macro in index.html. Anything else is one of the curated options
    (or, for an older submission bypassing the dropdown entirely, any
    other string) and is used as-is. A blank submission falls back to
    the material model_builder would fall back to anyway.
    """
    raw = form.get(key)
    if raw == model_builder.CUSTOM_MATERIAL:
        raw = form.get(f"{key}_custom", "")
    return (raw or "").strip() or model_builder.MATERIAL_FALLBACKS[key]


def _positive_float(raw, default: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _materials_from_form(form) -> dict:
    """Every material/boundary setting the form carries, by key.

    Split out from `new_run` so the dropdown-vs-custom-box handling can
    be tested without going through the route -- the same reasoning as
    `_solve_settings_from_form` below.
    """
    wirebond = form.get("mat_wirebond")
    if wirebond not in model_builder.WIREBOND_MATERIALS:
        wirebond = model_builder.MATERIAL_FALLBACKS["mat_wirebond"]
    return {
        "mat_box": _material_choice(form, "mat_box"),
        "mat_pcb": _material_choice(form, "mat_pcb"),
        "mat_chip": _material_choice(form, "mat_chip"),
        "mat_vacuum": _material_choice(form, "mat_vacuum"),
        "perfect_e_box": "perfect_e_box" in form,
        "box_conductivity": _positive_float(
            form.get("box_conductivity"), model_builder.DEFAULT_BOX_CONDUCTIVITY),
        "mat_conductor_pcb": _material_choice(form, "mat_conductor_pcb"),
        "mat_conductor_chip": _material_choice(form, "mat_conductor_chip"),
        "perfect_e_conductors": "perfect_e_conductors" in form,
        # Closed choice (see model_builder.WIREBOND_MATERIALS), so there is
        # no matching "<key>_custom" box for this one.
        "mat_wirebond": wirebond,
        "include_wirebonds": "include_wirebonds" in form,
    }


def _runs_for_display(runs: list) -> list:
    """`Run.list_runs()` rows with the run-list's presentation fields added:
    a friendly name, a relative time, a humanised stage, the one-line
    "what it swept" summary, and whether it has results to compare."""
    view = []
    for r in runs:
        started = _parse_dt(r.get("created_at")) or _run_id_dt(r["run_id"])
        date_text = _fmt_dt(started)
        view.append({
            **r,
            "name": r["label"] or date_text or r["run_id"],
            "date_text": date_text,
            "started_ago": _fmt_ago(started),
            "stage_display": _stage_display(r.get("stage"), r.get("failed", 0)),
            "swept_text": ", ".join(f'{s["label"]} ×{s["n"]}' for s in r["swept"]),
            "has_results": os.path.isfile(
                os.path.join(Run(r["run_id"]).plots_dir, plotting.RESULTS_FILENAME)),
        })
    return view


@app.route("/runs.json")
def runs_json():
    """The run list's live data. The index page polls this to keep each
    run's progress bar and stage current without reloading the form."""
    return jsonify(_runs_for_display(Run.list_runs()))


@app.route("/")
def index():
    content = eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE))
    parameters = eq.get_numeric_parameters(content)
    runs = _runs_for_display(Run.list_runs())
    settings, values, loaded_from = _form_starting_point(
        request.args.get("from_run"), request.args.get("combo")
    )
    return render_template(
        "index.html",
        parameters=parameters,
        groups=parameter_meta.grouped(parameters),
        runs=runs,
        # Ticked on arrival: the comparison page's "change the selection"
        # comes back here with the runs it was showing.
        compare_ids=set(filter(None, request.args.get("compare", "").split(","))),
        max_compare=compare.MAX_RUNS,
        drawings=_parameter_drawings(),
        meta=parameter_meta,
        settings=settings,
        values={name: parameter_meta.format_value(name, value) for name, value in values.items()},
        loaded_from=loaded_from,
        loaded_combo=request.args.get("combo"),
        has_no_chip_param=parameter_meta.NO_CHIP_PARAM in parameters,
        no_chip_ticked=_ticked("no_chip", values, parameters),
        screws_ticked=_ticked("screws_on", values, parameters),
        has_screws_param=parameter_meta.SCREWS_PARAM in parameters,
        vents_ticked=_ticked("vents_on", values, parameters),
        has_vents_param=parameter_meta.VENTS_PARAM in parameters,
        solution_types=registry.solution_types(),
        standalone_types=registry.standalone_types(),
        # Both types' controls are rendered and the form hides the one
        # that does not apply, so switching solution type is instant
        # rather than a round-trip. Only the chosen type's values are read
        # back -- see _solve_settings_from_form.
        solve_fields={
            t["value"]: registry.get(t["value"]).fields for t in registry.solution_types()
        },
        shared_solve_fields=registry.SHARED,
        current_solution_type=registry.solution_type_of(settings),
        material_options=model_builder.MATERIAL_OPTIONS,
        wirebond_materials=model_builder.WIREBOND_MATERIALS,
        custom_material=model_builder.CUSTOM_MATERIAL,
        default_box_conductivity=model_builder.DEFAULT_BOX_CONDUCTIVITY,
    )


@app.route("/runs/new", methods=["POST"])
def new_run():
    # A filled-in box means "use this"; an empty one leaves the parameter
    # at the equation file's value. A box holding a single value pins that
    # value for the whole run, and only boxes holding more than one value
    # become sweep dimensions. The names come from the equations file
    # rather than the form, so only real parameters count.
    content = eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE))
    available = eq.get_numeric_parameters(content)
    requested = []
    for name in available:
        if name in parameter_meta.BOOLEAN_PARAMETERS:
            continue  # driven by its own control (a tick box on the form)
        spec = request.form.get(f"range_{name}", "").strip()
        if not spec:
            continue
        try:
            values = expand_values(spec)
        except ValueError as exc:
            logger.warning("Ignoring %s=%r: %s", name, spec, exc)
            continue
        if values:
            # The box is filled in using the unit the form shows, which
            # for the sub-millimetre parameters is micrometres -- convert
            # back to the millimetres the equations file speaks before
            # anything downstream sees the numbers.
            values = [parameter_meta.to_model(name, value) for value in values]
            requested.append(SweepParameter(name=name, values=values))

    # The "Bare board" tick pins no_chip for the whole run, both ways: an
    # unticked box pins 0, so a chip run never depends on the equation file's
    # current value (see step_generation.check_baseline). A form field is
    # present only when ticked, so the unticked case is simply absent here.
    if parameter_meta.NO_CHIP_PARAM in available:
        requested.append(SweepParameter(
            name=parameter_meta.NO_CHIP_PARAM,
            values=[1.0 if "no_chip" in request.form else 0.0]))
    if parameter_meta.NO_CHIP_PARAM in available and "no_chip" in request.form:
        # A bare board with no pedestal asked for keeps the box's floor flat:
        # zero is the value the box has always rebuilt cleanly with. A typed
        # pedestal depth still wins, and a bare board with a recess is built
        # with its bottom copper carried over the recess (see hfss/vents.py).
        if "pedestal_depth" in available and not any(
                p.name == "pedestal_depth" for p in requested):
            requested.append(SweepParameter(name="pedestal_depth", values=[0.0]))

    # The "Corner screws" tick pins screws_on for the run, both ways: the
    # equation file's own value is 1 now, so an unticked box has to say 0
    # or the run quietly gets screws it was not asked for.
    if parameter_meta.SCREWS_PARAM in available:
        requested.append(SweepParameter(
            name=parameter_meta.SCREWS_PARAM,
            values=[1.0 if "screws_on" in request.form else 0.0]))

    # Same for the vent grooves: on in the equation file, so unticked says 0.
    if parameter_meta.VENTS_PARAM in available:
        requested.append(SweepParameter(
            name=parameter_meta.VENTS_PARAM,
            values=[1.0 if "vents_on" in request.form else 0.0]))

    sweep_params, parameter_defaults = split_swept_and_fixed(requested)
    if not sweep_params and not parameter_defaults:
        return redirect(url_for("index"))

    solve_settings = _solve_settings_from_form(request.form)
    settings = {
        **solve_settings,
        "aedt_version": request.form.get("aedt_version") or None,
        "student_version": "student_version" in request.form,
        "graphical": "graphical" in request.form,
        "poll_seconds": float(request.form.get("poll_seconds", 10.0)),
        "use_altium": "use_altium" in request.form,
        "materials": _materials_from_form(request.form),
    }
    # A driven run needs two real ports to have an S21 at all, and
    # port_count is geometry rather than an analysis setting -- so it is
    # checked here, before a sweep's worth of STEP files get built for
    # models that could never be given ports. A pair is checked for both.
    try:
        run = pipeline.create_run(sweep_params, settings, parameter_defaults)
    except ValueError as complaint:
        return redirect(url_for("index", error=str(complaint)))
    label = request.form.get("label", "").strip()[:80]
    if label:
        run.config["label"] = label
        run.save()
    _start_pipeline_thread(run)
    return redirect(url_for("run_status", run_id=run.run_id))


@app.route("/runs/<run_id>/stop", methods=["POST"])
def stop_run(run_id):
    """Ask a running pipeline to stop.

    Cooperative, not a kill: each stage finishes the combination it is in
    the middle of -- there is no way to interrupt a SolidWorks rebuild or
    an HFSS import part-way -- records it, and the thread ends. Nothing
    already built is lost, and Resume picks the run back up exactly where
    it stopped.
    """
    stop_event = _stop_events.get(run_id)
    if stop_event is not None and _is_running(run_id):
        stop_event.set()
        logger.info("Stop requested for run %s.", run_id)
    elif (driver := _driven_elsewhere(run_id)) is not None:
        # The pipeline process outlives a restart of this app, and it watches
        # the run's folder for this file (see pipeline_child).
        open(os.path.join(Run(run_id).run_dir, pipeline_child.STOP_FILE), "w").close()
        logger.info("Stop requested for run %s, driven by process %s.", run_id, driver)
    return redirect(url_for("run_status", run_id=run_id))


@app.route("/runs/<run_id>/resume", methods=["POST"])
def resume_run(run_id):
    elsewhere = _driven_elsewhere(run_id)
    if elsewhere:
        return redirect(url_for(
            "run_status", run_id=run_id,
            error=f"This run is already going in another process ({elsewhere}). "
                  "Stop it there first, then resume it here.",
        ))
    if not _is_running(run_id):
        run = Run.load(run_id)
        _start_pipeline_thread(run)
    return redirect(url_for("run_status", run_id=run_id))


@app.route("/runs/<run_id>/label", methods=["POST"])
def set_run_label(run_id):
    """Give a run a human name (or clear it with an empty box). Stored on
    the run's config so it shows in the run list and as the page title."""
    try:
        run = Run.load(run_id)
    except (OSError, ValueError):
        return redirect(url_for("index"))
    run.config["label"] = request.form.get("label", "").strip()[:80]
    run.save_config()
    return redirect(url_for("run_status", run_id=run_id))


# How many skipped combinations the run page lists individually. A sweep
# that oversteps the geometry can skip tens of thousands, and rendering a
# row for each one costs far more than it tells you -- they all say the
# same few things. The count and a sample carry the same information.
MAX_SKIPPED_ROWS = 100


def _phase_meta() -> list:
    """The phase list the page draws with -- names only, no server-side
    plumbing. All of them, including a phase this run does not use, so a
    position in the per-combination string always means the same thing."""
    return [
        {
            "key": phase["key"], "label": phase["label"], "what": phase["what"],
            "parallel": bool(phase.get("parallel")),
            # What to call a phase that is waiting on something other than
            # this machine -- "Queued", not "Analysis".
            "waiting_label": phase.get("waiting_label"),
        }
        for phase in run_manager.PHASES
    ]


def _phase_csv(run, combo: dict) -> str:
    """One combination's phases, in `run_manager.PHASES` order, as the one
    string the page needs.

    Comma-separated rather than an object per combination: this goes out
    on every poll for every combination, and a sweep can have thousands.
    The page splits it and paints one segment per phase.
    """
    states = run_manager.phase_states(combo, run.analysis_settings.get("use_altium", True))
    return ",".join(states[key] for key in run_manager.PHASE_KEYS)


def _combo_rows(run) -> dict:
    """The combinations to render, split into the ones being built and a
    capped sample of the ones skipped for not fitting."""
    combos = run.state.get("combos", {})
    buildable, skipped = {}, {}
    for combo_id, combo in combos.items():
        (skipped if combo.get("skip_reason") else buildable)[combo_id] = combo
    return {
        "buildable": buildable,
        # Kept beside the combinations rather than inside them: those are
        # the live state dicts, and anything put in one is written back
        # into state.json the next time a stage records progress.
        "phases": {combo_id: _phase_csv(run, combo) for combo_id, combo in buildable.items()},
        "skipped_sample": dict(itertools.islice(skipped.items(), MAX_SKIPPED_ROWS)),
        "skipped_total": len(skipped),
        "skipped_hidden": max(len(skipped) - MAX_SKIPPED_ROWS, 0),
        "total": len(combos),
    }


@app.route("/runs/<run_id>")
def run_status(run_id):
    run = Run.load(run_id)
    if run.parent_id:
        # A hidden part (its eigenmodes, or a refinement): shown on its run's page.
        return redirect(url_for("run_status", run_id=run.parent_id))
    shown = _shown_run(run)
    plot_exists = os.path.exists(os.path.join(run.plots_dir, plotting.PLOT_FILENAME))
    df = _results_frame(run)
    options = plotting.axis_options(df, run.sweep_param_names) if df is not None else []
    filters = plotting.filter_options(df, run.sweep_param_names) if df is not None else []
    # Which columns the plot opens on depends on what the run solved --
    # an eigenmode sweep and an S-parameter band are read along different
    # axes. plotting decides; this only passes the answer to the page.
    axes = plotting.default_axes(
        df if df is not None else pd.DataFrame(), run.sweep_param_names,
        registry.solution_type_of(run.config.get("analysis_settings", {})),
    )

    stage = run.state.get("stage")
    progress = shown.progress
    started = _run_started(run)
    try:
        updated = _dt.datetime.fromtimestamp(os.path.getmtime(run.state_path))
    except OSError:
        updated = None
    # state.json stops being rewritten once the run settles, so its mtime
    # is a good enough "finished at" for the elapsed time.
    duration = updated - started if (started and updated and stage in ("complete", "error")) else None
    elsewhere = _driven_elsewhere(run_id)

    return render_template(
        "run_status.html",
        run=run,
        run_name=_run_name(run),
        pair_note=_pair_note(run),
        line_checks=_line_checks(run),
        launch_summaries=_launch_summaries(run),
        eigenmodes=companion.eigenmode_table(run),
        has_fine=df is not None and plotting.has_fine_data(df),
        views=plotting.VIEWS,
        refine=_refine_panel(run),
        rows=_combo_rows(shown),
        phases=_phase_meta(),
        is_running=_is_running(run_id) or bool(elsewhere),
        is_stopping=_is_stopping(run_id),
        driven_elsewhere=elsewhere,
        plot_exists=plot_exists,
        progress=progress,
        stage_display=_stage_display(stage, progress["failed"]),
        started_text=_fmt_dt(started),
        updated_text=_fmt_ago(updated),
        duration_text=_fmt_dur(duration),
        meta=parameter_meta,
        has_common_dimensions=os.path.exists(os.path.join(run.run_dir, "common_dimensions.txt")),
        axis_options=options,
        smith_options=_smith_options(df),
        default_x=axes["x"],
        default_y=axes["y"],
        default_color=axes["color"] or "",
        disk_usage_mb=_run_disk_usage(run) / (1024 * 1024),
        heatmap_combos=_chip_field_combos(companion.prerequisite_of(run) or run),
        step_viewer_available=_step_viewer_available(),
        filter_options=filters,
        # A finished run's combination table is a record rather than
        # something to watch, so it starts folded away -- with hundreds of
        # rows it otherwise buries the results underneath it.
        combos_open=not run.is_complete,
    )


def _line_checks(run) -> list:
    """The verdict of each combination of a line-impedance run, for its page:
    pass or fail, the impedance range, and the gap that was solved against the
    gap the analytic calculation gives. `[]` for any other kind of run."""
    if registry.solution_type_of(run.analysis_settings) != "LineImpedance":
        return []
    checks = []
    for combo_id in run.state.get("combos", {}):
        path = os.path.join(run.combo_path(combo_id), "line_check.json")
        try:
            with open(path, encoding="utf-8") as handle:
                verdict = json.load(handle)
        except (OSError, ValueError):
            continue
        verdict["combo_id"] = combo_id
        checks.append(verdict)
    return checks


def _launch_summaries(run) -> list:
    """What each combination of a launch-study run came to, for its page: the S11
    levels and the time-domain read of the launch (`launch_summary.json`).
    `[]` for any other kind of run, and for combinations not yet read back."""
    if registry.solution_type_of(run.analysis_settings) != "LaunchStudy":
        return []
    found = []
    for combo_id in run.state.get("combos", {}):
        path = os.path.join(run.combo_path(combo_id), "launch_summary.json")
        try:
            with open(path, encoding="utf-8") as handle:
                summary = json.load(handle)
        except (OSError, ValueError):
            continue
        summary["combo_id"] = combo_id
        peaks = summary.get("tdr_peaks") or []
        summary["first_peak"] = peaks[0] if peaks else None
        found.append(summary)
    return found


def _pair_note(run) -> Optional[str]:
    """One line saying what a run that solves both is doing, or None."""
    if not run.config.get(companion.EIGEN_LINK):
        return None
    return ("Solves the eigenmodes and the S-parameters of every combination on the same "
            "geometry: eigenmodes first, then the S-parameter sweep with fine frequency "
            "windows around each mode. The modes are marked on the S21 plot and listed "
            "under the results.")


def _refine_panel(run) -> Optional[dict]:
    """What the run page's refine panel needs, or None when the run has
    nothing to refine: it must be a driven run with some finished
    combinations. Also lists the refinements already made from it -- they
    belong to this run, so they are listed here, not in the run list."""
    if registry.solution_type_of(run.analysis_settings) != "DrivenModal":
        return None
    children = []
    for child in companion.refinements_of(run):
        shown = _stage_display(child.state.get("stage"), child.progress["failed"])
        children.append({
            "run_id": child.run_id, "stage": shown["label"], "kind": shown["kind"],
            "ranges": child.analysis_settings.get("fine_windows_manual", ""),
            "combos": list(child.state["combos"]),
            "running": _is_running(child.run_id),
            "error": child.state.get("error"),
        })
    return {
        "combos": companion.refinable_combos(run),
        "children": children,
        "has_modes": bool(companion.eigenmode_table(run)),
    }


@app.route("/runs/<run_id>/refine", methods=["POST"])
def refine_run(run_id):
    """Start a refinement: re-solve chosen combinations of this driven run
    over finer frequency ranges, as a new run linked to this one."""
    run = Run.load(run_id)
    try:
        child = companion.create_refinement(
            run, request.form.get("ranges", ""), request.form.getlist("combo"))
    except ValueError as complaint:
        return redirect(url_for("run_status", run_id=run_id, error=str(complaint)))
    _start_pipeline_thread(child)
    return redirect(url_for("run_status", run_id=run_id) + "#refine")


@app.route("/runs/<run_id>/mode-ranges.json")
def mode_ranges_json(run_id):
    """The refine ranges for the eigenmodes between `lo` and `hi` GHz of the
    chosen combinations, to fill the form's ranges box."""
    run = Run.load(run_id)

    def number(name):
        try:
            return float(request.args[name])
        except (KeyError, ValueError):
            return None

    combos = request.args.getlist("combo") or companion.refinable_combos(run)
    return jsonify({"ranges": companion.mode_ranges(run, combos, number("lo"), number("hi"))})


def _run_disk_usage(run) -> int:
    total = 0
    for folder, _, files in os.walk(run.run_dir):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(folder, name))
            except OSError:
                pass
    return total


def _combo_project_file(run, combo_id: str) -> Optional[str]:
    """The .aedt project for a combination, once one has been built (it is
    created when the model is queued, so it exists from `queue_status` on).
    Falls back to any .aedt in the combo folder if the naming ever drifts."""
    candidate = model_builder.project_file_for(run, combo_id)
    if os.path.exists(candidate):
        return candidate
    matches = sorted(glob.glob(os.path.join(run.combo_dir(combo_id), "*.aedt")))
    return matches[0] if matches else None


@app.route("/runs/<run_id>/combos/<combo_id>/open-aedt", methods=["POST"])
def open_combo_in_aedt(run_id, combo_id):
    """Open one combination's AEDT project in a visible AEDT window on this
    machine. The web UI only ever binds to localhost, so "this machine" is
    also the one with AEDT installed and the one the user is sitting at.
    Read-only: it just hands the .aedt file to the OS to open."""
    try:
        run = Run.load(run_id)
    except (OSError, ValueError):
        return jsonify(ok=False, error="run not found"), 404
    if combo_id not in run.state.get("combos", {}):
        return jsonify(ok=False, error="unknown combination"), 404

    project_file = _combo_project_file(run, combo_id)
    if not project_file:
        return jsonify(ok=False, error="no AEDT project has been built for this combination yet"), 404

    opener = getattr(os, "startfile", None)
    if opener is None:
        return jsonify(ok=False, error="opening a project is only supported on Windows"), 400
    try:
        opener(project_file)  # hands the .aedt to its file association (AEDT), non-blocking
    except OSError as exc:
        logger.warning("Could not open %s in AEDT: %s", project_file, exc)
        return jsonify(ok=False, error=f"could not launch AEDT ({exc})"), 500

    logger.info("Opened %s in AEDT (combo %s of run %s)", project_file, combo_id, run_id)
    return jsonify(ok=True, path=project_file)


def _clear_queued_task(queue, task_id: str) -> None:
    """Remove one task from the queue, wherever it has got to.

    `queue_common.remove_task` clears a FAILED task and the task's
    project folder, which is most of the job and not all of it: a task
    still waiting in `pending`, or one the worker has already picked up
    into `in_progress`, is left behind by it. That is worth more than
    tidiness -- a task belonging to a run that no longer exists is the
    worker spending hours on results nobody can read, and the run page
    that would have shown them is gone.

    A task the worker is in the middle of is removed too. It will fail
    on its own missing file, which is the right outcome: the run it
    belonged to does not exist any more.
    """
    queue_common.remove_task(queue, task_id)  # failed/*.task.json + projects/<task_id>
    for folder in (queue.pending, queue.in_progress, queue.done, queue.failed):
        task_file = os.path.join(folder, f"{task_id}.task.json")
        for path in (task_file, task_file + ".error.log"):
            if not os.path.exists(path):
                continue
            try:
                os.remove(path)
            except OSError as exc:
                logger.warning("Could not remove %s: %s", path, exc)


@app.route("/runs/<run_id>/delete", methods=["POST"])
def delete_run(run_id):
    """Delete a run's folder, and any queue files its tasks still have.

    Guarded three ways, because there is no undo: the page asks first,
    the form has to come back with the run's own id typed in, and a run
    that is still going is refused outright.
    """
    # Every refusal below says so on the page it returns to. Redirecting
    # in silence is indistinguishable from a button that does nothing,
    # and the only account of what happened was a line in the server log
    # nobody watching the browser would ever see.
    if _is_running(run_id) or _driven_elsewhere(run_id):
        logger.warning("Refusing to delete run %s -- it is still running.", run_id)
        return redirect(url_for(
            "run_status", run_id=run_id,
            error="This run is still going. Stop it first, then delete it.",
        ))

    if request.form.get("confirm_run_id", "").strip() != run_id:
        logger.warning("Refusing to delete run %s -- confirmation did not match.", run_id)
        return redirect(url_for(
            "run_status", run_id=run_id,
            error=f"Nothing was deleted -- the confirmation did not match {run_id}.",
        ))

    try:
        run = Run.load(run_id)
    except (OSError, ValueError):
        logger.warning("Cannot load run %s to delete it.", run_id)
        return redirect(url_for("index", error=f"Could not read run {run_id} to delete it."))

    # anything of this run's still sitting in the queue goes too, so
    # deleting a run really does leave nothing behind
    # Lazily, so deleting a run still works with the share down: the
    # per-task removals below already tolerate failing individually.
    queue = resilient_io.queue_from_env("ANSYS_ANALYZE_QUEUE_PATH")
    # Its eigenmode run and refinements are parts of it and go with it.
    for each in [*companion.parts_of(run), run]:
        for combo in each.state.get("combos", {}).values():
            task_id = combo.get("task_id")
            if not task_id:
                continue
            try:
                _clear_queued_task(queue, task_id)
            except OSError as exc:
                logger.warning("Could not clear queue files for task %s: %s", task_id, exc)
        shutil.rmtree(each.run_dir, ignore_errors=True)
    logger.info("Deleted run %s", run_id)
    return redirect(url_for("index"))


@app.route("/runs/<run_id>/common_dimensions.txt")
def run_common_dimensions(run_id):
    run = Run.load(run_id)
    return send_from_directory(run.run_dir, "common_dimensions.txt", mimetype="text/plain")


@app.route("/runs/<run_id>/status.json")
def run_status_json(run_id):
    """What the run page polls every few seconds.

    Each combination goes out as one short string -- its phases, in
    order -- and only for combinations that can still change: a skipped
    one never will, and its parameters and reason were already rendered
    into the page. Sending the whole state instead meant a large sweep
    re-sent megabytes on every poll.
    """
    run = Run.load(run_id)
    if run.parent_id:
        run = Run.load(run.parent_id)
        run_id = run.run_id
    shown = _shown_run(run)
    prog = shown.progress
    elsewhere = _driven_elsewhere(run_id)
    return jsonify({
        "run_id": run.run_id,
        "stage": run.state.get("stage"),
        "stage_display": _stage_display(run.state.get("stage"), prog["failed"]),
        "progress": prog,
        "error": run.state.get("error"),
        "is_running": _is_running(run_id) or bool(elsewhere),
        "is_stopping": _is_stopping(run_id),
        "driven_elsewhere": elsewhere,
        # so the page can reload itself once, when the plot first appears
        "plot_ready": os.path.exists(os.path.join(run.plots_dir, plotting.PLOT_FILENAME)),
        "combos": {
            combo_id: _phase_csv(shown, combo)
            for combo_id, combo in shown.state["combos"].items()
            if not combo.get("skip_reason")
        },
    })


def _read_results_csv(run):
    csv_path = os.path.join(run.plots_dir, plotting.RESULTS_FILENAME)
    if not os.path.exists(csv_path):
        return None
    try:
        df = pd.read_csv(csv_path)
    except (OSError, ValueError):
        logger.warning("Could not read results for run %s", run.run_id)
        return None
    # Results from before the distortion option was removed still have its columns.
    return df.drop(columns=[c for c in df.columns if parameter_meta.is_retired(c)])


def _results_frame(run):
    """This run's combined results table, or None if it has none yet.

    A run that has been refined also carries its refinements' rows: the
    fine data sits in the same table as the broad data, told apart by the
    `sweep` column (and `refinement`, which says which run), so the plot
    can show either or both.
    """
    df = _read_results_csv(run)
    if df is None:
        return None
    children = []
    for child in companion.refinements_of(run):
        fine = _read_results_csv(child)
        if fine is not None and not fine.empty:
            children.append(fine.assign(**{plotting.REFINEMENT_COLUMN: child.run_id}))
    if children:
        df = pd.concat([df, *children], ignore_index=True)
    return df


def _smith_options(df) -> list:
    """The reflection ports a Smith chart can show for this table, for the
    plot picker. Empty -- so no Smith option at all -- when there are none,
    as for an eigenmode-only run."""
    if df is None:
        return []
    return [{"value": port, "label": smith.PORT_LABELS[port]} for port in smith.available_ports(df)]


def _port_impedance_ohm(run) -> float:
    """The reference a run's S-parameters were computed against, for the
    ohm readout of a Smith chart. Matches the solver's own default."""
    return float(run.analysis_settings.get("port_impedance_ohm", 50.0))


def _plot_filters(args) -> Dict[str, list]:
    """The results filter, read out of the plot URL.

    One `f_<column>` per swept parameter (and `f_mode` for a run with
    several modes), holding the values still
    ticked (`f_chip_width=0.25,0.5`). A parameter with no entry is
    not filtered, which is what leaving every box ticked means.
    """
    filters = {}
    for key, value in args.items(multi=False):
        if not key.startswith("f_"):
            continue
        filters[key[2:]] = [v for v in value.split(",") if v != ""]
    return filters


@app.route("/runs/<run_id>/plot")
def run_plot(run_id):
    """The figure itself, rendered for the requested axes and theme.

    Served as its own document so the run page can embed it in an iframe
    and swap axes by reloading just the figure, rather than rebuilding
    the whole page (the embedded plotly.js is far and away the largest
    thing on it).
    """
    run = Run.load(run_id)
    df = _results_frame(run)
    if df is None:
        return send_from_directory(run.plots_dir, plotting.PLOT_FILENAME)

    df = plotting.apply_filters(df, _plot_filters(request.args))
    if plotting.has_sparameters(df):
        df = plotting.apply_view(df, request.args.get("data", plotting.VIEW_BOTH))
        if df.empty:
            return Response(
                "<!doctype html><meta charset='utf-8'>"
                "<p style='font:14px system-ui;color:#888;padding:2rem;text-align:center'>"
                "No fine-range data for this selection yet -- refine the run, or pick another view.</p>",
                mimetype="text/html")
    if df.empty:
        return Response(
            "<!doctype html><meta charset='utf-8'>"
            "<p style='font:14px system-ui;color:#888;padding:2rem;text-align:center'>"
            "Every point is filtered out -- tick a value back on to see the plot.</p>",
            mimetype="text/html",
        )

    if request.args.get("view") == "smith":
        return Response(smith.smith_html(
            df,
            port=request.args.get("port", smith.DEFAULT_PORT),
            reference_ohm=_port_impedance_ohm(run),
            dark=request.args.get("theme") == "dark",
            plotlyjs_src=url_for("plotly_js"),
        ), mimetype="text/html")

    html = plotting.figure_html(
        df,
        run.sweep_param_names,
        x=request.args.get("x"),
        y=request.args.get("y"),
        color=request.args.get("color") or None,
        solution_type=registry.solution_type_of(run.config.get("analysis_settings", {})),
        dark=request.args.get("theme") == "dark",
        plotlyjs_src=url_for("plotly_js"),
        mode_markers=(companion.mode_markers(run, df["combo_id"].unique())
                      if plotting.has_sparameters(df) and "combo_id" in df.columns else None),
    )
    return Response(html, mimetype="text/html")


def _chip_field_file(run, combo_id: str) -> Optional[str]:
    """The chip's exported field samples for one combination, if post-
    processing got that far. It lands in the per-mode folder, whose name
    depends on the setup, so it is searched for rather than assumed."""
    pattern = os.path.join(run.combo_path(combo_id), "*", "chip_mag_E.fld")
    matches = sorted(glob.glob(pattern))
    return matches[0] if matches else None


def _chip_field_combos(run) -> list:
    """Combinations that have chip field data to draw.

    Only a post-processed combination can have any, so the state is
    filtered before the filesystem is touched at all: a sweep with
    thousands of combinations was otherwise costing one glob each on
    every page load, and (through `combo_dir`) leaving an empty folder
    behind for every combination it looked at.
    """
    return [
        combo_id for combo_id, combo in run.state.get("combos", {}).items()
        if combo.get("post_status") == "done" and _chip_field_file(run, combo_id)
    ]


def _chip_field_entries(run, combo_ids) -> list:
    """Top-face samples per combination, ready to draw."""
    entries = []
    for combo_id in combo_ids:
        path = _chip_field_file(run, combo_id)
        if not path:
            continue
        try:
            points = eigenmode_fields.read_field_points(path)
        except OSError:
            logger.warning("Could not read %s", path)
            continue
        if not points:
            continue
        axis = eigenmode_fields.thin_axis(points)
        highest = max(c[axis] for c, _ in points)
        extent = highest - min(c[axis] for c, _ in points)
        band = extent * 0.02
        top = [(c, v) for c, v in points if c[axis] >= highest - band]
        entries.append({"label": combo_id, "points": top or points, "axis": axis})
    return entries


@app.route("/runs/<run_id>/heatmap")
def run_heatmap(run_id):
    """Chip top-face |E|, for one combination or all of them at once."""
    run = Run.load(run_id)
    run = companion.prerequisite_of(run) or run   # the fields are the eigenmode part's
    wanted = request.args.get("combo") or "all"
    available = _chip_field_combos(run)
    combos = available if wanted == "all" else [c for c in available if c == wanted]

    # "All variants" is capped: each panel is a full interpolated
    # surface, so a sweep with hundreds of them would be tens of
    # megabytes and unreadable. Pick one from the list to see it alone.
    note = None
    if len(combos) > eigenmode_fields.MAX_HEATMAP_PANELS:
        note = (f"Showing {eigenmode_fields.MAX_HEATMAP_PANELS} of {len(combos)} variants -- "
                f"pick one from the list above to see it on its own.")
        combos = combos[:eigenmode_fields.MAX_HEATMAP_PANELS]

    html = eigenmode_fields.chip_heatmap_html(
        _chip_field_entries(run, combos),
        dark=request.args.get("theme") == "dark",
        plotlyjs_src=url_for("plotly_js"),
        note=note,
    )
    return Response(html, mimetype="text/html")


@app.route("/plotly.js")
def plotly_js():
    """Plotly itself, served once and cached.

    The figure is re-rendered on every axis change, and inlining the
    library into each render means re-sending several megabytes each
    time. Served from here it is fetched once per browser session. It
    comes out of the installed plotly package, so this still works with
    no internet connection.
    """
    from plotly.offline import get_plotlyjs

    response = Response(get_plotlyjs(), mimetype="application/javascript")
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


# The STEP viewer's third-party files, served from microwave_package/vendor/
# (see fetch_vendor.py). Only these names are servable, and the .wasm has
# to go out as application/wasm or the browser refuses to stream-compile it.
_VENDOR_MIME = {
    "three.module.min.js": "application/javascript",
    "OrbitControls.js": "application/javascript",
    "occt-import-js.js": "application/javascript",
    "occt-import-js.wasm": "application/wasm",
}


def _step_viewer_available() -> bool:
    """Whether fetch_vendor.py has been run -- the "3D view" button is
    hidden until it has."""
    return all((paths.VENDOR_DIR / name).is_file() for name in _VENDOR_MIME)


@app.route("/vendor/<name>")
def vendor_file(name):
    if name not in _VENDOR_MIME:
        return Response("not found", status=404)
    response = send_from_directory(str(paths.VENDOR_DIR), name, mimetype=_VENDOR_MIME[name])
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


@app.route("/runs/<run_id>/combos/<combo_id>/step")
def combo_step_file(run_id, combo_id):
    """The raw STEP file for one combination, for the in-browser viewer."""
    try:
        run = Run.load(run_id)
    except (OSError, ValueError):
        return Response("run not found", status=404)
    if combo_id not in run.state.get("combos", {}):
        return Response("unknown combination", status=404)

    # box is the representative preview -- lid_inside is the rest of the
    # enclosure, but this endpoint (and the in-browser viewer behind it)
    # only ever shows one file at a time. A run exported before the parts
    # were split has a single assembly STEP instead, found by the glob.
    step_paths = run.state["combos"][combo_id].get("step_paths") or {}
    step_path = step_paths.get("box")
    if not step_path or not os.path.exists(step_path):
        combo_dir = run.combo_dir(combo_id)
        matches = sorted(glob.glob(os.path.join(combo_dir, "*_box.STEP"))) or sorted(
            glob.glob(os.path.join(combo_dir, "*.STEP"))
        )
        step_path = matches[0] if matches else None
    if not step_path:
        return Response("no STEP file for this combination", status=404)

    folder, filename = os.path.split(step_path)
    return send_from_directory(folder, filename, mimetype="model/step")


# -- manufacturing files ---------------------------------------------------
# What a machine shop and a PCB fab need for one combination, listed in a
# panel on the run page and generated on demand. See workflow/manufacturing.

def _pipeline_busy() -> bool:
    """Whether any run of this process is driving SolidWorks/Altium/AEDT.
    A manufacturing job opens the same programs and the same base parts,
    and two same-named SolidWorks documents cannot be open at once."""
    return any(thread.is_alive() for thread in _active_threads.values())


def _manufacturing_target(run_id, combo_id):
    """(run, None) or (None, error response) for a manufacturing route."""
    try:
        run = Run.load(run_id)
    except (OSError, ValueError):
        return None, (jsonify(ok=False, error="run not found"), 404)
    if combo_id not in run.state.get("combos", {}):
        return None, (jsonify(ok=False, error="unknown combination"), 404)
    return run, None


@app.route("/runs/<run_id>/combos/<combo_id>/manufacturing.json")
def combo_manufacturing(run_id, combo_id):
    run, error = _manufacturing_target(run_id, combo_id)
    if error:
        return error
    groups = manufacturing.listing(run, combo_id)
    for group in groups:
        for entry in group["files"]:
            entry["url"] = (url_for("combo_manufacturing_file", run_id=run_id, combo_id=combo_id,
                                    key=entry["key"]) if entry["exists"] else None)
    return jsonify(
        combo_id=combo_id,
        groups=groups,
        generate_url=url_for("combo_manufacturing_generate", run_id=run_id, combo_id=combo_id),
        run_busy=_pipeline_busy(),
    )


@app.route("/runs/<run_id>/combos/<combo_id>/manufacturing/generate", methods=["POST"])
def combo_manufacturing_generate(run_id, combo_id):
    run, error = _manufacturing_target(run_id, combo_id)
    if error:
        return error
    group = (request.get_json(silent=True) or request.form or {}).get("group")
    if group not in manufacturing.GROUPS:
        return jsonify(ok=False, error="unknown group"), 400
    can, why = manufacturing.can_generate(run, combo_id, group)
    if not can:
        return jsonify(ok=False, error=why), 409
    if _pipeline_busy():
        return jsonify(ok=False, error="a run is using SolidWorks/Altium right now -- "
                                       "try again when it has finished"), 409
    started = manufacturing.JOBS.start(
        run_id, combo_id, group,
        lambda log: manufacturing.make_group(run, combo_id, group, log),
    )
    if not started:
        return jsonify(ok=False, error="already being generated"), 409
    return jsonify(ok=True), 202


@app.route("/runs/<run_id>/combos/<combo_id>/manufacturing/<key>")
def combo_manufacturing_file(run_id, combo_id, key):
    run, error = _manufacturing_target(run_id, combo_id)
    if error:
        return error
    spec = manufacturing.SPECS.get(key)
    if spec is None:
        return Response("unknown file", status=404)
    path = manufacturing.path_of(run, combo_id, key)
    if path is None:
        return Response("not generated yet", status=404)
    return send_file(path, mimetype=spec.mime, as_attachment=True,
                     download_name=manufacturing.download_name(run, combo_id, key))


@app.route("/runs/<run_id>/combos/<combo_id>/details.json")
def combo_details(run_id, combo_id):
    """Everything the run page's combination window shows.

    Fetched per click rather than embedded in the page: a sweep can have
    hundreds of combinations, and only the one you actually clicked is
    ever needed.
    """
    try:
        run = Run.load(run_id)
    except (OSError, ValueError):
        return jsonify(error="run not found"), 404
    combo = run.state.get("combos", {}).get(combo_id)
    if combo is None:
        return jsonify(error="unknown combination"), 404

    def described(source):
        return [
            {
                "label": parameter_meta.label_for(name),
                "value": parameter_meta.format_value(name, value),
                "unit": parameter_meta.unit_for(name) or "",
            }
            for name, value in source.items()
        ]

    results = []
    df = _results_frame(run)
    if df is not None and "combo_id" in df.columns:
        rows = df[df["combo_id"] == combo_id]
        for _, row in rows.iterrows():
            values = []
            for column, label in plotting.result_fields().items():
                if column in row and pd.notna(row[column]):
                    values.append({"label": label, "value": f"{float(row[column]):.6g}"})
            results.append({"mode": int(row["mode"]) if pd.notna(row.get("mode")) else None,
                            "values": values})

    has_project = _combo_project_file(run, combo_id) is not None
    states = run_manager.phase_states(combo, run.analysis_settings.get("use_altium", True))
    return jsonify({
        "combo_id": combo_id,
        "swept": described(combo.get("params", {})),
        "pinned": described(run.parameter_defaults),
        # Every phase, in order, with the word for what it is doing --
        # the same seven the row's bar is made of, spelled out.
        "status": [
            {"label": phase["label"], "what": phase["what"], "state": states[phase["key"]]}
            for phase in run_manager.PHASES
        ],
        "skip_reason": combo.get("skip_reason"),
        "altium_error": combo.get("altium_error"),
        # None once its results are in: whatever went wrong before a retry
        # succeeded is history, not something to show beside "Done".
        "error": None if combo.get("post_status") == "done" else (
            combo.get("post_error") or combo.get("model_error") or combo.get("step_error")),
        "results": results,
        "actions": {
            "step_url": (url_for("combo_step_file", run_id=run_id, combo_id=combo_id)
                         if _step_viewer_available() and combo.get("step_status") == "done" else None),
            "aedt_url": (url_for("open_combo_in_aedt", run_id=run_id, combo_id=combo_id)
                         if has_project else None),
            "start_url": url_for("index", from_run=run_id, combo=combo_id),
            "manufacturing_url": url_for("combo_manufacturing", run_id=run_id, combo_id=combo_id),
            "heatmap": bool(combo.get("post_status") == "done" and _chip_field_file(run, combo_id)),
        },
    })


@app.route("/runs/<run_id>/results.csv")
def run_results_csv(run_id):
    """The results table as the plot has it: for a run that was refined, the
    refinements' rows are in it too, told apart by the `sweep` column."""
    run = Run.load(run_id)
    df = _results_frame(run)
    if df is None:
        return send_from_directory(run.plots_dir, plotting.RESULTS_FILENAME, as_attachment=True)
    return Response(df.to_csv(index=False), mimetype="text/csv", headers={
        "Content-Disposition": f"attachment; filename={plotting.RESULTS_FILENAME}"})


@app.route("/runs/<run_id>/eigenmodes.csv")
def run_eigenmodes_csv(run_id):
    rows = companion.eigenmode_table(Run.load(run_id))
    return Response(pd.DataFrame(rows).to_csv(index=False), mimetype="text/csv", headers={
        "Content-Disposition": "attachment; filename=eigenmodes.csv"})


# -- comparing runs ----------------------------------------------------------
# A run id as Run.create makes them. Anything else in a comparison URL is
# dropped rather than joined onto a path.
_RUN_ID = re.compile(r"[A-Za-z0-9_\-]+")


def _requested_run_ids(args) -> list:
    """The run ids a comparison URL names, each once, oldest first.

    Read both as repeated `runs=` -- what the run list's checkboxes send
    -- and as one comma-separated `runs=`, which is how the comparison
    page links to itself. A run id is its start time, so sorted is oldest
    first: the first run made is the one the others are measured against.
    """
    wanted = []
    for value in args.getlist("runs"):
        for run_id in value.split(","):
            run_id = run_id.strip()
            if _RUN_ID.fullmatch(run_id) and run_id not in wanted:
                wanted.append(run_id)
    return sorted(wanted)


def _compared_runs(args):
    """The runs a comparison URL asks for, ready to compare -- and what to
    say about any that could not be."""
    notes, runs = [], []
    for run_id in _requested_run_ids(args):
        try:
            runs.append(Run.load(run_id))
        except (OSError, ValueError):
            logger.warning("Cannot load run %s to compare it.", run_id)
            notes.append(f"{run_id} could not be read, so it is not in this comparison.")
    if len(runs) > compare.MAX_RUNS:
        notes.append(f"A comparison takes up to {compare.MAX_RUNS} runs, so the "
                     f"{len(runs) - compare.MAX_RUNS} most recent were left out.")
        runs = runs[:compare.MAX_RUNS]
    entries = compare.compared(
        runs, [_run_name(run) for run in runs], [_results_frame(run) for run in runs])
    return entries, notes


def _compared_view(entry) -> dict:
    """One compared run, as the comparison page's key lists it."""
    run = entry.run
    results = entry.results if entry.has_results else None
    return {
        "run_id": entry.run_id,
        "letter": entry.letter,
        "name": entry.name,
        "title": entry.title,
        "slot": entry.slot,
        "url": url_for("run_status", run_id=entry.run_id),
        "started_text": _fmt_dt(_run_started(run)),
        "solution_type": registry.solution_type_of(run.analysis_settings),
        "swept_text": ", ".join(
            f'{parameter_meta.label_for(p["name"])} ×{len(p.get("values", []))}'
            for p in run.config.get("sweep_params", [])),
        "rows": len(results) if results is not None else 0,
        "combos": results["combo_id"].nunique() if results is not None and "combo_id" in results else 0,
    }


@app.route("/compare")
def compare_runs():
    """Several runs on one page: what they were made with and where that
    differs, their numbers point by point, and their results in one plot.

    Opened from the run list with the runs ticked. Everything below the
    settings works on the runs that have results; one that has none yet
    is still listed, and its settings still compared.
    """
    entries, notes = _compared_runs(request.args)
    runs = [entry.run for entry in entries]
    swept = compare.swept_names(runs)
    df = compare.combined_results(entries)

    solution_types = list(dict.fromkeys(registry.solution_type_of(r.analysis_settings) for r in runs))
    if len(solution_types) > 1:
        notes.append(
            f"These runs solved different things ({', '.join(solution_types)}). Their results "
            "share a column only where the name is the same, which is not always the same meaning."
        )
    notes += [f"{entry.name} has no results yet, so only its settings are compared here."
              for entry in entries if not entry.has_results]
    if len(entries) == 1:
        notes.append("There is only one run here -- tick another on the run list to compare it with.")

    differing, shared = compare.setting_rows(runs, held=[entry.held for entry in entries])
    axes = (compare.default_axes(df, swept, solution_types[0]) if not df.empty
            else {"x": None, "y": None, "color": None})
    table, table_note = compare.side_by_side(df, entries, swept)
    runs_param = ",".join(entry.run_id for entry in entries)
    views = [_compared_view(entry) for entry in entries]
    return render_template(
        "compare.html",
        entries=views,
        entries_by_id={view["run_id"]: view for view in views},
        symbols=plotting.RUN_SYMBOLS,
        notes=notes,
        differing=differing,
        shared=shared,
        unknown_values=(compare.NOT_RECORDED, compare.NOT_APPLICABLE),
        has_results=not df.empty,
        axis_options=plotting.axis_options(df, swept) if not df.empty else [],
        smith_options=_smith_options(df) if not df.empty else [],
        default_x=axes["x"],
        default_y=axes["y"],
        default_color=axes["color"] or "",
        filter_options=plotting.filter_options(df, swept) if not df.empty else [],
        run_id_column=plotting.RUN_ID_COLUMN,
        table=table,
        table_note=table_note,
        palette=plotting.RUN_COLOURS,
        runs_param=runs_param,
        change_url=url_for("index", compare=runs_param) + "#runs",
    )


@app.route("/compare/plot")
def compare_plot():
    """The comparison's figure, for the axes, filter and theme asked for --
    the comparison page's counterpart of `run_plot`."""
    entries, _ = _compared_runs(request.args)
    df = compare.combined_results(entries)
    if df.empty:
        return Response(
            "<!doctype html><meta charset='utf-8'>"
            "<p style='font:14px system-ui;color:#888;padding:2rem;text-align:center'>"
            "None of these runs has results to plot yet.</p>",
            mimetype="text/html",
        )
    swept = compare.swept_names(entry.run for entry in entries)
    # the first run's solver, as the page itself opens on
    solution_type = registry.solution_type_of(entries[0].run.analysis_settings)
    # What the page opens on, for any axis the URL leaves out. An empty
    # `color` is the "(none)" choice, which is not the same as no choice.
    axes = compare.default_axes(df, swept, solution_type)
    x, y = request.args.get("x") or axes["x"], request.args.get("y") or axes["y"]
    color = request.args.get("color", axes["color"]) or None

    df = plotting.apply_filters(df, _plot_filters(request.args))
    if df.empty:
        return Response(
            "<!doctype html><meta charset='utf-8'>"
            "<p style='font:14px system-ui;color:#888;padding:2rem;text-align:center'>"
            "Every point is filtered out -- tick a value back on to see the plot.</p>",
            mimetype="text/html",
        )

    dark = request.args.get("theme") == "dark"
    if request.args.get("view") == "smith":
        # Γ is referenced to each run's own port impedance already, so only
        # the ohm readout depends on it: the first run's is used, and says so.
        references = {_port_impedance_ohm(entry.run) for entry in entries}
        smith_note = None
        if len(references) > 1:
            smith_note = (f"These runs have different port impedances; the ohm readout uses "
                          f"{_port_impedance_ohm(entries[0].run):g} ohm, the first run's.")
        return Response(smith.smith_html(
            df,
            port=request.args.get("port", smith.DEFAULT_PORT),
            reference_ohm=_port_impedance_ohm(entries[0].run),
            dark=dark,
            plotlyjs_src=url_for("plotly_js"),
            run_colours=plotting.run_colours([entry.name for entry in entries], dark),
            note=smith_note,
        ), mimetype="text/html")

    off_axes = compare.runs_off_axes(df, x, y)
    html = plotting.figure_html(
        df,
        swept,
        x=x,
        y=y,
        color=color,
        solution_type=solution_type,
        dark=dark,
        plotlyjs_src=url_for("plotly_js"),
        # every compared run, hidden or not, so each keeps its own colour
        run_colours=plotting.run_colours([entry.name for entry in entries], dark),
        note=(f"Nothing from {', '.join(off_axes)} on these axes -- "
              f"{'it has' if len(off_axes) == 1 else 'they have'} no values for them."
              if off_axes else None),
    )
    return Response(html, mimetype="text/html")


@app.route("/compare/results.csv")
def compare_results_csv():
    """Every compared run's results in one CSV, each row saying which run."""
    entries, _ = _compared_runs(request.args)
    df = compare.combined_results(entries)
    if not df.empty:
        first = [plotting.RUN_ID_COLUMN, plotting.RUN_COLUMN]
        df = df[first + [column for column in df.columns if column not in first]]
    name = "comparison_" + "-".join(entry.run_id.removeprefix("run_") for entry in entries)
    # With a byte-order mark, because Excel reads a CSV without one in the
    # system code page -- and every run's name here has a "·" in it.
    return Response(
        "﻿" + df.to_csv(index=False), mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}.csv"'},
    )


def main() -> None:
    # 5000 unless PORT says otherwise, so a second instance can be run
    # beside one that is already serving without having to stop it.
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=False)


if __name__ == "__main__":
    main()
