"""
Part 1 -- Build the Ansys model + boundary conditions for the pedestal
eigenmode experiment.

Runs on the STUDENT version of Ansys Electronics Desktop. This script does
the geometry/material/boundary work only -- it never creates an analysis
setup and never calls Analyze, so it never touches the part of AEDT that
needs the full license.

For each STEP file it finds, it:
  1. Opens a new HFSS (Eigenmode) design and imports the STEP file.
  2. Assigns materials and boundary conditions (same logic as the
     original monolithic script).
  3. Saves and closes the .aedt project.
  4. Copies the whole project bundle (.aedt file + .aedb folder) into
     ANSYS_ANALYZE_QUEUE_PATH/projects/<task_id>/ -- Part 2 usually runs
     on a *different machine*, which cannot see this script's local
     output folder, so the project has to actually travel into the
     shared queue rather than just being pointed at.
  5. Writes a task JSON file into the queue's pending/ folder (paths
     inside it are relative to the queue root) for Part 2 to pick up and
     actually run the eigenmode analysis.
  6. Leaves a small queue_task.json marker in the local run folder so you
     can trace it back to the queued task_id later (see
     queue_common.write_local_marker and Part 3's --check_status).

See docs/ARCHITECTURE.md (in the ansys-analyze-worker repo), section 8
("Authoring a Part 1 script"), for the full guide this script is meant
to be a template for.

Requires `ansys-analyze-common` to be installed (`pip install
"git+https://github.com/d-s-t/ansys-analyze-worker.git#subdirectory=common"`,
or from requirements.txt in this repo) -- see that repo's README.
"""
from __future__ import annotations

import argparse
import glob
import os
import re
from typing import List, Optional

from ansys_analyze_common import queue_common

from ansys.aedt.core import Hfss  # noqa: E402


def get_installed_versions() -> List[str]:
    """
    Detect installed AEDT versions and return them as clean "YYYY.R"
    strings (e.g. "2025.2"), newest first.

    This deliberately avoids passing PyAEDT's own auto-detected version
    string straight back into itself: when only the Student version is
    installed, that string comes back as "2025.2SV" (the "SV" suffix
    identifies Student Version), and PyAEDT's own version-string parser
    then rejects that as invalid ("Version argument is not valid.").
    `student_version=True` is already how you tell PyAEDT you want the
    Student edition -- the version string passed alongside it must be the
    plain "2025.2" form, which is what this function produces.

    Two detection strategies, tried in order:
      1. ANSYSEM_ROOT### environment variables (fast, no PyAEDT import
         needed) -- but these aren't always present in every shell/user
         session even when AEDT itself is installed and working fine.
      2. PyAEDT's own internal version registry
         (ansys.aedt.core.internal.aedt_versions), which is how PyAEDT
         found "2025.2SV" in the first place -- we just strip the
         trailing letters (SV, or any other suffix) it doesn't strip
         itself before handing the string back to it.
    """
    versions = []
    for key in os.environ:
        if key.startswith("ANSYSEM_ROOT"):
            v_str = key.replace("ANSYSEM_ROOT", "")
            if len(v_str) == 3:
                versions.append(f"20{v_str[:2]}.{v_str[2]}")
    if versions:
        return sorted(set(versions), reverse=True)

    try:
        from ansys.aedt.core.internal.aedt_versions import aedt_versions as _aedt_versions
        raw_versions = list(_aedt_versions.installed_versions.keys())
    except Exception:
        raw_versions = []

    cleaned = {re.sub(r"[A-Za-z]+$", "", v).strip() for v in raw_versions}
    cleaned = {v for v in cleaned if v}
    return sorted(cleaned, reverse=True)


def calculate_min_freq(dim_file_path: str, safety_factor: float = 0.4, floor_ghz: float = 0.1) -> float:
    """
    Estimate a starting MinimumFrequency for the eigenmode sweep from the
    PCB footprint, then pull it DOWN by `safety_factor` before returning.

    Why the fix is needed: the naive estimate below (half-wavelength
    resonance across the larger PCB dimension) only accounts for the PCB
    itself. The actual enclosure (box/lid/pedestal cavity) is typically
    comparable in size or larger, so its true lowest cavity mode can sit
    below that naive estimate. Starting the eigenmode sweep too high
    silently skips real low-frequency modes -- which is exactly the
    problem being fixed here. `safety_factor` (default 0.4, i.e. start at
    40% of the naive value) gives headroom to catch those; `floor_ghz` is
    a hard lower bound so the first setup never gets an unreasonably tiny
    starting frequency.
    """
    with open(dim_file_path, 'r') as f:
        content = f.read()

    pcb_length_match = re.search(r'"pcb_length"\s*=\s*([0-9.]+)', content)
    pcb_side_match = re.search(r'"pcb_side"\s*=\s*([0-9.]+)', content)

    if not pcb_length_match or not pcb_side_match:
        raise ValueError("Could not find pcb_length or pcb_side in dimensions.txt")

    pcb_length = float(pcb_length_match.group(1))
    pcb_side = float(pcb_side_match.group(1))

    max_dim_m = max(pcb_length, pcb_side) * 1e-3
    c = 299792458
    wave_lambda = 2 * max_dim_m
    naive_min_freq_ghz = (c / wave_lambda) / 1e9

    return max(naive_min_freq_ghz * safety_factor, floor_ghz)


def get_next_run_dir(base_dir: str) -> "tuple[str, int]":
    os.makedirs(base_dir, exist_ok=True)
    existing_runs = glob.glob(os.path.join(base_dir, "run_*"))
    max_run = 0
    for run in existing_runs:
        match = re.search(r'run_(\d+)$', run)
        if match:
            max_run = max(max_run, int(match.group(1)))
    next_run = max_run + 1
    run_dir = os.path.join(base_dir, f"run_{next_run:03d}")
    os.makedirs(run_dir, exist_ok=True)
    return run_dir, next_run


def assign_materials_and_boundaries(hfss, args) -> dict:
    objects_dict = {}
    for obj in hfss.modeler.solid_objects:
        name = obj.name.lower()

        if 'vacuum' in name:
            obj.material_name = args.mat_vacuum
            objects_dict['vacuum'] = obj
        elif 'pcb' in name:
            obj.material_name = args.mat_pcb
            objects_dict['pcb'] = obj
        elif 'chip' in name and 'lid' not in name:
            obj.material_name = args.mat_chip
            objects_dict['chip'] = obj
        elif 'chip_lid' in name:
            obj.material_name = args.mat_box
            objects_dict['chip_lid'] = obj
            if args.perfect_e_box:
                hfss.assign_perfect_e(assignment=obj.name)
        elif 'lid' in name:
            obj.material_name = args.mat_box
            objects_dict['lid'] = obj
            if args.perfect_e_box:
                hfss.assign_perfect_e(assignment=obj.name)
        elif 'box' in name:
            obj.material_name = args.mat_box
            objects_dict['box'] = obj
            if args.perfect_e_box:
                hfss.assign_perfect_e(assignment=obj.name)

    return objects_dict


def build_one(
    args,
    step_file: str,
    run_dir: str,
    run_number: int,
    queue: "queue_common.QueuePaths",
    min_freq_ghz: float,
    aedt_version: Optional[str],
) -> None:
    print(f"\nBuilding model for {step_file}...")
    filename = os.path.basename(step_file)
    depth_match = re.search(r'^([0-9.]+)mm', filename)
    pedestal_depth = float(depth_match.group(1)) if depth_match else 0.0

    base_name = os.path.splitext(filename)[0]
    project_dir = os.path.join(run_dir, base_name)
    os.makedirs(project_dir, exist_ok=True)
    project_path = os.path.join(project_dir, f"{base_name}.aedt")

    hfss = Hfss(
        project=project_path,
        version=aedt_version,
        solution_type="Eigenmode",
        new_desktop=False,
        student_version=args.student_version,
        non_graphical=not args.graphical,
    )

    try:
        # Clear modeler/boundaries in case this project path was reused
        # from a previous failed attempt.
        if hfss.modeler.object_names:
            hfss.modeler.delete(hfss.modeler.object_names)
        for bound in hfss.boundaries:
            try:
                bound.delete()
            except Exception:
                pass

        print(f"Importing {step_file}...")
        hfss.modeler.import_3d_cad(step_file)

        objects_dict = assign_materials_and_boundaries(hfss, args)
        object_names = {key: obj.name for key, obj in objects_dict.items()}

        hfss.save_project(project_path)
        # Close before copying so the .aedb folder isn't mid-write / locked.
        hfss.close_project(name=hfss.project_name)
        hfss = None

        # project_dir may not be reachable from the machine running Part 2
        # (a different PC entirely, in the common case), so the whole
        # project bundle -- .aedt file + .aedb folder -- gets copied into
        # the shared queue itself, under a unique task_id subfolder. The
        # task file then only ever references paths relative to the queue
        # root, which both machines can resolve. See
        # queue_common.copy_project_to_queue() and docs/ARCHITECTURE.md.
        task_id = queue_common.new_task_id("eigenmode_chain")
        relative_project_file = queue_common.copy_project_to_queue(queue, task_id, project_dir)
        relative_output_dir = queue_common.project_dir_for_task(task_id)

        task = queue_common.build_task(
            task_type="eigenmode_chain",
            project_file=relative_project_file,
            output_dir=relative_output_dir,
            task_id=task_id,
            parameters={
                "start_min_freq_ghz": min_freq_ghz,
                "target_max_freq_ghz": args.target_max_freq,
                "modes": args.modes,
                "max_passes": args.max_passes,
                "min_passes": args.min_passes,
                "min_converged": args.min_converged,
                "max_delta_f": args.max_delta_f,
            },
            objects=object_names,
            post_processing=[
                {"op": "field_plot_surface", "params": {"object_key": "chip", "quantity": "Mag_E"}},
                {"op": "field_plot_volume", "params": {"object_key": "vacuum", "quantity": "Mag_E"}},
            ],
            metadata={
                "run": run_number,
                "pedestal_depth": pedestal_depth,
                "source_step_file": os.path.abspath(step_file),
            },
        )
        task_path = queue_common.write_task_atomically(queue, task)

        # So this local folder can be traced back to the queued task later
        # (e.g. by Part 3's --check_status), record the task_id here too.
        queue_common.write_local_marker(
            project_dir,
            task_id,
            queue.root,
            extra={
                "local_project_file": project_path,
                "queued_project_file": relative_project_file,
                "output_dir": relative_output_dir,
            },
        )

        print(f"Queued analysis task {task_id}: {task_path}")

    finally:
        # hfss is set to None above once the project has already been
        # saved and closed ahead of copying it into the queue; only close
        # here if something failed before that point.
        if hfss is not None:
            try:
                hfss.close_project(name=hfss.project_name)
            except Exception as e:
                print(f"Failed to close project gracefully: {e}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build HFSS Eigenmode models + boundaries for the pedestal experiment (Part 1)"
    )
    parser.add_argument("--step_dir", type=str, default="pedestal_experiment")
    parser.add_argument("--out_dir", type=str, default=r"pedestal_experiment\built_models")
    parser.add_argument("--dim_file", type=str, default=r"pedestal_experiment\dimensions.txt")
    parser.add_argument("--target_max_freq", type=float, default=20.0, help="Target maximum frequency in GHz")
    parser.add_argument("--modes", type=int, default=20, help="Number of modes per setup")
    parser.add_argument("--max_passes", type=int, default=99)
    parser.add_argument("--max_delta_f", type=float, default=1.0, help="Maximum Delta Frequency Per Pass (%%)")
    parser.add_argument("--min_passes", type=int, default=5)
    parser.add_argument("--min_converged", type=int, default=3)
    parser.add_argument(
        "--min_freq_safety_factor",
        type=float,
        default=0.4,
        help="Multiply the naive starting-frequency estimate by this factor so the "
             "eigenmode sweep starts low enough to catch all modes (see calculate_min_freq)",
    )
    parser.add_argument("--perfect_e_box", type=bool, default=True)
    parser.add_argument("--mat_box", type=str, default="aluminum")
    parser.add_argument("--mat_pcb", type=str, default="Rogers TMM 10i (tm)")
    parser.add_argument("--mat_chip", type=str, default="silicon")
    parser.add_argument("--mat_vacuum", type=str, default="vacuum")
    parser.add_argument("--graphical", action="store_true", help="Show the AEDT UI while building")
    parser.add_argument(
        "--aedt_version",
        type=str,
        default=None,
        help='Explicit AEDT version, e.g. "2025.2". Defaults to auto-detecting from '
             "ANSYSEM_ROOT### environment variables (see get_installed_versions) -- "
             "set this manually if that detection picks the wrong install or none at all.",
    )
    parser.add_argument(
        "--no_student_version",
        dest="student_version",
        action="store_false",
        default=True,
        help="Use this if you actually have a full license on this machine too",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    args.out_dir = os.path.abspath(args.out_dir)
    args.step_dir = os.path.abspath(args.step_dir)
    args.dim_file = os.path.abspath(args.dim_file)

    queue = queue_common.QueuePaths.from_env("ANSYS_ANALYZE_QUEUE_PATH")

    aedt_version = args.aedt_version
    if not aedt_version:
        detected = get_installed_versions()
        if detected:
            aedt_version = detected[0]
            print(f"Auto-detected AEDT version: {aedt_version}"
                  + (f" (other installs found: {detected[1:]})" if len(detected) > 1 else ""))
        else:
            print("Could not auto-detect an AEDT version from ANSYSEM_ROOT### environment "
                  "variables; letting PyAEDT guess (pass --aedt_version explicitly if this fails).")

    run_dir, run_number = get_next_run_dir(args.out_dir)
    print(f"Building models in {run_dir} (run {run_number})")

    min_freq_ghz = calculate_min_freq(args.dim_file, safety_factor=args.min_freq_safety_factor)
    print(f"Starting min frequency for eigenmode sweep: {min_freq_ghz:.3f} GHz")

    step_files = glob.glob(os.path.join(args.step_dir, "*.STEP")) + glob.glob(os.path.join(args.step_dir, "*.step"))
    # Remove duplicates because Windows file paths are case-insensitive, so both globs return the same files
    step_files = sorted(set(step_files))

    for step_file in step_files:
        try:
            build_one(args, step_file, run_dir, run_number, queue, min_freq_ghz, aedt_version)
        except Exception as e:
            print(f"Failed to build model for {step_file}: {e}")

    print(f"\nDone. Built {len(step_files)} model(s) and queued their analysis tasks in "
          f"{queue.pending}. Part 2 (the background worker) will pick them up from there.")


if __name__ == "__main__":
    main()
