"""
Builds/merges analysis results for a run into one combined table, from
the client's own per-task client_result.json files (written by
postprocess.py, right after it reopens each analyzed project -- see that
module's docstring for why the worker itself no longer produces this
data).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Callable

import pandas as pd

from ansys_analyze_common import queue_common

from ..design import parameter_meta

logger = logging.getLogger("microwave_package")


def build_combined_results(run, queue: "queue_common.QueuePaths", log: Callable[[str], None] = logger.info) -> pd.DataFrame:
    frames = []
    skipped = []

    for combo_id, combo in run.state["combos"].items():
        if combo.get("skip_reason"):
            skipped.append((combo_id, "skipped -- geometry does not fit"))
            continue

        task_id = combo.get("task_id")
        if not task_id:
            skipped.append((combo_id, "not queued"))
            continue

        if combo.get("post_status") != "done":
            skipped.append((combo_id, combo.get("post_status") or "not post-processed yet"))
            continue

        # Post-processing moves each task's files into the run folder and
        # clears the queue copy, so look there first; the queue path is
        # still checked for anything not archived (yet, or because its
        # transfer did not verify).
        candidates = []
        if combo.get("analysis_dir"):
            candidates.append(combo["analysis_dir"])
        candidates.append(queue_common.resolve_path(queue, queue_common.project_dir_for_task(task_id)))

        result_path = next(
            (os.path.join(d, "client_result.json")
             for d in candidates if os.path.exists(os.path.join(d, "client_result.json"))),
            None,
        )
        if result_path is None:
            skipped.append((combo_id, "no client_result.json yet"))
            continue

        with open(result_path) as f:
            summary = json.load(f)

        if summary.get("status") != "success":
            skipped.append((combo_id, summary.get("status", "unknown")))
            continue

        result = summary.get("result", {})
        # `rows` is what post-processing writes now -- a driven run's rows
        # are frequency points, not modes. `modes` is the same list under
        # the name it had before driven runs existed, so results produced
        # then still read back.
        rows = result.get("rows") or result.get("modes")
        if not rows:
            skipped.append((combo_id, "no result rows in client_result.json"))
            continue

        df = pd.DataFrame(rows)
        # Which solver produced these, carried per row so a table built
        # from several combinations still says what it holds. Older
        # results have no solution type recorded and read back as the
        # default one, which is what they were.
        if result.get("solution_type"):
            df["solution_type"] = result["solution_type"]
        # Swept parameters are reported in the unit they were entered in,
        # with the column named accordingly (`chip_width_um`), so the CSV
        # says what its numbers mean. The run's own state stays in mm.
        for key, value in combo.get("params", {}).items():
            df[parameter_meta.column_name(key)] = parameter_meta.to_display(key, value)
        df["combo_id"] = combo_id

        csv_path = os.path.join(run.combo_dir(combo_id), "results.csv")
        df.to_csv(csv_path, index=False)
        frames.append(df)

    if skipped:
        log(f"Skipping {len(skipped)} combo(s) when building results: "
            + ", ".join(f"{c} ({r})" for c, r in skipped))

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)
