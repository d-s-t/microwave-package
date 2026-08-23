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
from typing import Any, Callable, Dict, List


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
    faces = sorted(obj.faces, key=lambda f: f.area, reverse=True)
    return max(faces[:2], key=lambda f: f.center[2])


def _solution_of(mode_context) -> str:
    return f"{mode_context['setup_name']} : LastAdaptive"


def _intrinsics_of(mode_context) -> Dict[str, str]:
    return {"Mode": str(mode_context["mode"]), "Phase": "0deg"}


def read_field_file(path: str) -> List[float]:
    """The field values out of an exported .fld file.

    The format is a header line followed by one row per sample point --
    the coordinates first and the field value last. Anything that does
    not parse as a row of numbers (headers, blank lines, trailing notes)
    is skipped rather than trusted to be absent.
    """
    values: List[float] = []
    with open(path, "r", errors="replace") as handle:
        for line in handle:
            parts = line.split()
            if not parts:
                continue
            try:
                row = [float(p) for p in parts]
            except ValueError:
                continue  # header or comment
            if row:
                values.append(row[-1])
    return values


def op_field_statistics(hfss, task, mode_context, params, out_dir) -> Dict[str, Any]:
    """Max and mean of a field quantity over one object.

    The field is exported on the object itself (no sampling grid) and the
    statistics are computed from that file, because AEDT's own export of
    a *plot* only writes its native formats -- there is no CSV of plotted
    values to read back.

    Emits `max_<quantity>` / `mean_<quantity>`, e.g. max_mag_e. Use
    `key_prefix` if you run this for more than one object, so the second
    does not overwrite the first.
    """
    object_key = params["object_key"]
    quantity = params.get("quantity", "Mag_E")
    prefix = params.get("key_prefix", "")
    obj_name = _object_name(task, object_key)
    if not obj_name:
        return {"skipped": True, "reason": f"object '{object_key}' not found"}

    field_path = os.path.join(out_dir, f"{object_key}_{quantity.lower()}.fld")
    hfss.post.fields_calculator.export(
        quantity=quantity,
        solution=_solution_of(mode_context),
        output_file=field_path,
        intrinsics=_intrinsics_of(mode_context),
        assignment=obj_name,
        objects_type=params.get("objects_type", "Vol"),
    )

    if not os.path.exists(field_path):
        return {"skipped": True, "reason": f"no field file exported for '{object_key}'"}

    values = read_field_file(field_path)
    if not values:
        return {"skipped": True, "reason": f"no field values in {os.path.basename(field_path)}"}

    return {
        f"{prefix}max_{quantity.lower()}": max(values),
        f"{prefix}mean_{quantity.lower()}": sum(values) / len(values),
        f"{prefix}field_path": field_path,
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
