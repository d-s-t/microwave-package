"""
The corner screws in the HFSS model: the holes they sit in, and the screws.

Everything geometric is decided by `design.screws`; this only builds it.
The screws are the vendor's ISO 10642 M2 models (base_parts/screws), not a
drawing of them: each is imported, shortened to length, and moved onto its
corner. The holes are cut here, in the imported box and lid_inside and in
the board built by `copper_geometry`, as well as being in the SolidWorks
parts, so the model carries them whatever the parts do.

Two deliberate choices:

* The hole in the box is cut to the screw's own diameter, not the tap
  drill. The thread is a surface this model does not resolve, and a screw
  overlapping the metal it is threaded into is an overlap of two
  conductors that validation refuses. The model's thread crests touch the
  hole's wall, and nothing overlaps.
* The lid's countersink is cut to the head's own cone, continued up to the
  lid's top face, so the head sits flush and shares its surface; below the
  cone the lid's clearance hole is wider than the shank, leaving the small
  air gap there is on the bench.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from .. import paths
from ..design import screws as screw_plan

logger = logging.getLogger("microwave_package")

# Lets a cutting tool stand proud of the face it cuts through, so the cut
# does not leave a skin of zero thickness on that face.
OVERSHOOT = 0.05


def model_path(length: int) -> str:
    return str(paths.BASE_PARTS_DIR / "screws" / f"Screw_ISO_10642_M2x{length}_A2.STEP")


def _hole_tool(modeler, frame, u, v, radius, bottom, top, name):
    """A cylinder along the up axis, from `bottom` to `top`."""
    tool = modeler.create_cylinder(
        orientation="Y", origin=frame.point(u, v, bottom),
        radius=radius, height=top - bottom,
    )
    tool.name = name
    return tool.name


def _countersink_tool(modeler, frame, u, v, screws, lid_top, lid_bottom, name):
    """The lid's hole: the clearance cylinder all the way through, and the
    90 degree cone that continues the head's, from the clearance diameter
    up to the countersink diameter at the top face."""
    clear_r = screws.clearance_diameter / 2.0
    depth = screws.countersink_depth
    bore = _hole_tool(modeler, frame, u, v, clear_r,
                      lid_bottom - OVERSHOOT, lid_top, f"{name}_bore")
    cone = modeler.create_cone(
        orientation="Y", origin=frame.point(u, v, lid_top - depth),
        bottom_radius=clear_r, top_radius=screw_plan.COUNTERSINK_DIAMETER / 2.0, height=depth,
    )
    cone.name = f"{name}_cone"
    modeler.unite([bore, cone.name])
    return bore


def _import_screw(hfss, length: int, name: str, log) -> str:
    """Import one vendor model and return the name it was given."""
    before = set(hfss.modeler.object_names)
    hfss.modeler.import_3d_cad(model_path(length))
    new = [n for n in hfss.modeler.object_names if n not in before]
    if not new:
        raise ValueError(f"importing {model_path(length)} produced no body")
    if len(new) > 1:
        hfss.modeler.unite(new)
    hfss.modeler[new[0]].name = name
    return name


def build_screws(hfss, frame, stack, values, log=logger.info) -> Optional[List[str]]:
    """Cut the holes and stand the four screws up. Returns their names, or
    None when `screws_on` is off.

    The copper round each hole is not cut here: the Altium board is drawn
    without it, and `copper.txt` carries it into the model (see
    GenerateFromParams.pas, AddScrewBlocks). Raises ValueError, naming what
    to change, if the lid is too thin for the head, the screw is longer
    than any model, or it would come out of the box.
    """
    screws = screw_plan.plan(values)
    if screws is None:
        return None
    modeler = hfss.modeler

    lid = modeler["lid_inside"].bounding_box
    lid_bottom, lid_top = lid[1], lid[4]
    if lid_top - lid_bottom < screws.min_lid_thickness - 1e-6:
        raise ValueError(
            f"lid_inside is {lid_top - lid_bottom:.3f} mm thick, under the "
            f"{screws.min_lid_thickness:.2f} mm the countersunk head needs "
            f"({screw_plan.HEAD_HEIGHT:g} mm of head plus {screw_plan.MIN_LID_SEAT:g} mm of lid "
            f"under it) -- increase lid_width and rebuild the lid"
        )

    # The board rests on the floor, so the floor's top is the board's
    # underside; the hole goes right through, so the tip is flush with the
    # box's underside.
    floor_top = stack.pcb_bottom
    length = screw_plan.length_for(screws, lid_top, floor_top)
    tip = lid_top - length
    box_bottom = modeler["box"].bounding_box[1]
    if tip < box_bottom - 0.01:
        raise ValueError(
            f"a {length:.2f} mm screw would come out {box_bottom - tip:.2f} mm below the box's "
            f"underside -- the floor is thinner than screw_length expects; reduce screw_length "
            f"or raise screw_engagement"
        )
    model = screw_plan.model_length(length)
    trim = model - length
    log(f"Corner screws: {length:.2f} mm long (head top at {lid_top:.3f} mm, tip at {tip:.3f} mm), "
        f"from the M2x{model} model" + (f" cut {trim:.2f} mm shorter." if trim > 1e-6 else "."))

    shank_r = screw_plan.SCREW_DIAMETER / 2.0
    clear_r = screws.clearance_diameter / 2.0

    names: List[str] = []
    for index, (u, v) in enumerate(screws.positions, start=1):
        tag = f"screw_{index}"

        # box: down from the floor top to the tip, at the screw's own
        # diameter
        hole = _hole_tool(modeler, frame, u, v, shank_r, tip, floor_top, f"{tag}_box_hole")
        modeler.subtract("box", [hole], keep_originals=False)

        # board: the clearance hole through the substrate
        pcb_hole = _hole_tool(modeler, frame, u, v, clear_r,
                              stack.pcb_bottom - OVERSHOOT, stack.pcb_top + OVERSHOOT,
                              f"{tag}_pcb_hole")
        modeler.subtract("pcb_substrate", [pcb_hole], keep_originals=False)

        # lid: clearance bore and countersink
        sink = _countersink_tool(modeler, frame, u, v, screws, lid_top, lid_bottom, f"{tag}_sink")
        modeler.subtract("lid_inside", [sink], keep_originals=False)

        # the screw. The model sits on the origin with its tip at Y = 0 and
        # its flat top at Y = model; cut the tip off to length, then move
        # it so the top is flush with the lid and the axis is on the corner.
        _import_screw(hfss, model, tag, log)
        if trim > 1e-6:
            cutter = modeler.create_box(origin=[-3.0, -2.0, -3.0],
                                        sizes=[6.0, trim + 2.0, 6.0])
            cutter.name = f"{tag}_trim"
            modeler.subtract(tag, [cutter.name], keep_originals=False)
        x, _, z = frame.point(u, v, 0.0)
        modeler.move([tag], [x, lid_top - model, z])
        names.append(tag)

    log(f"Cut the screw holes in box, lid_inside, pcb_substrate and the copper, and imported "
        f"{', '.join(names)}.")
    return names