"""
Make a standalone run from a finished TV0 run and a test-line copper.txt, so the variant can be
refined and solved like any run, without rebuilding the box and the lid.

    python make_variant_parent.py <parent run folder> <variant copper.txt> --label "TV3: ..." [--route-width W]

The parent is a finished bare-board run whose copper is the straight reference: TV0 for TV1 and TV2,
and TV0b (the same board with route_width 2.7) for TV3 to TV5. The script checks that the variant and
the parent agree outside the test section, so the parent is the variant's differential reference.

What it writes (a new folder under the runs root, never over an existing one):

    run_<YYYYmmdd_HHMMSS>/
        run_config.json      the TV0 run's config, with the label; no part_of, part_role or refines_run_id
        state.json           the finished run's structure: geometry done (step and Altium), the AEDT
                             stages left to run, and no results, so nothing claims a solve that did not happen
        GEOMETRY_ONLY.txt    says the same in words
        models/default/      copper.txt (the variant), bonds.txt, dimensions.txt, the box STEP, the lid STEP
                             (the lid only when the channel the variant needs is TV0's), and variant.json

The lid. TV0's lid has one straight relief channel, route_width wide (1.2 mm), over the trace. The
lid is perfect-E, so copper outside that channel is shorted to it. The script measures how far the
variant's signal copper reaches from the centre line and refuses to copy TV0's lid for a variant
that leaves the channel. With --route-width W it writes W into dimensions.txt and run_config.json,
drops the copied lid so the pipeline rebuilds it from those equations, and refuses if the copper
still does not fit inside W. Nothing here runs AEDT, Altium, SolidWorks or the web app.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import sys
from typing import Dict, List, Optional, Sequence

from microwave_package.design import equations
from microwave_package.design import test_lines
from microwave_package.workflow import layout_image

CHANNEL_MARGIN_MM = 0.15  # the lid's wall must stay this far from the CPW footprint (test_lines.POCKET_MARGIN)
GEOMETRY_FILES = ("copper.txt", "bonds.txt", "dimensions.txt")
STEP_FILES = {"box": "default_box.STEP", "lid_inside": "default_lid_inside.STEP"}
DROP_KEYS = ("part_of", "part_role", "refines_run_id")


class VariantRefused(ValueError):
    """The variant cannot be made as asked: its copper does not fit the lid's channel."""


def signal_reach(copper_path: str) -> float:
    """How far the signal copper reaches from the centre line: the largest |y| of any signal track
    (half its width included) or region, found by the renderer's own connectivity."""
    layout = layout_image.read_layout(copper_path)
    reach = 0.0
    for track in layout.signal_tracks:
        reach = max(reach, max(abs(track.y1), abs(track.y2)) + track.width / 2.0)
    for region in layout.signal_regions:
        reach = max(reach, max(abs(y) for _, y in region))
    for x, y in layout.route:
        reach = max(reach, abs(y))
    return reach


def _route_width_of(dims_text: str) -> float:
    values = equations.get_numeric_parameters(dims_text)
    return float(values.get("route_width", 1.2))


def _new_run_id(runs_root: str) -> str:
    base = _dt.datetime.now().strftime("run_%Y%m%d_%H%M%S")
    candidate, n = base, 1
    while os.path.exists(os.path.join(runs_root, candidate)):
        n += 1
        candidate = f"{base}_{n}"
    return candidate


def make_parent(source_run: str, copper_path: str, runs_root: str, label: str,
                route_width: Optional[float] = None) -> str:
    """Write the standalone run and return its folder. Raises VariantRefused, and writes nothing,
    when the variant does not fit the channel."""
    source_models = os.path.join(source_run, "models", "default")
    for name in GEOMETRY_FILES[2:]:
        if not os.path.isfile(os.path.join(source_models, name)):
            raise FileNotFoundError(f"{source_run}: no models/default/{name}; give a finished TV0 run")
    with open(os.path.join(source_models, "dimensions.txt"), encoding="utf-8") as f:
        dims_text = f.read()
    tv0_width = _route_width_of(dims_text)
    width = tv0_width if route_width is None else float(route_width)
    with open(copper_path, encoding="utf-8") as f:
        variant_text = f.read()
    with open(os.path.join(source_models, "copper.txt"), encoding="utf-8") as f:
        parent_text = f.read()
    if test_lines.section_free_lines(variant_text) != test_lines.section_free_lines(parent_text):
        raise VariantRefused(
            "the parent's copper differs from the variant's outside the test section, so the parent is not "
            "its differential reference (TV1 and TV2 need TV0; TV3 to TV5 need TV0b, the same board at route_width 2.7)."
        )
    reach = signal_reach(copper_path) + test_lines.GAP  # the CPW footprint: trace, both gaps
    half_channel = width / 2.0
    if reach > half_channel - CHANNEL_MARGIN_MM:
        raise VariantRefused(
            f"the variant's signal reaches {reach:.2f} mm from the centre line, but the lid's relief "
            f"channel (route_width {width:g} mm) is only {half_channel:.2f} mm each side. The lid is "
            f"perfect-E, so the copper would short to it. Pass --route-width with a channel that holds "
            f"{reach + CHANNEL_MARGIN_MM:.2f} mm each side ({2 * (reach + CHANNEL_MARGIN_MM):.1f} mm in all)."
        )
    rewidth = route_width is not None and abs(width - tv0_width) > 1e-12

    with open(os.path.join(source_run, "run_config.json"), encoding="utf-8") as f:
        config = json.load(f)
    for key in DROP_KEYS:
        config.pop(key, None)
    config["label"] = label
    config["created_at"] = _dt.datetime.now().isoformat(timespec="seconds")
    config.setdefault("parameter_defaults", {})
    if rewidth:
        config["parameter_defaults"]["route_width"] = width

    os.makedirs(runs_root, exist_ok=True)
    run_id = _new_run_id(runs_root)
    run_dir = os.path.join(runs_root, run_id)
    models = os.path.join(run_dir, "models", "default")
    os.makedirs(models)
    os.makedirs(os.path.join(run_dir, "plots"))

    shutil.copyfile(copper_path, os.path.join(models, "copper.txt"))
    shutil.copyfile(os.path.join(source_models, "bonds.txt"), os.path.join(models, "bonds.txt"))
    dims = dims_text
    if rewidth:
        dims = equations.set_parameter(dims_text, "route_width", width)
    with open(os.path.join(models, "dimensions.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write(dims)
    sidecar = os.path.join(os.path.dirname(os.path.abspath(copper_path)), "variant.json")
    if os.path.isfile(sidecar):
        shutil.copyfile(sidecar, os.path.join(models, "variant.json"))

    step_paths: Dict[str, str] = {}
    step_status = "pending"
    if not rewidth:
        # TV0's box and lid are right for this variant: copy them and say the geometry is done
        step_status = "done"
        for key, name in STEP_FILES.items():
            src = os.path.join(source_models, name)
            if not os.path.isfile(src):
                step_status = "pending"
                step_paths = {}
                break
            shutil.copyfile(src, os.path.join(models, name))
            step_paths[key] = os.path.join(models, name)
    else:
        shutil.copyfile(os.path.join(source_models, STEP_FILES["box"]),
                        os.path.join(models, STEP_FILES["box"]))

    with open(os.path.join(run_dir, "run_config.json"), "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    state = {
        "stage": "complete",
        "combos": {
            "default": {
                "params": {},
                "step_status": step_status,
                "step_paths": step_paths,
                "step_error": None,
                "skip_reason": None,
                "task_id": None,
                "queue_status": None,
                "post_status": None,
                "post_error": None,
                "altium_status": "done",
                "altium_error": None,
                "model_status": "pending",
                "project_path": None,
                "object_names": None,
                "ports": None,
                "handoff_status": None,
                "fetch_status": None,
                "analysis_dir": models,
            }
        },
    }
    with open(os.path.join(run_dir, "state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    with open(os.path.join(run_dir, "GEOMETRY_ONLY.txt"), "w", encoding="utf-8") as f:
        f.write("Geometry from a test line (copper.txt), copied box and lid from TV0 where the channel allows it.\n"
                "No AEDT model and no results: the solve is still to be run.\n")
    return run_dir


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Make a standalone run from a finished TV0 run and a variant copper.txt.")
    parser.add_argument("parent", help="the parent run folder: TV0, or TV0b for the 2.7 mm pocket")
    parser.add_argument("copper", help="the variant's copper.txt")
    parser.add_argument("--label", required=True, help="the run's label, as the web app shows it")
    parser.add_argument("--route-width", type=float, help="widen the lid's relief channel to this width (mm)")
    parser.add_argument("--runs-root", help="where the run folder goes (default: the repo's runs folder)")
    args = parser.parse_args(argv)
    if args.runs_root:
        runs_root = args.runs_root
    else:
        from microwave_package import paths

        runs_root = str(paths.RUNS_ROOT)
    try:
        run_dir = make_parent(args.parent, args.copper, runs_root, args.label, args.route_width)
    except VariantRefused as exc:
        print(f"make_variant_parent: refused: {exc}", file=sys.stderr)
        return 2
    print(run_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
