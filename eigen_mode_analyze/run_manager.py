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
import threading
import time
from typing import Dict, List, Optional

from . import constraints
from . import equations as eq
from . import parameter_meta
from . import paths
from .sweep import SweepParameter, combinations, combo_id_for

logger = logging.getLogger("eigen_mode_analyze")


def progress_of(state: dict) -> Dict[str, int]:
    """How far a run has got, counted from its combinations.

    One definition, used by both the run list and the run page, so the
    numbers agree: a combination is *done* once it is post-processed,
    *failed* if analysis or post-processing failed, *skipped* if its
    geometry never fitted, and otherwise still on its way.
    """
    combos = state.get("combos", {})
    done = failed = skipped = 0
    for combo in combos.values():
        if combo.get("skip_reason"):
            skipped += 1
        elif combo.get("queue_status") == "failed" or combo.get("post_status") == "failed":
            failed += 1
        elif combo.get("post_status") == "done":
            done += 1
    total = len(combos)
    return {
        "total": total,
        "done": done,
        "failed": failed,
        "skipped": skipped,
        "pending": total - done - failed - skipped,
    }


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

    # -- creation / loading -------------------------------------------------
    @classmethod
    def create(
        cls,
        sweep_params: List[SweepParameter],
        analysis_settings: dict,
        parameter_defaults: Optional[Dict[str, float]] = None,
    ) -> "Run":
        run_id = _dt.datetime.now().strftime("run_%Y%m%d_%H%M%S")
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
        combos = combinations(sweep_params, parameter_defaults)

        # Each combination's full geometry -- the equations file's values
        # with the run's pinned values and then the combo's own on top --
        # is checked before anything is built, so a combination that
        # cannot physically fit is marked here and skipped later rather
        # than failing somewhere inside SolidWorks.
        base_values = _equation_file_values()
        run.state = {
            "stage": "created",
            "combos": {
                combo_id_for(combo): {
                    "params": combo,
                    "step_status": None,
                    "step_path": None,
                    "step_error": None,
                    # Why this combination will not be built, or None when
                    # it is buildable (see constraints.py).
                    "skip_reason": constraints.check_combination(
                        {**base_values, **(parameter_defaults or {}), **combo}
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
        run.config = cls._read_json(run.config_path)
        run.state = cls._read_json(run.state_path)
        return run

    @classmethod
    def list_runs(cls) -> List[dict]:
        """Summaries of every run under RUNS_ROOT, newest first, for the web UI's run list / --resume."""
        runs_root = str(paths.RUNS_ROOT)
        if not os.path.isdir(runs_root):
            return []
        summaries = []
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
            swept = [
                {"label": parameter_meta.label_for(p["name"]), "n": len(p.get("values", []))}
                for p in config.get("sweep_params", [])
            ]
            summaries.append({
                "run_id": run_id,
                "label": (config.get("label") or "").strip(),
                "created_at": config.get("created_at"),
                "stage": state.get("stage"),
                "swept": swept,
                **progress_of(state),
            })
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
    def is_complete(self) -> bool:
        combos = self.state["combos"]
        return bool(combos) and all(
            c.get("skip_reason")
            or c.get("queue_status") == "failed"
            or c.get("post_status") in ("done", "failed")
            for c in combos.values()
        )
