"""
Web UI for the eigenmode-analysis client pipeline: configure a parameter
sweep (reading available parameters straight from the shared SolidWorks
equations file), start or resume a run, and watch its live status --
STEP generation, AEDT build+queue, worker analysis, and the final plot
-- without touching a terminal.

Run with:
    python -m eigen_mode_analyze.webapp
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
import logging
import os
import re
import shutil
import sys
import threading
import traceback
from typing import Dict, Optional

import pandas as pd
from flask import (Flask, Response, jsonify, redirect, render_template, request,
                   send_from_directory, url_for)
from markupsafe import Markup

from ansys_analyze_common import queue_common

from . import equations as eq
from . import (constraints, model_builder, parameter_meta, paths, pipeline,
               plotting, post_processing, run_manager)
from . import resilient_io
from .pipeline import run_pipeline
from .run_manager import Run
from .sweep import SweepParameter, expand_values, split_swept_and_fixed

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s", stream=sys.stdout)
logger = logging.getLogger("eigen_mode_analyze")

app = Flask(__name__)


# -- display helpers -----------------------------------------------------
# The pipeline's internal stage names, mapped to something readable and a
# colour kind ("ok"/"busy"/"bad"/"muted") the .pill class understands.
_STAGE_DISPLAY = {
    "created": ("Created", "muted"),
    "generating_and_queuing": ("Building geometry", "busy"),
    "geometry_ready": ("Waiting for the queue", "busy"),
    "monitoring": ("Analysing", "busy"),
    "postprocessing": ("Post-processing", "busy"),
    "plotting": ("Plotting results", "busy"),
    "complete": ("Complete", "ok"),
    "error": ("Error", "bad"),
}


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


# In-process registry of running pipeline threads, keyed by run_id -- so
# "resume" can tell whether a run is already active, and so a run
# survives navigating away from its status page.
_active_threads: Dict[str, threading.Thread] = {}
_stop_events: Dict[str, threading.Event] = {}


def _start_pipeline_thread(run: Run) -> None:
    # Deliberately does not touch the share: a run has to be startable
    # with the queue offline, because the geometry is built here and
    # only the handoff needs the network.
    queue = resilient_io.queue_from_env("ANSYS_ANALYZE_QUEUE_PATH")
    stop_event = threading.Event()
    _stop_events[run.run_id] = stop_event

    # Clear any error left over from a previous failed attempt at this
    # run, so a stale traceback doesn't linger on the page while this
    # (re)start is in progress.
    run.state.pop("error", None)
    run.save()

    def _target():
        # This thread (not Flask's main thread) is what drives
        # step_generation.py's SolidWorks COM automation. COM requires
        # each thread that uses it to call CoInitialize() itself first --
        # Flask's main thread happens to get this for free as a side
        # effect of other startup code, but a plain threading.Thread
        # never does. Without it, win32com.client.Dispatch(...) can
        # still succeed by *attaching* to an already-running SolidWorks
        # instance (via the Running Object Table) -- which is why this
        # can appear to work the first time -- but fails outright the
        # moment SolidWorks isn't already open and COM actually has to
        # launch a new instance for this thread.
        import pythoncom

        pythoncom.CoInitialize()
        try:
            run_pipeline(run, queue, log=logger.info, stop_event=stop_event)
        except Exception:
            # Logged here for the server console, AND saved onto the
            # run's state so the web UI itself can show what happened --
            # previously this was only ever visible in the console,
            # which is easy to miss if you're just watching the browser.
            logger.exception("Pipeline for run %s crashed", run.run_id)
            run.state["stage"] = "error"
            run.state["error"] = traceback.format_exc()
            run.save()
        finally:
            pythoncom.CoUninitialize()

    thread = threading.Thread(target=_target, daemon=True, name=f"pipeline-{run.run_id}")
    _active_threads[run.run_id] = thread
    thread.start()


def _is_running(run_id: str) -> bool:
    thread = _active_threads.get(run_id)
    return thread is not None and thread.is_alive()


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


def _distortion_mode_for(run: Run, combo_id: Optional[str]) -> str:
    """Which "Distortions" radio to preselect when the form is opened from
    an existing run: the mode that would reproduce how that run handled
    the `no_distortion` flag.

    A specific combination's own value wins -- that is the case where you
    took one point of an old "both" sweep as the basis for a new run.
    """
    param = parameter_meta.DISTORTION_PARAM
    if combo_id:
        combo = run.state.get("combos", {}).get(combo_id) or {}
        value = combo.get("params", {}).get(param)
        if value is not None:
            return "with" if float(value) == 0 else "none"
    for spec in run.config.get("sweep_params", []):
        if spec.get("name") == param:
            values = {float(v) for v in spec.get("values", [])}
            if len(values) > 1:
                return "both"
            return "with" if values == {0.0} else "none"
    defaults = run.config.get("parameter_defaults", {})
    if param in defaults:
        return "with" if float(defaults[param]) == 0 else "none"
    return parameter_meta.DEFAULT_DISTORTION_MODE


def _distortion_group(parameters) -> Optional[dict]:
    """The distortion-geometry group, limited to the parameters the
    equations file actually has -- so this section simply disappears if
    the file predates them."""
    names = [n for n in parameter_meta.DISTORTION_GROUP["names"] if n in parameters]
    return {**parameter_meta.DISTORTION_GROUP, "names": names} if names else None


def _distortion_limits(parameters, values) -> dict:
    """The largest each distortion parameter can be, for the form's hint.

    Rendered server-side so the numbers are right before any script
    runs; the page then recomputes them as the boxes change (the limits
    move with the PCB and chip dimensions, not just with the distortion).
    """
    starting = {**parameters, **{name: value for name, value in values.items() if value is not None}}
    maxima = constraints.max_values(starting)
    return {
        name: {"max": maximum, "note": constraints.limit_note(name, maximum)}
        for name, maximum in maxima.items()
        if name in parameters
    }


def _form_starting_point(from_run: Optional[str], combo_id: Optional[str]):
    """The analysis settings and per-parameter values to open the form with.

    Defaults to carrying over the most recent run, so the settings you
    used last time are already there; `from_run` picks a specific one
    instead, and `combo_id` starts from one combination of that run (its
    own values on top of whatever that run had pinned), which is the way
    to take a single point from a sweep and build a new sweep around it.

    Returns (settings, values, loaded_from_run_id, distortion_mode).
    """
    run_id = from_run or _latest_run_id()
    if not run_id:
        return dict(pipeline.DEFAULT_ANALYSIS_SETTINGS), {}, None, parameter_meta.DEFAULT_DISTORTION_MODE
    try:
        run = Run.load(run_id)
    except (OSError, ValueError, KeyError):
        logger.warning("Could not load settings from run %s", run_id)
        return dict(pipeline.DEFAULT_ANALYSIS_SETTINGS), {}, None, parameter_meta.DEFAULT_DISTORTION_MODE

    settings = {**pipeline.DEFAULT_ANALYSIS_SETTINGS, **run.config.get("analysis_settings", {})}
    values = run.starting_values()
    if combo_id:
        combo = run.state.get("combos", {}).get(combo_id)
        if combo:
            values = {**run.parameter_defaults, **combo.get("params", {})}
    return settings, values, run_id, _distortion_mode_for(run, combo_id)


def _runs_for_display(runs: list) -> list:
    """`Run.list_runs()` rows with the run-list's presentation fields added:
    a friendly name, a relative time, a humanised stage, and the one-line
    "what it swept" summary."""
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
    settings, values, loaded_from, distortion_mode = _form_starting_point(
        request.args.get("from_run"), request.args.get("combo")
    )
    return render_template(
        "index.html",
        parameters=parameters,
        groups=parameter_meta.grouped(parameters),
        runs=runs,
        distortion_geometry_params=parameter_meta.DISTORTION_GEOMETRY_PARAMETERS,
        drawings=_parameter_drawings(),
        meta=parameter_meta,
        settings=settings,
        values={name: parameter_meta.format_value(name, value) for name, value in values.items()},
        loaded_from=loaded_from,
        loaded_combo=request.args.get("combo"),
        has_distortion_param=parameter_meta.DISTORTION_PARAM in parameters,
        distortion_mode=distortion_mode,
        distortion_group=_distortion_group(parameters),
        distortion_limits=_distortion_limits(parameters, values),
        constraint_payload=constraints.client_payload(),
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
            continue  # driven by its own control (see the "Distortions" radio below)
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

    # The "Distortions" radio: "none"/"with" pin the no_distortion flag
    # for the whole run (one extra value on every combo, not a sweep
    # dimension); "both" sweeps it over 0 and 1, building every other
    # combination twice. Guarded on the flag actually being in the
    # equations file, the same as every range box above.
    mode = request.form.get("distortion_mode", parameter_meta.DEFAULT_DISTORTION_MODE)
    if parameter_meta.DISTORTION_PARAM in available:
        distortion_values = parameter_meta.DISTORTION_MODES.get(mode)
        if distortion_values:
            requested.append(SweepParameter(
                name=parameter_meta.DISTORTION_PARAM, values=list(distortion_values)
            ))

    sweep_params, parameter_defaults = split_swept_and_fixed(requested)
    if not sweep_params and not parameter_defaults:
        return redirect(url_for("index"))

    # A run with no distorted combination never uses the distortion's
    # geometry, so sweeping it there would multiply the run out for
    # nothing. Those values are dropped, and the run records why so the
    # status page can say so rather than leaving you to notice the
    # combination count did not change.
    notes = []
    if mode == "none":
        ignored = [p.name for p in sweep_params if p.name in parameter_meta.DISTORTION_GEOMETRY_PARAMETERS]
        if ignored:
            sweep_params = [p for p in sweep_params if p.name not in ignored]
            names = ", ".join(parameter_meta.label_for(n) for n in ignored)
            notes.append(
                f"This run builds no distorted geometry, so the sweep on {names} was ignored."
            )
            logger.info("Ignoring distortion-geometry sweep (%s) -- this run has no distorted combination.", names)

    settings = {
        "modes": int(request.form.get("modes", 20)),
        "max_passes": int(request.form.get("max_passes", 99)),
        "min_passes": int(request.form.get("min_passes", 5)),
        "min_converged": int(request.form.get("min_converged", 3)),
        "max_delta_f": float(request.form.get("max_delta_f", 1.0)),
        "min_freq_safety_factor": float(request.form.get("min_freq_safety_factor", 0.4)),
        "aedt_version": request.form.get("aedt_version") or None,
        "student_version": "student_version" in request.form,
        "graphical": "graphical" in request.form,
        "poll_seconds": float(request.form.get("poll_seconds", 10.0)),
        "use_altium": "use_altium" in request.form,
        "materials": {
            "mat_box": request.form.get("mat_box") or "aluminum",
            "mat_pcb": request.form.get("mat_pcb") or "Rogers TMM 10i (tm)",
            "mat_chip": request.form.get("mat_chip") or "silicon",
            "mat_vacuum": request.form.get("mat_vacuum") or "vacuum",
            "perfect_e_box": "perfect_e_box" in request.form,
            "mat_conductor_pcb": request.form.get("mat_conductor_pcb") or "copper",
            "mat_conductor_chip": request.form.get("mat_conductor_chip") or "niobium",
            "perfect_e_conductors": "perfect_e_conductors" in request.form,
        },
    }
    run = Run.create(sweep_params, settings, parameter_defaults)
    label = request.form.get("label", "").strip()[:80]
    if notes or label:
        if notes:
            run.config["notes"] = notes
        if label:
            run.config["label"] = label
        run.save()
    _start_pipeline_thread(run)
    return redirect(url_for("run_status", run_id=run.run_id))


@app.route("/runs/<run_id>/resume", methods=["POST"])
def resume_run(run_id):
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


def _combo_rows(run) -> dict:
    """The combinations to render, split into the ones being built and a
    capped sample of the ones skipped for not fitting."""
    combos = run.state.get("combos", {})
    buildable, skipped = {}, {}
    for combo_id, combo in combos.items():
        (skipped if combo.get("skip_reason") else buildable)[combo_id] = combo
    return {
        "buildable": buildable,
        "skipped_sample": dict(itertools.islice(skipped.items(), MAX_SKIPPED_ROWS)),
        "skipped_total": len(skipped),
        "skipped_hidden": max(len(skipped) - MAX_SKIPPED_ROWS, 0),
        "total": len(combos),
    }


@app.route("/runs/<run_id>")
def run_status(run_id):
    run = Run.load(run_id)
    plot_exists = os.path.exists(os.path.join(run.plots_dir, plotting.PLOT_FILENAME))
    df = _results_frame(run)
    options = plotting.axis_options(df, run.sweep_param_names) if df is not None else []
    filters = plotting.filter_options(df, run.sweep_param_names) if df is not None else []
    # Which columns the plot opens on depends on what the run solved --
    # an eigenmode sweep and an S-parameter band are read along different
    # axes. plotting decides; this only passes the answer to the page.
    axes = plotting.default_axes(
        df if df is not None else pd.DataFrame(), run.sweep_param_names
    )

    stage = run.state.get("stage")
    progress = run_manager.progress_of(run.state)
    started = _run_started(run)
    try:
        updated = _dt.datetime.fromtimestamp(os.path.getmtime(run.state_path))
    except OSError:
        updated = None
    # state.json stops being rewritten once the run settles, so its mtime
    # is a good enough "finished at" for the elapsed time.
    duration = updated - started if (started and updated and stage in ("complete", "error")) else None

    return render_template(
        "run_status.html",
        run=run,
        run_name=run.label or _fmt_dt(started) or run.run_id,
        rows=_combo_rows(run),
        is_running=_is_running(run_id),
        plot_exists=plot_exists,
        progress=progress,
        stage_display=_stage_display(stage, progress["failed"]),
        started_text=_fmt_dt(started),
        updated_text=_fmt_ago(updated),
        duration_text=_fmt_dur(duration),
        meta=parameter_meta,
        has_common_dimensions=os.path.exists(os.path.join(run.run_dir, "common_dimensions.txt")),
        axis_options=options,
        default_x=axes["x"],
        default_y=axes["y"],
        default_color=axes["color"] or "",
        disk_usage_mb=_run_disk_usage(run) / (1024 * 1024),
        heatmap_combos=_chip_field_combos(run),
        step_viewer_available=_step_viewer_available(),
        filter_options=filters,
        # A finished run's combination table is a record rather than
        # something to watch, so it starts folded away -- with hundreds of
        # rows it otherwise buries the results underneath it.
        combos_open=not run.is_complete,
    )


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


@app.route("/runs/<run_id>/delete", methods=["POST"])
def delete_run(run_id):
    """Delete a run's folder, and any queue files its tasks still have.

    Guarded three ways, because there is no undo: the page asks first,
    the form has to come back with the run's own id typed in, and a run
    that is still going is refused outright.
    """
    if _is_running(run_id):
        logger.warning("Refusing to delete run %s -- it is still running.", run_id)
        return redirect(url_for("run_status", run_id=run_id))

    if request.form.get("confirm_run_id", "").strip() != run_id:
        logger.warning("Refusing to delete run %s -- confirmation did not match.", run_id)
        return redirect(url_for("run_status", run_id=run_id))

    try:
        run = Run.load(run_id)
    except (OSError, ValueError):
        logger.warning("Cannot load run %s to delete it.", run_id)
        return redirect(url_for("index"))

    # anything of this run's still sitting in the queue goes too, so
    # deleting a run really does leave nothing behind
    # Lazily, so deleting a run still works with the share down: the
    # per-task removals below already tolerate failing individually.
    queue = resilient_io.queue_from_env("ANSYS_ANALYZE_QUEUE_PATH")
    for combo in run.state.get("combos", {}).values():
        task_id = combo.get("task_id")
        if not task_id:
            continue
        try:
            queue_common.remove_task(queue, task_id)
            done_task_file = os.path.join(queue.done, f"{task_id}.task.json")
            if os.path.exists(done_task_file):
                os.remove(done_task_file)
        except OSError as exc:
            logger.warning("Could not clear queue files for task %s: %s", task_id, exc)

    shutil.rmtree(run.run_dir, ignore_errors=True)
    logger.info("Deleted run %s", run_id)
    return redirect(url_for("index"))


@app.route("/runs/<run_id>/common_dimensions.txt")
def run_common_dimensions(run_id):
    run = Run.load(run_id)
    return send_from_directory(run.run_dir, "common_dimensions.txt", mimetype="text/plain")


@app.route("/runs/<run_id>/status.json")
def run_status_json(run_id):
    """What the run page polls every few seconds.

    Only the three status fields go out, and only for combinations that
    can still change -- a skipped one never will, and its parameters and
    reason were already rendered into the page. Sending the whole state
    instead meant a large sweep re-sent megabytes on every poll.
    """
    run = Run.load(run_id)
    prog = run_manager.progress_of(run.state)
    return jsonify({
        "run_id": run.run_id,
        "stage": run.state.get("stage"),
        "stage_display": _stage_display(run.state.get("stage"), prog["failed"]),
        "progress": prog,
        "error": run.state.get("error"),
        "is_running": _is_running(run_id),
        # so the page can reload itself once, when the plot first appears
        "plot_ready": os.path.exists(os.path.join(run.plots_dir, plotting.PLOT_FILENAME)),
        "combos": {
            combo_id: {
                "step_status": combo.get("step_status"),
                "queue_status": combo.get("queue_status"),
                "post_status": combo.get("post_status"),
            }
            for combo_id, combo in run.state["combos"].items()
            if not combo.get("skip_reason")
        },
    })


def _results_frame(run):
    """This run's combined results table, or None if it has none yet."""
    csv_path = os.path.join(run.plots_dir, "all_results.csv")
    if not os.path.exists(csv_path):
        return None
    try:
        return pd.read_csv(csv_path)
    except (OSError, ValueError):
        logger.warning("Could not read results for run %s", run.run_id)
        return None


def _plot_filters(args) -> Dict[str, list]:
    """The results filter, read out of the plot URL.

    One `f_<column>` per swept parameter, holding the values still
    ticked (`f_distortion_radius=0.25,0.5`). A parameter with no entry is
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
    if df.empty:
        return Response(
            "<!doctype html><meta charset='utf-8'>"
            "<p style='font:14px system-ui;color:#888;padding:2rem;text-align:center'>"
            "Every point is filtered out -- tick a value back on to see the plot.</p>",
            mimetype="text/html",
        )

    html = plotting.figure_html(
        df,
        run.sweep_param_names,
        x=request.args.get("x"),
        y=request.args.get("y"),
        color=request.args.get("color") or None,
        dark=request.args.get("theme") == "dark",
        plotlyjs_src=url_for("plotly_js"),
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
            points = post_processing.read_field_points(path)
        except OSError:
            logger.warning("Could not read %s", path)
            continue
        if not points:
            continue
        axis = post_processing.thin_axis(points)
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
    wanted = request.args.get("combo") or "all"
    available = _chip_field_combos(run)
    combos = available if wanted == "all" else [c for c in available if c == wanted]

    # "All variants" is capped: each panel is a full interpolated
    # surface, so a sweep with hundreds of them would be tens of
    # megabytes and unreadable. Pick one from the list to see it alone.
    note = None
    if len(combos) > plotting.MAX_HEATMAP_PANELS:
        note = (f"Showing {plotting.MAX_HEATMAP_PANELS} of {len(combos)} variants -- "
                f"pick one from the list above to see it on its own.")
        combos = combos[:plotting.MAX_HEATMAP_PANELS]

    html = plotting.chip_heatmap_html(
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


# The STEP viewer's third-party files, served from eigen_mode_analyze/vendor/
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

    step_path = run.state["combos"][combo_id].get("step_path")
    if not step_path or not os.path.exists(step_path):
        matches = sorted(glob.glob(os.path.join(run.combo_dir(combo_id), "*.STEP")))
        step_path = matches[0] if matches else None
    if not step_path:
        return Response("no STEP file for this combination", status=404)

    folder, filename = os.path.split(step_path)
    return send_from_directory(folder, filename, mimetype="model/step")


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
            for column, label in plotting.RESULT_FIELDS.items():
                if column in row and pd.notna(row[column]):
                    values.append({"label": label, "value": f"{float(row[column]):.6g}"})
            results.append({"mode": int(row["mode"]) if pd.notna(row.get("mode")) else None,
                            "values": values})

    has_project = _combo_project_file(run, combo_id) is not None
    return jsonify({
        "combo_id": combo_id,
        "swept": described(combo.get("params", {})),
        "pinned": described(run.parameter_defaults),
        "status": {
            "step": combo.get("step_status") or ("skipped" if combo.get("skip_reason") else "pending"),
            "queue": combo.get("queue_status") or "not queued",
            "post": combo.get("post_status") or "pending",
        },
        "skip_reason": combo.get("skip_reason"),
        "error": combo.get("post_error") or combo.get("step_error"),
        "results": results,
        "actions": {
            "step_url": (url_for("combo_step_file", run_id=run_id, combo_id=combo_id)
                         if _step_viewer_available() and combo.get("step_status") == "done" else None),
            "aedt_url": (url_for("open_combo_in_aedt", run_id=run_id, combo_id=combo_id)
                         if has_project else None),
            "start_url": url_for("index", from_run=run_id, combo=combo_id),
            "heatmap": bool(combo.get("post_status") == "done" and _chip_field_file(run, combo_id)),
        },
    })


@app.route("/runs/<run_id>/results.csv")
def run_results_csv(run_id):
    run = Run.load(run_id)
    return send_from_directory(run.plots_dir, "all_results.csv", as_attachment=True)


def main() -> None:
    app.run(host="127.0.0.1", port=5000, debug=False)


if __name__ == "__main__":
    main()
