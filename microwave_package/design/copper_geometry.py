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
import math
from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .cavity import Cavity

logger = logging.getLogger("microwave_package")

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
    """One land, with its own rotation about its centre.

    `xsize` and `ysize` are the pad's dimensions in the PAD's frame, not
    the board's -- which is how Altium holds them, and why `rotation`
    cannot be dropped. A placed connector's 1.4 x 0.48 signal land sits
    on the board along the feedline but still reports 0.48 x 1.4, so a
    reader that takes those as board extents builds the launch across
    the line instead of along it.

    Defaults to 0, so a copper.txt written before rotation was exported
    still reads -- every pad the generator drew itself was axis-aligned.
    """
    layer: str
    x: float
    y: float
    xsize: float
    ysize: float
    rotation: float = 0.0

    def corners(self) -> Tuple[Tuple[float, float], ...]:
        """The pad's four corners in BOARD coordinates."""
        angle = math.radians(self.rotation)
        cos, sin = math.cos(angle), math.sin(angle)
        half_x, half_y = self.xsize / 2.0, self.ysize / 2.0
        return tuple(
            (self.x + u * cos - v * sin, self.y + u * sin + v * cos)
            for u, v in ((-half_x, -half_y), (half_x, -half_y),
                         (half_x, half_y), (-half_x, half_y))
        )


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
                # The rotation is optional: a copper.txt written before
                # it was exported has six fields, and every pad in one
                # of those was axis-aligned anyway.
                if len(fields) == 5:
                    layer, x, y, xs, ys = fields
                    rot = 0.0
                else:
                    layer, x, y, xs, ys, rot = fields
                current.pads.append(
                    Pad(layer, float(x), float(y), float(xs), float(ys), float(rot))
                )
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

    def vector(self, du: float, dv: float, dw: float = 0.0) -> List[float]:
        """One Altium displacement in assembly coordinates: `point`
        without the offsets, which move points but not the steps
        between them."""
        return [du, dw, -dv]

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
    cavity: Cavity,
    log=logger.info,
) -> List[str]:
    """Create the PCB and die dielectrics, with the die in its opening.

    Named `pcb_substrate` and `chip_substrate` so the material rules give
    them the board and die permittivities.

    Altium would have been the natural place to build these, but its
    solid export is precisely the call that cannot be reached from a
    script. Building them here is the better answer anyway: substrate
    and copper share one frame, so they cannot drift apart.

    What is NOT decided here is the opening. Its size, its depth and
    whether it goes through are `cavity`'s, computed once by
    `cavity.plan` and handed to the Altium generator in the same breath,
    so the volume taken out of the board in the model is the feature
    drawn on the board. That includes the wiggle room: the opening is
    wider than the die by `cavity_clearance`, which leaves a ring of air
    around it here exactly as it does on the bench.
    """
    origin, sizes = frame.box(
        -pcb_half_length, -pcb_half_side, pcb_half_length, pcb_half_side,
        stack.pcb_bottom, stack.pcb_top - stack.pcb_bottom,
    )
    pcb = modeler.create_box(origin=origin, sizes=sizes)
    pcb.name = "pcb_substrate"

    # A through cutout takes the whole thickness; a blind pocket is
    # milled from the top face down by its own depth, leaving a floor.
    if cavity.through:
        pocket_base = stack.pcb_bottom
        pocket_height = stack.pcb_top - stack.pcb_bottom
    else:
        pocket_base = stack.pcb_top - cavity.depth
        pocket_height = cavity.depth

    pocket_origin, pocket_sizes = frame.box(
        -cavity.half_length, -cavity.half_side,
        cavity.half_length, cavity.half_side,
        pocket_base, pocket_height,
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
        f"Built pcb_substrate (a {cavity.kind} {cavity.length:g} x {cavity.side:g} mm "
        f"for the die) and chip_substrate, top faces flush at {stack.chip_top:g} mm."
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

    A `thickness` of 0 draws the layers as sheets lying on the faces
    instead -- for a film too thin to mesh as a solid, see
    `model_builder.SHEET_BELOW_MM`. The metal is then a boundary on the
    sheet rather than a material.
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
        for (xsize, ysize, rotation), where in _positions_by(
                pads, lambda p: (p.xsize, p.ysize, p.rotation)).items():
            # Built from its corners rather than its extents, so a
            # rotated land comes out at its real angle. _extrude_polygon
            # still makes a plain box of anything axis-aligned, which is
            # every pad the generator draws itself and every connector
            # land at a quarter turn -- the sweep is only reached by a
            # pad at some other angle.
            #
            # Pads of one shape are copies of each other, and built a
            # block at a time: a row of bond fingers is one pad.
            def pad_at(u, v, xsize=xsize, ysize=ysize, rotation=rotation,
                       layer=layer, base=base, height=height):
                corners = Pad(layer, u, v, xsize, ysize, rotation).corners()
                return _extrude_polygon(modeler, corners, base, height, frame)

            parts.extend(_replicate(modeler, frame, where, pad_at, log=log))

        name = f"{prefix}_copper_{layer}"
        created[name] = _unite_as(modeler, parts, name)

    if copper.vias:
        # Vias run along the up axis, which is Y here rather than Z, and
        # each is a solid post -- see `_via_post`. A stitching grid is
        # thousands of them, so they are built a block at a time (see
        # `blocks`), and each block is left a body of its own: joining
        # them is a boolean over every post at once, which on the dense
        # boards is itself one of the long AEDT calls this avoids.
        parts = []
        for (diameter,), where in _positions_by(copper.vias, lambda v: (v.diameter,)).items():
            def post_at(u, v, diameter=diameter):
                return _via_post(modeler, Via(u, v, diameter, 0.0), frame,
                                 bottom_face, top_face)

            parts.extend(_replicate(modeler, frame, where, post_at, log=log))
        name = f"{prefix}_vias"
        created[name] = _name_each(modeler, parts, name)

    log(
        f"Built {len(copper)} copper records for {copper.name} as "
        + ", ".join(sorted(created))
    )
    return created


def _via_post(modeler, via: Via, frame: Frame, bottom_face: float,
              top_face: float) -> str:
    """One plated-through via, as a solid copper post through the board.

    A real via is a drilled hole with copper plated on its wall, and it
    used to be built that way: a hollow barrel, the board drilled out
    round it, and the hole carried on through the copper on both layers.
    Every one of those is a cut through thousands of holes at once, and
    AEDT's cost for such a cut grows with the square of the count -- the
    densest boards spent over ten minutes in it, during which PyAEDT
    held Python's lock and froze the whole web app.

    None of it reaches the fields. The bore is a round waveguide a tenth
    of a millimetre across, cut off near 1.8 THz, so at these frequencies
    no field goes in, and the current runs on the outside of the barrel,
    which the post keeps. `via.hole` is read and deliberately not used.

    The board is not drilled for it either: the post overlaps the
    substrate, and the model's material override lets the metal win
    there (see `model_builder.build_model`). Nor is it carved out of the
    vacuum, which it never reaches -- see `model_builder.carve_vacuum`.

    And it has VIA_SIDES flat sides rather than a round one -- see there.
    """
    return modeler.create_cylinder(
        orientation="Y",
        origin=frame.point(via.x, via.y, bottom_face),
        radius=via.diameter / 2.0,
        height=top_face - bottom_face,
        num_sides=VIA_SIDES,
    ).name


# How many flat sides a via post has, with its corners on the via's own
# circle. The mesher has to cut a true cylinder into facets anyway, finer
# than this, and a board carries thousands of them: with octagons the
# 2959 posts of a 70 x 30 mm board on a 0.8 mm grid mesh to 419k elements
# in half the time, where true cylinders made 950k. An octagon's
# perimeter is 2.6% short of the circle's -- far below anything that
# moves a mode here, where the post is a twentieth of a wavelength across
# even at 15 GHz in the board.
VIA_SIDES = 8


# Two vertices closer than this are the same vertex. 0.1 um is orders
# below anything a board is made or meshed to, and four orders above
# Altium's own coordinate quantum of 1/10000 mil (2.54 nm).
COINCIDENT_MM = 1e-4


def _distinct(points):
    """`points` with consecutive duplicates dropped, treating the outline
    as closed.

    AEDT refuses a polyline containing a zero-length segment -- it comes
    back as `GrpcApiError: Failed to execute gRPC AEDT command:
    CreatePolyline`, which says nothing about which shape or why.

    Not hypothetical, and not something the generator can avoid: the
    vendor's own connector land arrives with three of them, where the
    anti-pad arc meets the escape slot. Altium holds coordinates in
    1/10000 mil, so a vertex the vendor drew once lands on a neighbour
    when the arc is flattened -- one pair is exactly coincident and two
    more are 10 and 30 nm apart. Every shape this project draws itself
    is far too coarse to hit it, which is why it only appeared once a
    real footprint was placed.
    """
    out: List[Tuple[float, float]] = []
    for point in points:
        if not out or math.dist(out[-1], point) > COINCIDENT_MM:
            out.append(point)
    # The outline closes, so the last vertex has to differ from the first
    # as well -- otherwise the closing segment is the zero-length one.
    while len(out) > 3 and math.dist(out[0], out[-1]) <= COINCIDENT_MM:
        out.pop()
    return out


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

    A `height` of 0 leaves it a sheet, whatever its shape -- see `build`.
    """
    # Before anything else: a duplicate vertex makes a four-cornered
    # rectangle look like a five-sided polygon to the check below, as
    # well as being what AEDT chokes on.
    points = _distinct(points)
    if len(points) < 3:
        raise RuntimeError(
            f"a region collapsed to {len(points)} distinct vertices and cannot be "
            f"built as a solid -- it carries no area"
        )

    if height == 0:
        return modeler.create_polyline(
            points=[frame.point(u, v, base) for u, v in points],
            close_surface=True,
            cover_surface=True,
        ).name

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

    A `height` of 0 makes it a disc lying in the board plane -- see `build`.
    """
    if height == 0:
        # "ZX" is the plane, so the disc's axis is Y, the up axis.
        return modeler.create_circle(orientation="ZX", origin=frame.point(u, v, w_base),
                                     radius=radius)
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
        # along the feedline). As a rectangle it becomes a box, which is
        # unambiguous regardless of extrusion direction, unlike
        # thicken_sheet -- or a sheet, at height 0.
        x_lo, x_hi = sorted((track.x1, track.x2))
        names = [_extrude_polygon(
            modeler,
            [(x_lo, track.y1 - half), (x_hi, track.y1 - half),
             (x_hi, track.y1 + half), (x_lo, track.y1 + half)],
            base, height, frame,
        )]
    elif height == 0:
        nu, nv = -dv / length * half, du / length * half
        names = [_extrude_polygon(
            modeler,
            [(track.x1 + nu, track.y1 + nv), (track.x2 + nu, track.y2 + nv),
             (track.x2 - nu, track.y2 - nv), (track.x1 - nu, track.y1 - nv)],
            base, height, frame,
        )]
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


def _unite_or_warn(modeler, parts: Sequence[str], what: str, log=logger.info) -> None:
    """Unite `parts`, and say so in the run log when AEDT will not.

    PyAEDT's unite does not raise when AEDT refuses: it writes to its own
    log, undoes, and returns False. The parts then stay behind as bodies
    no material rule matches -- vacuum -- which is how the bond wires once
    went missing from every model with signal bonds, with nothing in the
    run log but the generic unmatched-body warning.
    """
    if not modeler.unite(list(parts)):
        log(f"WARNING: AEDT would not unite {what}; the parts left over will not get "
            "the right material -- check them in the model.")


def _name_each(modeler, parts: Sequence[str], name: str) -> List[str]:
    """Name `parts` `name`, `name_2`, `name_3`... without uniting them.

    For bodies that need a name the material rules match, but have no
    need to be one body -- the via blocks in `build`.
    """
    names: List[str] = []
    for part in (p for p in parts if p):
        names.append(name if not names else f"{name}_{len(names) + 1}")
        modeler[part].name = names[-1]
    return names


# --- many copies at once ----------------------------------------------
#
# A board carries thousands of copies of a few features: a stitching
# grid of vias, rows of bond fingers, a fan of bond wires. Building each
# copy with a call of its own is what made a model take hours -- a
# 7654-via board was still building after two. Every call is a round
# trip to AEDT, and every boolean makes PyAEDT re-list the whole model
# and check its own cache against it, which costs more the more bodies
# the model already holds, so each via cost more than the last.
#
# The layouts are regular, though. The generator steps every grid by a
# fixed pitch, so the positions fall into a few rectangular blocks -- that
# 7654-via board is 22 of them -- and AEDT can lay a block out itself:
# build one copy, then DuplicateAlongLine along each side of the block
# with "attach to original", which puts the clones into the SAME body.
# A block of any size costs one copy and two calls, and the model never
# holds more than a handful of bodies.


@dataclass(frozen=True)
class Block:
    """A rectangular array of positions on the board.

    `count` positions `step` apart make a row, and the row repeats `rows`
    times, `row_step` apart. A lone position is a 1 x 1 block.
    """
    u: float
    v: float
    step: Tuple[float, float] = (0.0, 0.0)
    count: int = 1
    row_step: Tuple[float, float] = (0.0, 0.0)
    rows: int = 1

    def __len__(self) -> int:
        return self.count * self.rows

    def points(self) -> List[Tuple[float, float]]:
        return [
            (self.u + i * self.step[0] + j * self.row_step[0],
             self.v + i * self.step[1] + j * self.row_step[1])
            for j in range(self.rows)
            for i in range(self.count)
        ]


def blocks(points: Sequence[Tuple[float, float]]) -> List[Block]:
    """Cover `points` with as few blocks as the layout allows.

    Every point lands in exactly one block, within a few COINCIDENT_MM of
    where it was: a block puts its copies on a perfect lattice, and
    Altium's coordinate quantum leaves the real ones nanometres off it.
    At worst five along a row -- one for the row's own fit, and two each
    for its start and its length against the row it was stacked with --
    and two across it, so under 6 * COINCIDENT_MM (0.6 um) in all.
    Rows are tried both ways round, along u and along v, and whichever
    needs fewer blocks wins. Points with no pattern at all still come out
    right, one block each -- no worse than building them one at a time.
    """
    points = [(float(u), float(v)) for u, v in points]
    if not points:
        return []
    return min((_blocks_along(points, axis) for axis in (0, 1)), key=len)


def _blocks_along(points: List[Tuple[float, float]], along: int) -> List[Block]:
    """`blocks`, with the rows running along axis `along` (0 = u, 1 = v)."""
    across = 1 - along

    def pair(a: float, c: float) -> Tuple[float, float]:
        return (a, c) if along == 0 else (c, a)

    # Rows: each line of points, split into evenly spaced runs.
    runs = []  # (points in order along the row, step)
    for line in _lines(points, across):
        line.sort(key=lambda p: p[along])
        for first, count, step in _even_runs([p[along] for p in line]):
            runs.append((line[first:first + count], step))

    # Runs that are the same row moved sideways stack into one block. The
    # bucket only narrows the search; the test that decides is the one
    # below, which keeps every point within two COINCIDENT_MM of its copy.
    stacks: Dict[Tuple[int, int, int], List[list]] = {}
    for row, step in runs:
        start, span = row[0][along], step * (len(row) - 1)
        key = (len(row), round(start / (10 * COINCIDENT_MM)), round(span / (10 * COINCIDENT_MM)))
        for stack in stacks.setdefault(key, []):
            first_row, first_step = stack[0]
            if (abs(first_row[0][along] - start) <= COINCIDENT_MM
                    and abs(first_step * (len(row) - 1) - span) <= COINCIDENT_MM):
                stack.append((row, step))
                break
        else:
            stacks[key].append([(row, step)])

    out = []
    for stack in (s for bucket in stacks.values() for s in bucket):
        stack.sort(key=lambda r: r[0][0][across])
        for first, rows, row_step in _even_runs([r[0][0][across] for r in stack]):
            row, step = stack[first]
            out.append(Block(
                u=row[0][0], v=row[0][1],
                step=pair(step, 0.0), count=len(row),
                row_step=pair(0.0, row_step), rows=rows,
            ))
    return out


def _lines(points, across: int) -> List[List[Tuple[float, float]]]:
    """`points` gathered into lines of equal `across` coordinate, where
    equal means within COINCIDENT_MM of the first point on the line."""
    lines: List[List[Tuple[float, float]]] = []
    for point in sorted(points, key=lambda p: p[across]):
        if lines and point[across] - lines[-1][0][across] <= COINCIDENT_MM:
            lines[-1].append(point)
        else:
            lines.append([point])
    return lines


def _even_runs(values: Sequence[float]) -> List[Tuple[int, int, float]]:
    """Ascending `values`, split greedily into evenly spaced runs.

    Returns (first index, count, step) for each run. Every value in a run
    is within COINCIDENT_MM of `values[first] + k * step`: each new value
    narrows the range of steps that still fits all of them, and the run
    ends when none would -- so a gentle drift cannot creep in a little at
    a time. Two values closer than COINCIDENT_MM are the same value, and
    never become a step of their own.
    """
    runs = []
    i = 0
    while i < len(values):
        low, high = -math.inf, math.inf
        j = i + 1
        while j < len(values):
            gap, k = values[j] - values[i], j - i
            if k == 1 and gap <= COINCIDENT_MM:
                break
            low_k = max(low, (gap - COINCIDENT_MM) / k)
            high_k = min(high, (gap + COINCIDENT_MM) / k)
            if low_k > high_k:
                break
            low, high = low_k, high_k
            j += 1
        count = j - i
        step = 0.0
        if count > 1:
            step = min(max((values[j - 1] - values[i]) / (count - 1), low), high)
        runs.append((i, count, step))
        i = j
    return runs


def _replicate(modeler, frame: Frame, points: Sequence[Tuple[float, float]],
               make: Callable[[float, float], str], log=logger.info) -> List[str]:
    """Build `make(u, v)` at every one of `points`, a block at a time.

    `make` builds one copy anchored at board position (u, v) and returns
    its body's name. Each block's first copy is built by `make`, and AEDT
    clones it along the block's two sides, attached to the original -- so
    the clones join its body instead of each becoming a body of its own.
    Returns one body per block, carrying every copy in it.
    """
    bodies = []
    for block in blocks(points):
        body = make(block.u, block.v)
        try:
            for step, clones in ((block.step, block.count), (block.row_step, block.rows)):
                if clones > 1 and not modeler.duplicate_along_line(
                        body, frame.vector(*step), clones=clones,
                        attach=True, duplicate_assignment=False):
                    raise RuntimeError("DuplicateAlongLine reported failure")
        except Exception as exc:
            # Slow and right beats fast and wrong: a block without its
            # clones is a patch of missing vias nothing downstream sees.
            log(f"WARNING: AEDT would not clone a block of {len(block)} ({exc}); "
                "building them one at a time instead.")
            modeler.delete(body)
            bodies.extend(make(u, v) for u, v in block.points())
            continue
        bodies.append(body)
    return bodies


def _positions_by(items, shape) -> Dict[tuple, List[Tuple[float, float]]]:
    """Where each shape of feature sits: `shape(item)` -> [(x, y), ...].

    Only features of one shape can be copies of each other; `blocks`
    then finds the pattern in where they are.
    """
    grouped: Dict[tuple, List[Tuple[float, float]]] = {}
    for item in items:
        grouped.setdefault(shape(item), []).append((item.x, item.y))
    return grouped


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


# A bond does not balance the wire on the pad. The tool mashes the end
# of it out into a foot -- a ball bond's flattened ball, a wedge bond's
# heel -- wider than the wire it came from and pressed onto the metal,
# with the wire rising out of it. Both constants are in wire diameters,
# which is how a bonder's setup sheet gives them.
FOOT_WIDTH = 2.0
FOOT_MARGIN = 0.125


def foot_of(diameter: float) -> Tuple[float, float]:
    """The (radius, height) of the foot carrying a wire of `diameter`.

    Sized by one rule: a sphere of the WIRE's own radius, centred at the
    foot's centre, must fit inside the foot with FOOT_MARGIN to spare.

    That rule is the whole point. `build_bond_wires` ends the arc at the
    foot's centre, and the arc's end cap is a disc of one wire radius
    about that point -- so every point of it is exactly a radius from
    the centre, whichever way the cap is tilted. Fit the sphere and the
    cap is buried for ANY approach angle, with no dependence on the loop
    height, the span, or where the two pads sit relative to each other.

    Getting this wrong is not subtle but it is easy to miss in a tree:
    end the arc at the foot's TOP face instead, as this first did, and
    half the cap stands proud of the foot. That shows up as a flat, cut
    end of wire poking out of the bond -- and where the exposed cap runs
    into the foot's top face it leaves a thin wedge, which is a sliver
    for the mesher and the kind of feature Ansys asks be kept above
    0.001 model units.
    """
    half = diameter * (0.5 + FOOT_MARGIN)
    return diameter * FOOT_WIDTH / 2.0, 2.0 * half


def wire_path(bond, frame: Frame, pcb_top: float, chip_top: float,
              height: float, segments: int = 12) -> List[List[float]]:
    """Points along one bond loop, in assembly coordinates.

    A real bond leaves the pad steeply, arcs over and comes down on the
    die; the loop is what keeps it clear of the die edge. A half sine
    gives that shape with the apex `height` above the ends, and needs no
    special-casing when the two ends sit at different heights -- which
    they do not, now the die is let into the board flush.

    `pcb_top`/`chip_top` are where the wire's CENTRELINE starts and
    ends, which the caller sets at the centre of each foot -- half a
    foot height above the pad -- so that both ends of the arc finish
    buried. See `foot_of` for why the centre and not the top face.
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
    """Create every bond wire as a swept circular profile on two feet.

    `pcb_top`/`chip_top` are the height of the COPPER surface a wire
    lands on -- the caller passes substrate-top-face-plus-plating, not
    the bare substrate.

    Each end gets a FOOT: a short cylinder of conductor standing on the
    pad, with the arc's end buried in it. This is not decoration. HFSS
    joins conductors through shared mesh, which wants either overlapping
    volume or a coincident face, and the arc on its own offers neither:
    run at pad height it sinks half its cross-section into the pad and
    the substrate under it, and raised clear by a radius -- which is what
    this did before -- it only ever touches the pad along a line of zero
    width. Ansys names "imperfect tangential contacts" as a cause of
    mesh failure outright, and asks that a contact between objects be at
    least 0.001 model units across; a tangent is zero. When it does mesh
    the result is worse than a failure, because the model still solves:
    either the bond comes out open, which for the signal collapses S21
    and for the ground quietly unreferences the die, or the current
    crowds through a near-singular contact and the adaptive passes chase
    that instead of the interconnect.

    Sinking the wire into the pad instead would work on the board, where
    the copper is 35 um of a 25 um wire's diameter, and not on the die,
    whose metal is 200 NANOmetres -- a wire buried half a diameter there
    is a wire buried 12 um in silicon. The foot avoids the choice: it
    stands ON the metal, so its underside is a coincident face on either
    side, and at FOOT_WIDTH diameters across it lands some 2000 square
    microns of contact rather than a line.

    The arc ends at the foot's CENTRE, which is what buries its cut end
    rather than leaving it standing proud -- `foot_of` has the rule and
    what it looks like when it is wrong.

    They are united into one body -- feet and wires together: the model
    wants a handful of named solids, not one per wire, and the material
    rules match on the name. The union is only with itself; the join to
    the pad is the coincident face, which is why nothing here has to
    know which copper body it came down on.

    Bonds that span the same way are one wire moved sideways -- a row of
    ground bonds down one side of the die -- so like the vias they are
    built a block at a time (see `blocks`): one bond, cloned along the row.
    """
    foot_radius, foot_height = foot_of(diameter)
    bury = foot_height / 2.0

    def bond_body(bond) -> str:
        # The wire first: unite takes the name of the first body in its
        # list, so every body this makes carries a wire's name.
        parts = [modeler.create_polyline(
            points=wire_path(bond, frame,
                             pcb_top + bury, chip_top + bury, height),
            xsection_type="Circle",
            xsection_width=diameter,
        ).name]
        for x, y, pad_top in ((bond.pcb_x, bond.pcb_y, pcb_top),
                              (bond.chip_x, bond.chip_y, chip_top)):
            parts.append(modeler.create_cylinder(
                orientation="Y",
                origin=frame.point(x, y, pad_top),
                radius=foot_radius,
                height=foot_height,
            ).name)
        _unite_or_warn(modeler, parts, "a bond wire and its feet", log)
        return parts[0]

    # Grouped by span: the vector from the board end to the die end. Only
    # bonds with the same one are the same wire.
    spans: Dict[Tuple[float, float], list] = {}
    for bond in bond_list:
        span = (round(bond.chip_x - bond.pcb_x, 9), round(bond.chip_y - bond.pcb_y, 9))
        spans.setdefault(span, []).append(bond)

    made = []
    for group in spans.values():
        first = group[0]
        du, dv = first.chip_x - first.pcb_x, first.chip_y - first.pcb_y

        def bond_at(u, v, first=first, du=du, dv=dv):
            return bond_body(replace(first, pcb_x=u, pcb_y=v, chip_x=u + du, chip_y=v + dv))

        made.extend(_replicate(modeler, frame, [(b.pcb_x, b.pcb_y) for b in group],
                               bond_at, log=log))

    # Feet that touch. The two signal wires at each end of the line land
    # side by side on pads that tile the trace, and at FOOT_WIDTH their
    # feet meet along a single line -- the tangent contact described
    # above, and one the modeller cannot cut: carving the vacuum around
    # it leaves no vacuum at all ("Null body found for part vacuum"). Two
    # ball bonds that touch are one blob on the bench, so the waist
    # between them is filled in.
    bridges = _bridge_touching_feet(modeler, bond_list, frame, pcb_top, chip_top,
                                    foot_radius, foot_height, log)
    made.extend(bridges)

    if not made:
        return []
    if len(made) > 1:
        _unite_or_warn(modeler, made, f"the {len(made)} pieces of {name!r}", log)
    modeler[made[0]].name = name

    wires = len(bond_list)
    signal = sum(1 for b in bond_list if b.kind == "signal")
    log(f"Built {wires} bond wires on {2 * wires} feet as {name!r} "
        f"({signal} signal, {wires - signal} ground"
        + (f"; {len(bridges)} pairs of touching feet bridged)" if bridges else ")"))
    return [name]


# How wide the bridge between two touching feet is, as a fraction of a
# foot's width. Narrower than the feet, so the bridge's sides cut into
# both of them: a bridge exactly as wide would only trade the one
# tangent contact between the feet for two between bridge and feet.
FOOT_BRIDGE = 0.8


def _bridge_touching_feet(modeler, bond_list, frame: Frame, pcb_top: float,
                          chip_top: float, radius: float, height: float,
                          log=logger.info) -> List[str]:
    """Fill the waist between every two feet that touch or overlap.

    Only between feet of the same kind. A signal foot touching a ground
    one is a short in the layout itself, and bridging it would build that
    short in solid metal instead of saying so.
    """
    bridges = []
    for end, pad_top in (("pcb", pcb_top), ("chip", chip_top)):
        feet = [((getattr(b, f"{end}_x"), getattr(b, f"{end}_y")), b.kind) for b in bond_list]
        for i, (a, kind_a) in enumerate(feet):
            for b, kind_b in feet[i + 1:]:
                apart = math.dist(a, b)
                if apart <= COINCIDENT_MM or apart > 2.0 * radius + COINCIDENT_MM:
                    continue
                if kind_a != kind_b:
                    log(f"WARNING: a {kind_a} and a {kind_b} bond foot touch on the {end} "
                        f"at {a} and {b} -- a short in the layout. Not bridged.")
                    continue
                # Unit vector along the bridge, and its half-width across.
                tu, tv = (b[0] - a[0]) / apart, (b[1] - a[1]) / apart
                nu, nv = -tv * radius * FOOT_BRIDGE, tu * radius * FOOT_BRIDGE
                corners = [(a[0] + nu, a[1] + nv), (b[0] + nu, b[1] + nv),
                           (b[0] - nu, b[1] - nv), (a[0] - nu, a[1] - nv)]
                bridges.append(_extrude_polygon(modeler, corners, pad_top, height, frame))
    return bridges


def clear_cavity_area(
    modeler,
    frame: Frame,
    stack: Stack,
    cavity: Cavity,
    targets: Sequence[str],
    log=logger.info,
) -> None:
    """Cut the die's opening out of the board's copper.

    The generator already keeps copper out of the opening -- the trace
    stops either side of it and both ground planes are drawn round it --
    so on a board built by the current generator this cuts nothing. It
    stays because it costs one boolean and it is the only thing standing
    between a copper.txt written by an older generator and a model with
    a ground plane spanning the hole in its own board. A stray conductor
    across the cavity is exactly the sort of thing that moves a mode,
    and it would not look wrong in a tree.

    The cut spans the full stack plus a copper thickness at each end, so
    it takes the top and bottom layers together whether the opening is
    blind or through. In plane it is the opening exactly, with no added
    margin: the opening already stands `cavity_clearance` clear of the
    die's own copper, so there is no coincident face here to work around
    -- which is what the margin that used to be here was for, back when
    this cut was made at the die's own edge.
    """
    if not targets:
        return

    margin = max(stack.copper_thickness, stack.chip_metal_thickness) * 2.0
    origin, sizes = frame.box(
        -cavity.half_length, -cavity.half_side,
        cavity.half_length, cavity.half_side,
        stack.pcb_bottom - margin,
        (stack.pcb_top - stack.pcb_bottom) + 2 * margin,
    )
    tool = modeler.create_box(origin=origin, sizes=sizes)
    # Named rather than left as NewObject_*, so an operation that fails
    # to consume it is obvious in the tree instead of anonymous.
    tool.name = "cavity_area_cut"
    modeler.subtract(list(targets), ["cavity_area_cut"], keep_originals=False)
    log(f"Cleared the die's opening out of {', '.join(targets)}.")
