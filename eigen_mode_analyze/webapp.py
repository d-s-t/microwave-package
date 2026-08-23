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

import glob
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
from . import parameter_meta, paths, pipeline, plotting, post_processing
from .pipeline import run_pipeline
from .run_manager import Run
from .sweep import SweepParameter, expand_values, split_swept_and_fixed

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s", stream=sys.stdout)
logger = logging.getLogger("eigen_mode_analyze")

app = Flask(__name__)

# In-process registry of running pipeline threads, keyed by run_id -- so
# "resume" can tell whether a run is already active, and so a run
# survives navigating away from its status page.
_active_threads: Dict[str, threading.Thread] = {}
_stop_events: Dict[str, threading.Event] = {}


def _start_pipeline_thread(run: Run) -> None:
    queue = queue_common.QueuePaths.from_env("ANSYS_ANALYZE_QUEUE_PATH")
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


def _form_starting_point(from_run: Optional[str], combo_id: Optional[str]):
    """The analysis settings and per-parameter values to open the form with.

    Defaults to carrying over the most recent run, so the settings you
    used last time are already there; `from_run` picks a specific one
    instead, and `combo_id` starts from one combination of that run (its
    own values on top of whatever that run had pinned), which is the way
    to take a single point from a sweep and build a new sweep around it.
    """
    run_id = from_run or _latest_run_id()
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


@app.route("/")
def index():
    content = eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE))
    parameters = eq.get_numeric_parameters(content)
    runs = Run.list_runs()
    settings, values, loaded_from = _form_starting_point(
        request.args.get("from_run"), request.args.get("combo")
    )
    return render_template(
        "index.html",
        parameters=parameters,
        groups=parameter_meta.grouped(parameters),
        runs=runs,
        drawings=_parameter_drawings(),
        meta=parameter_meta,
        settings=settings,
        values={name: parameter_meta.format_value(name, value) for name, value in values.items()},
        loaded_from=loaded_from,
        loaded_combo=request.args.get("combo"),
    )


@app.route("/runs/new", methods=["POST"])
def new_run():
    # A filled-in box means "use this"; an empty one leaves the parameter
    # at the equation file's value. A box holding a single value pins that
    # value for the whole run, and only boxes holding more than one value
    # become sweep dimensions. The names come from the equations file
    # rather than the form, so only real parameters count.
    content = eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE))
    requested = []
    for name in eq.get_numeric_parameters(content):
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
    sweep_params, parameter_defaults = split_swept_and_fixed(requested)
    if not sweep_params and not parameter_defaults:
        return redirect(url_for("index"))

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
        "materials": {
            "mat_box": request.form.get("mat_box", "aluminum"),
            "mat_pcb": request.form.get("mat_pcb", "Rogers TMM 10i (tm)"),
            "mat_chip": request.form.get("mat_chip", "silicon"),
            "mat_vacuum": request.form.get("mat_vacuum", "vacuum"),
            "perfect_e_box": "perfect_e_box" in request.form,
        },
    }
    run = Run.create(sweep_params, settings, parameter_defaults)
    _start_pipeline_thread(run)
    return redirect(url_for("run_status", run_id=run.run_id))


@app.route("/runs/<run_id>/resume", methods=["POST"])
def resume_run(run_id):
    if not _is_running(run_id):
        run = Run.load(run_id)
        _start_pipeline_thread(run)
    return redirect(url_for("run_status", run_id=run_id))


@app.route("/runs/<run_id>")
def run_status(run_id):
    run = Run.load(run_id)
    plot_exists = os.path.exists(os.path.join(run.plots_dir, "modes_scatter_plot.html"))
    df = _results_frame(run)
    options = plotting.axis_options(df, run.sweep_param_names) if df is not None else []
    swept = [parameter_meta.column_name(n) for n in run.sweep_param_names]
    return render_template(
        "run_status.html",
        run=run,
        is_running=_is_running(run_id),
        plot_exists=plot_exists,
        meta=parameter_meta,
        has_common_dimensions=os.path.exists(os.path.join(run.run_dir, "common_dimensions.txt")),
        axis_options=options,
        default_x=(swept[0] if swept else "combo_id"),
        default_y=plotting.DEFAULT_Y,
        disk_usage_mb=_run_disk_usage(run) / (1024 * 1024),
        heatmap_combos=_chip_field_combos(run),
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
    queue = queue_common.QueuePaths.from_env("ANSYS_ANALYZE_QUEUE_PATH")
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
    run = Run.load(run_id)
    return jsonify({
        "run_id": run.run_id,
        "stage": run.state.get("stage"),
        "error": run.state.get("error"),
        "is_running": _is_running(run_id),
        "combos": run.state["combos"],
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
        return send_from_directory(run.plots_dir, "modes_scatter_plot.html")

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
    pattern = os.path.join(run.combo_dir(combo_id), "*", "chip_mag_E.fld")
    matches = sorted(glob.glob(pattern))
    return matches[0] if matches else None


def _chip_field_combos(run) -> list:
    return [c for c in run.state.get("combos", {}) if _chip_field_file(run, c)]


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

    html = plotting.chip_heatmap_html(
        _chip_field_entries(run, combos),
        dark=request.args.get("theme") == "dark",
        plotlyjs_src=url_for("plotly_js"),
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


@app.route("/runs/<run_id>/results.csv")
def run_results_csv(run_id):
    run = Run.load(run_id)
    return send_from_directory(run.plots_dir, "all_results.csv", as_attachment=True)


def main() -> None:
    app.run(host="127.0.0.1", port=5000, debug=False)


if __name__ == "__main__":
    main()
