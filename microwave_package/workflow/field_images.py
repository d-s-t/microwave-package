"""
The field images of a field-image part (see companion.create_field_images).

The worker evaluates the fields of a part's one frequency after it solves
it, and writes each export to fields/<name>.fld beside its result.json (its
ARCHITECTURE.md section 3). The file is plain text: a header, then one row
per point, `x y z value`, with the coordinates in metres though the header
says millimetres, z changing fastest and x slowest, and `Nan` where there is
no field. This module reads those files, draws each export as a two-panel
picture (log10 and linear colour scales, grey where there is no field), and
keeps a summary the run page reads.

Nothing here touches AEDT: a field part is read from its files only, so the
student licence never has to open a project whose mesh is over its limit.
"""
from __future__ import annotations

import json
import logging
import os
import warnings
from typing import Callable, Dict, List, Optional

logger = logging.getLogger("microwave_package")

FIELD_DIR = "fields"
IMAGE_DIR = "field_images"
SUMMARY_NAME = "summary.json"
RESULT_NAME = "result.json"

AXES = ("x", "y", "z")
# What each quantity is called on the page, and its SI unit (HFSS works in SI).
QUANTITY_NAMES = {"E": ("|E|", "V/m"), "H": ("|H|", "A/m")}
# What each place a plane can be is called on the page (hfss/driven/field_exports.py).
PLACES = {
    "substrate_mid": "middle of the board",
    "substrate_top": "0.5 mm above the board",
    "cavity_mid": "middle of the cavity above the board",
    "section_xy": "vertical section along the line",
    "vacuum_volume": "whole cavity, largest value along the vertical",
}


def is_field_part(analysis_settings: dict) -> bool:
    """Whether a run is a field-image part, from its analysis settings: its one job is to draw fields."""
    return (analysis_settings or {}).get("field_frequency_ghz") is not None


def images_dir(run_dir: str) -> str:
    return os.path.join(run_dir, IMAGE_DIR)


def read_summary(run_dir: str) -> Optional[dict]:
    """What the part's pictures came to (see `draw_run`), or None before they are drawn."""
    try:
        with open(os.path.join(images_dir(run_dir), SUMMARY_NAME), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def place_of(name: str) -> str:
    """The place an export's name stands for, as the page says it."""
    return PLACES.get(name.rsplit("_", 1)[0], name)


def quantity_of(name: str) -> Optional[str]:
    """The letter an export's name ends with: E or H, or None."""
    tail = name.rsplit("_", 1)[-1]
    return tail if tail in QUANTITY_NAMES else None


class FieldGrid:
    """The field magnitude on a grid: `axes` are the x, y and z coordinates of
    the grid in millimetres, and `values` is the field at each point (NaN where
    there is none), indexed the same way."""

    def __init__(self, axes, values):
        self.axes = axes
        self.values = values


def read_fld(path: str) -> FieldGrid:
    """The values of one exported field file, on the grid they were taken on.

    Coordinates are converted from the file's metres to millimetres, and the
    grid is rebuilt from the rows themselves, so the order the rows come in
    does not matter. Raises ValueError for a file with no values, or whose
    points do not fill a grid exactly once each.
    """
    import numpy as np

    coords, values = [], []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            parts = line.split()
            if len(parts) != 4:
                continue  # the two header lines, and anything blank
            try:
                x, y, z, value = (float(part) for part in parts)
            except ValueError:
                continue
            coords.append((x, y, z))
            values.append(value)
    name = os.path.basename(path)
    if not values:
        raise ValueError(f"{name} has no field values in it.")
    points = np.round(np.asarray(coords, dtype=float) * 1000.0, 6)
    axes = [np.unique(points[:, axis]) for axis in range(3)]
    shape = tuple(len(axis) for axis in axes)
    index = tuple(np.searchsorted(axes[axis], points[:, axis]) for axis in range(3))
    seen = np.zeros(shape, dtype=bool)
    seen[index] = True
    if int(np.prod(shape)) != len(values) or int(seen.sum()) != len(values):
        raise ValueError(f"{name}: its {len(values)} points do not fill a grid of {shape} once each.")
    grid = np.full(shape, np.nan)
    grid[index] = np.asarray(values, dtype=float)
    return FieldGrid(axes, grid)


def _picture(grid: FieldGrid):
    """The 2-D values to draw, the two axes they run across and up, and the extent.

    A plane is drawn as it is. A volume is drawn as its largest value along y,
    the vertical, which shows where the field is strongest in each x-z spot.
    """
    import numpy as np

    varying = [axis for axis, count in enumerate(grid.values.shape) if count > 1]
    if len(varying) < 2:
        raise ValueError("A grid with fewer than two varying axes has no picture to draw.")
    if len(varying) == 3:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)  # an all-NaN column is simply no field
            data = np.nanmax(grid.values, axis=1)
        across, up = 0, 2
    else:
        across, up = varying
        squeeze = tuple(axis for axis in range(3) if axis not in varying)
        data = np.squeeze(grid.values, axis=squeeze)
    extent = [float(grid.axes[across][0]), float(grid.axes[across][-1]),
              float(grid.axes[up][0]), float(grid.axes[up][-1])]
    return data.T, across, up, extent


def draw_png(grid: FieldGrid, png_path: str, title: str, quantity: str) -> None:
    """Write one export as a PNG: the field on a log10 colour scale and on a
    linear one, side by side, with a colour bar each and grey where there is
    no field. Drawn with the Agg canvas, so no window is opened."""
    import numpy as np
    from matplotlib import colormaps
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    symbol, unit = QUANTITY_NAMES[quantity]
    data, across, up, extent = _picture(grid)
    linear = np.ma.masked_invalid(data)
    logarithmic = np.ma.log10(np.ma.masked_less_equal(linear, 0.0))
    cmap = colormaps["viridis"].with_extremes(bad="#bdbdbd")  # grey where there is no field

    figure = Figure(figsize=(11.5, 4.8), dpi=100, layout="constrained")
    FigureCanvasAgg(figure)
    left, right = figure.subplots(1, 2)
    for ax, values, label in ((left, logarithmic, f"log10 {symbol} ({unit})"),
                              (right, linear, f"{symbol} ({unit})")):
        image = ax.imshow(values, origin="lower", extent=extent, cmap=cmap,
                          aspect="equal", interpolation="nearest")
        figure.colorbar(image, ax=ax, label=label, shrink=0.9)
        ax.set_xlabel(f"{AXES[across]} (mm)")
        ax.set_ylabel(f"{AXES[up]} (mm)")
    left.set_title("log10 colour scale")
    right.set_title("linear colour scale")
    figure.suptitle(title)
    figure.savefig(png_path, format="png")


def outcome_of(combo_dir: str) -> Dict[str, object]:
    """What the worker said about one combination's exports, from its result.json.

    `problem` is None when the worker answered, and otherwise says why it did
    not (no result, not a success, or a result from before field exports).
    `exports` has one record per export the worker reported: its name, its
    quantity, whether it came out and where its file is, and the worker's error
    and AEDT messages when it did not.
    """
    try:
        with open(os.path.join(combo_dir, RESULT_NAME), encoding="utf-8") as handle:
            result = json.load(handle)
    except (OSError, ValueError):
        return {"problem": "the worker left no readable result.json for this combination", "exports": []}
    if not isinstance(result, dict) or result.get("status") != "success":
        status = result.get("status") if isinstance(result, dict) else None
        return {"problem": f"the worker did not report success (status {status!r})", "exports": []}
    block = result.get("field_exports")
    if not isinstance(block, dict):
        return {"problem": "the worker's result has no field_exports -- it may be older than version 0.2.0",
                "exports": []}
    exports = []
    for name, entry in block.items():
        entry = entry if isinstance(entry, dict) else {}
        ok = entry.get("success") is True
        file = entry.get("file") if ok else None
        error = None if ok else (entry.get("error") or "the worker did not say why")
        if ok and not os.path.isfile(os.path.join(combo_dir, file or "")):
            ok, error = False, "the worker reported the file, but it is not there"
        exports.append({
            "name": name, "quantity": quantity_of(name), "ok": ok, "file": file,
            "points": entry.get("points"), "error": error,
            "aedt_messages": list(entry.get("aedt_messages") or []),
        })
    return {"problem": None, "exports": exports}


def record_outcome(run, combo_id: str, combo_dir: str) -> None:
    """Note on a combination what the worker said about its exports.

    The combination is done once the worker has answered, even when some or
    all of its exports failed: those failures are on the run page. It is
    failed only when there is no usable answer at all.
    """
    outcome = outcome_of(combo_dir)
    if outcome["problem"]:
        run.update_combo(combo_id, post_status="failed", post_error=outcome["problem"])
    else:
        run.update_combo(combo_id, post_status="done", post_error=None)


def _title(frequency, name: str, quantity: str) -> str:
    symbol, _unit = QUANTITY_NAMES[quantity]
    suffix = " (largest value along y)" if name.startswith("vacuum_volume") else ""
    return f"{symbol} at {frequency} GHz: {place_of(name)}{suffix}"


def draw_run(run, log: Callable[[str], None] = logger.info) -> Dict[str, object]:
    """Draw the picture of every export that came back, for every combination
    of the part, and write `field_images/summary.json` for the run page.

    Returns how many pictures were drawn, and the first error there was (for
    the part's own state), or None when there was none.
    """
    try:
        import matplotlib  # noqa: F401 -- checked first, so a missing install says so plainly
    except ImportError:
        message = "matplotlib is not installed on this machine, so the field images cannot be drawn."
        log(message)
        return {"drawn": 0, "first_error": message}

    frequency = run.analysis_settings["field_frequency_ghz"]
    folder = images_dir(run.run_dir)
    os.makedirs(folder, exist_ok=True)
    combos: Dict[str, dict] = {}
    drawn = 0
    first_error: Optional[str] = None
    for combo_id, combo in run.state["combos"].items():
        if combo.get("skip_reason"):
            continue
        if combo.get("post_status") == "done":
            outcome = outcome_of(run.combo_path(combo_id))
            problem, exports = outcome["problem"], outcome["exports"]
        else:
            problem = combo.get("post_error") or "the worker did not finish this combination"
            exports = []
        for export in exports:
            export["place"] = place_of(export["name"])
            if export["ok"] and export["quantity"] is None:
                export["ok"], export["error"] = False, "its name does not say whether it is E or H"
            if export["ok"]:
                png = f"{combo_id}__{export['name']}.png"
                try:
                    grid = read_fld(os.path.join(run.combo_path(combo_id), export["file"]))
                    draw_png(grid, os.path.join(folder, png),
                             _title(frequency, export["name"], export["quantity"]), export["quantity"])
                    export["image"] = png
                    drawn += 1
                except (OSError, ValueError) as exc:
                    export["ok"], export["error"] = False, f"could not read or draw it: {exc}"
                    log(f"{combo_id}: {export['name']} could not be drawn ({exc}).")
            if not export["ok"] and first_error is None:
                first_error = export["error"]
        if problem and first_error is None:
            first_error = problem
        combos[combo_id] = {"problem": problem, "exports": exports}

    summary = {"frequency_ghz": frequency, "quantities": run.analysis_settings.get("field_quantities", []),
               "combos": combos}
    with open(os.path.join(folder, SUMMARY_NAME), "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, default=str)
    log(f"Field images: {drawn} picture(s) drawn for {len(combos)} combination(s).")
    return {"drawn": drawn, "first_error": first_error if drawn == 0 else None}


def verdict(run_dir: str) -> str:
    """A field part's verdict in the words of workflow/solver_status.py:
    "failed" when none of its exports came out, "warning" when some did not,
    "ok" when all did, and "unknown" before its pictures are drawn."""
    summary = read_summary(run_dir)
    if summary is None:
        return "unknown"
    ok = failed = 0
    for combo in summary.get("combos", {}).values():
        if combo.get("problem"):
            failed += 1
        for export in combo.get("exports", []):
            if export.get("ok") and export.get("image"):
                ok += 1
            else:
                failed += 1
    if failed and not ok:
        return "failed"
    if failed:
        return "warning"
    return "ok" if ok else "unknown"


def part_exports(summary: Optional[dict]) -> List[dict]:
    """The combinations of a summary as the page lists them, in the order drawn."""
    if not summary:
        return []
    return [{"combo_id": combo_id, **combo} for combo_id, combo in summary.get("combos", {}).items()]
