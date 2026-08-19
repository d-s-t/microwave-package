"""
Builds/merges analysis results for a run into one combined table, from
the worker's raw per-task result.json files (see ansys-analyze-worker's
ARCHITECTURE.md sections 5 and 9 -- the worker deliberately doesn't build
a CSV itself, that's the client pipeline's job).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Callable

import pandas as pd

from ansys_analyze_common import queue_common

logger = logging.getLogger("eigen_mode_analyze")


def build_combined_results(run, queue: "queue_common.QueuePaths", log: Callable[[str], None] = logger.info) -> pd.DataFrame:
    frames = []
    skipped = []

    for combo_id, combo in run.state["combos"].items():
        task_id = combo.get("task_id")
        if not task_id:
            skipped.append((combo_id, "not queued"))
            continue

        output_dir = queue_common.resolve_path(queue, queue_common.project_dir_for_task(task_id))
        result_path = os.path.join(output_dir, "result.json")
        if not os.path.exists(result_path):
            skipped.append((combo_id, "no result.json yet"))
            continue

        with open(result_path) as f:
            summary = json.load(f)

        if summary.get("status") != "success":
            skipped.append((combo_id, summary.get("status", "unknown")))
            continue

        modes = summary.get("result", {}).get("modes")
        if not modes:
            skipped.append((combo_id, "no mode data in result.json"))
            continue

        df = pd.DataFrame(modes)
        for key, value in combo.get("params", {}).items():
            df[key] = value
        df["combo_id"] = combo_id

        csv_path = os.path.join(run.combo_dir(combo_id), "modes_results.csv")
        df.to_csv(csv_path, index=False)
        frames.append(df)

    if skipped:
        log(f"Skipping {len(skipped)} combo(s) when building results: "
            + ", ".join(f"{c} ({r})" for c, r in skipped))

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)
