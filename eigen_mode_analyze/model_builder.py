"""
AEDT model-building step: for each STEP file, build an HFSS Eigenmode
model (geometry, materials, boundaries, and its single analysis setup --
everything the student license covers), then queue it with the worker for
analysis. Never calls Analyze/analyze_setup itself -- that's the worker's
job, on the full license, in a different process entirely (see
ansys-analyze-worker's ARCHITECTURE.md section 8).
"""
from __future__ import annotations

import logging
import os
import re
from typing import Callable, Dict, List, Optional

from ansys_analyze_common import queue_common

from . import equations as eq

logger = logging.getLogger("eigen_mode_analyze")


def get_installed_versions() -> List[str]:
    """
    Detect installed AEDT versions and return them as clean "YYYY.R"
    strings (e.g. "2025.2"), newest first.

    This deliberately avoids passing PyAEDT's own auto-detected version
    string straight back into itself: when only the Student version is
    installed, that string comes back as "2025.2SV" (the "SV" suffix
    identifies Student Version), and PyAEDT's own version-string parser
    then rejects that as invalid. `student_version=True` is already how
    you tell PyAEDT you want the Student edition -- the version string
    passed alongside it must be the plain "2025.2" form.

    Two detection strategies, tried in order:
      1. ANSYSEM_ROOT### environment variables (fast, no PyAEDT import
         needed) -- but not always present even when AEDT works fine.
      2. PyAEDT's own internal version registry, stripping the trailing
         letters (SV, or any other suffix) it doesn't strip itself.
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


def calculate_min_freq(dimensions_txt_content: str, safety_factor: float = 0.4, floor_ghz: float = 0.1) -> float:
    """
    Estimate a starting MinimumFrequency for the eigenmode sweep from the
    PCB footprint, then pull it DOWN by `safety_factor` before returning.

    The naive estimate (half-wavelength resonance across the larger PCB
    dimension) only accounts for the PCB itself -- the actual enclosure
    (box/lid/pedestal cavity) is typically comparable in size or larger,
    so its true lowest cavity mode can sit below that naive estimate.
    Starting the sweep too high silently skips real low-frequency modes.
    `safety_factor` (default 0.4, i.e. start at 40% of the naive value)
    gives headroom to catch those; `floor_ghz` is a hard lower bound.
    """
    params = eq.get_numeric_parameters(dimensions_txt_content)
    try:
        pcb_length = params["pcb_length"]
        pcb_side = params["pcb_side"]
    except KeyError as exc:
        raise ValueError(f"Could not find {exc} in the equations file") from exc

    max_dim_m = max(pcb_length, pcb_side) * 1e-3
    c = 299792458
    wave_lambda = 2 * max_dim_m
    naive_min_freq_ghz = (c / wave_lambda) / 1e9
    return max(naive_min_freq_ghz * safety_factor, floor_ghz)


def assign_materials_and_boundaries(hfss, mat_settings: dict) -> Dict[str, object]:
    objects_dict = {}
    for obj in hfss.modeler.solid_objects:
        name = obj.name.lower()

        if "vacuum" in name:
            obj.material_name = mat_settings["mat_vacuum"]
            objects_dict["vacuum"] = obj
        elif "pcb" in name:
            obj.material_name = mat_settings["mat_pcb"]
            objects_dict["pcb"] = obj
        elif "chip" in name and "lid" not in name:
            obj.material_name = mat_settings["mat_chip"]
            objects_dict["chip"] = obj
        elif "chip_lid" in name:
            obj.material_name = mat_settings["mat_box"]
            objects_dict["chip_lid"] = obj
            if mat_settings["perfect_e_box"]:
                hfss.assign_perfect_e(assignment=obj.name)
        elif "lid" in name:
            obj.material_name = mat_settings["mat_box"]
            objects_dict["lid"] = obj
            if mat_settings["perfect_e_box"]:
                hfss.assign_perfect_e(assignment=obj.name)
        elif "box" in name:
            obj.material_name = mat_settings["mat_box"]
            objects_dict["box"] = obj
            if mat_settings["perfect_e_box"]:
                hfss.assign_perfect_e(assignment=obj.name)

    return objects_dict


SETUP_NAME = "Setup_1"


def create_eigenmode_setup(hfss, dimensions_txt_content: str, analysis_settings: dict):
    """Create the project's single Eigenmode setup. The student license
    covers `create_setup` -- it's only `analyze`/`analyze_setup` that needs
    the full license, which is why this still runs here (client side,
    student version) and the worker only ever runs the setup this
    produces, never creates one itself (see ARCHITECTURE.md section 8)."""
    min_freq_ghz = calculate_min_freq(
        dimensions_txt_content, safety_factor=analysis_settings["min_freq_safety_factor"]
    )
    setup = hfss.create_setup(name=SETUP_NAME)
    setup.props["MinimumFrequency"] = f"{min_freq_ghz}GHz"
    setup.props["NumModes"] = analysis_settings["modes"]
    setup.props["MaximumPasses"] = analysis_settings["max_passes"]
    setup.props["MinimumPasses"] = analysis_settings["min_passes"]
    setup.props["MinimumConvergedPasses"] = analysis_settings["min_converged"]
    setup.props["MaximumDeltaFreqPerPass"] = analysis_settings["max_delta_f"]
    return setup


def build_and_queue(
    combo_id: str,
    step_path: str,
    dimensions_txt_content: str,
    run,
    queue: "queue_common.QueuePaths",
    analysis_settings: dict,
    aedt_version: Optional[str],
    student_version: bool = True,
    graphical: bool = False,
    log: Callable[[str], None] = logger.info,
) -> None:
    """Build an HFSS model from step_path, assign materials/boundaries,
    create its (single) Eigenmode setup, and queue it for the worker to
    analyze. No-ops if this combo is already queued (resume support)."""
    if run.state["combos"][combo_id].get("task_id"):
        log(f"{combo_id} already queued (task {run.state['combos'][combo_id]['task_id']}) -- skipping.")
        return

    from ansys.aedt.core import Hfss

    combo_dir = run.combo_dir(combo_id)
    project_path = os.path.join(combo_dir, f"{combo_id}.aedt")

    hfss = Hfss(
        project=project_path,
        version=aedt_version,
        solution_type="Eigenmode",
        new_desktop=False,
        student_version=student_version,
        non_graphical=not graphical,
    )

    try:
        if hfss.modeler.object_names:
            hfss.modeler.delete(hfss.modeler.object_names)
        for bound in hfss.boundaries:
            try:
                bound.delete()
            except Exception:
                pass

        log(f"Importing {step_path}...")
        hfss.modeler.import_3d_cad(step_path)

        objects_dict = assign_materials_and_boundaries(hfss, analysis_settings["materials"])
        object_names = {key: obj.name for key, obj in objects_dict.items()}

        setup = create_eigenmode_setup(hfss, dimensions_txt_content, analysis_settings)

        hfss.save_project(project_path)
        hfss.close_project(name=hfss.project_name)
        hfss = None

        task_id = queue_common.new_task_id("eigenmode_analyze")
        relative_project_file = queue_common.copy_project_to_queue(queue, task_id, combo_dir)
        relative_output_dir = queue_common.project_dir_for_task(task_id)

        task = queue_common.build_task(
            task_type="eigenmode_analyze",
            project_file=relative_project_file,
            output_dir=relative_output_dir,
            task_id=task_id,
            # The project was just built with this solution type above --
            # passing it through lets the worker skip auto-detecting it
            # from the AEDT session when it reopens the project. See
            # queue_common.build_task()'s docstring for why that matters.
            solution_type="Eigenmode",
            objects=object_names,
            post_processing=[
                {"op": "field_plot_surface", "params": {"object_key": "chip", "quantity": "Mag_E"}},
                {"op": "field_plot_volume", "params": {"object_key": "vacuum", "quantity": "Mag_E"}},
            ],
            metadata={
                "combo_id": combo_id,
                "params": run.state["combos"][combo_id]["params"],
                "source_step_file": os.path.abspath(step_path),
                "setup_name": setup.name,
            },
        )
        task_path = queue_common.write_task_atomically(queue, task)
        queue_common.write_local_marker(
            combo_dir,
            task_id,
            queue.root,
            extra={
                "local_project_file": project_path,
                "queued_project_file": relative_project_file,
                "output_dir": relative_output_dir,
            },
        )
        run.update_combo(combo_id, task_id=task_id, queue_status="pending")
        log(f"Queued analysis task {task_id} for {combo_id}: {task_path}")

    finally:
        if hfss is not None:
            try:
                hfss.close_project(name=hfss.project_name)
            except Exception as e:
                log(f"Failed to close project gracefully: {e}")
