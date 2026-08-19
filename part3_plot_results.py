"""
Part 3 -- Build/merge pedestal eigenmode results into plots, check task
status, and clean up failed tasks.

Part 2 does NOT write modes_results.csv anymore. It only writes raw mode
data (frequency, Q, post-processing outputs) into result.json inside each
task's queue/projects/<task_id>/ folder -- building the per-task CSV and
merging every task's CSV into one combined table is Part 3's job (this
keeps Part 2 a thin, generic "run the analysis, hand back the numbers"
worker, and keeps all the plot-shaping/formatting decisions in the one
script per experiment that actually cares about them).

This script never touches AEDT -- pure data wrangling, plotting, and
queue housekeeping. Three things it can do:

  1. Plot (default): for every completed task under the queue's
     projects/ tree, build (or refresh) a modes_results.csv right there
     from result.json, then combine every task's CSV into one DataFrame
     and produce the frequency-vs-pedestal-depth scatter plot.
  2. --check_status: walk a local built_models/ tree for the
     queue_task.json markers Part 1 left behind, and print each one's
     current stage (pending/in_progress/done/failed/unknown).
  3. --cleanup: like --check_status, but for every FAILED task it prints
     the error and interactively asks [R]emove / [T]ry again / [S]kip.
     Remove deletes the task's files from the queue (failed/*.task.json
     + error log, and queue/projects/<task_id>/) AND the local
     built_models/... folder that queued it. Try again re-queues the
     task (moves it back to pending/) so the worker picks it up again,
     leaving the project bundle in place.

See docs/ARCHITECTURE.md (in the ansys-analyze-worker repo), section 9
("Authoring a Part 3 script").

Requires `ansys-analyze-common` to be installed (`pip install
"git+https://github.com/d-s-t/ansys-analyze-worker.git#subdirectory=common"`,
or from requirements.txt in this repo) -- see that repo's README.
"""
from __future__ import annotations

import argparse
import glob
import json
import os

import matplotlib.pyplot as plt
import pandas as pd

from ansys_analyze_common import queue_common


def parse_run_selector(selector: str, available_runs: list) -> "set[int] | None":
    """
    Turn a --runs string into a set of selected run numbers, resolved
    against `available_runs` (the run numbers actually present in the
    data). Returns None for "no filtering" (select everything, INCLUDING
    tasks with no "run" in their metadata at all).

    Supported syntax:
      - ":"  or "" (default)   -> everything (returns None)
      - "start:stop:step"      -> Python list-slice semantics, applied
                                    POSITIONALLY over the sorted list of
                                    available run numbers -- e.g. "-2:"
                                    means "the last 2 runs that actually
                                    exist", "::2" means "every other run",
                                    exactly like list[start:stop:step].
                                    Any of start/stop/step may be omitted
                                    (e.g. ":5", "2:", "::-1").
      - "2,5,7"                -> exactly those run numbers
      - "4"                    -> exactly that run number

    Raises ValueError on malformed input.
    """
    selector = selector.strip()
    if selector in ("", ":"):
        return None

    sorted_runs = sorted(set(available_runs))

    if ":" in selector:
        parts = selector.split(":")
        if len(parts) > 3:
            raise ValueError(f"Invalid --runs slice {selector!r} -- expected start:stop:step")
        parts = [p.strip() for p in parts] + [""] * (3 - len(parts))
        start_s, stop_s, step_s = parts
        try:
            start = int(start_s) if start_s else None
            stop = int(stop_s) if stop_s else None
            step = int(step_s) if step_s else None
        except ValueError:
            raise ValueError(f"Invalid --runs slice {selector!r} -- start/stop/step must be integers")
        return set(sorted_runs[slice(start, stop, step)])

    if "," in selector:
        try:
            return {int(x.strip()) for x in selector.split(",") if x.strip()}
        except ValueError:
            raise ValueError(f"Invalid --runs list {selector!r} -- expected comma-separated integers")

    try:
        return {int(selector)}
    except ValueError:
        raise ValueError(f"Invalid --runs value {selector!r} -- expected a run number, list, or slice")


def build_and_load_results(queue: "queue_common.QueuePaths", run_selector: str = ":") -> pd.DataFrame:
    """
    For every task under queue.projects with a result.json, (re)build its
    modes_results.csv right there from the raw mode data, then combine
    all of them into one DataFrame. Skips tasks that failed, that somehow
    have no mode data, or that fall outside `run_selector` (see
    parse_run_selector), reporting what was skipped and why.
    """
    result_json_paths = glob.glob(os.path.join(queue.projects, "*", "result.json"))
    if not result_json_paths:
        raise FileNotFoundError(
            f"No result.json files found under {queue.projects} -- has Part 2 completed any tasks yet?"
        )

    # First pass: figure out which run numbers actually exist, so slice-
    # style selectors ("-2:", "::2", ...) have something to resolve
    # against. Tasks predating the "run" metadata field (or from a
    # task_type that doesn't set one) just won't have a run number.
    summaries = []
    available_runs = []
    for result_json_path in result_json_paths:
        with open(result_json_path) as f:
            summary = json.load(f)
        summaries.append((result_json_path, summary))
        run_number = summary.get("metadata", {}).get("run")
        if run_number is not None:
            available_runs.append(int(run_number))

    selected_runs = parse_run_selector(run_selector, available_runs)
    if selected_runs is not None:
        print(f"--runs {run_selector!r} -> runs {sorted(selected_runs)} "
              f"(available: {sorted(set(available_runs))})")

    frames = []
    skipped = []
    for result_json_path, summary in summaries:
        task_dir = os.path.dirname(result_json_path)

        if selected_runs is not None:
            run_number = summary.get("metadata", {}).get("run")
            if run_number is None or int(run_number) not in selected_runs:
                skipped.append((task_dir, f"excluded by --runs (run={run_number})"))
                continue

        if summary.get("status") != "success":
            skipped.append((task_dir, summary.get("status", "unknown")))
            continue

        modes = summary.get("result", {}).get("modes")
        if not modes:
            skipped.append((task_dir, "no mode data in result.json"))
            continue

        df = pd.DataFrame(modes)
        for key, value in summary.get("metadata", {}).items():
            df[key] = value

        csv_path = os.path.join(task_dir, "modes_results.csv")
        df.to_csv(csv_path, index=False)

        df["source_task_dir"] = task_dir
        frames.append(df)

    if skipped:
        print(f"Skipping {len(skipped)} run(s):")
        for task_dir, reason in skipped:
            print(f"  - {task_dir} ({reason})")

    if not frames:
        raise RuntimeError("No successful runs with mode data found (after any --runs filtering); nothing to plot.")

    return pd.concat(frames, ignore_index=True)


def plot_scatter(all_data: pd.DataFrame, out_dir: str) -> None:
    if all_data.empty:
        print("No data to plot.")
        return

    value_col = "max_mag_e" if "max_mag_e" in all_data.columns else None

    plt.figure(figsize=(10, 6))
    if value_col:
        scatter = plt.scatter(
            all_data["freq_ghz"],
            all_data["pedestal_depth"],
            c=all_data[value_col],
            cmap="viridis",
            s=50,
            edgecolors="k",
        )
        cbar = plt.colorbar(scatter)
        cbar.set_label("Max E Magnitude on Chip")
    else:
        plt.scatter(all_data["freq_ghz"], all_data["pedestal_depth"], s=50, edgecolors="k")

    plt.gca().invert_yaxis()
    plt.xlabel("Frequency (GHz)")
    plt.ylabel("Pedestal Depth (mm)")
    plt.title("Modes: Frequency vs Pedestal Depth")
    plt.grid(True, linestyle="--", alpha=0.7)
    plt.tight_layout()

    plot_path = os.path.join(out_dir, "modes_scatter_plot.png")
    plt.savefig(plot_path, dpi=300)
    print(f"Scatter plot saved to {plot_path}")


def _iter_local_markers(built_models_dir: str):
    for marker_path in glob.glob(os.path.join(built_models_dir, "**", "queue_task.json"), recursive=True):
        with open(marker_path) as f:
            marker = json.load(f)
        yield marker_path, marker


def check_status(built_models_dir: str, queue: "queue_common.QueuePaths") -> None:
    """
    Find every queue_task.json marker under built_models_dir (Part 1
    leaves one per model it built -- see queue_common.write_local_marker)
    and print the current status of each corresponding queued task.
    """
    rows = []
    for marker_path, marker in _iter_local_markers(built_models_dir):
        task_id = marker.get("task_id", "?")
        rows.append(
            {
                "local_folder": os.path.dirname(marker_path),
                "task_id": task_id,
                "status": queue_common.get_task_status(queue, task_id),
                "queued_at": marker.get("queued_at", "?"),
            }
        )

    if not rows:
        print(f"No queue_task.json markers found under {built_models_dir}")
        return

    width = max(len(r["local_folder"]) for r in rows)
    for r in sorted(rows, key=lambda r: r["queued_at"]):
        print(f"{r['local_folder']:<{width}}  {r['status']:<12} {r['task_id']}")

    counts: dict = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("\nSummary:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))


def cleanup_failed_tasks(built_models_dir: str, queue: "queue_common.QueuePaths") -> None:
    """
    Interactively resolve every FAILED task found via local
    queue_task.json markers: for each, print the error and ask
    [R]emove / [T]ry again / [S]kip.
    """
    markers = list(_iter_local_markers(built_models_dir))
    if not markers:
        print(f"No queue_task.json markers found under {built_models_dir}")
        return

    any_failed = False
    for marker_path, marker in markers:
        task_id = marker.get("task_id", "?")
        local_folder = os.path.dirname(marker_path)
        status = queue_common.get_task_status(queue, task_id)
        if status != "failed":
            continue
        any_failed = True

        print(f"\nFAILED: {local_folder}")
        print(f"  task_id: {task_id}")
        error_text = queue_common.read_error_log(queue, task_id)
        if error_text:
            first_line = error_text.strip().splitlines()[0]
            print(f"  error:   {first_line}")

        while True:
            choice = input("  [R]emove / [T]ry again / [S]kip? ").strip().lower()
            if choice in ("r", "remove"):
                queue_common.remove_task(queue, task_id, local_project_dir=local_folder)
                print("  Removed (queue files + local folder deleted).")
                break
            if choice in ("t", "try again", "retry"):
                if queue_common.retry_task(queue, task_id):
                    print("  Re-queued -- the worker will pick it up again.")
                else:
                    print("  Could not find this task in failed/ -- nothing to retry.")
                break
            if choice in ("s", "skip", ""):
                print("  Skipped.")
                break
            print("  Please enter R, T, or S.")

    if not any_failed:
        print("No failed tasks found.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Merge/plot pedestal eigenmode results, check task status, or clean up failed tasks (Part 3)"
    )
    parser.add_argument(
        "--queue_dir",
        type=str,
        default=os.environ.get("ANSYS_ANALYZE_QUEUE_PATH"),
        help="Queue root. Defaults to $ANSYS_ANALYZE_QUEUE_PATH.",
    )
    parser.add_argument("--out_dir", type=str, default=r"pedestal_experiment\plots", help="Where to save the combined CSV/plot")
    parser.add_argument(
        "--check_status",
        action="store_true",
        help="Instead of plotting, report each local built model's queue status",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Instead of plotting, interactively remove or retry failed tasks",
    )
    parser.add_argument(
        "--built_models_dir",
        type=str,
        default=r"pedestal_experiment\built_models",
        help="Local folder Part 1 built models into (needed for --check_status / --cleanup)",
    )
    parser.add_argument(
        "--runs",
        type=str,
        default=":",
        help="Which run(s) to plot/merge, by run number: ':' for all (default), "
             "'start:stop:step' as a slice over the sorted list of available runs "
             "(e.g. '-2:' for the last 2, '::2' for every other), "
             "'2,5,7' for an explicit list, or '4' for a single run.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.queue_dir:
        raise SystemExit(
            "No queue directory given -- pass --queue_dir or set ANSYS_ANALYZE_QUEUE_PATH."
        )
    queue = queue_common.QueuePaths(os.path.abspath(args.queue_dir))

    if args.cleanup:
        cleanup_failed_tasks(os.path.abspath(args.built_models_dir), queue)
        return

    if args.check_status:
        check_status(os.path.abspath(args.built_models_dir), queue)
        return

    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)

    all_data = build_and_load_results(queue, run_selector=args.runs)

    combined_csv = os.path.join(out_dir, "all_results.csv")
    all_data.to_csv(combined_csv, index=False)
    print(f"Combined {len(all_data)} rows from {all_data['source_task_dir'].nunique()} run(s) into {combined_csv}")

    plot_scatter(all_data, out_dir)


if __name__ == "__main__":
    main()
