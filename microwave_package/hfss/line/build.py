"""
Drawing the line-impedance section in HFSS: solids, materials, ports, setup.

`section.py` says WHAT the section is; this turns it into a model. The solids
are named by the rules `model_builder.MATERIAL_RULES` match on, so the
section gets the same materials and boundaries as the full package from the
same code (`assign_materials_and_boundaries`): the substrate is the run's
`mat_pcb` (or `mat_chip`), the copper is perfect E unless the run says
otherwise, the lid and the floor are the box's material.

The ports are wave ports on the two end faces. HFSS solves the 2D mode of
the whole cross-section -- trace, gaps, grounds, the lid's channel -- and
that is what makes this a check of the BUILT line: the cover over the trace
is in it, which the analytic CPW solve is not.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from ...design import constraints, copper_geometry, cpw, equations as eq
from .. import model_builder, registry
from . import section as sec

logger = logging.getLogger("microwave_package")

SETUP_NAME = "Setup_1"
SWEEP_NAME = "Sweep_1"
SECTION_FILE = "section.json"


def make_section(values: Dict[str, float], settings: Dict[str, Any], combo_dir: Optional[str],
                 log=logger.info) -> Tuple[sec.Section, float]:
    """The section for one combination, and the analytic gap it is checked against.

    Taken from the combination's copper file when there is one, else
    re-derived from the parameters. Raises `registry.PortPlacementError`
    for a section that cannot be solved, with the reason.
    """
    which = settings.get("which_line", sec.BOARD)
    materials = settings.get("materials", {})
    material = materials.get("mat_pcb" if which == sec.BOARD else "mat_chip")
    eps_r = cpw.permittivity_of(material)
    height = values.get("pcb_width" if which == sec.BOARD else "chip_width", 0.381)
    metal = values.get("copper_thickness" if which == sec.BOARD else "chip_metal_thickness", 0.0)
    trace = constraints.chip_trace(values) if which == sec.CHIP else values["cpw_trace_width"]
    try:
        # The board's line is solved under the lid it is built with; the die's
        # as the layout check solves it (no cover).
        analytic = cpw.solve_gap_for_z0(
            trace_width=trace, substrate_height=height, eps_r=eps_r,
            z0=float(settings.get("line_z0_ohm", 50.0)), conductor_thickness=metal,
            **(constraints.lid_cover(values) if which == sec.BOARD else {}))
    except ValueError as exc:
        analytic = None
        log(f"The analytic 50 ohm gap could not be solved for this line ({exc}); the check goes on "
            "without it.")
    length = float(settings.get("section_length_mm", 6.0))
    half_width = float(settings.get("section_half_width_mm", 3.0))

    built = None
    forced_gap = float(settings.get("line_gap_mm", 0.0) or 0.0)
    if forced_gap > 0:
        # A gap asked for by hand (to see what HFSS makes of it, whatever the
        # layout would have drawn): the section is built from the parameters.
        built = sec.from_parameters(which, values, material, eps_r, length, half_width, forced_gap)
        built.notes.append(f"the gap was set by hand (line_gap_mm = {forced_gap:g} mm)")
    copper_path = os.path.join(combo_dir, "copper.txt") if combo_dir else None
    if built is None and copper_path and os.path.isfile(copper_path):
        boards = copper_geometry.read_file(copper_path)
        board = boards.get(sec.COPPER_BOARD_NAME[which])
        if board is not None:
            built = sec.from_copper(board, which, values, material, eps_r, length, half_width, analytic)
        elif which == sec.CHIP:
            raise registry.PortPlacementError(
                "This board has no chip line: the layout has no 'chip' board (it was drawn as a "
                "bare board, or without Altium).")
    if built is None:
        if analytic is None:
            raise registry.PortPlacementError(
                "There is no copper layout to measure and the analytic gap cannot be solved, so "
                "there is no line to check.")
        built = sec.from_parameters(which, values, material, eps_r, length, half_width, analytic)
    problems = built.problems()
    if problems:
        raise registry.PortPlacementError("The line section cannot be built: " + "; ".join(problems) + ".")
    return built, analytic if analytic is not None else float("nan")


def draw_section(hfss, section: sec.Section, materials: Dict[str, Any], port_ohm: float,
                 port_modes: int, log=logger.info) -> Tuple[Dict[str, str], List[str]]:
    """Make the solids, give them materials, carve the vacuum and place the ports.

    Returns (object_names, port_names). The caller has already deleted
    whatever the design held and turned material override on.
    """
    modeler = hfss.modeler
    sheets: List[str] = []
    thin = section.copper_thickness < model_builder.SHEET_BELOW_MM

    for part in section.parts():
        is_copper_layer = "_copper_" in part.name
        if part.shape == "cylinder":
            solid = modeler.create_cylinder(
                orientation="Y", origin=[part.lo[0], part.lo[1], part.lo[2]],
                radius=part.radius, height=part.hi[1] - part.lo[1], name=part.name)
        elif is_copper_layer and thin:
            # A film this thin is a sheet carrying a boundary, as in the package.
            x0, y, z0 = part.lo
            x1, _y1, z1 = part.hi
            solid = modeler.create_polyline(
                points=[[x0, part.lo[1], z0], [x1, part.lo[1], z0], [x1, part.lo[1], z1], [x0, part.lo[1], z1]],
                close_surface=True, cover_surface=True, name=part.name)
            sheets.append(solid.name)
        else:
            solid = modeler.create_box(
                origin=list(part.lo),
                sizes=[part.hi[i] - part.lo[i] for i in range(3)], name=part.name)
        if not solid:
            raise registry.PortPlacementError(f"Could not draw {part.name} for the line section.")

    lo, hi = section.region()
    vacuum = modeler.create_box(origin=list(lo), sizes=[hi[i] - lo[i] for i in range(3)], name="vacuum")
    if not vacuum:
        raise registry.PortPlacementError("Could not draw the vacuum box around the line section.")

    objects_dict = model_builder.assign_materials_and_boundaries(
        hfss, materials, log=log, sheets=sheets or None)
    model_builder.carve_vacuum(hfss, objects_dict, log=log)
    object_names = {key: obj.name for key, obj in objects_dict.items()}

    port_names = _ports(hfss, section, port_ohm, port_modes, log)
    return object_names, port_names


def _ports(hfss, section: sec.Section, port_ohm: float, port_modes: int, log) -> List[str]:
    names = []
    for end in (0, 1):
        x, y_lo, y_hi, z_lo, z_hi = section.port_sheet(end)
        name = f"port_{end + 1}"
        sheet = hfss.modeler.create_rectangle(
            orientation="YZ", origin=[x, y_lo, z_lo], sizes=[y_hi - y_lo, z_hi - z_lo],
            name=f"{name}_sheet")
        if not sheet:
            raise registry.PortPlacementError(f"Could not draw the port face for {name}.")
        start, stop = section.integration_line(end)
        try:
            port = hfss.wave_port(
                assignment=sheet.name, integration_line=[list(start), list(stop)],
                modes=port_modes, impedance=port_ohm, name=name, renormalize=True)
        except Exception as exc:
            raise registry.PortPlacementError(f"Could not create wave port {name} at x = {x:g} mm: {exc}") from exc
        if not port:
            raise registry.PortPlacementError(f"HFSS refused wave port {name} at x = {x:g} mm.")
        log(f"Wave port {name} on the end face at x = {x:g} mm, {port_modes} mode(s), "
            f"renormalised to {port_ohm:g} ohm.")
        names.append(name)
    return names


def create_setup(hfss, dimensions_txt_content: str, settings: Dict[str, Any], log=logger.info):
    """A driven-modal setup over the check's band: adapt at the top, then a
    short interpolating sweep. Cheap on purpose -- a few millimetres of line."""
    f_start = float(settings["line_f_start_ghz"])
    f_stop = float(settings["line_f_stop_ghz"])
    if f_stop <= f_start:
        raise ValueError(f"The line check needs f_stop > f_start; got {f_start} -> {f_stop} GHz.")
    setup = hfss.create_setup(name=SETUP_NAME)
    setup.props["Frequency"] = f"{f_stop}GHz"
    setup.props["MaxDeltaS"] = settings["line_delta_s"]
    setup.props["MaximumPasses"] = settings["max_passes"]
    setup.props["MinimumPasses"] = settings["min_passes"]
    setup.props["MinimumConvergedPasses"] = settings["min_converged"]
    setup.update()
    sweep = hfss.create_linear_count_sweep(
        setup=SETUP_NAME, unit="GHz", start_frequency=f_start, stop_frequency=f_stop,
        num_of_freq_points=int(settings["line_points"]), name=SWEEP_NAME,
        sweep_type="Interpolating", save_fields=False)
    if not sweep:
        raise RuntimeError(f"Could not add the {SWEEP_NAME} frequency sweep to {SETUP_NAME}.")
    log(f"Line check setup: adaptive at {f_stop:g} GHz (MaxDeltaS {settings['line_delta_s']}), "
        f"{settings['line_points']} points over {f_start:g}-{f_stop:g} GHz.")
    return setup


def build_model(hfss, combo_id: str, combo_dir: str, dimensions_txt_content: str, run,
                settings: Dict[str, Any], log=logger.info) -> Tuple[Dict[str, str], List[str]]:
    """The whole line-impedance model for one combination (the solver's
    `build_model` hook): section, materials, ports, setup, and a record of what
    was drawn beside the project (`section.json`) for the results to read."""
    values = eq.get_numeric_parameters(dimensions_txt_content)
    section, analytic = make_section(values, settings, combo_dir, log=log)
    log(f"Line section ({section.which}): trace {section.trace_width:g} mm, gap {section.gap:g} mm "
        f"(analytic gap {analytic:.4g} mm), {section.substrate_material} eps {section.eps_r:g}, "
        f"lid channel {section.route_width:g} x {section.route_clearence:g} mm, "
        f"{len(section.vias)} via post(s); geometry from {section.source}.")
    for note in section.notes:
        log(f"NOTE: {note}")
    object_names, port_names = draw_section(
        hfss, section, settings.get("materials", {}),
        float(settings.get("line_port_ohm", 50.0)), int(settings.get("line_modes", 1)), log=log)
    create_setup(hfss, dimensions_txt_content, settings, log=log)
    record = {
        **section.summary(),
        "analytic_gap_mm": analytic,
        "target_ohm": float(settings.get("line_z0_ohm", 50.0)),
        "tolerance_ohm": float(settings.get("line_tolerance_ohm", 2.5)),
        "port_ohm": float(settings.get("line_port_ohm", 50.0)),
        "notes": section.notes,
    }
    with open(os.path.join(combo_dir, SECTION_FILE), "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2)
    return object_names, port_names
