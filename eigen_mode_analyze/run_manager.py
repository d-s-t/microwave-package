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

from . import paths
from .sweep import SweepParameter, combinations, combo_id_for

logger = logging.getLogger("eigen_mode_analyze")


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
        combos = combinations(sweep_params)
        run.state = {
            "stage": "created",
            "combos": {
                combo_id_for(combo): {
                    "params": combo,
                    "step_status": None,
                    "step_path": None,
                    "step_error": None,
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
            state_path = os.path.join(runs_root, run_id, "state.json")
            if not os.path.isfile(state_path):
                continue
            try:
                state = cls._read_json(state_path)
            except (OSError, json.JSONDecodeError):
                continue
            combos = state.get("combos", {})
            done = sum(1 for c in combos.values() if c.get("queue_status") == "done")
            failed = sum(1 for c in combos.values() if c.get("queue_status") == "failed")
            summaries.append({
                "run_id": run_id,
                "stage": state.get("stage"),
                "total": len(combos),
                "done": done,
                "failed": failed,
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

    def set_stage(self, stage: str) -> None:
        with self._lock:
            self.state["stage"] = stage
            self.save_state()

    def update_combo(self, combo_id: str, **fields) -> None:
        with self._lock:
            self.state["combos"][combo_id].update(fields)
            self.save_state()

    def combo_dir(self, combo_id: str) -> str:
        d = os.path.join(self.run_dir, "models", combo_id)
        os.makedirs(d, exist_ok=True)
        return d

    @property
    def sweep_param_names(self) -> List[str]:
        return [p["name"] for p in self.config.get("sweep_params", [])]

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
            c.get("queue_status") == "failed" or c.get("post_status") in ("done", "failed")
            for c in combos.values()
        )
