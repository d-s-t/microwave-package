"""
The opening in the board the die drops into: how big it is, how deep it
goes, and whether it goes all the way through.

It is DRAWN IN ALTIUM -- a Board Cutout region where it goes through,
an embedded Cavity region carrying its own depth where it does not --
and rebuilt in HFSS as the volume taken out of the board. Both sides ask
this module for the numbers rather than working them out for themselves,
for the same reason `bonds` exists: two independent derivations of one
feature drift apart the first time a parameter moves, and a pocket
0.1 mm bigger in the model than on the board is not something a tree
view shows you.

Three things are decided here.

**The wiggle room.** The opening is cut `cavity_clearance` larger than
the die on every side. A die and a milled pocket of exactly equal size
do not assemble: the router works to a few tens of microns either way,
and an undersized pocket is scrap. This is a swept parameter rather than
a drafting constant because the ring of air it leaves around the die
sits inside the cavity, where the fields are -- so it belongs in the
equations file, where it can be swept and is recorded with the run.

**Depth gets no matching allowance, on purpose.** Cutting deeper does
not help the die fit; it drops the die below the board's top face and
lengthens every bond wire, which is a change to the electrical geometry
rather than a fabrication margin. The pocket is cut to the die's
thickness and the two top faces finish flush -- the arrangement
`copper_geometry.Stack` is built around.

**Through or blind is decided here, not in the DelphiScript**, so there
is one rule and the script only draws. The rule is not
`chip_width >= pcb_width`: a pocket that would leave a sliver of
laminate under the die cannot be milled at all, and with the shipped
defaults the two thicknesses are the same number, so an exact
comparison sits right on the knife edge. See MIN_FLOOR.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from . import parameter_meta

# Per-side clearance between the die and the opening, used when the
# equations file does not carry one -- an older run's dimensions.txt,
# or a caller working from a partial parameter set.
DEFAULT_CLEARANCE = 0.1

# The thinnest floor worth leaving under a blind pocket. Below this the
# fabricator is depth-milling to within a few tens of microns of
# breaking through, which is not a pocket anyone will quote on -- so the
# opening becomes a through cutout instead.
#
# It is a fabrication rule rather than a design choice, which is why it
# is a constant here and not a parameter in the equations file: nobody
# sweeps it, and the run is not more reproducible for recording it.
MIN_FLOOR = 0.1


@dataclass(frozen=True)
class Cavity:
    """The opening, in board coordinates: half-extents about the board
    centre, and how far down from the board's top face it is cut.

    `through` is not `depth >= pcb_width` restated -- it is the decision
    itself, made once in `plan` and carried, so the DelphiScript and the
    HFSS builder cannot answer it differently.
    """
    half_length: float          # along the feedline
    half_side: float            # across it
    depth: float                # down from the board's top face
    through: bool

    @property
    def length(self) -> float:
        return 2.0 * self.half_length

    @property
    def side(self) -> float:
        return 2.0 * self.half_side

    @property
    def kind(self) -> str:
        """How it reads in a log line or a report."""
        return "through cutout" if self.through else "blind pocket"

    @property
    def is_empty(self) -> bool:
        """True for the bare board (`no_chip`), which has no opening at
        all: nothing is cut, and every consumer leaves its copper whole."""
        return self.half_length <= 0.0 or self.half_side <= 0.0


def plan(values: Dict[str, float]) -> Cavity:
    """The opening for one combination's equation-file values.

    With `no_chip` set there is no die to drop in, so there is no opening
    either: an empty `Cavity`, which Altium reads as "no opening" (a
    zero-size one) and the HFSS builder skips.
    """
    if parameter_meta.is_bare_board(values):
        return Cavity(half_length=0.0, half_side=0.0, depth=0.0, through=False)

    clearance = values.get("cavity_clearance", DEFAULT_CLEARANCE)
    chip_length = values.get("chip_length", 6.3)
    chip_side = values.get("chip_side", 6.3)
    chip_width = values.get("chip_width", 0.381)
    pcb_width = values.get("pcb_width", 0.381)

    through = (pcb_width - chip_width) < MIN_FLOOR
    return Cavity(
        half_length=chip_length / 2.0 + clearance,
        half_side=chip_side / 2.0 + clearance,
        # A through cutout is cut the whole way; a pocket stops at the
        # die's own thickness so the two top faces finish flush.
        depth=pcb_width if through else chip_width,
        through=through,
    )


def as_parameters(cavity: Cavity) -> Dict[str, float]:
    """The opening as parameter-file entries for the DelphiScript.

    Full sizes rather than half-extents, matching how `chip_length` and
    `chip_side` already cross, and the through/blind decision as a plain
    flag so the script draws what it is told instead of re-deriving it
    from a thickness comparison of its own.
    """
    return {
        "cavity_length": cavity.length,
        "cavity_side": cavity.side,
        "cavity_depth": cavity.depth,
        "cavity_through": 1.0 if cavity.through else 0.0,
    }
