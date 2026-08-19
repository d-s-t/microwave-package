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

import logging
import os
import sys
import threading
import traceback
from typing import Dict

from flask import Flask, jsonify, redirect, render_template, request, send_from_directory, url_for

from ansys_analyze_common import queue_common

from . import equations as eq
from . import paths
from .pipeline import run_pipeline
from .run_manager import Run
from .sweep import SweepParameter, expand_values

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

    thread = threading.Thread(target=_target, daemon=True, name=f"pipeline-{run.run_id}")
    _active_threads[run.run_id] = thread
    thread.start()


def _is_running(run_id: str) -> bool:
    thread = _active_threads.get(run_id)
    return thread is not None and thread.is_alive()


@app.route("/")
def index():
    content = eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE))
    parameters = eq.get_numeric_parameters(content)
    runs = Run.list_runs()
    return render_template("index.html", parameters=parameters, runs=runs)


@app.route("/runs/new", methods=["POST"])
def new_run():
    sweep_params = []
    for name in request.form.getlist("param_name"):
        spec = request.form.get(f"range_{name}", "").strip()
        if not spec:
            continue
        sweep_params.append(SweepParameter(name=name, values=expand_values(spec)))
    if not sweep_params:
        return redirect(url_for("index"))

    settings = {
        "target_max_freq": float(request.form.get("target_max_freq", 20.0)),
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
    run = Run.create(sweep_params, settings)
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
    return render_template("run_status.html", run=run, is_running=_is_running(run_id), plot_exists=plot_exists)


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


@app.route("/runs/<run_id>/plot")
def run_plot(run_id):
    run = Run.load(run_id)
    return send_from_directory(run.plots_dir, "modes_scatter_plot.html")


@app.route("/runs/<run_id>/results.csv")
def run_results_csv(run_id):
    run = Run.load(run_id)
    return send_from_directory(run.plots_dir, "all_results.csv", as_attachment=True)


def main() -> None:
    app.run(host="127.0.0.1", port=5000, debug=False)


if __name__ == "__main__":
    main()
