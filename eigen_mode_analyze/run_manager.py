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
import os
from typing import Dict, List

from . import paths
from .sweep import SweepParameter, combinations, combo_id_for


class Run:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.run_dir = os.path.join(str(paths.RUNS_ROOT), run_id)
        self.plots_dir = os.path.join(self.run_dir, "plots")
        self.config_path = os.path.join(self.run_dir, "run_config.json")
        self.state_path = os.path.join(self.run_dir, "state.json")
        self.config: dict = {}
        self.state: dict = {"stage": "created", "combos": {}}

    # -- creation / loading -------------------------------------------------
    @classmethod
    def create(cls, sweep_params: List[SweepParameter], analysis_settings: dict) -> "Run":
        run_id = _dt.datetime.now().strftime("run_%Y%m%d_%H%M%S")
        run = cls(run_id)
        os.makedirs(run.run_dir, exist_ok=True)
        os.makedirs(run.plots_dir, exist_ok=True)

        run.config = {
            "sweep_params": [{"name": p.name, "values": p.values} for p in sweep_params],
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
                }
                for combo in combos
            },
        }
        run.save()
        return run

    @classmethod
    def load(cls, run_id: str) -> "Run":
        run = cls(run_id)
        with open(run.config_path) as f:
            run.config = json.load(f)
        with open(run.state_path) as f:
            run.state = json.load(f)
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
                with open(state_path) as f:
                    state = json.load(f)
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
    def save(self) -> None:
        os.makedirs(self.run_dir, exist_ok=True)
        with open(self.config_path, 'w') as f:
            json.dump(self.config, f, indent=2, default=str)
        with open(self.state_path, 'w') as f:
            json.dump(self.state, f, indent=2, default=str)

    def set_stage(self, stage: str) -> None:
        self.state["stage"] = stage
        self.save()

    def update_combo(self, combo_id: str, **fields) -> None:
        self.state["combos"][combo_id].update(fields)
        self.save()

    def combo_dir(self, combo_id: str) -> str:
        d = os.path.join(self.run_dir, "models", combo_id)
        os.makedirs(d, exist_ok=True)
        return d

    @property
    def sweep_param_names(self) -> List[str]:
        return [p["name"] for p in self.config.get("sweep_params", [])]

    @property
    def is_complete(self) -> bool:
        combos = self.state["combos"]
        return bool(combos) and all(c.get("queue_status") in ("done", "failed") for c in combos.values())
