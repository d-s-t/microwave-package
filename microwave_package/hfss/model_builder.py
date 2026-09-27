"""
AEDT model-building step: for each STEP file, build an HFSS model
(geometry, materials, boundaries, ports if the run has any, and its
single analysis setup -- everything the student license covers), then
queue it with the worker for analysis. Never calls Analyze/analyze_setup
itself -- that's the worker's job, on the full license, in a different
process entirely (see ansys-analyze-worker's ARCHITECTURE.md section 8).

The geometry, materials and boundaries here are the same whatever is
being solved on them. Everything that is not -- which setup to create,
whether there are ports and where they go -- is asked of the run's
solver rather than branched on. See registry.py for that contract.
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from ansys_analyze_common import queue_common

from ..design import equations as eq
from . import registry

logger = logging.getLogger("microwave_package")


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


# What a material setting falls back to when the run does not carry one.
# Runs created before a key existed post an empty string for it, and an
# empty material is not "leave it alone" -- it leaves the body on HFSS's
# default of vacuum, which is indistinguishable from a real choice.
MATERIAL_FALLBACKS = {
    "mat_box": "aluminum",
    "mat_pcb": "Rogers TMM 10i (tm)",
    "mat_chip": "silicon",
    "mat_vacuum": "vacuum",
    "mat_conductor_pcb": "copper",
    "mat_conductor_chip": "niobium",
    # Aluminum superconducts at the millikelvin temperatures this package
    # is meant for; gold (the other real option a bonder loads) does not,
    # so it is the one to reach for deliberately rather than the default.
    "mat_wirebond": "aluminum",
}


# The web form's dropdown for each material setting below: common, real
# HFSS library names for the role that setting plays, so picking one is a
# click rather than a remembered exact library string. Not exhaustive --
# the library has hundreds of entries -- so the form pairs this with a
# "Custom..." choice that falls back to a free-text box (see
# CUSTOM_MATERIAL and webapp._materials_from_form), which is what keeps an
# existing run's material -- whatever it is -- always representable.
#
# The wirebond is deliberately not here: its choices are closed, not
# curated -- see WIREBOND_MATERIALS below.
MATERIAL_OPTIONS: Dict[str, List[str]] = {
    "mat_box": ["aluminum", "copper", "brass", "gold"],
    "mat_pcb": [
        "Rogers TMM 10i (tm)", "Rogers RT/duroid 5880 (tm)",
        "Rogers RT/duroid 6010LM (tm)", "FR4_epoxy", "sapphire",
    ],
    "mat_chip": ["silicon", "sapphire", "GaAs", "SiC"],
    "mat_vacuum": ["vacuum", "air"],
    "mat_conductor_pcb": ["copper", "gold", "silver", "aluminum"],
    "mat_conductor_chip": ["niobium", "aluminum", "tantalum"],
}

# What the web form posts back for "Custom...", in place of one of the
# curated options above. Can't collide with a real HFSS material name --
# those never start with two underscores.
CUSTOM_MATERIAL = "__custom__"

# The wirebond's material, unlike the ones above, is not a curated pick
# from an open-ended library -- a bonder loads one wire at a time, and
# this package's process only ever loads one of these two, so the form
# offers exactly these two and nothing else.
WIREBOND_MATERIALS = ["gold", "aluminum"]


# The SMP connector's materials. All of it is vendor geometry, not a
# design variable, so it is hardcoded here rather than exposed as run
# settings -- `place_connectors` assigns it to the imported bodies
# directly, and the `connector` / `connector_dielectric` material rules
# below carry no material of their own as a result.
# See github.com/d-s-t/wr-smp-connector for the datasheet references.
#
# Bead: Wuerth's datasheet material is LCP; permittivity ~3.6 and
# dielectric loss tangent ~0.003 at 10 GHz.
CONNECTOR_DIELECTRIC_MATERIAL = "SMP_LCP"
CONNECTOR_DIELECTRIC_PERMITTIVITY = 3.6
CONNECTOR_DIELECTRIC_LOSS_TANGENT = 0.003
# Outer and centre conductor: brass (datasheet body + centre contact),
# with 0.127 um Au over Ni plating that is thinner than the skin depth
# above ~1 GHz, so bulk brass is the honest first approximation. A stock
# HFSS library material, so no properties to set. There is no perfect-E
# boundary on the connector -- it carries its real metal.
CONNECTOR_CONDUCTOR_MATERIAL = "brass"


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
# the body also gets a perfect-E boundary; None means never. A `material`
# of None means the body's material is set where it is created (the three
# SMP connector bodies) -- the rule then only records the body and
# applies its boundary, if any.
#
# Names come from the STEP, which gets them from the SolidWorks
# components -- see base_parts/altium/README.md for where copper body
# names come from.
#
# Note that the chip's copper needs its own rules ahead of the bare
# "chip" one. Without them "chip_copper_top" matched "chip" and was
# given SILICON -- a conductor modelled as a dielectric, which solves
# perfectly happily and is completely wrong.
MATERIAL_RULES = [
    # (substring, key in objects_dict, material setting, perfect-E setting)
    ("vacuum",          "vacuum",          "mat_vacuum",          None),
    ("lid_inside",      "lid_inside",      "mat_box",             "perfect_e_box"),
    ("chip_conductor",  "chip_conductor",  "mat_conductor_chip",  "perfect_e_conductors"),
    ("chip_copper",     "chip_copper",     "mat_conductor_chip",  "perfect_e_conductors"),
    ("chip_via",        "chip_vias",       "mat_conductor_chip",  "perfect_e_conductors"),
    ("chip_substrate",  "chip",            "mat_chip",            None),
    ("pcb_copper",      "pcb_copper",      "mat_conductor_pcb",   "perfect_e_conductors"),
    ("pcb_via",         "pcb_vias",        "mat_conductor_pcb",   "perfect_e_conductors"),
    ("pcb_substrate",   "pcb",             "mat_pcb",             None),
    ("wirebond",        "wirebond",        "mat_wirebond",        "perfect_e_conductors"),
    # The SMP connector imports as three bodies, and place_connectors sets
    # the material on all three (LCP bead, brass conductors) -- hence None
    # for both rules. The bead rule must precede the bare "connector" one.
    # Neither gets a perfect-E boundary: the connector carries its real
    # metal.
    ("dielectric",      "connector_dielectric", None,             None),
    ("connector",       "connector",       None,                  None),
    # The bare names come last, so the specific ones above win.
    ("lid",             "lid",             "mat_box",             "perfect_e_box"),
    ("box",             "box",             "mat_box",             "perfect_e_box"),
    ("chip",            "chip",            "mat_chip",            None),
    ("pcb",             "pcb",             "mat_pcb",             None),
]


# box and lid_inside each come from their own standalone STEP now (see
# step_generation.py) and carry no PCB or chip at all. This guards
# against nothing in that pair, but stays in case either STEP is ever
# built from a source that still carries superseded copies -- the die is
# pocketed into the board and the copper built in the same frame here
# instead, so an imported copy would overlap the one we build.
SUPERSEDED_BY_LAYOUT = ("pcb", "chip")


def _drop_superseded_solids(hfss, log) -> None:
    """Delete the imported substrates we are about to rebuild.

    Two solids occupying one region is not a small inaccuracy: HFSS
    either errors or resolves it silently by material priority, and the
    silent case is worse because the model still solves.
    """
    doomed = []
    for obj in hfss.modeler.solid_objects:
        name = obj.name.lower()
        # Anything lid-like is enclosure, not die. lid_inside shares no
        # "pcb"/"chip" wording today; this is cheap insurance against a
        # future part that does.
        if "lid" in name:
            continue
        if name in SUPERSEDED_BY_LAYOUT:
            doomed.append(obj.name)
    if doomed:
        hfss.modeler.delete(doomed)
        log(f"Removed the imported {', '.join(doomed)} -- rebuilt from the layout instead.")


def _ensure_connector_dielectric_material(hfss, log) -> None:
    """Add the SMP connector bead's LCP material to the project, once.

    Datasheet LCP: permittivity ~3.6, dielectric loss tangent ~0.003 at
    10 GHz. Hardcoded rather than a run setting -- it is vendor geometry.
    """
    try:
        if hfss.materials.exists_material(CONNECTOR_DIELECTRIC_MATERIAL):
            return
        material = hfss.materials.add_material(CONNECTOR_DIELECTRIC_MATERIAL)
        material.permittivity = CONNECTOR_DIELECTRIC_PERMITTIVITY
        material.dielectric_loss_tangent = CONNECTOR_DIELECTRIC_LOSS_TANGENT
        material.update()
    except Exception as exc:
        log(f"WARNING: could not create the {CONNECTOR_DIELECTRIC_MATERIAL} material ({exc}).")


def _combined_bounding_box(hfss, names: List[str]) -> List[float]:
    """Union of several bodies' bounding boxes, as [xmin, ymin, zmin, xmax, ymax, zmax]."""
    boxes = [hfss.modeler[n].bounding_box for n in names]
    return [
        min(b[0] for b in boxes), min(b[1] for b in boxes), min(b[2] for b in boxes),
        max(b[3] for b in boxes), max(b[4] for b in boxes), max(b[5] for b in boxes),
    ]


def _classify_connector_bodies(hfss, names: List[str]):
    """Sort the three split-STEP bodies into (outer, centre, dielectric).

    Prefers the STEP product names (SMP_outer_conductor / SMP_center_conductor
    / SMP_dielectric); falls back to volume order -- outer ~33 > dielectric
    ~11 > centre ~1 mm^3 -- if the importer named them something else.
    Returns None when there are not exactly three bodies to sort.
    """
    if len(names) != 3:
        return None

    def by_key(*keys):
        for n in names:
            low = n.lower()
            if any(k in low for k in keys):
                return n
        return None

    outer, centre, diel = by_key("outer"), by_key("center", "centre"), by_key("diel")
    if not (outer and centre and diel) or len({outer, centre, diel}) != 3:
        try:
            ordered = sorted(names, key=lambda n: float(hfss.modeler[n].volume), reverse=True)
        except Exception:
            return None
        outer, diel, centre = ordered
    return outer, centre, diel


def place_connectors(hfss, frame, board_top: float, half_length: float,
                     port_space: float, count: int, log=logger.info) -> List[str]:
    """Import the split SMP connector model once per port and set it on its launch.

    The vendor ships one fused solid; github.com/d-s-t/wr-smp-connector
    splits it into three named bodies -- outer conductor, centre conductor
    and dielectric bead -- so each port becomes `connector_N_outer`,
    `connector_N_center` and `connector_N_dielectric`. All three get their
    material here (brass conductors, LCP bead -- vendor geometry, not run
    settings), and the connector carries no perfect-E boundary.

    Placement is by the BOUNDING BOX of the three bodies together, not by
    the model's own origin. A vendor model's origin can sit anywhere, and
    this one's does not coincide with the footprint origin, which is what
    left the connectors offset from their launches. Aligning measured
    faces instead is self-correcting: the outer face lands port_space from
    the board edge, the underside sits on the board, and the body is
    centred across it, whatever the file happens to use as its datum.
    """
    from .. import paths
    from ..design import smp

    model = paths.SMP_MODEL_FILE
    if not model.is_file():
        log(f"No SMP model at {model} -- building the board without connectors.")
        return []

    _ensure_connector_dielectric_material(hfss, log)

    placed: List[str] = []
    for index in range(int(count)):
        sign = -1.0 if index == 0 else 1.0
        before = set(hfss.modeler.object_names)
        try:
            hfss.modeler.import_3d_cad(
                str(model),
                healing=True,
                refresh_all_ids=True,
                import_materials=False,
                group_by_assembly=False,
                create_group=False,
                separate_disjoints_lumped_object=False,
            )
        except Exception as exc:
            log(f"WARNING: could not import the SMP model ({exc}); no connector placed.")
            return placed

        new_objects = [n for n in hfss.modeler.object_names if n not in before]
        if not new_objects:
            log("WARNING: importing the SMP model added no object; no connector placed.")
            return placed

        prefix = f"connector_{index + 1}"
        classified = _classify_connector_bodies(hfss, new_objects)
        if classified is None:
            # Not the three-body split we expect. Name whatever arrived so
            # the `connector` rule still catches it, treat it all as brass,
            # and carry on.
            log(
                f"WARNING: the SMP model imported {len(new_objects)} bodies, not 3; "
                "treating them all as connector conductors."
            )
            bodies = []
            for i, obj_name in enumerate(new_objects):
                body = prefix if i == 0 else f"{prefix}_{i}"
                hfss.modeler[obj_name].name = body
                bodies.append(body)
            conductor_bodies, dielectric = list(bodies), None
        else:
            outer, centre, diel = classified
            hfss.modeler[outer].name = f"{prefix}_outer"
            hfss.modeler[centre].name = f"{prefix}_center"
            hfss.modeler[diel].name = f"{prefix}_dielectric"
            bodies = [f"{prefix}_outer", f"{prefix}_center", f"{prefix}_dielectric"]
            conductor_bodies = [f"{prefix}_outer", f"{prefix}_center"]
            dielectric = f"{prefix}_dielectric"

        try:
            # Stand it up: the model has Z up, the assembly has Y up. The
            # three bodies rotate and move as one list so the assembly
            # stays rigid.
            hfss.modeler.rotate(bodies, "X", smp.model_rotation_degrees())
            # Turn it to face the board. The near connector is the far one
            # through half a revolution about the up axis.
            if sign < 0:
                hfss.modeler.rotate(bodies, "Y", 180.0)

            box = _combined_bounding_box(hfss, bodies)
            move = smp.model_alignment(box, frame, board_top, half_length, port_space, sign)
            hfss.modeler.move(bodies, move)
            log(f"Placed {prefix}: bounding box now {_combined_bounding_box(hfss, bodies)}")
        except Exception as exc:
            log(
                f"WARNING: {prefix} was imported but could not be positioned ({exc}). "
                "It is in the model at the wrong place."
            )

        # Vendor materials, set here rather than through the rule table:
        # brass on the conductors, LCP on the bead, no perfect-E boundary.
        for body in conductor_bodies:
            try:
                hfss.modeler[body].material_name = CONNECTOR_CONDUCTOR_MATERIAL
                hfss.modeler[body].solve_inside = False
            except Exception as exc:
                log(
                    f"WARNING: {body} could not be set to "
                    f"{CONNECTOR_CONDUCTOR_MATERIAL} ({exc}); it stays on the modeller default."
                )
        if dielectric is not None:
            try:
                hfss.modeler[dielectric].material_name = CONNECTOR_DIELECTRIC_MATERIAL
                hfss.modeler[dielectric].solve_inside = True
            except Exception as exc:
                log(
                    f"WARNING: {dielectric} could not be set to "
                    f"{CONNECTOR_DIELECTRIC_MATERIAL} ({exc}); it stays on the modeller default."
                )

        placed.extend(bodies)

    return placed


def place_lid_inside(hfss, frame, stack, new_objects: List[str], log=logger.info) -> Optional[str]:
    """Move the imported lid_inside body onto the board it sandwiches.

    lid_inside comes from its own standalone STEP (base_parts/lid_inside.SLDPRT,
    exported directly from the part -- see step_generation.py), not from
    an assembly, so there is no SolidWorks mate that put it in place. This
    is the AEDT-side equivalent, in the same style as `model_alignment`
    below for the SMP connector: measure the imported body's real bounding
    box and move it onto the reference points the design actually wants --
    centred on the same frame as the board and the box's cavity, resting
    on the board's actual top surface (the copper, not the bare substrate
    under it) rather than wherever lid_inside's own part origin happened
    to land it.

    No rotation: unlike the SMP connector's vendor model, lid_inside is
    authored in-house alongside box.SLDPRT, dimensioned the same way
    (its Sketch1 is pcb_length/2 x pcb_side/2, exactly like box's own
    outer sketch), so it already sits right-side up in the same axes as
    the rest of the assembly.

    Its top face is free to sit above box's own rim -- confirmed as the
    intended design, not a fit problem this code should second-guess.
    Whatever closes over it from there is outside this model.
    """
    if not new_objects:
        log("WARNING: lid_inside did not import -- no body to place.")
        return None

    if len(new_objects) > 1:
        hfss.modeler.unite(new_objects)
    hfss.modeler[new_objects[0]].name = "lid_inside"

    x_min, y_min, z_min, x_max, y_max, z_max = hfss.modeler["lid_inside"].bounding_box
    dx = frame.x_offset - (x_min + x_max) / 2.0
    dy = stack.pcb_copper_top - y_min
    dz = frame.z_offset - (z_min + z_max) / 2.0
    hfss.modeler.move(["lid_inside"], [dx, dy, dz])

    log(f"Placed lid_inside: bounding box now {hfss.modeler['lid_inside'].bounding_box}")
    return "lid_inside"


def build_vacuum(hfss, frame, stack, pcb_half_length: float, pcb_half_side: float,
                 log=logger.info) -> Optional[str]:
    """Build the interior air fill directly, instead of importing one.

    base_parts/vacuum.SLDPRT used to supply this, mated to box in the old
    assembly -- but it turns out not to be linked to default.txt at all
    (SolidWorks' own Equation Manager reports zero equations on it), so
    its shape was likely held by in-context references to box/lid/
    chip_lid's own edges rather than by parameters. None of that survives
    lid_inside replacing lid and chip_lid outside the assembly, so rather
    than carry a part whose size cannot be trusted to track a sweep,
    vacuum is built here the same way the PCB and chip substrates already
    are: from the frame the board uses and one measurement of box's own
    imported envelope, so it is never stale.

    It is deliberately a plain block, the full height of box's own
    bounding box -- not trimmed to the cavity floor, the pedestal, or
    lid_inside's pocket. `carve_vacuum` then subtracts every other solid
    in the model from it unconditionally, box included, so nothing here
    has to know the shape of what it is sized around.
    """
    try:
        box_box = hfss.modeler["box"].bounding_box
    except Exception as exc:
        log(f"WARNING: could not measure 'box' to size the vacuum fill ({exc}); no vacuum body built.")
        return None
    box_bottom, box_top = box_box[1], box_box[4]

    origin, sizes = frame.box(
        -pcb_half_length, -pcb_half_side, pcb_half_length, pcb_half_side,
        box_bottom, box_top - box_bottom,
    )
    vacuum = hfss.modeler.create_box(origin=origin, sizes=sizes)
    vacuum.name = "vacuum"
    log(
        f"Built vacuum fill {2 * pcb_half_length:g} x {2 * pcb_half_side:g} x "
        f"{box_top - box_bottom:.3f} mm from box's own measured envelope."
    )
    return "vacuum"


def cavity_frame(hfss, fallback_x: float, fallback_z: float, log=logger.info):
    """Where the board sits, measured from the imported cavity.

    The board is centred in the box's cavity, so the cavity's own centre
    is the datum -- and measuring it beats computing it. The equations
    give box_length as pcb_length + 2*wall_width, but the assembly also
    carries mounting wings that extend it, so anything derived from
    those two numbers alone lands half a wing out. Measuring the imported
    body gets it right without this code having to know what else the
    mechanical design grew.

    That body is box now: vacuum is built later, from this very frame
    (see build_vacuum), so there is none to measure yet. box's bounding
    box includes its mounting wings, so its centre is the cavity's only
    while the wings are symmetric -- which box.SLDPRT's are (it is drawn
    about its own origin). "vacuum" is still looked for first, for a
    STEP that brings its own.

    Falls back to the computed values, loudly, if the cavity is not
    there to measure.
    """
    from ..design import copper_geometry as cg

    for wanted in ("vacuum", "box"):
        for obj in hfss.modeler.solid_objects:
            if wanted not in obj.name.lower():
                continue
            try:
                x_min, _y_min, z_min, x_max, _y_max, z_max = obj.bounding_box
            except Exception:
                continue
            frame = cg.Frame(
                x_offset=(x_min + x_max) / 2.0,
                z_offset=(z_min + z_max) / 2.0,
            )
            log(
                f"Board datum taken from {obj.name}: centre "
                f"({frame.x_offset:.3f}, {frame.z_offset:.3f}) mm."
            )
            return frame

    log(
        "WARNING: no vacuum or box body to measure, so the board is placed from "
        f"the equations instead ({fallback_x:.3f}, {fallback_z:.3f}). If the "
        "assembly has mounting wings this will be off by half of one."
    )
    return cg.Frame(x_offset=fallback_x, z_offset=fallback_z)


@dataclass(frozen=True)
class LayoutContext:
    """Where the layout put things, for whoever needs it after the fact.

    `build_layout` works all of this out to place copper, and a driven
    run's ports need the same numbers to sit under the trace or against
    the launch. Recomputing them from the equation file a second time
    would be two places to get the stack-up wrong, so the one that
    already did it hands its answer back instead.
    """
    frame: "copper_geometry.Frame"
    stack: "copper_geometry.Stack"
    values: Dict[str, float]


def build_layout(hfss, combo_dir: str, dimensions_txt_content: str,
                 lid_inside_names: List[str], include_wirebonds: bool = True,
                 log=logger.info) -> Optional["LayoutContext"]:
    """Rebuild the boards from the layout: substrates, copper and bonds.

    Also builds the vacuum fill and places lid_inside -- see `build_vacuum`
    and `place_lid_inside`. Both need only the frame and the stack-up, not
    the Altium layout, so they happen below whether or not this
    combination has one; nothing else in the model can substitute for
    them if this returns early.

    `include_wirebonds=False` skips only the bond wires themselves --
    everything else in this function runs exactly as it would otherwise,
    including the pads the wires would have landed on (those are ordinary
    copper on the board and the die, drawn by Altium either way). That is
    what makes a "with bonds" and a "without bonds" run of the same
    combination comparable: the two models differ by the wire and nothing
    else.

    Returns where the layout put things, or None when there is no copper
    file -- a run built with Altium off is still perfectly valid, and was
    the only kind of run until the Altium stage existed.
    """
    from ..design import cavity as cavity_plan, copper_geometry as cg, equations as eq

    v = eq.get_numeric_parameters(dimensions_txt_content)

    pcb_length = v.get("pcb_length", 26.0)
    pcb_side = v.get("pcb_side", 10.0)
    pcb_width = v.get("pcb_width", 0.381)
    chip_width = v.get("chip_width", 0.381)
    wall = v.get("wall_width", 1.0)
    pedestal = v.get("pedestal_depth", 0.0)

    # The assembly origin is a corner, so the board centre sits inside
    # the box -- but not simply half of (pcb_length + 2*wall_width),
    # because the assembly also carries mounting wings that extend it.
    # Measured from the cavity rather than computed, so the mechanical
    # design can grow features this code has never heard of.
    frame = cavity_frame(
        hfss,
        fallback_x=(pcb_length + 2 * wall) / 2.0,
        fallback_z=(pcb_side + 2 * wall) / 2.0,
        log=log,
    )

    # Heights follow box_height = wall_width*1.5 + pedestal_depth +
    # pcb_width + 2*copper_thickness: the board floats on the floor and
    # the pedestal, the die is let into the board so their faces finish
    # flush, and the extra half wall_width above the copper is
    # lid_inside's own headroom (see place_lid_inside).
    pcb_bottom = wall + pedestal
    pcb_top = pcb_bottom + pcb_width
    stack = cg.Stack(
        pcb_bottom=pcb_bottom,
        pcb_top=pcb_top,
        chip_bottom=pcb_top - chip_width,
        chip_top=pcb_top,
        copper_thickness=v.get("copper_thickness", 0.035),
        chip_metal_thickness=v.get("chip_metal_thickness", 0.0002),
    )

    _drop_superseded_solids(hfss, log)
    build_vacuum(
        hfss, frame, stack,
        pcb_half_length=pcb_length / 2.0, pcb_half_side=pcb_side / 2.0, log=log,
    )
    place_lid_inside(hfss, frame, stack, lid_inside_names, log=log)

    copper_path = os.path.join(combo_dir, "copper.txt")
    if not os.path.isfile(copper_path):
        log(
            "No copper.txt for this combination -- box, lid_inside and vacuum are "
            "built, but there is no board to build a layout from."
        )
        return None
    boards = cg.read_file(copper_path)

    # The opening the die sits in. Asked for rather than worked out here,
    # because the Altium generator asked the same function the same
    # question when it drew the board -- so the hole in the model is the
    # feature on the board, wiggle room and all.
    opening = cavity_plan.plan(v)

    cg.build_substrates(
        hfss.modeler, frame, stack,
        pcb_half_length=pcb_length / 2.0,
        pcb_half_side=pcb_side / 2.0,
        chip_half_length=v.get("chip_length", 6.3) / 2.0,
        chip_half_side=v.get("chip_side", 6.3) / 2.0,
        cavity=opening,
        log=log,
    )

    package = boards.get("package")
    if package is not None and len(package):
        made = cg.build(
            hfss.modeler, package, frame,
            top_face=stack.pcb_top, bottom_face=stack.pcb_bottom,
            thickness=stack.copper_thickness, prefix="pcb", log=log,
        )
        # The generator already draws the copper round the opening, so
        # this normally cuts nothing. It is here for the copper.txt an
        # older generator wrote, where it is the difference between a
        # model and a model with a ground plane across its own cutout.
        cg.clear_cavity_area(
            hfss.modeler, frame, stack, opening,
            targets=[name for names in made.values() for name in names],
            log=log,
        )
        # A via barrel and the board cannot share a volume: drill them.
        if "pcb_vias" in made:
            cg.drill_vias(
                hfss.modeler, "pcb_substrate", package.vias, frame,
                top_face=stack.pcb_top, bottom_face=stack.pcb_bottom, log=log,
            )
            # The drill stops at the substrate faces; the plated hole does
            # not. Carry it through the lands and planes on both layers,
            # so the through-hole is open end to end and not capped by
            # copper.
            cg.open_via_holes(
                hfss.modeler,
                made.get("pcb_copper_top", []) + made.get("pcb_copper_bottom", []),
                package.vias, frame,
                top_face=stack.pcb_top, bottom_face=stack.pcb_bottom,
                copper_thickness=stack.copper_thickness, log=log,
            )

    chip = boards.get("chip")
    if chip is not None and len(chip):
        made_chip = cg.build(
            hfss.modeler, chip, frame,
            top_face=stack.chip_top, bottom_face=stack.chip_bottom,
            thickness=stack.chip_metal_thickness, prefix="chip", log=log,
        )
        # Same reasoning as the package: if the die carries through-vias,
        # the die substrate has to lose their footprint too, and the die
        # metal on both faces has to lose the drilled hole.
        if "chip_vias" in made_chip:
            cg.drill_vias(
                hfss.modeler, "chip_substrate", chip.vias, frame,
                top_face=stack.chip_top, bottom_face=stack.chip_bottom, log=log,
            )
            cg.open_via_holes(
                hfss.modeler,
                made_chip.get("chip_copper_top", [])
                + made_chip.get("chip_copper_bottom", []),
                chip.vias, frame,
                top_face=stack.chip_top, bottom_face=stack.chip_bottom,
                copper_thickness=stack.chip_metal_thickness, log=log,
            )

    # The connectors are real vendor geometry rather than a stand-in, so
    # the launch is represented rather than left as bare copper. They
    # rest on the board's actual top surface -- the copper, not the bare
    # substrate underneath it (stack.pcb_top).
    place_connectors(
        hfss, frame,
        board_top=stack.pcb_copper_top,
        half_length=pcb_length / 2.0,
        port_space=v.get("port_space", 1.5),
        count=v.get("port_count", 2),
        log=log,
    )

    # The wires come from the same layout file Altium placed the pads
    # from, so each one lands on its pad -- on the copper, again, not the
    # substrate.
    from ..design import bonds as bond_layout

    bonds_path = os.path.join(combo_dir, "bonds.txt")
    if not include_wirebonds:
        log("Wire bonds turned off for this run -- the die is not bonded.")
    elif os.path.isfile(bonds_path):
        cg.build_bond_wires(
            hfss.modeler,
            bond_layout.read_file(bonds_path),
            frame,
            pcb_top=stack.pcb_copper_top,
            chip_top=stack.chip_copper_top,
            height=v.get("bond_height", 0.2),
            diameter=v.get("bond_wire_diameter", 0.025),
            log=log,
        )
    else:
        log("No bonds.txt for this combination -- the die is not bonded.")

    return LayoutContext(frame=frame, stack=stack, values=v)


# Tried in turn when the preferred material will not stick. The last
# one is deliberately dull: a conductor modelled as the wrong metal is a
# small error, while one left as vacuum is a completely different model.
LAST_RESORT_CONDUCTOR = "copper"
LAST_RESORT_DIELECTRIC = "Rogers TMM 10i (tm)"


def _try_material(hfss, obj, material: str) -> bool:
    """Set a body's material and check it actually took.

    Assigning a name the library does not have does NOT raise -- the body
    simply stays on the modeller default, which is vacuum, and in the
    tree that is indistinguishable from a deliberate choice.
    """
    try:
        obj.material_name = material
    except Exception:
        return False
    try:
        return (obj.material_name or "").strip().lower() == material.strip().lower()
    except Exception:
        return True  # cannot read it back; assume the write worked


def _assign_material(hfss, obj, material: str, is_conductor: bool, log) -> bool:
    """Give a body a material, falling back until one sticks.

    Niobium is the case this exists for: it is the right material for a
    superconducting die but many libraries do not carry it, and the
    failure is silent. Rather than leave the body as vacuum -- which is
    not a conductor at all, and changes the physics rather than
    approximating it -- try to add the material, then settle for a plain
    metal and say so.
    """
    if _try_material(hfss, obj, material):
        return True

    # Some libraries will take it once it exists.
    try:
        hfss.materials.add_material(material)
        if _try_material(hfss, obj, material):
            log(f"Added {material!r} to the project material library for {obj.name}.")
            return True
    except Exception:
        pass

    stand_in = LAST_RESORT_CONDUCTOR if is_conductor else LAST_RESORT_DIELECTRIC
    if stand_in != material and _try_material(hfss, obj, stand_in):
        log(
            f"WARNING: the material library would not take {material!r} for "
            f"{obj.name}, so it is {stand_in!r} instead. Add {material!r} to the "
            "library if its properties matter -- with a perfect-E boundary they "
            "do not."
        )
        return True

    return False


def assign_materials_and_boundaries(hfss, mat_settings: dict, log=logger.info) -> Dict[str, object]:
    """Give every solid in the imported model its material and boundary.

    A body whose name matches no rule is left alone and WARNED ABOUT
    rather than silently defaulting. A mis-exported or renamed body is
    the kind of thing that otherwise shows up as a puzzling frequency
    shift days later.
    """
    objects_dict = {}
    unmatched = []
    unnamed = []
    fell_back = set()
    rejected = []

    for obj in hfss.modeler.solid_objects:
        name = obj.name.lower()
        for pattern, key, material_key, perfect_e_key in MATERIAL_RULES:
            if pattern not in name:
                continue
            if material_key is None:
                # The body's material is set where it is created (the SMP
                # connector bead). Nothing to resolve, and not a warning.
                pass
            else:
                material = mat_settings.get(material_key) or MATERIAL_FALLBACKS.get(material_key)
                if material:
                    if not mat_settings.get(material_key):
                        fell_back.add(material_key)
                    is_conductor = "conductor" in material_key
                    if not _assign_material(hfss, obj, material, is_conductor, log):
                        rejected.append(f"{obj.name} ({material})")
                else:
                    unnamed.append(obj.name)
            objects_dict.setdefault(key, obj)
            if perfect_e_key and mat_settings.get(perfect_e_key):
                hfss.assign_perfect_e(assignment=obj.name)
            break
        else:
            unmatched.append(obj.name)

    if rejected:
        log(
            "WARNING: the modeller would not accept these materials, so those "
            "bodies keep its default of vacuum: " + ", ".join(sorted(rejected))
            + ". A name the material library does not have is the usual cause; "
            "add it to the library or set a different one in the run settings."
        )
    if fell_back:
        # Worth saying out loud. A run whose settings predate a material
        # key posts an empty string for it, and an unset material leaves
        # the body on HFSS's default -- which is vacuum, and looks in the
        # tree exactly like a deliberate choice.
        log(
            "NOTE: no value was configured for " + ", ".join(sorted(fell_back))
            + "; used the built-in defaults instead."
        )
    if unnamed:
        log(
            "WARNING: these solids matched a rule but no material could be "
            "resolved for them, so they keep the modeller default (vacuum): "
            + ", ".join(sorted(unnamed))
        )
    if unmatched:
        log(
            "WARNING: no material rule matched these solids, so they keep "
            "the modeller default: " + ", ".join(sorted(unmatched))
            + ". Check the body names against MATERIAL_RULES."
        )
    return objects_dict


def carve_vacuum(hfss, objects_dict, log=logger.info) -> None:
    """Cut everything else in the model out of the vacuum body.

    `build_vacuum` makes vacuum a plain block spanning box's own measured
    envelope -- it does not know box's floor, pedestal or any other
    feature's shape, and does not try to. So unlike the old SolidWorks-
    hand-fitted vacuum this replaces, NOTHING is assumed pre-excluded:
    box, lid_inside, the substrates, copper, vias, bonds and connectors
    are all subtracted from it here. Two solids claiming one region is
    not a small inaccuracy: HFSS either errors or silently resolves it by
    material priority, which is worse because the model still solves.

    The tool list is read straight from `hfss.modeler.solid_objects`,
    not from `objects_dict`. Several material rules legitimately match
    MORE than one body -- pcb_copper matches both the top and bottom
    layer, connector matches two conductor bodies per port -- and
    objects_dict keeps only the first body per rule key, so building the
    tool list from it silently carved just one of them and left the rest
    overlapping vacuum. That is exactly how a second connector went
    uncarved.
    """
    vacuum = objects_dict.get("vacuum")
    if vacuum is None:
        return

    tools = [
        obj.name
        for obj in hfss.modeler.solid_objects
        if obj.name.lower() != "vacuum"
    ]
    if not tools:
        return

    try:
        # keep_originals: every one of these bodies must survive being
        # cut out of the vacuum -- they are the things being simulated.
        hfss.modeler.subtract(vacuum.name, tools, keep_originals=True)
        log(f"Carved {len(tools)} bodies out of the vacuum: {', '.join(sorted(tools))}.")
    except Exception as exc:
        log(
            f"WARNING: could not subtract {len(tools)} bodies from the vacuum ({exc}). "
            "The model has overlapping solids and its results should not be trusted."
        )


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


def reusable_model(run, combo_id: str, log=logger.info) -> Optional[Dict[str, str]]:
    """The body names of an existing model that need not be rebuilt.

    Returns None when the model genuinely has to be built. Split out from
    `build_model` so this decision can be exercised without starting
    AEDT -- it is the decision that makes resuming an offline-prepared
    run cheap, so it is worth being sure about.

    A model counts as reusable only if it was recorded as built, its
    project file is still on disk, and its body names were kept. Any of
    those missing means the record is stale and the model is rebuilt.
    """
    combo = run.state["combos"][combo_id]

    if combo.get("task_id"):
        log(f"{combo_id} already queued (task {combo['task_id']}) -- skipping.")
        return combo.get("object_names") or {}

    if combo.get("model_status") != "built":
        return None
    names = combo.get("object_names")
    if not names:
        # Without the body names there is nothing to build a task from.
        return None
    existing = combo.get("project_path") or project_file_for(run, combo_id)
    if not os.path.isfile(existing):
        log(f"{combo_id}: recorded as built but {existing} is gone -- rebuilding.")
        return None

    log(f"{combo_id}: model already built -- reusing {os.path.basename(existing)}.")
    return names


def build_model(
    combo_id: str,
    step_paths: Dict[str, str],
    dimensions_txt_content: str,
    run,
    analysis_settings: dict,
    aedt_version: Optional[str],
    student_version: bool = True,
    graphical: bool = False,
    log: Callable[[str], None] = logger.info,
) -> Optional[Dict[str, str]]:
    """Build an HFSS model from step_paths ({"box": ..., "lid_inside": ...}),
    assign materials and boundaries, and create its single Eigenmode setup.

    Takes no queue and touches no network: this is local work, and it
    has to be possible with the share down. Returns the body names the
    task will need, or None if the model could not be made ready.

    No-ops if this combination is already queued, or already built --
    which is what lets a run be prepared offline and handed over later
    without rebuilding anything.
    """
    reusable = reusable_model(run, combo_id, log=log)
    if reusable is not None:
        return reusable

    from ansys.aedt.core import Hfss

    combo_dir = run.combo_dir(combo_id)
    project_path = project_file_for(run, combo_id)

    solver = registry.for_settings(analysis_settings)

    hfss = Hfss(
        project=project_path,
        version=aedt_version,
        solution_type=solver.value,
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

        log(f"Importing {step_paths['box']}...")
        hfss.modeler.import_3d_cad(step_paths["box"])

        log(f"Importing {step_paths['lid_inside']}...")
        before_lid = set(hfss.modeler.object_names)
        hfss.modeler.import_3d_cad(step_paths["lid_inside"])
        lid_inside_names = [n for n in hfss.modeler.object_names if n not in before_lid]

        # The boards are not in the STEP in any usable form -- Altium's
        # export drops copper, and the substrates are rebuilt here so the
        # die can be pocketed into the board and everything shares one
        # frame. Must happen BEFORE materials are assigned, or the new
        # bodies exist without any. lid_inside is placed and the vacuum
        # fill built in here too -- see build_layout's docstring.
        layout = build_layout(
            hfss, combo_dir, dimensions_txt_content, lid_inside_names,
            include_wirebonds=analysis_settings["materials"].get("include_wirebonds", True),
            log=log,
        )

        objects_dict = assign_materials_and_boundaries(hfss, analysis_settings["materials"], log=log)
        carve_vacuum(hfss, objects_dict, log=log)
        object_names = {key: obj.name for key, obj in objects_dict.items()}

        # After the carve, not before: a boolean renumbers the faces of
        # everything it touches, and a port is attached to a face id.
        try:
            port_names = solver.place_ports(hfss, layout, analysis_settings, log=log)
        except registry.PortPlacementError as exc:
            log(
                f"{combo_id}: {exc} Not saving or queueing this combination -- a driven "
                "model with no ports has nothing to excite and no S-matrix to read."
            )
            return

        solver.create_setup(hfss, dimensions_txt_content, analysis_settings, log=log)

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

        object_names = _remember_built_model(
            run, combo_id, project_path, object_names, port_names, log
        )
        return object_names

    finally:
        close_project_safely(hfss, log=log)


def build_and_queue(
    combo_id: str,
    step_paths: Dict[str, str],
    dimensions_txt_content: str,
    run,
    queue: "queue_common.QueuePaths",
    analysis_settings: dict,
    aedt_version: Optional[str],
    student_version: bool = True,
    graphical: bool = False,
    log: Callable[[str], None] = logger.info,
) -> None:
    """Build the model and hand it straight to the worker.

    The two halves are separate functions because they need different
    things -- the build needs AEDT, the handoff needs the network -- but
    when both are available this is the whole job in one call.
    """
    object_names = build_model(
        combo_id, step_paths, dimensions_txt_content, run, analysis_settings,
        aedt_version, student_version=student_version, graphical=graphical, log=log,
    )
    if object_names is None:
        return
    queue_model(combo_id, step_paths, run, queue, object_names, log=log)


def _remember_built_model(run, combo_id, project_path, object_names,
                          port_names, log) -> Dict[str, str]:
    """Record that this combination's model exists and is ready to hand over.

    The body names are stored with it because the task needs them and
    they are only known while the model is being built. Without this, a
    handoff deferred to a later session would have to reopen the project
    just to rediscover them.

    The port names go in beside them for the same reason, minus the need:
    nothing downstream addresses a port by name -- S-parameters are
    indexed by port number -- but a run that came out wrong is much
    easier to read back when its record says which ports were built and
    which were not.
    """
    run.update_combo(
        combo_id,
        model_status="built",
        project_path=project_path,
        object_names=object_names,
        ports=port_names,
    )
    log(f"{combo_id}: model built and saved to {os.path.basename(project_path)}.")
    return object_names


def queue_model(
    combo_id: str,
    step_paths: Dict[str, str],
    run,
    queue: "queue_common.QueuePaths",
    object_names: Dict[str, str],
    log: Callable[[str], None] = logger.info,
) -> None:
    """Hand an already-built model to the worker.

    Split out from the build so the two can happen at different times:
    the model is built on this machine with no network at all, and only
    this half needs the queue to be reachable.

    Recorded as its own phase (`handoff_status`), because it is its own
    kind of slow: an AEDT project is hundreds of megabytes and the queue
    is usually a network share, so "copying it across" is a thing a
    combination can visibly be in the middle of.
    """
    if run.state["combos"][combo_id].get("task_id"):
        log(f"{combo_id} is already queued -- skipping.")
        return

    run.update_combo(combo_id, handoff_status="running")
    combo_dir = run.combo_dir(combo_id)
    project_path = project_file_for(run, combo_id)
    analysis_settings = run.config["analysis_settings"]
    solver = registry.for_settings(analysis_settings)
    # The task id prefixes the queue folder this combination's project is
    # copied into, so it says which kind of run it came from even though
    # `task_type` no longer does -- every task is a "run_setups" task now.
    task_id = queue_common.new_task_id(solver.task_id_prefix)
    relative_project_file = queue_common.copy_project_to_queue(queue, task_id, combo_dir)
    relative_output_dir = queue_common.project_dir_for_task(task_id)

    task = queue_common.build_task(
        # What the worker should DO, which is the same whatever kind of
        # setup this is: open the project and run the setup(s) already in
        # it. `solution_type` below is what says which kind they are.
        # (The worker still answers to the old "eigenmode_analyze" name as
        # a legacy alias, but nothing here should send it any more.)
        task_type="run_setups",
        project_file=relative_project_file,
        output_dir=relative_output_dir,
        task_id=task_id,
        # The project was just built with this solution type above --
        # passing it through lets the worker skip auto-detecting it
        # from the AEDT session when it reopens the project. See
        # queue_common.build_task()'s docstring for why that matters.
        solution_type=solver.value,
        objects=object_names,
        post_processing=solver.post_processing_ops(analysis_settings),
        metadata={
            "combo_id": combo_id,
            "params": run.state["combos"][combo_id]["params"],
            "source_step_files": {k: os.path.abspath(p) for k, p in step_paths.items()},
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
    run.update_combo(combo_id, task_id=task_id, handoff_status="done", queue_status="pending")
    log(f"Queued analysis task {task_id} for {combo_id}: {task_path}")

