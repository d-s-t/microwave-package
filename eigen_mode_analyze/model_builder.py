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
import time
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


# How an imported solid's name decides its material and boundary.
#
# This used to be an if/elif chain of substring tests, which was fine
# while every body was either the box, the lid, the PCB or the chip. It
# stops being fine the moment copper exists: "pcb_copper" contains
# "pcb", so the old chain would have assigned it Rogers and nobody would
# have noticed until the eigenfrequencies came out wrong.
#
# So the rules are explicit and ORDERED, most specific first, and the
# first match wins. `perfect_e` names the setting that decides whether
# the body also gets a perfect-E boundary; None means never.
#
# Names come from the STEP, which gets them from the SolidWorks
# components -- see base_parts/altium/README.md for where copper body
# names come from.
MATERIAL_RULES = [
    # (substring, key in objects_dict, material setting, perfect-E setting)
    ("vacuum",          "vacuum",          "mat_vacuum",          None),
    ("chip_lid",        "chip_lid",        "mat_box",             "perfect_e_box"),
    ("chip_conductor",  "chip_conductor",  "mat_conductor_chip",  "perfect_e_conductors"),
    ("chip_substrate",  "chip",            "mat_chip",            None),
    ("pcb_copper",      "pcb_copper",      "mat_conductor_pcb",   "perfect_e_conductors"),
    ("pcb_via",         "pcb_vias",        "mat_conductor_pcb",   "perfect_e_conductors"),
    ("pcb_substrate",   "pcb",             "mat_pcb",             None),
    ("wirebond",        "wirebond",        "mat_conductor_pcb",   "perfect_e_conductors"),
    ("connector",       "connector",       "mat_box",             "perfect_e_connector"),
    # The bare names come last, so the specific ones above win.
    ("lid",             "lid",             "mat_box",             "perfect_e_box"),
    ("box",             "box",             "mat_box",             "perfect_e_box"),
    ("chip",            "chip",            "mat_chip",            None),
    ("pcb",             "pcb",             "mat_pcb",             None),
]


def build_copper(hfss, combo_dir: str, dimensions_txt_content: str, log=logger.info) -> None:
    """Rebuild the layout's copper in the model, if the layout exported any.

    Silently does nothing when there is no copper file: a run built from
    a SolidWorks-only assembly is still perfectly valid, and was the only
    kind of run until the Altium stage existed.
    """
    from . import copper_geometry, equations as eq

    copper_path = os.path.join(combo_dir, "copper.txt")
    if not os.path.isfile(copper_path):
        log("No copper.txt for this combination -- building the model without copper.")
        return

    values = eq.get_numeric_parameters(dimensions_txt_content)
    boards = copper_geometry.read_file(copper_path)

    # The substrates sit centred on the assembly origin, so a board's
    # copper needs no XY offset -- the datum the Altium generator keeps.
    # Z is the only thing this side has to work out.
    plans = [
        ("package", "pcb", copper_geometry.Placement(
            z_top=values.get("pcb_width", 0.381),
            z_bottom=0.0,
            thickness=values.get("copper_thickness", 0.035),
        )),
        ("chip", "chip", copper_geometry.Placement(
            z_top=values.get("pcb_width", 0.381) + values.get("chip_width", 0.381),
            z_bottom=values.get("pcb_width", 0.381),
            thickness=values.get("chip_metal_thickness", 0.0002),
        )),
    ]
    for board_name, prefix, placement in plans:
        board = boards.get(board_name)
        if board is None or not len(board):
            continue
        copper_geometry.build(hfss.modeler, board, placement, prefix=prefix, log=log)

    # The bonds are computed rather than read: Altium has no scriptable
    # wirebond object, and the loop profile matters more to the field
    # solve than to the layout anyway.
    bond_count = int(values.get("bond_count", 0))
    if bond_count:
        pcb_top = values.get("pcb_width", 0.381)
        chip_top = pcb_top + values.get("chip_width", 0.381)
        half_chip = values.get("chip_length", 6.3) / 2.0
        span = values.get("bonding_clearence", 1.0)
        inset = values.get("bond_pad_length", 0.3)
        for sign in (-1.0, +1.0):
            copper_geometry.build_wirebonds(
                hfss.modeler,
                copper_geometry.BondSpec(
                    x_start=sign * (half_chip + span),
                    x_end=sign * (half_chip - inset),
                    z_start=pcb_top,
                    z_end=chip_top,
                    count=bond_count,
                    pitch=values.get("bond_pad_pitch", 0.4),
                    height=values.get("bond_height", 0.2),
                    diameter=values.get("bond_wire_diameter", 0.025),
                ),
                log=log,
            )


def assign_materials_and_boundaries(hfss, mat_settings: dict, log=logger.info) -> Dict[str, object]:
    """Give every solid in the imported model its material and boundary.

    A body whose name matches no rule is left alone and WARNED ABOUT
    rather than silently defaulting. A mis-exported or renamed body is
    the kind of thing that otherwise shows up as a puzzling frequency
    shift days later.
    """
    objects_dict = {}
    unmatched = []

    for obj in hfss.modeler.solid_objects:
        name = obj.name.lower()
        for pattern, key, material_key, perfect_e_key in MATERIAL_RULES:
            if pattern not in name:
                continue
            material = mat_settings.get(material_key)
            if material:
                obj.material_name = material
            objects_dict.setdefault(key, obj)
            if perfect_e_key and mat_settings.get(perfect_e_key):
                hfss.assign_perfect_e(assignment=obj.name)
            break
        else:
            unmatched.append(obj.name)

    if unmatched:
        log(
            "WARNING: no material rule matched these solids, so they keep "
            "the modeller default: " + ", ".join(sorted(unmatched))
            + ". Check the body names against MATERIAL_RULES."
        )
    return objects_dict


def carve_vacuum(hfss, objects_dict, log=logger.info) -> None:
    """Cut the conductors out of the vacuum body.

    The vacuum arrives from the STEP as the cavity fill, sized back when
    the only things in the cavity were the substrates. The copper, vias
    and bonds built here now occupy part of that same volume, and two
    solids claiming one region is not a small inaccuracy -- HFSS either
    errors or silently resolves it by material priority, which is worse
    because the model still solves.
    """
    vacuum = objects_dict.get("vacuum")
    if vacuum is None:
        return

    tools = [
        obj.name
        for key, obj in objects_dict.items()
        if key != "vacuum" and ("copper" in key or "via" in key or "wirebond" in key)
    ]
    if not tools:
        return

    try:
        # keep_originals: the conductors must survive being cut out of the
        # vacuum -- they are the thing being simulated.
        hfss.modeler.subtract(vacuum.name, tools, keep_originals=True)
        log(f"Carved {len(tools)} conductor bodies out of the vacuum.")
    except Exception as exc:
        log(
            f"WARNING: could not subtract the conductors from the vacuum ({exc}). "
            "The model has overlapping solids and its results should not be trusted."
        )


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
    setup.props["MaxDeltaFreq"] = analysis_settings["max_delta_f"]
    return setup


def project_file_for(run, combo_id: str) -> str:
    """Where this combo's AEDT project lives -- and, because AEDT names a
    project after its file, what that project is called inside the AEDT
    session.

    The name carries the run id because the AEDT session is shared and
    long-lived (`new_desktop=False`): a project some earlier run left
    open -- a crash, a stopped run, a window the user still has on
    screen -- stays in that session under its own name. Combo ids repeat
    from run to run (a sweep with nothing swept calls its only combo
    "default" every single time), so a plain `<combo_id>.aedt` puts two
    same-named projects in one session, and from that point on AEDT
    refuses every command that addresses a project by name ("Cannot
    perform command because multiple projects are named default") --
    including the close below, which then dies inside PyAEDT on the
    None it gets handed back.

    A project an earlier version of this code already wrote under the
    old `<combo_id>.aedt` name keeps that name, so resuming a run
    started before this change reopens its project instead of building a
    second one beside it -- the queue copy wants exactly one .aedt file
    per combo folder.
    """
    combo_dir = run.combo_dir(combo_id)
    legacy_path = os.path.join(combo_dir, f"{combo_id}.aedt")
    if os.path.exists(legacy_path):
        return legacy_path
    return os.path.join(combo_dir, f"{run.run_id}__{combo_id}.aedt")


def close_project_safely(hfss, log: Callable[[str], None] = logger.info) -> bool:
    """Close whatever project `hfss` has open, without ever raising.

    PyAEDT's own `close_project()` asks the session for the project
    object and calls `.GetPath()` on it unchecked, so when the session
    cannot hand one back -- the project is already gone, or its name is
    ambiguous -- it dies with `AttributeError: 'NoneType' object has no
    attribute 'GetPath'` rather than reporting a close that failed. By
    the time we close, the model is built and saved to disk; a close
    going wrong must not take the whole run down with it.

    Returns True when the project is no longer open in AEDT.
    """
    if hfss is None:
        return True

    try:
        name = hfss.project_name
    except Exception:
        name = None
    if not name:
        log("AEDT reports no open project -- nothing to close.")
        return True

    try:
        if hfss.close_project(name=name) is False:
            log(f"AEDT does not have a project named {name} open -- nothing to close.")
        return True
    except Exception as exc:
        log(f"PyAEDT could not close project {name} ({exc}); asking the desktop to close it directly.")

    try:
        hfss.odesktop.CloseProject(name)
        return True
    except Exception as exc:
        log(f"Failed to close AEDT project {name}: {exc}")
        return False


def wait_until_unlocked(project_path: str, timeout: float = 15.0) -> bool:
    """Wait for AEDT to drop the project's `.lock` file, which is what it
    leaves behind while the project is still open. The lock travels with
    the folder when the project is copied to the queue, and the worker
    refuses to open a locked project -- so a project that never unlocks
    is one that must not be queued."""
    lock_path = f"{project_path}.lock"
    deadline = time.monotonic() + timeout
    while os.path.exists(lock_path):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)
    return True


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
    project_path = project_file_for(run, combo_id)

    hfss = Hfss(
        project=project_path,
        version=aedt_version,
        solution_type="Eigenmode",
        new_desktop=False,
        student_version=student_version,
        non_graphical=not graphical,
        # A run that died with its project open leaves an .aedt.lock behind,
        # and PyAEDT refuses to reopen a locked project -- which would make
        # that combo unresumable forever. The lock is only reached when the
        # project is NOT already open in this session (PyAEDT attaches to it
        # instead when it is), and these projects live in the run's own
        # folder where nothing else opens them, so a lock here is a leftover.
        remove_lock=True,
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

        # Copper is not in the STEP -- Altium's export drops it, so it is
        # rebuilt here from the geometry read out of the layout. This must
        # happen BEFORE materials are assigned, or the copper bodies exist
        # without one. See copper_geometry for why it works this way.
        build_copper(hfss, combo_dir, dimensions_txt_content, log=log)

        objects_dict = assign_materials_and_boundaries(hfss, analysis_settings["materials"], log=log)
        carve_vacuum(hfss, objects_dict, log=log)
        object_names = {key: obj.name for key, obj in objects_dict.items()}

        create_eigenmode_setup(hfss, dimensions_txt_content, analysis_settings)

        hfss.save_project(project_path)
        closed = close_project_safely(hfss, log=log)
        hfss = None
        # The lock file, not the close call, is what decides whether this
        # combo can be queued: it is what the worker trips over.
        if not wait_until_unlocked(project_path):
            log(
                f"{combo_id} was built and saved, but AEDT still holds "
                f"{os.path.basename(project_path)} open -- not queueing it, because the worker "
                f"cannot open a locked project. Close it in AEDT (or restart AEDT), then run "
                f"again to pick it up."
            )
            return
        if not closed:
            log(f"{combo_id}'s project unlocked despite that close error -- queueing it anyway.")

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
                # The four numbers the run page plots. The chip's are taken
                # on its top face only -- the surface the circuit is on --
                # while the vacuum's cover the whole cavity volume.
                {"op": "field_statistics", "params": {
                    "object_key": "chip", "quantity": "Mag_E",
                    "region": "top_surface", "key": "chip_mag_E",
                }},
                {"op": "field_statistics", "params": {
                    "object_key": "vacuum", "quantity": "Mag_E",
                    "region": "volume", "key": "vacuum_mag_E",
                }},
                # Best-effort picture of that same chip face.
                {"op": "field_plot_surface", "params": {"object_key": "chip", "quantity": "Mag_E"}},
            ],
            metadata={
                "combo_id": combo_id,
                "params": run.state["combos"][combo_id]["params"],
                "source_step_file": os.path.abspath(step_path),
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
        close_project_safely(hfss, log=log)
