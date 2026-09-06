"""
The board geometry Altium describes, rebuilt in HFSS.

Altium cannot hand us solids. Its STEP export drops copper by design,
and the Parasolid exporter -- which does support copper, even per layer
-- exposes its option setters to scripting but not the call that
performs the export (see `base_parts/altium/README.md`). Reading the
primitives, however, works perfectly.

So the layout crosses as data and is rebuilt here: substrates, copper,
vias and wire bonds. That is better than the CAD round trip would have
been in three ways:

  * every solid gets exactly the name its rule in `model_builder`
    expects, instead of whatever a STEP translator decided to call it,
  * nothing depends on imported face ids, which change on every rebuild
    and are what makes face-based mates fragile,
  * substrate and copper are built in one frame from one set of
    parameters, so they cannot drift apart.

The coordinate frames are the subtle part, and getting them wrong does
not look wrong -- it produces a model that solves and reports plausible
nonsense. See `Frame`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("eigen_mode_analyze")

TOP = "top"
BOTTOM = "bottom"


@dataclass(frozen=True)
class Track:
    layer: str
    x1: float
    y1: float
    x2: float
    y2: float
    width: float


@dataclass(frozen=True)
class Via:
    x: float
    y: float
    diameter: float
    hole: float


@dataclass(frozen=True)
class Pad:
    layer: str
    x: float
    y: float
    xsize: float
    ysize: float


@dataclass(frozen=True)
class Region:
    layer: str
    points: Tuple[Tuple[float, float], ...]


@dataclass
class BoardCopper:
    """Everything conductive on one board."""
    name: str
    tracks: List[Track] = field(default_factory=list)
    vias: List[Via] = field(default_factory=list)
    pads: List[Pad] = field(default_factory=list)
    regions: List[Region] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.tracks) + len(self.vias) + len(self.pads) + len(self.regions)

    def on_layer(self, layer: str) -> Tuple[List[Track], List[Pad], List[Region]]:
        return (
            [t for t in self.tracks if t.layer == layer],
            [p for p in self.pads if p.layer == layer],
            [r for r in self.regions if r.layer == layer],
        )


class CopperParseError(ValueError):
    """A malformed record, reported with its line number.

    Worth being strict about: a silently skipped via is a missing piece
    of shielding that nothing downstream would notice.
    """


def parse(text: str) -> Dict[str, BoardCopper]:
    """Parse the file written by `base_parts/altium/ExportCopper.pas`."""
    boards: Dict[str, BoardCopper] = {}
    current: Optional[BoardCopper] = None

    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("board="):
            name = line.partition("=")[2].strip()
            current = boards.setdefault(name, BoardCopper(name=name))
            continue

        if current is None:
            raise CopperParseError(f"line {number}: record before any 'board=' line")

        kind, _, rest = line.partition("|")
        fields = rest.split("|")
        try:
            if kind == "track":
                layer, x1, y1, x2, y2, w = fields
                current.tracks.append(
                    Track(layer, float(x1), float(y1), float(x2), float(y2), float(w))
                )
            elif kind == "via":
                x, y, dia, hole = fields
                current.vias.append(Via(float(x), float(y), float(dia), float(hole)))
            elif kind == "pad":
                layer, x, y, xs, ys = fields
                current.pads.append(Pad(layer, float(x), float(y), float(xs), float(ys)))
            elif kind == "region":
                layer, coords = fields
                numbers = [float(v) for v in coords.split(",") if v != ""]
                if len(numbers) < 6 or len(numbers) % 2:
                    raise ValueError(f"{len(numbers)} coordinates")
                points = tuple(
                    (numbers[i], numbers[i + 1]) for i in range(0, len(numbers), 2)
                )
                current.regions.append(Region(layer, points))
            else:
                raise CopperParseError(f"line {number}: unknown record type {kind!r}")
        except CopperParseError:
            raise
        except (ValueError, TypeError) as exc:
            raise CopperParseError(f"line {number}: bad {kind} record ({exc}): {line!r}") from None

    return boards


def read_file(path: str) -> Dict[str, BoardCopper]:
    with open(path, "r", encoding="utf-8") as f:
        return parse(f.read())


# --- placing it in the model -----------------------------------------
#
# Altium and SolidWorks do not agree on which way is up, and that
# disagreement is invisible if you get it wrong: the model still solves,
# and reports plausible nonsense with the copper lying in the wrong
# plane entirely.
#
# Altium works in (u, v, w): u along the feedline, v across it, w normal
# to the board. The SolidWorks assembly has Y normal to the board and X
# along the feedline. Mapping u to X and w to Y forces the third axis --
# for a right-handed frame Z must be -v, since u x w = -v. Nothing here
# may quietly "tidy away" that sign.


@dataclass(frozen=True)
class Frame:
    """Maps Altium board coordinates into assembly coordinates.

    `x_offset` and `z_offset` carry the board centre to where it sits in
    the box. The assembly origin is a corner of the box, so these are
    half the box size rather than zero: the board is centred in the
    cavity, not on the origin.
    """
    x_offset: float = 0.0
    z_offset: float = 0.0

    def point(self, u: float, v: float, w: float) -> List[float]:
        """One Altium point in assembly coordinates."""
        return [u + self.x_offset, w, -v + self.z_offset]

    def box(self, u1: float, v1: float, u2: float, v2: float,
            w_base: float, w_height: float) -> Tuple[List[float], List[float]]:
        """A slab in the board plane, as (origin, sizes) for a box.

        Sizes come back positive with the origin at the low corner,
        which is what the modeller wants however the inputs were ordered
        and whichever way the axis flip turned them.
        """
        p1 = self.point(u1, v1, w_base)
        p2 = self.point(u2, v2, w_base + w_height)
        origin = [min(p1[i], p2[i]) for i in range(3)]
        sizes = [abs(p2[i] - p1[i]) for i in range(3)]
        return origin, sizes


@dataclass(frozen=True)
class Stack:
    """Where each face of the stack sits along the up axis.

    The die lies IN the board, not on it: the PCB is pocketed to the
    depth of the die so their top faces finish flush. That is what keeps
    the wire bonds short and roughly level, and it is why `chip_top` and
    `pcb_top` are normally equal.
    """
    pcb_bottom: float
    pcb_top: float
    chip_bottom: float
    chip_top: float
    copper_thickness: float
    chip_metal_thickness: float

    @property
    def pcb_copper_top(self) -> float:
        """The board's actual top surface: the top of the copper, not
        the substrate it sits on. Anything that rests ON the board --
        a connector's underside, a bond wire's foot -- has to land here,
        or it sinks one plating thickness into the pad."""
        return self.pcb_top + self.copper_thickness

    @property
    def chip_copper_top(self) -> float:
        """The die's actual top surface, by the same reasoning."""
        return self.chip_top + self.chip_metal_thickness


def build_substrates(
    modeler,
    frame: Frame,
    stack: Stack,
    pcb_half_length: float,
    pcb_half_side: float,
    chip_half_length: float,
    chip_half_side: float,
    log=logger.info,
) -> List[str]:
    """Create the PCB and die dielectrics, with the die in its pocket.

    Named `pcb_substrate` and `chip_substrate` so the material rules give
    them the board and die permittivities.

    Altium would have been the natural place to build these, but its
    solid export is precisely the call that cannot be reached from a
    script. Building them here is the better answer anyway: the pocket
    can be cut exactly, and substrate and copper share one frame, so
    they cannot drift apart.
    """
    origin, sizes = frame.box(
        -pcb_half_length, -pcb_half_side, pcb_half_length, pcb_half_side,
        stack.pcb_bottom, stack.pcb_top - stack.pcb_bottom,
    )
    pcb = modeler.create_box(origin=origin, sizes=sizes)
    pcb.name = "pcb_substrate"

    pocket_origin, pocket_sizes = frame.box(
        -chip_half_length, -chip_half_side, chip_half_length, chip_half_side,
        stack.chip_bottom, stack.chip_top - stack.chip_bottom,
    )
    pocket = modeler.create_box(origin=pocket_origin, sizes=pocket_sizes)
    modeler.subtract("pcb_substrate", [pocket.name], keep_originals=False)

    chip_origin, chip_sizes = frame.box(
        -chip_half_length, -chip_half_side, chip_half_length, chip_half_side,
        stack.chip_bottom, stack.chip_top - stack.chip_bottom,
    )
    chip = modeler.create_box(origin=chip_origin, sizes=chip_sizes)
    chip.name = "chip_substrate"

    log(
        "Built pcb_substrate (pocketed for the die) and chip_substrate, "
        f"top faces flush at {stack.chip_top:g} mm."
    )
    return ["pcb_substrate", "chip_substrate"]


def build(
    modeler,
    copper: BoardCopper,
    frame: Frame,
    top_face: float,
    bottom_face: float,
    thickness: float,
    prefix: str,
    log=logger.info,
) -> Dict[str, List[str]]:
    """Create one board's copper.

    Top-layer copper grows outward from `top_face`, bottom-layer copper
    outward from `bottom_face`, and vias span between them. Returns the
    names created, so the caller can check they are the ones the material
    rules match on.
    """
    created: Dict[str, List[str]] = {}

    for layer, base, height in (
        (TOP, top_face, thickness),
        (BOTTOM, bottom_face, -thickness),
    ):
        tracks, pads, regions = copper.on_layer(layer)
        if not (tracks or pads or regions):
            continue

        parts: List[str] = []
        for region in regions:
            parts.append(_extrude_polygon(modeler, region.points, base, height, frame))
        for track in tracks:
            parts.extend(_extrude_track(modeler, track, base, height, frame, log=log))
        for pad in pads:
            origin, sizes = frame.box(
                pad.x - pad.xsize / 2, pad.y - pad.ysize / 2,
                pad.x + pad.xsize / 2, pad.y + pad.ysize / 2,
                base, height,
            )
            parts.append(modeler.create_box(origin=origin, sizes=sizes).name)

        name = f"{prefix}_copper_{layer}"
        created[name] = _unite_as(modeler, parts, name)

    if copper.vias:
        # Vias run along the up axis, which is Y here rather than Z.
        parts = [
            modeler.create_cylinder(
                orientation="Y",
                origin=frame.point(via.x, via.y, bottom_face),
                radius=via.diameter / 2.0,
                height=top_face - bottom_face,
            ).name
            for via in copper.vias
        ]
        name = f"{prefix}_vias"
        created[name] = _unite_as(modeler, parts, name)

    log(
        f"Built {len(copper)} copper records for {copper.name} as "
        + ", ".join(sorted(created))
    )
    return created


def _is_rectangle(points) -> bool:
    """Whether a region is an axis-aligned rectangle."""
    if len(points) != 4:
        return False
    us = {round(u, 9) for u, _v in points}
    vs = {round(v, 9) for _u, v in points}
    return len(us) == 2 and len(vs) == 2


def _extrude_polygon(modeler, points, base, height, frame: Frame) -> str:
    """A region as a solid.

    Rectangles -- which is most of what the generator draws -- become
    boxes. `thicken_sheet`'s extrusion direction follows the sheet's
    normal, which depends on the polyline's winding order, and that
    winding is not something this code controls once points have gone
    through the coordinate frame's own reflection (v negated). A box's
    origin and size are unambiguous regardless of winding.

    Anything else is swept instead, along an EXPLICIT vector, which
    keeps the same property: the direction is stated here rather than
    inferred from the outline.

    The shaped case is not hypothetical and must not fall back to a
    bounding box. The coplanar ground planes carry the connector's
    circular anti-pad cut out of their inner edge, and a bounding box
    would fill that void back in with solid copper -- silently, since
    the layer is united into one body afterwards and the result looks
    exactly like a correctly-built plane.
    """
    if _is_rectangle(points):
        us = [u for u, _v in points]
        vs = [v for _u, v in points]
        origin, sizes = frame.box(min(us), min(vs), max(us), max(vs), base, height)
        return modeler.create_box(origin=origin, sizes=sizes).name

    sheet = modeler.create_polyline(
        points=[frame.point(u, v, base) for u, v in points],
        close_surface=True,
        cover_surface=True,
    )
    # `height` is along the up axis, which is Y in assembly axes, and is
    # negative for the bottom layer -- the vector carries that sign.
    if not modeler.sweep_along_vector(sheet.name, [0.0, height, 0.0]):
        raise RuntimeError(
            f"could not sweep region {sheet.name} into a solid; it would "
            f"remain a sheet and carry no copper"
        )
    return sheet.name


def _cylinder(modeler, frame: Frame, u: float, v: float, w_base: float,
             height: float, radius: float, log=logger.info):
    """A cylinder along the up axis, with the sign of `height` normalised.

    `create_cylinder` extrudes from its origin by `height` along
    `orientation`; a negative height (the bottom copper layer) is not
    independently confirmed to extrude the right way, so this always
    passes a positive height from the lower face upward instead of
    trusting the sign.
    """
    w0 = min(w_base, w_base + height)
    return modeler.create_cylinder(
        orientation="Y", origin=frame.point(u, v, w0),
        radius=radius, height=abs(height),
    )


def _extrude_track(modeler, track: Track, base, height, frame: Frame,
                   log=logger.info) -> List[str]:
    """A track is a rectangle plus a round cap at each end.

    The caps are not decoration: without them a run of tracks meeting at
    an angle leaves wedge-shaped gaps, and a gap in a ground plane is
    exactly the kind of defect that moves a mode.
    """
    half = track.width / 2.0
    du, dv = track.x2 - track.x1, track.y2 - track.y1
    length = (du * du + dv * dv) ** 0.5

    if length == 0:
        return [_cylinder(modeler, frame, track.x1, track.y1, base, height, half).name]

    if abs(dv) < 1e-9:
        # Horizontal run -- every track this generator draws is one of
        # these (there is exactly one AddTrack call per board, always
        # along the feedline). A box is unambiguous regardless of
        # extrusion direction, unlike thicken_sheet.
        x_lo, x_hi = sorted((track.x1, track.x2))
        origin, sizes = frame.box(x_lo, track.y1 - half, x_hi, track.y1 + half, base, height)
        names = [modeler.create_box(origin=origin, sizes=sizes).name]
    else:
        log(
            "WARNING: a non-horizontal track was extruded via thicken_sheet, whose "
            "direction is not independently confirmed for this orientation -- "
            "check it in the model."
        )
        # Unit normal in the board plane, to reach the edges of the track.
        nu, nv = -dv / length * half, du / length * half
        corners = [
            frame.point(track.x1 + nu, track.y1 + nv, base),
            frame.point(track.x2 + nu, track.y2 + nv, base),
            frame.point(track.x2 - nu, track.y2 - nv, base),
            frame.point(track.x1 - nu, track.y1 - nv, base),
        ]
        poly = modeler.create_polyline(points=corners, close_surface=True, cover_surface=True)
        modeler.thicken_sheet(poly, height)
        names = [poly.name]

    for u, v in ((track.x1, track.y1), (track.x2, track.y2)):
        names.append(_cylinder(modeler, frame, u, v, base, height, half).name)
    return names


def _unite_as(modeler, parts: Sequence[str], name: str) -> List[str]:
    """Unite `parts` into one solid called `name`.

    The name is what matters: `model_builder.MATERIAL_RULES` matches on
    it, so a rename here silently changes a material.
    """
    parts = [p for p in parts if p]
    if not parts:
        return []
    if len(parts) > 1:
        modeler.unite(parts)
    modeler[parts[0]].name = name
    return [name]


# --- wire bonds -------------------------------------------------------
#
# Built here rather than in Altium. AD26 models bond wires -- it has
# wirebond rules, violation types and JEDEC profile constants -- but
# exposes no wirebond entry in its scriptable object-id enum, so a
# script cannot create one. That turned out not to matter: a bond is EM
# geometry, not layout, and building it here means the loop profile is
# exactly what the model needs.
#
# The endpoints come from `bonds.py`, the same file Altium places its
# pads from, so a wire always lands on its pad.


def wire_path(bond, frame: Frame, pcb_top: float, chip_top: float,
              height: float, segments: int = 12) -> List[List[float]]:
    """Points along one bond loop, in assembly coordinates.

    A real bond leaves the pad steeply, arcs over and comes down on the
    die; the loop is what keeps it clear of the die edge. A half sine
    gives that shape with the apex `height` above the ends, and needs no
    special-casing when the two ends sit at different heights -- which
    they do not, now the die is let into the board flush.
    """
    import math

    points = []
    for i in range(segments + 1):
        t = i / segments
        u = bond.pcb_x + (bond.chip_x - bond.pcb_x) * t
        v = bond.pcb_y + (bond.chip_y - bond.pcb_y) * t
        w = pcb_top + (chip_top - pcb_top) * t + height * math.sin(math.pi * t)
        points.append(frame.point(u, v, w))
    return points


def build_bond_wires(modeler, bond_list, frame: Frame, pcb_top: float,
                     chip_top: float, height: float, diameter: float,
                     name: str = "wirebond", log=logger.info) -> List[str]:
    """Create every bond wire as a swept circular profile.

    `pcb_top`/`chip_top` are the height of the COPPER surface a wire
    lands on -- the caller passes substrate-top-face-plus-plating, not
    the bare substrate. The whole path is then raised by one more wire
    radius here, so the tube's UNDERSIDE sits tangent to the pad rather
    than its centreline. A tube centred exactly on the pad surface has
    half its cross-section sunk into the pad and the substrate below it,
    which is exactly what an intersection check flags.

    They are united into one body: the model wants a handful of named
    solids, not one per wire, and the material rules match on the name.
    """
    radius = diameter / 2.0
    made = []
    for bond in bond_list:
        wire = modeler.create_polyline(
            points=wire_path(bond, frame, pcb_top + radius, chip_top + radius, height),
            xsection_type="Circle",
            xsection_width=diameter,
        )
        made.append(wire.name)

    if not made:
        return []
    if len(made) > 1:
        modeler.unite(made)
    modeler[made[0]].name = name

    signal = sum(1 for b in bond_list if b.kind == "signal")
    log(f"Built {len(made)} bond wires as {name!r} ({signal} signal, {len(made) - signal} ground)")
    return [name]


def clear_chip_area(
    modeler,
    frame: Frame,
    stack: Stack,
    chip_half_length: float,
    chip_half_side: float,
    targets: Sequence[str],
    log=logger.info,
) -> None:
    """Cut the die's footprint out of the board's copper.

    The board is pocketed for the die, so copper crossing that footprint
    would be hanging over a hole -- or, where the pocket is blind because
    the board is thicker than the die, sitting under the die with no gap.
    Either way it is copper that is not there on the real board, and a
    stray conductor is exactly the sort of thing that moves a mode.

    The cut spans the full stack plus a copper thickness at each end, so
    it takes the top and bottom layers together however the pocket
    turned out. It is also a shade WIDER in plane than the die itself,
    for the same reason a real cutout carries a fab keep-out: the die's
    own copper (its trace and pads) runs exactly to the die edge, and an
    exactly-coincident cut boundary shares a face with it -- which reads
    as an intersection even though nothing actually overlaps.
    """
    if not targets:
        return

    margin = max(stack.copper_thickness, stack.chip_metal_thickness) * 2.0
    origin, sizes = frame.box(
        -chip_half_length - margin, -chip_half_side - margin,
        chip_half_length + margin, chip_half_side + margin,
        stack.pcb_bottom - margin,
        (stack.pcb_top - stack.pcb_bottom) + 2 * margin,
    )
    tool = modeler.create_box(origin=origin, sizes=sizes)
    # Named rather than left as NewObject_*, so an operation that fails
    # to consume it is obvious in the tree instead of anonymous.
    tool.name = "chip_area_cut"
    modeler.subtract(list(targets), ["chip_area_cut"], keep_originals=False)
    log(f"Cleared the die footprint out of {', '.join(targets)}.")


def drill_vias(modeler, substrate: str, vias: str, log=logger.info) -> None:
    """Cut the via barrels out of the substrate.

    Without this the barrels and the board claim the same volume. HFSS
    either errors or resolves the overlap by material priority, and the
    quiet case is the dangerous one: the model still solves, with copper
    and dielectric occupying one region.

    The barrels are kept -- they are the conductor being modelled; it is
    the board that gets the holes.
    """
    try:
        modeler.subtract(substrate, [vias], keep_originals=True)
        log(f"Drilled {vias} out of {substrate}.")
    except Exception as exc:
        log(
            f"WARNING: could not drill {vias} out of {substrate} ({exc}). "
            "The board and its vias overlap and the model should not be trusted."
        )
