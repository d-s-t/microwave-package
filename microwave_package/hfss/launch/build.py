"""
Drawing the launch study in HFSS: one SMP connector on a short board.

`geometry.py` says what the copper is; this turns it into a model. The
board is the package's own stack seen from one end: the box floor, the
substrate, the copper (drawn by `copper_geometry.build`, the code that draws
the full package's), and the lid on top of the copper with its relief
channel and the window the connector stands through. The connector is the
vendor model, placed by `model_builder.place_connectors`, and port 1 is the
same wave port across its air bore the full runs use (`ports.py`'s
`connector` reference plane). Port 2 is a 50 ohm CPW wave port on the cut end
of the board, so the line is matched at that end and S11 is the launch alone.

Names follow `model_builder.MATERIAL_RULES`, so the materials and boundaries
come from the same code as the package's.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from ...design import constraints, copper_geometry as cg, cpw, equations as eq, smp
from .. import model_builder, registry
from ..driven import ports as driven_ports
from . import geometry

logger = logging.getLogger("microwave_package")

SETUP_NAME = "Setup_1"
SWEEP_NAME = "Sweep_1"
RECORD_FILE = "launch.json"
# The box floor under the board (mm). The package's is thicker; here it only
# has to be metal for the board's bottom copper to stand on.
FLOOR_MM = 0.5
# How far the deeper lid relief over the launch reaches past the pad, step and taper (mm).
POCKET_MARGIN = 0.5
# The real lid cuts the relief channel deeper in the last millimetre before each connector
# window (base_parts/lid_inside.SLDPRT; its STEP shows the ceiling at 0.3 mm along the line and
# 0.4 mm over the 1.0 mm between x = 12.65 and the window's inner wall at 13.65, the channel
# 1.2 wide throughout). It is not parameter-driven. It matters: the connector's centre-pin tab
# runs under that stretch to the signal pad, and under the plain 0.3 mm ceiling it touches the lid.
END_RELIEF_DEPTH = 0.4
END_RELIEF_LENGTH = 1.0
# Half-width of the CPW port's face on the cut (mm), as the line check's section has it: wider and the
# board beside the via fence supports a second mode inside the band.
DEFAULT_PORT_HALF_WIDTH = 1.5


def make_launch(values: Dict[str, float], settings: Dict[str, Any],
                log=logger.info) -> Tuple[geometry.Launch, float, float]:
    """(launch, eps_r, analytic gap) for one combination.

    The gap is solved for the target impedance under the lid the board is
    built with, as `design/altium_generation` does for the real board."""
    material = settings.get("materials", {}).get("mat_pcb")
    eps_r = cpw.permittivity_of(material)
    height = values.get("pcb_width", 0.381)
    metal = values.get("copper_thickness", 0.035)
    z0 = float(settings.get("launch_z0_ohm", 50.0))
    try:
        gap = cpw.solve_gap_for_z0(
            trace_width=values["cpw_trace_width"], substrate_height=height, eps_r=eps_r,
            z0=z0, conductor_thickness=metal, **constraints.lid_cover(values))
    except ValueError as exc:
        raise registry.PortPlacementError(
            f"The 50 ohm gap cannot be solved for a {values['cpw_trace_width']:g} mm trace on "
            f"{material}: {exc}") from exc
    if gap < constraints.MIN_CPW_GAP - 1e-9:
        raise registry.PortPlacementError(
            f"A {values['cpw_trace_width']:g} mm trace on {material} needs a {gap * 1000:.0f} um gap for "
            f"{z0:g} ohm, under the {constraints.MIN_CPW_GAP * 1000:.0f} um the layout check allows "
            "(AEDT cannot unite the copper below it). Choose a wider trace.")
    clearance = constraints.check_launch_clearance(values, gap)
    if clearance:
        raise registry.PortPlacementError(clearance + ".")
    launch = geometry.from_values(
        values, gap,
        trace_length=float(settings.get("launch_trace_length_mm", 8.0)),
        half_width=float(settings.get("launch_half_width_mm", 3.5)))
    problems = launch.problems()
    if problems:
        raise registry.PortPlacementError("The launch cannot be built: " + "; ".join(problems) + ".")
    return launch, eps_r, gap


def _subtract(hfss, blank: str, tools: List[str]) -> None:
    hfss.modeler.subtract([blank], tools, keep_originals=False)


def _bottom_cutout(hfss, launch: geometry.Launch, frame: cg.Frame, made: Dict[str, List[str]],
                   pcb_bottom: float, drawn: float, log) -> None:
    """Take the metal from under the pad: a round hole in the bottom copper and
    a matching pocket in the box floor, down to the model's floor of the air.
    The pocket is a machined feature of the real box, whose depth is a design
    choice of its own; here it is the model's whole floor (`FLOOR_MM`)."""
    if not drawn:
        raise registry.PortPlacementError(
            "The bottom cutout needs the copper drawn as a solid; this thickness is drawn as a sheet.")
    radius = launch.bottom_cutout_radius
    x = frame.x_offset + launch.pin_u
    for blank, base, height, tool in ((list(made.get("pcb_copper_bottom", [])), pcb_bottom - drawn - 0.05, drawn + 0.1,
                                      "launch_cutout_copper_cut"),
                                (["box_floor"], 0.0, pcb_bottom + 0.05, "launch_cutout_floor_cut")):
        if not blank:
            continue
        disc = hfss.modeler.create_cylinder(
            orientation="Y", origin=[x, base, 0.0], radius=radius, height=height, name=tool)
        if not disc:
            raise registry.PortPlacementError("Could not draw the bottom cutout.")
        hfss.modeler.subtract(blank, [disc.name], keep_originals=False)
    log(f"Bottom cutout under the pad: radius {radius:g} mm through the bottom copper and the floor.")


def draw_lid(hfss, launch: geometry.Launch, values: Dict[str, float], frame: cg.Frame,
             lid_bottom: float, log) -> str:
    """The lid on the copper: a slab with the relief channel cut out of its
    underside along the line, a deeper pocket over the launch when
    `launch_relief_depth` asks for one, and the window the connector stands in."""
    H = launch.half_length
    lid_width = values.get("lid_width", 1.5)
    route_width = values.get("route_width", 1.2)
    route_depth = values.get("route_clearence", 0.3)
    hw = launch.half_width
    lid = hfss.modeler.create_box(
        origin=[0.0, lid_bottom, -hw], sizes=[2.0 * H, lid_width, 2.0 * hw], name="lid_inside")
    if not lid:
        raise registry.PortPlacementError("Could not draw the lid.")

    tools: List[str] = []

    def tool(name: str, x0: float, x1: float, half_z: float, depth: float, below: float = 0.0):
        box = hfss.modeler.create_box(
            origin=[x0, lid_bottom - below, -half_z], sizes=[x1 - x0, depth + below, 2.0 * half_z], name=name)
        if not box:
            raise registry.PortPlacementError(f"Could not draw {name} for the lid.")
        tools.append(box.name)

    if route_width > 0 and route_depth > 0:
        tool("lid_channel_cut", 0.0, 2.0 * H, route_width / 2.0, route_depth, below=0.05)
    pocket = values.get("launch_relief_depth", 0.0)
    if pocket > 0 and route_width > 0:
        # From the board end to just past the pad's inboard end, the pull-back step and the taper
        # (the connector's window has already taken everything out to the land's far edge).
        reach = launch.origin_u + max(launch.pull_reach, smp.SIGNAL_V + launch.pad_length / 2.0) \
            + launch.taper_length
        x_end = frame.x_offset + reach + POCKET_MARGIN
        tool("lid_launch_pocket_cut", 0.0, x_end, route_width / 2.0, pocket, below=0.05)
        log(f"Lid relief over the launch: {pocket:g} mm deep from the board end to {x_end:g} mm "
            f"({POCKET_MARGIN:g} mm past the pad, step and taper).")
    window = values.get("port_length", 5.1) + 2.0 * values.get("smp_window_extra", 0.25)
    cx = frame.x_offset + launch.origin_u
    x0, x1 = cx - window / 2.0, cx + window / 2.0
    if route_width > 0 and END_RELIEF_DEPTH > route_depth:
        # The window's inboard wall is at x1; the deeper stretch runs inboard from it. It starts
        # 0.05 mm inside the window, which is cut out whole there, so the two share no face.
        tool("lid_end_relief_cut", x1 - 0.05, x1 + END_RELIEF_LENGTH, route_width / 2.0,
             END_RELIEF_DEPTH, below=0.05)
    box = hfss.modeler.create_box(
        origin=[x0, lid_bottom - 0.05, -window / 2.0], sizes=[x1 - x0, lid_width + 0.1, window],
        name="lid_window_cut")
    if not box:
        raise registry.PortPlacementError("Could not draw the connector's window in the lid.")
    tools.append(box.name)
    _subtract(hfss, lid.name, tools)
    return lid.name


def draw_model(hfss, launch: geometry.Launch, values: Dict[str, float], settings: Dict[str, Any],
               eps_r: float, log=logger.info) -> Tuple[Dict[str, str], List[str], Dict[str, float]]:
    """Solids, materials, connector, ports. Returns (object_names, port_names, heights)."""
    modeler = hfss.modeler
    H, hw = launch.half_length, launch.half_width
    frame = cg.Frame(x_offset=H, z_offset=0.0)

    pcb_width = values.get("pcb_width", 0.381)
    copper = values.get("copper_thickness", 0.035)
    drawn = 0.0 if copper < model_builder.SHEET_BELOW_MM else copper
    pcb_bottom = FLOOR_MM
    pcb_top = pcb_bottom + pcb_width
    copper_top = pcb_top + drawn
    lid_top = copper_top + values.get("lid_width", 1.5)

    vacuum = modeler.create_box(origin=[0.0, 0.0, -hw], sizes=[2.0 * H, lid_top, 2.0 * hw], name="vacuum")
    floor = modeler.create_box(origin=[0.0, 0.0, -hw], sizes=[2.0 * H, FLOOR_MM, 2.0 * hw], name="box_floor")
    substrate = modeler.create_box(
        origin=[0.0, pcb_bottom, -hw], sizes=[2.0 * H, pcb_width, 2.0 * hw], name="pcb_substrate")
    if not (vacuum and floor and substrate):
        raise registry.PortPlacementError("Could not draw the vacuum, floor and substrate.")

    board = geometry.board(launch)
    made = cg.build(modeler, board, frame, top_face=pcb_top, bottom_face=pcb_bottom,
                    thickness=drawn, prefix="pcb", log=log)
    sheets: List[str] = []
    if not drawn:
        sheets = [name for key, names in made.items() if "_copper_" in key for name in names]
        log(f"{', '.join(sheets)}: {copper * 1000:g} um of metal, under "
            f"{model_builder.SHEET_BELOW_MM * 1000:g} um, so drawn as a sheet.")

    if launch.bottom_cutout_radius > 0:
        _bottom_cutout(hfss, launch, frame, made, pcb_bottom, drawn, log)

    draw_lid(hfss, launch, values, frame, copper_top, log)

    model_builder.place_connectors(
        hfss, frame, board_top=copper_top, half_length=H,
        port_space=values.get("port_space", 1.5), count=1, log=log)

    objects = model_builder.assign_materials_and_boundaries(
        hfss, settings.get("materials", {}), log=log, sheets=sheets or None)
    model_builder.carve_vacuum(hfss, objects, log=log)
    object_names = {key: obj.name for key, obj in objects.items()}

    impedance = float(settings.get("launch_port_ohm", 50.0))
    port_names = driven_ports._connector_wave_ports(hfss, impedance, log=log)
    port_half = float(settings.get("launch_port_half_width_mm", DEFAULT_PORT_HALF_WIDTH))
    least = values.get("route_width", 1.2) / 2.0 + 0.2
    if port_half < least or port_half > launch.half_width:
        raise registry.PortPlacementError(
            f"The CPW port's half-width ({port_half:g} mm) must be between {least:g} mm (the lid's channel "
            f"and its walls) and the board's half-width ({launch.half_width:g} mm).")
    port_names += [_cpw_port(hfss, launch, port_half, lid_top, impedance,
                             int(settings.get("launch_modes", 1)), pcb_top, copper_top, log)]
    heights = {"pcb_bottom": pcb_bottom, "pcb_top": pcb_top, "copper_top": copper_top, "lid_top": lid_top}
    return object_names, port_names, heights


def _cpw_port(hfss, launch: geometry.Launch, port_half: float, lid_top: float, impedance: float,
              modes: int, pcb_top: float, copper_top: float, log) -> str:
    """A wave port on the cut end of the board: the line's cross-section out to
    `port_half` either side, its voltage path from the trace across one gap to
    the ground. The rest of the end face stays a perfect conductor."""
    x = 2.0 * launch.half_length
    name = "port_2"
    sheet = hfss.modeler.create_rectangle(
        orientation="YZ", origin=[x, 0.0, -port_half], sizes=[lid_top, 2.0 * port_half], name=f"{name}_sheet")
    if not sheet:
        raise registry.PortPlacementError("Could not draw the face for the CPW wave port.")
    y = (pcb_top + copper_top) / 2.0 if copper_top > pcb_top else pcb_top
    half = launch.trace / 2.0
    try:
        port = hfss.wave_port(
            assignment=sheet.name, integration_line=[[x, y, half], [x, y, half + launch.gap]],
            modes=modes, impedance=impedance, name=name, renormalize=True)
    except Exception as exc:
        raise registry.PortPlacementError(f"Could not create the CPW wave port at x = {x:g} mm: {exc}") from exc
    if not port:
        raise registry.PortPlacementError(f"HFSS refused the CPW wave port at x = {x:g} mm.")
    log(f"Wave port {name} on the cut end at x = {x:g} mm, {modes} mode(s), renormalised to {impedance:g} ohm.")
    return name


def create_setup(hfss, dimensions_txt_content: str, settings: Dict[str, Any], log=logger.info):
    """Adapt at the top of the band, then an interpolating sweep over it."""
    f_start = float(settings["launch_f_start_ghz"])
    f_stop = float(settings["launch_f_stop_ghz"])
    if f_stop <= f_start:
        raise ValueError(f"The launch study needs f_stop > f_start; got {f_start} -> {f_stop} GHz.")
    setup = hfss.create_setup(name=SETUP_NAME)
    setup.props["Frequency"] = f"{f_stop}GHz"
    setup.props["MaxDeltaS"] = settings["launch_delta_s"]
    setup.props["MaximumPasses"] = settings["max_passes"]
    setup.props["MinimumPasses"] = settings["min_passes"]
    setup.props["MinimumConvergedPasses"] = settings["min_converged"]
    setup.update()
    sweep = hfss.create_linear_count_sweep(
        setup=SETUP_NAME, unit="GHz", start_frequency=f_start, stop_frequency=f_stop,
        num_of_freq_points=int(settings["launch_points"]), name=SWEEP_NAME,
        sweep_type="Interpolating", save_fields=False)
    if not sweep:
        raise RuntimeError(f"Could not add the {SWEEP_NAME} frequency sweep to {SETUP_NAME}.")
    log(f"Launch setup: adaptive at {f_stop:g} GHz (MaxDeltaS {settings['launch_delta_s']}), "
        f"{settings['launch_points']} points over {f_start:g}-{f_stop:g} GHz.")
    return setup


def build_model(hfss, combo_id: str, combo_dir: str, dimensions_txt_content: str, run,
                settings: Dict[str, Any], log=logger.info) -> Tuple[Dict[str, str], List[str]]:
    """The whole launch model for one combination (the solver's `build_model`
    hook), and a record of what was drawn beside the project for the results."""
    values = eq.get_numeric_parameters(dimensions_txt_content)
    launch, eps_r, analytic = make_launch(values, settings, log=log)
    log(f"Launch ({combo_id}): trace {launch.trace:g} mm, gap {launch.gap:.4g} mm, pad "
        f"{launch.pad_width:g} x {launch.pad_length:g} mm, taper {launch.taper_length:g} mm, "
        f"void radius {launch.void_radius:g} mm, clearance {launch.clearance:g} mm; board "
        f"{2 * launch.half_length:g} x {2 * launch.half_width:g} mm, {eps_r:g} permittivity.")
    object_names, port_names, heights = draw_model(hfss, launch, values, settings, eps_r, log=log)
    create_setup(hfss, dimensions_txt_content, settings, log=log)
    record = {
        "trace_width_mm": launch.trace, "gap_mm": launch.gap,
        "pad_width_mm": launch.pad_width, "pad_length_mm": launch.pad_length,
        "taper_length_mm": launch.taper_length, "void_radius_mm": launch.void_radius,
        "launch_clearance_mm": launch.clearance,
        "relief_depth_mm": values.get("route_clearence"), "relief_width_mm": values.get("route_width"),
        "launch_relief_depth_mm": values.get("launch_relief_depth", 0.0),
        "substrate": settings.get("materials", {}).get("mat_pcb"), "eps_r": eps_r,
        "board_length_mm": 2.0 * launch.half_length, "board_width_mm": 2.0 * launch.half_width,
        "vias": len(geometry.vias(launch)), "bottom_cutout_radius_mm": launch.bottom_cutout_radius,
        "port_ohm": float(settings.get("launch_port_ohm", 50.0)),
        "heights_mm": heights,
    }
    with open(os.path.join(combo_dir, RECORD_FILE), "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2)
    return object_names, port_names
