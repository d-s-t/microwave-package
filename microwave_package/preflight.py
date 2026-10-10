"""
Pre-manufacturing checks that need no solver: run them for a parameter set,
get a report.

    python -m microwave_package.preflight --candidate
    python -m microwave_package.preflight --set cpw_trace_width=0.4 --mat-pcb "Rogers RT/duroid 5880 (tm)"
    python -m microwave_package.preflight --candidate --out preflight_report.md

It evaluates what the tool CAN check by itself -- the layout rules, the analytic
CPW gap and what it leaves out, the fabricator's limits, the screw fits, the
cryogenic materials risk -- and lists, with how to run each, what it CANNOT
(the simulations, the datasheet comparisons, the measurements). The list
beside each result is `PRE_MANUFACTURING.md`'s checklist in executable form: a
`pass` here is necessary, not sufficient, for ordering a board.

Statuses: `pass`, `fail` (fix before ordering), `warn` (read it, decide),
`manual` (a person or a simulation has to do it), `info`.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from . import paths
from .design import constraints, cpw, equations as eq
from .design import screws as screw_plan

PASS, FAIL, WARN, MANUAL, INFO = "pass", "fail", "warn", "manual", "info"

# The board the box study ended on (RESULTS_CONCLUSIONS.md, "the candidate"):
# everything else is default.txt's.
CANDIDATE_VALUES = {"cpw_trace_width": 0.4, "route_width": 1.0, "route_clearence": 0.2}
CANDIDATE_PCB = "Rogers RT/duroid 5880 (tm)"

# What a typical PCB fabricator can do (mm). A specific fab's numbers replace
# these with --fab name=value; a check against the wrong fab is worse than none,
# which is why they are listed in the report and not buried.
DEFAULT_FAB = {
    "min_trace": 0.1,          # narrowest etched line
    "min_gap": 0.1,            # narrowest etched space (also the tool's own limit)
    "min_drill": 0.15,         # smallest finished via hole
    "min_annular_ring": 0.075,  # copper left round a via hole, per side
    "max_aspect_ratio": 8.0,   # board thickness / drill, for reliable plating
    "min_pad": 0.1,
}

# The finished via hole the generator draws (copper.txt: `via|x|y|diameter|hole`).
VIA_HOLE = 0.15

# Out-of-plane (z) coefficient of thermal expansion, ppm/degC, from the
# manufacturers' data sheets. Copper is 17. A laminate far above that works
# the plated via barrels every thermal cycle; at 4 K cycling is the use.
CTE_Z_PPM = {
    "Rogers TMM 10i (tm)": 16.0,
    "Rogers RT/duroid 5880 (tm)": 237.0,
    "Rogers RT/duroid 6010/6010LM (tm)": 24.0,
    "Rogers RO3003 (tm)": 17.0,
    "Rogers RO4003 (tm)": 46.0,
    "FR4_epoxy": 70.0,
    "sapphire": 6.0,
}
CTE_Z_RISK_PPM = 50.0


@dataclass
class Check:
    id: str
    group: str
    title: str
    status: str
    detail: str = ""
    how: str = ""

    @property
    def automatic(self) -> bool:
        return self.status != MANUAL


def _values(overrides: Optional[Dict[str, float]] = None) -> Dict[str, float]:
    base = eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))
    return {**base, **(overrides or {})}


def _safely(check_id: str, group: str, title: str, fn: Callable[[], List[Check]]) -> List[Check]:
    try:
        return fn()
    except Exception as exc:                       # a broken check must not hide the others
        return [Check(check_id, group, title, WARN, f"could not be evaluated: {exc}")]


# ---------------------------------------------------------------------------
# the automatic checks
# ---------------------------------------------------------------------------

def layout_checks(values, mat_pcb) -> List[Check]:
    how = "automatic: constraints.check_combination / check_via_grid / check_layout (also run by every run)"
    out = []
    for check_id, title, reason in (
        ("layout.fit", "The geometry fits (box, board, connector, chip, bonds)",
         constraints.check_combination(values)),
        ("layout.vias", "The via lattice is dense enough to be placed", constraints.check_via_grid(values)),
        ("layout.build", "The layout can be drawn (gap solves, fence fits, lid clears the trace, screws fit)",
         constraints.check_layout(values, mat_pcb)),
    ):
        out.append(Check(check_id, "Layout", title, FAIL if reason else PASS, reason or "", how))
    return out


def cpw_checks(values, mat_pcb, mat_chip, has_chip) -> List[Check]:
    out = []
    trace = values["cpw_trace_width"]
    eps = cpw.permittivity_of(mat_pcb)
    h = values.get("pcb_width", 0.381)
    metal = values.get("copper_thickness", 0.035)
    gap = cpw.solve_gap_for_z0(trace_width=trace, substrate_height=h, eps_r=eps, conductor_thickness=metal)
    z_back = cpw.z0_gcpw(trace, gap, h, eps, metal)
    out.append(Check(
        "cpw.gap", "CPW", f"Board line: analytic 50 ohm gap on {mat_pcb} (eps {eps:g})",
        PASS if gap >= constraints.MIN_CPW_GAP else FAIL,
        f"trace {trace:g} mm, gap {gap * 1000:.0f} um (fab minimum {constraints.MIN_CPW_GAP * 1000:.0f} um); "
        f"the formula read back gives {z_back:.2f} ohm",
        "automatic; the independent check is the HFSS 'Line impedance' solution type"))
    route_width = values.get("route_width", 0.0)
    want = trace + 2.0 * gap + 0.2
    out.append(Check(
        "cpw.lid", "CPW", "The lid's relief channel clears the whole CPW (trace + two gaps + margin)",
        PASS if route_width >= want else WARN,
        f"route_width {route_width:g} mm against trace + 2 gap + 0.2 = {want:.3f} mm; relief depth "
        f"{values.get('route_clearence', 0.0) * 1000:g} um. The analytic gap does NOT see the lid "
        f"(a lid 40 um over the trace made a 5880 board unusable until the channel was widened)",
        "automatic for the margin; the real effect needs the HFSS 'Line impedance' check, lid included"))
    if has_chip:
        try:
            chip_gap = cpw.solve_gap_for_z0(
                trace_width=trace, substrate_height=values.get("chip_width", 0.381),
                eps_r=cpw.permittivity_of(mat_chip), conductor_thickness=values.get("chip_metal_thickness", 0.0))
            out.append(Check("cpw.chip_gap", "CPW", f"Chip line: analytic gap on {mat_chip}",
                             PASS if chip_gap < 5.0 else WARN,
                             f"trace {trace:g} mm gap {chip_gap:.3f} mm. The die's line is on high-permittivity "
                             "silicon with no ground below; a gap this wide or an unreachable 50 ohm means the "
                             "chip's trace should not be the board's", "automatic; confirm in HFSS (which_line = chip)"))
        except ValueError as exc:
            out.append(Check("cpw.chip_gap", "CPW", f"Chip line: analytic gap on {mat_chip}", FAIL, str(exc)))
    return out


def fab_checks(values, fab) -> List[Check]:
    how = "automatic against the fab limits listed under 'Fabricator limits used'"
    trace = values["cpw_trace_width"]
    out = [Check("fab.trace", "Fabrication", "Narrowest line", PASS if trace >= fab["min_trace"] else FAIL,
                 f"cpw_trace_width {trace * 1000:.0f} um against {fab['min_trace'] * 1000:.0f} um", how)]
    via = values.get("via_diameter", 0.3)
    ring = (via - VIA_HOLE) / 2.0
    out.append(Check("fab.drill", "Fabrication", "Via hole size and aspect ratio",
                     PASS if VIA_HOLE >= fab["min_drill"] and values.get("pcb_width", 0.381) / VIA_HOLE <= fab["max_aspect_ratio"] else FAIL,
                     f"hole {VIA_HOLE * 1000:.0f} um (min {fab['min_drill'] * 1000:.0f}), board "
                     f"{values.get('pcb_width', 0.381) * 1000:.0f} um thick: aspect ratio "
                     f"{values.get('pcb_width', 0.381) / VIA_HOLE:.1f}:1 (max {fab['max_aspect_ratio']:g}:1)", how))
    out.append(Check("fab.ring", "Fabrication", "Annular ring of the vias", PASS if ring >= fab["min_annular_ring"] else WARN,
                     f"via {via * 1000:.0f} um over a {VIA_HOLE * 1000:.0f} um hole leaves {ring * 1000:.0f} um "
                     f"per side (fab minimum {fab['min_annular_ring'] * 1000:.0f} um)", how))
    out.append(Check("fab.spacing", "Fabrication", "Via spacing and clearance to copper",
                     PASS if values.get("via_min_spacing", 0.2) >= fab["min_gap"] else WARN,
                     f"via_min_spacing {values.get('via_min_spacing', 0.2) * 1000:.0f} um against the "
                     f"{fab['min_gap'] * 1000:.0f} um minimum space", how))
    pad = values.get("bond_pad_width", 0.15)
    out.append(Check("fab.bondpad", "Fabrication", "Smallest bond pad", PASS if pad >= fab["min_pad"] else WARN,
                     f"bond_pad_width {pad * 1000:.0f} um (min {fab['min_pad'] * 1000:.0f} um)", how))
    return out


def mechanical_checks(values) -> List[Check]:
    if not screw_plan.enabled(values):
        return [Check("mech.screws", "Mechanical", "Corner screws", INFO, "screws_on is 0: no screw checks")]
    plan = screw_plan.plan(values)
    how = "automatic: design/screws.py rules"
    out = []
    seat = values.get("lid_width", 1.5)
    out.append(Check("mech.countersink", "Mechanical", "The lid is thick enough for the countersunk head",
                     PASS if seat >= plan.min_lid_thickness else FAIL,
                     f"lid_width {seat:g} mm against head {screw_plan.HEAD_HEIGHT:g} + seat "
                     f"{screw_plan.MIN_LID_SEAT:g} = {plan.min_lid_thickness:g} mm", how))
    engagement = values.get("screw_engagement", plan.engagement)
    out.append(Check("mech.thread", "Mechanical", "Thread depth in the box floor",
                     PASS if engagement >= 1.5 * screw_plan.SCREW_DIAMETER else
                     (WARN if engagement >= screw_plan.MIN_ENGAGEMENT else FAIL),
                     f"screw_engagement {engagement:g} mm = {engagement / screw_plan.SCREW_DIAMETER:.2f} x "
                     f"the {screw_plan.SCREW_DIAMETER:g} mm screw (1.5x is the usual minimum for steel in aluminium)",
                     how))
    out.append(Check("mech.holes", "Mechanical", "Screw holes: clearance in lid and PCB, tap drill in the box",
                     PASS if plan.clearance_diameter > screw_plan.SCREW_DIAMETER > plan.tap_diameter else FAIL,
                     f"clearance {plan.clearance_diameter:g} mm, M2, tap drill {plan.tap_diameter:g} mm "
                     "(the same four positions are in the lid, the PCB and the box)", how))
    return out


def cryo_checks(mat_pcb, mat_chip, values) -> List[Check]:
    z = CTE_Z_PPM.get(mat_pcb)
    out = []
    if z is None:
        out.append(Check("cryo.cte", "Cryogenic", f"z-axis CTE of {mat_pcb}", WARN,
                         "not in the tool's table; look it up and compare with copper (17 ppm/degC)"))
    else:
        out.append(Check(
            "cryo.cte", "Cryogenic", f"z-axis CTE of {mat_pcb} against copper",
            PASS if z <= CTE_Z_RISK_PPM else WARN,
            f"{z:g} ppm/degC against copper's 17"
            + ("" if z <= CTE_Z_RISK_PPM else
               ": a laminate this far from copper works the plated via barrels every thermal cycle, and "
               "cycling to 4 K is the use. Ask the fab for cryogenic via reliability data, or use a "
               "copper-matched laminate (RO3003, TMM)"),
            "automatic table; the answer is a fab and test question"))
    out.append(Check("cryo.box", "Cryogenic", "The box is aluminium, superconducting below about 1.2 K", INFO,
                     "perfect E is the default wall model: it places modes and dips right and exaggerates their "
                     "depth; confirm with a finite-conductivity run (box_conductivity)"))
    return out


# ---------------------------------------------------------------------------
# what only a simulation, a datasheet or a person can do
# ---------------------------------------------------------------------------

MANUAL_CHECKS = [
    ("sim.line", "Simulation", "HFSS 50 ohm line check, board and chip",
     "Run the 'Line impedance' solution type with which_line = board, and = chip if the board has a die. "
     "Pass: Zo within 50 +- 2.5 ohm at every frequency in the band; compare its gap with the analytic one."),
    ("sim.driven", "Simulation", "Driven S21 / S11 of the final board WITH the chip",
     "Driven Modal, connector reference plane, the whole band, with the chip. Pass: the band the design needs "
     "above the S21 floor, no unexplained notch; read S11 as well as S21."),
    ("sim.eigen", "Simulation", "Eigenmodes cover the band",
     "Eigenmode run with the search start (min_freq_safety_factor) and the number of modes reaching past the top "
     "of the band; every S21 notch should match a mode within a few tens of MHz or be explained."),
    ("sim.finite", "Simulation", "Finite-conductivity box run",
     "Uncheck 'Perfect E on box/lid_inside' and set box_conductivity (about 1e8 S/m for 6061 at helium "
     "temperature). Pass: the band and the notches survive the loss."),
    ("sim.fine", "Simulation", "Fine windows around every mode",
     "Driven Modal with Both: the fine windows are automatic. Pass: no notch deeper than the spec between the "
     "windows' ends, on a grid fine enough to see a mode of Q 10000 (its width is f/Q)."),
    ("sim.tolerance", "Simulation", "Tolerance runs",
     "Sweep cpw_trace_width, the solved gap, the permittivity (a few percent), route_clearence and "
     "route_width about the candidate. Pass: the band holds across the fab's tolerance."),
    ("dfm.smp", "Manufacturing", "SMP footprint against the connector datasheet",
     "Compare the land pattern the tool uses (design/smp.py, measured from the vendor library) with the "
     "current datasheet drawing of the part number, in the fab's CAM."),
    ("dfm.finish", "Manufacturing", "Surface finish suitable for wire bonding",
     "Soft gold or ENEPIG on the bond pads; ENIG's nickel is a poor bonding surface."),
    ("dfm.vias", "Manufacturing", "Via fill or cap, and trapped air",
     "Vias are SOLID posts in the model but drilled holes on the board. Decide whether they are filled or capped, "
     "and how trapped air escapes (see Venting in PRE_MANUFACTURING.md)."),
    ("dfm.fits", "Manufacturing", "Mechanical fits",
     "Screw holes aligned across lid, PCB and box; thread depth; countersink depth; lid flatness (the lid "
     "must sit on the ground copper along the line, and nothing may hold it off it)."),
    ("cryo.cycle", "Cryogenic", "Clamping and contact through cooldown",
     "Differential contraction of aluminium box, laminate and screws: contact pressure between lid and "
     "ground copper, and thermalization of the board."),
    ("meas.rt", "Measurement", "Room temperature: VNA S-parameters and TDR",
     "Delivered board in the box: S11 / S21 against the simulation, and a TDR of the line (50 ohm along it)."),
    ("meas.cold", "Measurement", "Cold: the same S-parameters at base temperature",
     "Compare with the room-temperature set; modes move with the dielectric constant."),
]


def run(values: Optional[Dict[str, float]] = None, mat_pcb: str = "Rogers TMM 10i (tm)",
        mat_chip: str = "silicon", fab: Optional[Dict[str, float]] = None,
        has_chip: Optional[bool] = None) -> List[Check]:
    values = _values(values) if values is None or "pcb_length" not in values else dict(values)
    fab = {**DEFAULT_FAB, **(fab or {})}
    if has_chip is None:
        has_chip = float(values.get("no_chip", 0.0)) < 0.5
    checks: List[Check] = []
    checks += _safely("layout", "Layout", "Layout rules", lambda: layout_checks(values, mat_pcb))
    checks += _safely("cpw", "CPW", "CPW gap", lambda: cpw_checks(values, mat_pcb, mat_chip, has_chip))
    checks += _safely("fab", "Fabrication", "Fabrication limits", lambda: fab_checks(values, fab))
    checks += _safely("mech", "Mechanical", "Mechanical fits", lambda: mechanical_checks(values))
    checks += _safely("cryo", "Cryogenic", "Cryogenic materials", lambda: cryo_checks(mat_pcb, mat_chip, values))
    checks += [Check(i, g, t, MANUAL, d) for i, g, t, d in MANUAL_CHECKS]
    return checks


def summary(checks: List[Check]) -> Dict[str, int]:
    counts = {PASS: 0, FAIL: 0, WARN: 0, MANUAL: 0, INFO: 0}
    for c in checks:
        counts[c.status] += 1
    return counts


def render_markdown(checks: List[Check], values: Dict[str, float], mat_pcb: str,
                    fab: Optional[Dict[str, float]] = None, title: str = "Preflight report") -> str:
    counts = summary(checks)
    lines = [f"# {title}", "",
             f"{counts[FAIL]} fail, {counts[WARN]} warn, {counts[PASS]} pass, {counts[MANUAL]} manual "
             f"(not checkable by the tool), {counts[INFO]} info.", "",
             f"PCB material: {mat_pcb}. Values that differ from default.txt: "
             + (", ".join(f"{k} = {values[k]:g}" for k in sorted(values)
                          if k in _values() and abs(values[k] - _values()[k]) > 1e-12) or "none") + ".", ""]
    group = None
    for c in sorted(checks, key=lambda c: (c.group, c.status == MANUAL)):
        if c.group != group:
            if group is not None:
                lines.append("")
            group = c.group
            lines += [f"## {group}", ""]
        mark = {PASS: "PASS", FAIL: "**FAIL**", WARN: "warn", MANUAL: "manual", INFO: "info"}[c.status]
        lines.append(f"- [{mark}] **{c.title}** -- {c.detail}" + (f" _({c.how})_" if c.how else ""))
    lines += ["", "## Fabricator limits used", ""] + [f"- {k}: {v:g} mm" if k != "max_aspect_ratio"
                                                       else f"- {k}: {v:g}:1" for k, v in {**DEFAULT_FAB, **(fab or {})}.items()]
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the automatic pre-manufacturing checks for a parameter set")
    parser.add_argument("--candidate", action="store_true",
                        help="the box study's candidate: RT/duroid 5880, 0.4 mm trace, 1.0 mm lid channel, 200 um relief")
    parser.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                        help="override a default.txt parameter (repeatable), in the file's own unit (mm)")
    parser.add_argument("--mat-pcb", default=None, help="PCB material as the new-run form names it")
    parser.add_argument("--mat-chip", default="silicon")
    parser.add_argument("--fab", action="append", default=[], metavar="NAME=MM", help="a fabricator limit")
    parser.add_argument("--no-chip", action="store_true", help="a bare board")
    parser.add_argument("--out", default=None, help="write the report here as well as printing it")
    args = parser.parse_args(argv)

    overrides: Dict[str, float] = dict(CANDIDATE_VALUES) if args.candidate else {}
    for item in args.set:
        name, _, value = item.partition("=")
        overrides[name.strip()] = float(value)
    if args.no_chip:
        overrides["no_chip"] = 1.0
    mat_pcb = args.mat_pcb or (CANDIDATE_PCB if args.candidate else "Rogers TMM 10i (tm)")
    fab = {k.strip(): float(v) for k, _, v in (f.partition("=") for f in args.fab)}
    values = _values(overrides)
    checks = run(values, mat_pcb, args.mat_chip, fab)
    report = render_markdown(checks, values, mat_pcb, fab,
                             title="Preflight report" + (" -- the candidate board" if args.candidate else ""))
    print(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(report)
    return 1 if summary(checks)[FAIL] else 0


if __name__ == "__main__":
    sys.exit(main())
