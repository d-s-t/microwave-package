"""
Generic, reusable post-processing operations run against an already-
analyzed project, driven by a task's `post_processing` list, e.g.:

    "post_processing": [
        {"op": "field_plot_surface", "params": {
            "object_key": "chip", "quantity": "Mag_E"
        }},
        {"op": "field_plot_volume", "params": {
            "object_key": "vacuum", "quantity": "Mag_E"
        }}
    ]

These used to run inside the worker, right after it solved the setup.
They've moved here because they're pure read/export operations against
solution data that's already on disk -- the student version can open a
solved project and pull field plots out of it just fine, it just can't
run the analysis itself (see docs/ARCHITECTURE.md in ansys-analyze-worker,
section 12). Keeping them client-side also means the worker never has to
do two things (analyze, then post-process) back to back -- it just runs
the setup and hands the project back.

Each op is a function `(hfss, task, mode_context, params, out_dir) -> dict`
that returns whatever it produced (paths, extracted values, ...); the
caller merges that dict into the result row for that mode.

To add a new op: write a function here with that signature and register
it in POST_PROCESSING_OPS. postprocess.py automatically gets access to it
via the task file -- no other code changes needed.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional, Tuple


def _object_name(task: Dict[str, Any], object_key: str):
    return task.get("objects", {}).get(object_key)


def _resolve_object(hfss, task: Dict[str, Any], object_key: str):
    """The Object3d for an object key, or None.

    `hfss.modeler[name]` is a fuzzy lookup that falls through to querying
    groups, which is where the "Failed to execute gRPC AEDT command:
    GetObjectsInGroup" failures came from. `get_object_from_name` asks
    for the object directly.
    """
    obj_name = _object_name(task, object_key)
    if not obj_name:
        return None
    try:
        return hfss.modeler.get_object_from_name(obj_name)
    except Exception:
        return None


def _top_face(obj):
    """The large face at the far end of the body's thinnest axis.

    For a slab like the chip the two biggest faces are its top and
    bottom; which axis separates them depends on how the part is
    oriented, so it is taken from the bounding box (the thinnest
    direction) rather than assumed to be z.
    """
    faces = sorted(obj.faces, key=lambda f: f.area, reverse=True)[:2]
    try:
        box = obj.bounding_box  # [xmin, ymin, zmin, xmax, ymax, zmax]
        extents = [box[3] - box[0], box[4] - box[1], box[5] - box[2]]
        axis = extents.index(min(extents))
    except Exception:
        axis = 2
    return max(faces, key=lambda f: f.center[axis])


def _solution_of(mode_context) -> str:
    return f"{mode_context['setup_name']} : LastAdaptive"


def _intrinsics_of(mode_context) -> Dict[str, str]:
    return {"Mode": str(mode_context["mode"]), "Phase": "0deg"}


def read_field_points(path: str) -> List[Tuple[List[float], float]]:
    """The sample points out of an exported .fld file, as (xyz, value).

    The format is a header line followed by one row per sample point --
    the coordinates first and the field value last. Anything that does
    not parse as a row of numbers (headers, blank lines, trailing notes)
    is skipped rather than trusted to be absent.
    """
    points: List[Tuple[List[float], float]] = []
    with open(path, "r", errors="replace") as handle:
        for line in handle:
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                row = [float(p) for p in parts]
            except ValueError:
                continue  # header or comment
            points.append((row[:-1], row[-1]))
    return points


def thin_axis(points: List[Tuple[List[float], float]]) -> int:
    """Which axis the sampled body is thinnest along.

    The chip is a thin slab, so its smallest extent is its thickness
    direction and the "top" face is the one at the far end of that axis.
    Working it out from the exported points means this follows the part's
    orientation instead of assuming the thickness runs along z.
    """
    axes = range(min(3, min(len(coords) for coords, _ in points)))
    extents = [
        max(coords[axis] for coords, _ in points) - min(coords[axis] for coords, _ in points)
        for axis in axes
    ]
    return extents.index(min(extents))


def top_surface_values(points: List[Tuple[List[float], float]],
                       axis: Optional[int] = None,
                       tolerance: float = 0.02) -> List[float]:
    """Values on the outermost layer of samples along the thin axis.

    `tolerance` is a fraction of the body's thickness, so it scales with
    the part rather than assuming millimetres: samples within that much
    of the highest coordinate count as being on the top face.
    """
    if not points:
        return []
    if axis is None:
        axis = thin_axis(points)

    coordinates = [coords[axis] for coords, _ in points]
    highest, lowest = max(coordinates), min(coordinates)
    band = (highest - lowest) * tolerance
    return [value for coords, value in points if coords[axis] >= highest - band]


def op_field_statistics(hfss, task, mode_context, params, out_dir) -> Dict[str, Any]:
    """Max and mean of a field quantity over one object.

    The field is exported on the object itself (no sampling grid) and the
    statistics are computed from that file, because AEDT's own export of
    a *plot* only writes its native formats -- there is no CSV of plotted
    values to read back.

    `region` picks what the statistics cover: "volume" for the whole
    body, or "top_surface" for just the outermost layer of samples along
    the body's thin axis -- which for the chip is the face the circuit is
    on. Either way the export is of the volume; the top face is selected
    from the sample coordinates afterwards, so it follows the part's
    orientation rather than assuming which axis points up.

    `key` names the outputs, e.g. key="chip_mag_E" emits chip_mag_E_max
    and chip_mag_E_mean.
    """
    object_key = params["object_key"]
    quantity = params.get("quantity", "Mag_E")
    region = params.get("region", "volume")
    key = params.get("key", f"{object_key}_{quantity.lower()}")
    obj_name = _object_name(task, object_key)
    if not obj_name:
        return {"skipped": True, "reason": f"object '{object_key}' not found"}

    field_path = os.path.join(out_dir, f"{key}.fld")
    # post.export_field_file, not fields_calculator.export: the latter maps
    # the field onto a grid of points and needs a grid to be described, and
    # without one it returns without writing anything -- which is why this
    # used to report "no field file exported". This one writes the field at
    # the body's own mesh nodes, each with its coordinates.
    hfss.post.export_field_file(
        quantity,
        solution=_solution_of(mode_context),
        output_file=field_path,
        assignment=obj_name,
        objects_type=params.get("objects_type", "Vol"),
        intrinsics=None,
    )

    if not os.path.exists(field_path):
        return {"skipped": True, "reason": f"no field file exported for '{object_key}'"}

    points = read_field_points(field_path)
    if not points:
        return {"skipped": True, "reason": f"no field values in {os.path.basename(field_path)}"}

    values = top_surface_values(points) if region == "top_surface" else [v for _, v in points]
    if not values:
        return {"skipped": True, "reason": f"no {region} samples for '{object_key}'"}

    return {
        f"{key}_max": max(values),
        f"{key}_mean": sum(values) / len(values),
        f"{key}_samples": len(values),
    }


def op_field_plot_surface(hfss, task, mode_context, params, out_dir) -> Dict[str, Any]:
    object_key = params["object_key"]
    quantity = params.get("quantity", "Mag_E")
    obj = _resolve_object(hfss, task, object_key)
    if obj is None:
        return {"skipped": True, "reason": f"object '{object_key}' not found"}

    face = _top_face(obj) if params.get("face", "top") == "top" else obj.faces[0]

    setup_name = mode_context["setup_name"]
    mode_idx = mode_context["mode"]
    plot_name = f"{setup_name}_Mode_{mode_idx}_{object_key}_surface_{quantity}"

    plot = hfss.post.create_fieldplot_surface(
        [face.id],
        quantity,
        setup=_solution_of(mode_context),
        plot_name=plot_name,
        intrinsics=_intrinsics_of(mode_context),
    )

    img_path = os.path.join(out_dir, f"{object_key}_surface_{quantity}.jpg")
    hfss.post.export_field_jpg(img_path, plot_name, getattr(plot, "plot_folder", "Field_Plot"))

    return {"image_path": img_path}


def op_field_plot_volume(hfss, task, mode_context, params, out_dir) -> Dict[str, Any]:
    object_key = params["object_key"]
    quantity = params.get("quantity", "Mag_E")
    # the volume plot takes object names, so there is no need to look the
    # object itself up in the modeler
    obj_name = _object_name(task, object_key)
    if not obj_name:
        return {"skipped": True, "reason": f"object '{object_key}' not found"}

    setup_name = mode_context["setup_name"]
    mode_idx = mode_context["mode"]
    plot_name = f"{setup_name}_Mode_{mode_idx}_{object_key}_volume_{quantity}"

    plot = hfss.post.create_fieldplot_volume(
        [obj_name],
        quantity,
        setup=_solution_of(mode_context),
        plot_name=plot_name,
        intrinsics=_intrinsics_of(mode_context),
    )

    img_path = os.path.join(out_dir, f"{object_key}_volume_{quantity}.jpg")
    hfss.post.export_field_jpg(img_path, plot_name, getattr(plot, "plot_folder", "Field_Plot"))

    return {"image_path": img_path}


POST_PROCESSING_OPS: Dict[str, Callable[..., Dict[str, Any]]] = {
    "field_plot_surface": op_field_plot_surface,
    "field_plot_volume": op_field_plot_volume,
    "field_statistics": op_field_statistics,
}


def run_post_processing(hfss, task: Dict[str, Any], mode_context: Dict[str, Any], out_dir: str) -> Dict[str, Any]:
    """Run every op listed in task['post_processing'] for a single mode."""
    combined: Dict[str, Any] = {}
    for step in task.get("post_processing", []):
        op_name = step.get("op")
        op_fn = POST_PROCESSING_OPS.get(op_name)
        if op_fn is None:
            combined.setdefault("post_processing_errors", []).append(
                f"Unknown post_processing op '{op_name}'"
            )
            continue
        try:
            result = op_fn(hfss, task, mode_context, step.get("params", {}), out_dir)
            combined.update(result or {})
        except Exception as exc:  # keep going so one bad op doesn't kill the rest
            combined.setdefault("post_processing_errors", []).append(f"{op_name} failed: {exc}")
    return combined


# --- showing what was exported ----------------------------------------
#
# The heatmap lives here rather than with the run's other plots because
# it is an eigenmode picture end to end: it draws the .fld files the ops
# above export, per mode, and no other solution type produces anything
# it could draw. The web UI reaches in for this one view.

def _interpolate(points, axis: int, resolution: int = 120):
    """Turn scattered mesh-node samples into a grid that can be drawn as
    a heatmap, over the two axes that are not the body's thin one."""
    import numpy as np
    from scipy.interpolate import griddata

    across = [a for a in range(3) if a != axis]
    # coordinates come out of AEDT in metres; millimetres read better
    horizontal = np.array([c[across[0]] for c, _ in points]) * 1000.0
    vertical = np.array([c[across[1]] for c, _ in points]) * 1000.0
    values = np.array([v for _, v in points])

    x = np.linspace(horizontal.min(), horizontal.max(), resolution)
    y = np.linspace(vertical.min(), vertical.max(), resolution)
    grid_x, grid_y = np.meshgrid(x, y)
    grid_z = griddata((horizontal, vertical), values, (grid_x, grid_y), method="linear")
    return x, y, grid_z, across


# How many chips the "all variants" grid draws. Every extra panel is a
# full interpolated surface, so a few hundred of them is tens of
# megabytes and several seconds -- and unreadable besides.
MAX_HEATMAP_PANELS = 12


def chip_heatmap_html(
    entries: List[dict],
    dark: bool = False,
    plotlyjs_src: Optional[str] = None,
    columns: int = 3,
    note: Optional[str] = None,
) -> str:
    """Chip top-face |E| as a heatmap, for one variant or all of them.

    Every panel is drawn against one shared colour axis, so the grid can
    be read across variants -- a per-panel scale would make the weakest
    and strongest chips look identical.
    """
    from plotly.subplots import make_subplots
    import plotly.graph_objects as go

    usable = [e for e in entries if e.get("points")]
    if not usable:
        return "<p>No chip field data in this run yet.</p>"

    lowest = min(min(v for _, v in e["points"]) for e in usable)
    highest = max(max(v for _, v in e["points"]) for e in usable)

    count = len(usable)
    cols = 1 if count == 1 else min(columns, count)
    rows = (count + cols - 1) // cols

    # Plotly caps the gap between panels at 1/(rows-1), so a sweep with
    # hundreds of combinations would otherwise fail outright rather than
    # draw anything. Shrink the gap to fit, and give each row a fixed
    # height so a tall grid stays readable instead of being squashed.
    spacing = min(0.12, 0.8 / (rows - 1)) if rows > 1 else 0.0

    figure = make_subplots(
        rows=rows, cols=cols,
        subplot_titles=[e["label"] for e in usable],
        horizontal_spacing=0.08, vertical_spacing=spacing,
    )

    axis_names = None
    for index, entry in enumerate(usable):
        row, col = index // cols + 1, index % cols + 1
        x, y, z, across = _interpolate(entry["points"], entry["axis"])
        axis_names = ("xyz"[across[0]], "xyz"[across[1]])
        figure.add_trace(
            go.Heatmap(x=x, y=y, z=z, coloraxis="coloraxis",
                       hovertemplate="%{x:.2f}, %{y:.2f} mm<br>|E| = %{z:.4g}<extra></extra>"),
            row=row, col=col,
        )
        figure.update_xaxes(title_text=f"{axis_names[0]} (mm)", row=row, col=col,
                            constrain="domain")
        figure.update_yaxes(title_text=f"{axis_names[1]} (mm)", row=row, col=col,
                            scaleanchor=f"x{'' if index == 0 else index + 1}", scaleratio=1)

    figure.update_layout(
        coloraxis=dict(
            cmin=lowest, cmax=highest, colorscale="Turbo",
            colorbar=dict(title="|E| (V/m)"),
        ),
        template="plotly_dark" if dark else "plotly_white",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        height=max(320, 300 * rows),
        margin=dict(l=60, r=30, t=50, b=50),
        showlegend=False,
    )

    if not plotlyjs_src:
        return figure.to_html(include_plotlyjs=True, full_html=True)
    fragment = figure.to_html(include_plotlyjs=False, full_html=False)
    scheme = "dark" if dark else "light"
    banner = (
        f"<p style='margin:.4rem .6rem;font:13px system-ui;opacity:.7'>{note}</p>"
        if note else ""
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<script src='{plotlyjs_src}'></script>"
        "<style>html,body{margin:0;padding:0;background:transparent;"
        f"color-scheme:{scheme}}}</style>"
        f"</head><body>{banner}{fragment}</body></html>"
    )
