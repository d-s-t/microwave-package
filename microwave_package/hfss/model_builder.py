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

import errno
import functools
import inspect
import itertools
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ansys_analyze_common import queue_common

from ..design import equations as eq
from ..workflow import queue_hold
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


def aedt_version_for(requested: Optional[str]) -> Optional[str]:
    """The version to hand PyAEDT: the run's own, or the newest installed.

    Never None while AEDT is installed. Given None, PyAEDT falls back to a
    setting shared by the whole process, which its own version check sets
    to the internal "2025.2SV" while a Desktop is starting -- and then
    rejects that very string ("Version argument is not valid"). A start
    that fails partway leaves it there, and every later call in the
    process fails the same way until the web app is restarted.
    """
    if requested:
        return requested
    versions = get_installed_versions()
    return versions[0] if versions else None


# One stretch of AEDT work at a time, across the whole process.
#
# The Student licence runs a single AEDT, and PyAEDT keeps one Desktop per
# process in module globals, so every run the web app drives at once --
# each in a pipeline thread of its own -- shares it without knowing. On
# 2026-09-29 two runs post-processed at the same moment: one thread was
# starting the Desktop, and the other, finding none registered yet, read
# PyAEDT's half-set version and failed ("Version argument is not valid").
# Had it got past that, it would have started a second AEDT, which the
# licence refuses. A model build and a post-processing each hold this
# for their whole length.
#
# That is one process only. The web app starts each run's pipeline as a
# child PROCESS, so two runs can each hold their own copy of AEDT_LOCK at
# once, and the licence still has one AEDT (2026-11-08: two model builds
# at 13:38 and two read-outs at 16:15, and one of each pair crashed). So
# `one_aedt_at_a_time` also holds an OS file lock that every process on
# this PC shares -- see `_acquire_file_lock`. The thread lock is taken
# first, the file lock second, and only by the outermost call in a process.
AEDT_LOCK = threading.RLock()

AEDT_LOCK_ENV = "MICROWAVE_AEDT_LOCK_PATH"
# The holder writes who it is into the first bytes of the file. The lock
# itself is a byte range far past that text: on Windows a byte range that
# msvcrt has locked cannot be read through another handle.
_AEDT_INFO_BYTES = 4096
_AEDT_LOCK_OFFSET = 1 << 20
_POLL_SECONDS = 0.5
_REPORT_EVERY_SECONDS = 60

# Only ever changed while AEDT_LOCK is held, so no thread-local is needed.
_aedt_depth = 0        # how many one_aedt_at_a_time calls this process is inside
_aedt_lock_fd = None   # the file lock they hold together, while _aedt_depth > 0


def aedt_lock_path() -> str:
    """The file every process on this PC locks around AEDT. Read at call
    time, so a test can point it somewhere else with AEDT_LOCK_ENV."""
    override = os.environ.get(AEDT_LOCK_ENV)
    if override:
        return override
    return str(Path.home() / ".microwave_package" / "aedt_session.lock")


def _try_file_lock(fd) -> bool:
    """Take the file lock without waiting. False when another process has it."""
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, _AEDT_LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EDEADLK):
                return False
            raise
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _holder_text(path: str) -> str:
    """Who holds the file lock, from the text the holder wrote. Empty or
    garbled text (the holder is between truncating and writing) is 'unknown'."""
    try:
        with open(path, "rb") as handle:
            info = json.loads(handle.read(_AEDT_INFO_BYTES).decode("utf-8") or "null")
        return f"held by pid {info['pid']} ({info['function']}) since {info['since']}"
    except (OSError, ValueError, KeyError, TypeError):
        return "held by unknown process"


def _acquire_file_lock(name: str, log: Callable[[str], None]) -> int:
    """Wait until no process holds the AEDT file lock, then take it.

    The OS frees the lock when its holder exits for any reason, so there is
    no stale file to clear by hand. There is no timeout: a build or a
    read-out can take an hour. Returns the open file descriptor, which
    must go to _release_file_lock."""
    path = aedt_lock_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if not _try_file_lock(fd):
            log(f"waiting for AEDT: {_holder_text(path)}")
            waiting_since = time.monotonic()
            reported_at = waiting_since
            while not _try_file_lock(fd):
                time.sleep(_POLL_SECONDS)
                if time.monotonic() - reported_at >= _REPORT_EVERY_SECONDS:
                    reported_at = time.monotonic()
                    log(f"still waiting for AEDT: {_holder_text(path)}")
            log(f"AEDT is free after {time.monotonic() - waiting_since:.0f} s; going ahead.")
        # Written only now that we hold the lock, and always from offset 0.
        info = json.dumps({"pid": os.getpid(), "function": name,
                           "since": time.strftime("%H:%M:%S")}).encode("utf-8")
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, info[:_AEDT_INFO_BYTES])
        return fd
    except BaseException:
        os.close(fd)  # closing the handle is what frees the lock
        raise


def _release_file_lock(fd: int) -> None:
    try:
        # Empty the text first, so a waiter never reads the last holder's name.
        os.ftruncate(fd, 0)
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, _AEDT_LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _log_of(fn, args, kwargs) -> Callable[[str], None]:
    """The call's own `log` argument if it has one (by keyword or position),
    else the module logger -- so a wait shows up in the run's log."""
    try:
        log = inspect.signature(fn).bind_partial(*args, **kwargs).arguments.get("log")
    except TypeError:
        log = None
    return log if callable(log) else logger.info


def one_aedt_at_a_time(fn):
    """Run `fn` holding AEDT_LOCK and the machine-wide AEDT file lock.

    Only the outermost such call in a process takes the file lock; a nested
    call (same thread, already inside) just runs, or it would wait on itself."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        global _aedt_depth, _aedt_lock_fd
        with AEDT_LOCK:
            if _aedt_depth == 0:
                _aedt_lock_fd = _acquire_file_lock(fn.__name__, _log_of(fn, args, kwargs))
            _aedt_depth += 1
            try:
                return fn(*args, **kwargs)
            finally:
                _aedt_depth -= 1
                if _aedt_depth == 0:
                    fd, _aedt_lock_fd = _aedt_lock_fd, None
                    _release_file_lock(fd)
    return wrapper


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
# Names the library lacks are made by MISSING_FROM_LIBRARY instead.
#
# The wirebond is deliberately not here: its choices are closed, not
# curated -- see WIREBOND_MATERIALS below.
MATERIAL_OPTIONS: Dict[str, List[str]] = {
    "mat_box": ["aluminum", "copper", "brass", "gold"],
    "mat_pcb": [
        "Rogers TMM 10i (tm)", "Rogers RT/duroid 5880 (tm)",
        # Both in the stock library under exactly these names. RO3003's
        # z-axis expansion (25 ppm/C, x/y 17/16) is near copper's, which
        # 5880's 237 ppm/C is not -- the reason it is here, for a board that
        # is cooled to millikelvin.
        "Rogers RO3003 (tm)", "Rogers RO4003 (tm)",
        "Rogers RT/duroid 6010/6010LM (tm)", "FR4_epoxy", "sapphire",
    ],
    "mat_chip": ["silicon", "sapphire", "gallium_arsenide", "SiC"],
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

# What the web UI calls each material/boundary setting, in the order the
# new-run form lists them -- for anything besides the form that has to
# name one, like the comparison page's list of what two runs differ in.
MATERIAL_LABELS: Dict[str, str] = {
    "mat_box": "Box material",
    "mat_pcb": "PCB material",
    "mat_chip": "Chip material",
    "mat_vacuum": "Vacuum material",
    "perfect_e_box": "Perfect E on box/lid_inside",
    "box_conductivity": "Box conductivity when not perfect E (S/m)",
    "mat_conductor_pcb": "PCB conductor",
    "mat_conductor_chip": "Chip conductor",
    "perfect_e_conductors": "Perfect E on the conductors",
    "mat_wirebond": "Wire bond material",
    "include_wirebonds": "Include wire bonds",
}


# The box and lid are aluminium, which is superconducting below ~1.2 K, so
# perfect E is the right default for them. Perfect E puts the mode and dip
# POSITIONS in the right place but exaggerates dip depth and Q: nothing is
# lost in the walls. With perfect E off, the box and lid_inside get a
# finite-conductivity boundary of this conductivity instead -- 6061 at
# liquid-helium temperature is of the order of 1e8 S/m (room temperature
# is 3.8e7), and a purer aluminium is higher still.
DEFAULT_BOX_CONDUCTIVITY = 1e8

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
    # The corner screws are steel in life; here they take the box's metal
    # and, with it, its perfect-E setting, so a perfect-E box has perfect-E
    # screws and the with/without comparison changes only the geometry.
    ("screw",           "screw",           "mat_box",             "perfect_e_box"),
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


# Set by build_model for a launch-type run that ignores its connectors
# (launch_connectors vacuum / dielectric): every connector body gets this
# material when it is placed, so the vacuum carve sees them as ordinary
# dielectrics and the later history replay stays sound.
CONNECTOR_NEUTRAL_MATERIAL: Optional[str] = None


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

        if CONNECTOR_NEUTRAL_MATERIAL:
            for body in list(conductor_bodies) + ([dielectric] if dielectric else []):
                try:
                    hfss.modeler[body].material_name = CONNECTOR_NEUTRAL_MATERIAL
                    hfss.modeler[body].solve_inside = True
                except Exception as exc:
                    log(f"WARNING: {body} could not be set to {CONNECTOR_NEUTRAL_MATERIAL} ({exc}).")
            log(f"Connector bodies of {prefix} set to {CONNECTOR_NEUTRAL_MATERIAL} (connectors ignored).")

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


_CORNER_CUT_SERIALS = itertools.count(1)


def clip_to_outline(hfss, frame, half_length: float, half_side: float, radius: float,
                    base: float, height: float, targets: List[str],
                    log=logger.info) -> None:
    """Round the PCB's corners off `targets`, so they follow the board's outline.

    Each corner's cut is its square less the quarter circle that leaves the
    corner round (`pcb_outline.corner_cuts`). Each is named, shaped, and then
    subtracted from the targets. The targets keep their names, which is what
    the material rules match on. The cut spans `base` to `base + height`, so
    every target must lie inside that height.

    One cut at a time rather than all four united. Each cut gets a name that
    no earlier cut in this session used: a new cut given the name of a cut
    already consumed (the vacuum's, then the board's) was not found by the
    subtract that followed.

    Nothing happens for a sharp board (`radius` 0) or with no targets.
    """
    if radius <= 0 or not targets:
        return
    from ..design import pcb_outline

    serial = next(_CORNER_CUT_SERIALS)
    for number, cut in enumerate(pcb_outline.corner_cuts(half_length, half_side, radius), 1):
        tool = f"pcb_corner_cut_{serial}_{number}"
        u1, v1, u2, v2 = cut.box
        origin, sizes = frame.box(u1, v1, u2, v2, base, height)
        block = hfss.modeler.create_box(origin=origin, sizes=sizes)
        block.name = tool
        cu, cv = cut.centre
        disc = hfss.modeler.create_cylinder(
            orientation="Y", origin=frame.point(cu, cv, base),
            radius=cut.radius, height=height,
        )
        hfss.modeler.subtract(tool, [disc.name], keep_originals=False)
        hfss.modeler.subtract(list(targets), [tool], keep_originals=False)
    log(f"Rounded {', '.join(targets)} to the {radius:g} mm PCB corners.")


def build_vacuum(hfss, frame, stack, pcb_half_length: float, pcb_half_side: float,
                 log=logger.info, corner_radius: float = 0.0) -> Optional[str]:
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
    lid_inside's pocket. `carve_vacuum` then subtracts every dielectric
    from it, and every metal body -- box included -- takes its own shape
    out by material override, so nothing here has to know the shape of
    what it is sized around.
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
    # The vacuum is the cavity's air, so it has the cavity's rounded corners.
    # A sharp box here would fill the corners the box metal occupies.
    clip_to_outline(
        hfss, frame, pcb_half_length, pcb_half_side, corner_radius,
        base=box_bottom, height=box_top - box_bottom, targets=["vacuum"], log=log,
    )
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
    # The metal drawn as sheets (see SHEET_BELOW_MM), which carries its
    # metal as a boundary instead of a material.
    sheets: List[str] = field(default_factory=list)
    # How far from the die's trace axis the die's coplanar ground starts
    # (half the trace plus the gap), None when the die has no copper. A
    # port across the CPW gap has to reach exactly that far.
    chip_ground_offset: Optional[float] = None


# Metal thinner than this is drawn as a sheet on the face it lies on, with
# a conductor boundary, instead of as a solid.
#
# The die's 200 nm film is the case: the TAU mesher cannot resolve a body
# that thin next to a 70 mm board, and reports a "mesh leak" on it --
# HFSS's word for a body whose mesh is more than 10% off its own surface,
# i.e. largely missing. A sheet is how thin films are meshed anyway. The
# board's 35 um copper is resolved as a solid, and stays one.
SHEET_BELOW_MM = 0.001


def board_centre_fallback(v: Dict[str, float]) -> "tuple[float, float]":
    """Where the board's centre sits in the box, worked out from the
    equations alone: the middle of the box's walls, ignoring any mounting
    wings.

    This is only the fallback for `cavity_frame`, which measures the real
    centre from the imported box. The 3D view (hfss/viewer_parts) uses it
    the same way, when there is no box geometry to measure.
    """
    pcb_length = v.get("pcb_length", 26.0)
    pcb_side = v.get("pcb_side", 10.0)
    wall = v.get("wall_width", 1.0)
    return (pcb_length + 2 * wall) / 2.0, (pcb_side + 2 * wall) / 2.0


def layout_stack(v: Dict[str, float]) -> "copper_geometry.Stack":
    """Where each face of the board and the die sits along the box's up
    axis, from the equations (the box floor is at 0).

    The board rests at floor_width + pedestal_depth: on the floor's top,
    or on the rim of the pedestal recess the die sits over. Its top face
    is pcb_width above that. The die is let into the board, so its top
    finishes flush with the board's top. The lid sits on the board's
    copper (`place_lid_inside`), which `Stack.pcb_copper_top` gives.

    floor_width is worked out in design/screws.py, not read from the file:
    with the screws on it is thick enough to thread a screw into, and it
    is never thinner than the wall.
    """
    from ..design import copper_geometry as cg
    from ..design import screws as screw_plan

    pcb_width = v.get("pcb_width", 0.381)
    chip_width = v.get("chip_width", 0.381)
    pedestal = v.get("pedestal_depth", 0.0)
    pcb_bottom = screw_plan.floor_width(v) + pedestal
    pcb_top = pcb_bottom + pcb_width
    # The metal's thickness AS DRAWN: a film below SHEET_BELOW_MM is a
    # sheet, so it has none in the model, and whatever rests on it --
    # a bond foot on the die -- rests on the face it lies on.
    copper = v.get("copper_thickness", 0.035)
    chip_metal = v.get("chip_metal_thickness", 0.0002)
    return cg.Stack(
        pcb_bottom=pcb_bottom,
        pcb_top=pcb_top,
        chip_bottom=pcb_top - chip_width,
        chip_top=pcb_top,
        copper_thickness=0.0 if copper < SHEET_BELOW_MM else copper,
        chip_metal_thickness=0.0 if chip_metal < SHEET_BELOW_MM else chip_metal,
    )


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
    from ..design import (
        cavity as cavity_plan, copper_geometry as cg, equations as eq, parameter_meta,
        pcb_outline,
    )

    v = eq.get_numeric_parameters(dimensions_txt_content)

    pcb_length = v.get("pcb_length", 26.0)
    pcb_side = v.get("pcb_side", 10.0)
    # The board's rounded corners. 0 for a board from before the cavity had
    # rounded corners, which is then sharp like its box.
    pcb_radius = pcb_outline.corner_radius_of(v)

    # The assembly origin is a corner, so the board centre sits inside
    # the box -- but not simply half of (pcb_length + 2*wall_width),
    # because the assembly also carries mounting wings that extend it.
    # Measured from the cavity rather than computed, so the mechanical
    # design can grow features this code has never heard of.
    fallback_x, fallback_z = board_centre_fallback(v)
    frame = cavity_frame(hfss, fallback_x=fallback_x, fallback_z=fallback_z, log=log)

    # The heights: see layout_stack.
    stack = layout_stack(v)
    # The thicknesses as the equations give them, before the sheet rule,
    # for the log lines that report which metal was drawn as a sheet.
    copper = v.get("copper_thickness", 0.035)
    chip_metal = v.get("chip_metal_thickness", 0.0002)
    sheets: List[str] = []

    def remember_sheets(made, drawn, real):
        names = [name for key, built in made.items() if "_copper_" in key for name in built]
        if names and not drawn:
            sheets.extend(names)
            # Said, because a sweep of this thickness changes nothing below it.
            log(f"{', '.join(names)}: {real * 1000:g} um of metal, under "
                f"{SHEET_BELOW_MM * 1000:g} um, so drawn as a sheet -- its thickness "
                "does not enter the model.")

    _drop_superseded_solids(hfss, log)
    build_vacuum(
        hfss, frame, stack,
        pcb_half_length=pcb_length / 2.0, pcb_half_side=pcb_side / 2.0, log=log,
        corner_radius=pcb_radius,
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
    bare = parameter_meta.is_bare_board(v)
    if bare:
        log("Bare board (no_chip): no die, opening, bond pads or wire bonds -- "
            "the CPW runs unbroken from one connector to the other.")

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
    made: Dict[str, List[str]] = {}
    if package is not None and len(package):
        made = cg.build(
            hfss.modeler, package, frame,
            top_face=stack.pcb_top, bottom_face=stack.pcb_bottom,
            thickness=stack.copper_thickness, prefix="pcb", log=log,
        )
        remember_sheets(made, stack.copper_thickness, copper)
        # The bottom copper gets a hole the size of the opening, blind or through
        # (see copper_geometry.clear_cavity_area). The Altium generator cuts the same
        # hole since 2026-11-10; this cut also removes the back plane that a copper.txt
        # drawn before then has whole under a blind pocket, and the ground plane an
        # older generator drew across its own cutout.
        cg.clear_cavity_area(
            hfss.modeler, frame, stack, opening,
            targets=[name for names in made.values() for name in names],
            log=log,
        )
        if not opening.is_empty:
            cg.mark_bottom_hole(combo_dir)
        # The vias are solid posts standing in the undrilled board -- the
        # material override lets the metal win where they overlap. See
        # `copper_geometry._via_post` for why they are not drilled.

    # The board's rounded corners: the substrate and any copper that reaches
    # a corner are cut to the outline here. Only the bodies that can reach a
    # corner are in the boolean (see `copper_geometry.copper_reaching_corners`),
    # and sheets are left out, since a boolean on a sheet is the risky case.
    if pcb_radius > 0:
        half_length, half_side = pcb_length / 2.0, pcb_side / 2.0
        planes, vias = (
            cg.copper_reaching_corners(package, half_length, half_side, pcb_radius)
            if package is not None and len(package) else (False, False)
        )
        targets = ["pcb_substrate"]
        for key, names in made.items():
            wanted = vias if key.endswith("_vias") else planes
            if not wanted:
                continue
            kept = [name for name in names if name not in sheets]
            if len(kept) < len(names):
                log(f"WARNING: {', '.join(n for n in names if n in sheets)} is a sheet, not "
                    "clipped to the rounded corners.")
            targets += kept
        # 0.1 mm past the board's faces on each side, so the tool's faces
        # never coincide with the substrate's or the copper's.
        clip_to_outline(
            hfss, frame, half_length, half_side, pcb_radius,
            base=stack.pcb_bottom - 0.1, height=(stack.pcb_top - stack.pcb_bottom) + 0.2,
            targets=targets, log=log,
        )

    # A bare board never has a die, whatever an earlier run left in
    # copper.txt.
    chip = None if bare else boards.get("chip")
    if chip is not None and len(chip):
        # Through-vias in the die, if it ever carries any, are posts in
        # the die in the same way.
        made = cg.build(
            hfss.modeler, chip, frame,
            top_face=stack.chip_top, bottom_face=stack.chip_bottom,
            thickness=stack.chip_metal_thickness, prefix="chip", log=log,
        )
        remember_sheets(made, stack.chip_metal_thickness, chip_metal)

    # The connectors are real vendor geometry rather than a stand-in, so
    # the launch is represented rather than left as bare copper. They
    # rest on the board's actual top surface -- the copper, not the bare
    # substrate underneath it (stack.pcb_top).
    if CONNECTOR_NEUTRAL_MATERIAL == "removed":
        log("Connectors are not added to the model (launch_connectors = removed).")
    else:
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
    if bare:
        pass  # nothing to bond to; said above
    elif not include_wirebonds:
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

    # The corner screws, if asked for: holes in box, lid_inside, the board
    # and its copper, then the screws themselves. After everything they
    # cut through exists.
    from . import screws as screw_builder

    try:
        screw_builder.build_screws(hfss, frame, stack, v, log=log)
    except ValueError as exc:
        raise RuntimeError(f"Corner screws: {exc}") from exc

    from . import vents as vent_builder

    try:
        vent_builder.build_vents(hfss, frame, stack, v, log=log)
    except ValueError as exc:
        raise RuntimeError(f"Vents: {exc}") from exc

    ground_offset = None
    if chip is not None:
        edges = [abs(across) for region in chip.regions if region.layer == "top"
                 for _, across in region.points]
        ground_offset = min(edges) if edges else None
    return LayoutContext(frame=frame, stack=stack, values=v, sheets=sheets,
                         chip_ground_offset=ground_offset)


# Tried in turn when the preferred material will not stick. The last
# one is deliberately dull: a conductor modelled as the wrong metal is a
# small error, while one left as vacuum is a completely different model.
LAST_RESORT_CONDUCTOR = "copper"
LAST_RESORT_DIELECTRIC = "Rogers TMM 10i (tm)"

# Materials a run can ask for that the stock AEDT library does not carry,
# with the properties to create them with.
#
# Only these are ever added to a project. For any other name the library
# does not know, PyAEDT's `add_material` does not fail -- it quietly
# creates a BLANK material, permittivity 1 and conductivity 0, which is
# vacuum under the material's name. That is what every "niobium" chip
# conductor was until 2026-09-28: with "Perfect E on the conductors" off,
# no metal on the die at all.
MISSING_FROM_LIBRARY: Dict[str, Dict[str, float]] = {
    # Bulk, at room temperature (resistivity 152 nOhm m), like the
    # library's own copper and aluminum. At the package's millikelvin
    # operating point niobium superconducts; "Perfect E on the
    # conductors" is the setting for that.
    "niobium": {"conductivity": 6.58e6},
    # 4H-SiC across its c axis. The library's SiC entries are thermal
    # only -- no permittivity at all, so vacuum to HFSS. No loss given.
    "sic": {"permittivity": 9.7},
}


def _ensure_missing_material(hfss, material: str, log) -> bool:
    """Make sure a MISSING_FROM_LIBRARY material exists with its real properties.

    Corrects one that is already in the project too: a project an earlier
    build left behind carries the blank "niobium" from before, and
    assigning that would pass every check while still being vacuum.

    False when `material` is one of these and could not be made right --
    the caller must not assign it then, since what the project holds
    under that name may still be the blank.
    """
    properties = MISSING_FROM_LIBRARY.get(material.strip().lower())
    if properties is None:
        return True
    try:
        made = hfss.materials.exists_material(material) or hfss.materials.add_material(material)
        for key, value in properties.items():
            setattr(made, key, value)
        made.update()
    except Exception as exc:
        log(f"WARNING: could not give {material!r} its real properties ({exc}), so it is "
            "not used.")
        return False
    log(f"{material!r} is not in the AEDT library, so it is set here: "
        + ", ".join(f"{key} {value:g}" for key, value in properties.items()) + ".")
    return True


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
    superconducting die but the stock library does not carry it, and the
    failure is silent. Rather than leave the body as vacuum -- which is
    not a conductor at all, and changes the physics rather than
    approximating it -- a material in MISSING_FROM_LIBRARY is created with
    its real properties, and any other name the library lacks settles for
    a plain stand-in, and says so.
    """
    if _ensure_missing_material(hfss, material, log) and _try_material(hfss, obj, material):
        return True

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


def _assign_box_conductivity(hfss, name: str, mat_settings: dict, log) -> bool:
    """A finite-conductivity boundary on the box (or its lid_inside) for a
    run that turned perfect E off for it.

    The conductivity is the run's `box_conductivity`, not the box
    material's library value: that is a room-temperature figure, and the
    box sits at cryogenic temperature. Not given a thickness -- an
    Eigenmode design refuses one on this boundary, and the solid already
    is the metal -- so it is the surface impedance of a bulk wall.
    Returns whether the boundary was made.
    """
    try:
        sigma = float(mat_settings.get("box_conductivity") or DEFAULT_BOX_CONDUCTIVITY)
    except (TypeError, ValueError):
        sigma = DEFAULT_BOX_CONDUCTIVITY
    if sigma <= 0:
        sigma = DEFAULT_BOX_CONDUCTIVITY
    try:
        hfss.assign_finite_conductivity(name, conductivity=sigma, use_thickness=False)
    except Exception as exc:
        log(f"WARNING: could not give {name} a finite conductivity of {sigma:g} S/m ({exc}); "
            "it is left without a boundary.")
        return False
    return True


def assign_materials_and_boundaries(hfss, mat_settings: dict, log=logger.info,
                                    sheets: Optional[List[str]] = None) -> Dict[str, object]:
    """Give every solid in the imported model its material and boundary.

    A body whose name matches no rule is left alone and WARNED ABOUT
    rather than silently defaulting. A mis-exported or renamed body is
    the kind of thing that otherwise shows up as a puzzling frequency
    shift days later.

    `sheets` is the metal drawn as sheets (see SHEET_BELOW_MM). A sheet
    has no material of its own, so its metal is a boundary instead:
    perfect-E under the same setting as a solid, and otherwise a
    finite-conductivity boundary of the rule's material -- the surface
    impedance a solid film's faces get anyway. Not at the film's
    thickness: an Eigenmode design refuses a thickness on that boundary.
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
            elif perfect_e_key == "perfect_e_box":
                _assign_box_conductivity(hfss, obj.name, mat_settings, log)
            break
        else:
            unmatched.append(obj.name)

    for name in sheets or ():
        rule = next((r for r in MATERIAL_RULES if r[0] in name.lower()), None)
        if rule is None or rule[2] is None:
            unmatched.append(name)
            continue
        _pattern, key, material_key, perfect_e_key = rule
        objects_dict.setdefault(key, hfss.modeler[name])
        if perfect_e_key and mat_settings.get(perfect_e_key):
            hfss.assign_perfect_e(assignment=name)
            continue
        material = mat_settings.get(material_key) or MATERIAL_FALLBACKS[material_key]
        if not mat_settings.get(material_key):
            fell_back.add(material_key)
        if not (_ensure_missing_material(hfss, material, log)
                and hfss.materials.exists_material(material)):
            log(f"WARNING: the material library does not have {material!r} for the sheet "
                f"{name}, so it is {LAST_RESORT_CONDUCTOR!r} instead.")
            material = LAST_RESORT_CONDUCTOR
        hfss.assign_finite_conductivity(name, material=material)

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
    """Cut the dielectrics out of the vacuum body; leave the metal to HFSS.

    `build_vacuum` makes vacuum a plain block spanning box's own measured
    envelope -- it does not know box's floor, pedestal or any other
    feature's shape, and does not try to. Two DIELECTRICS claiming one
    region is an error HFSS refuses to solve, so every body whose inside
    is solved -- the substrates, the connector beads, any air of its own
    -- is subtracted here.

    The metal is not. The model runs with material override on (see
    `build_model`), so wherever a conductor overlaps the vacuum the
    conductor wins: box, lid_inside, copper, bonds and the connectors'
    metal take their own shape out of the vacuum without a boolean. That
    is how the via posts in the board already work. Subtracting them as
    well used to be done, and it bought nothing but a fragile boolean:
    with a pedestal, the die stands on box's pocket rim with its sides
    flush to the pocket walls, and Parasolid failed to cut both out --
    not while the model was built, but every time AEDT reopened the
    project and replayed its history, which left the vacuum an empty
    body on the worker.

    Conductor or not is read off each body's own `solve_inside`, which
    material assignment has already set: HFSS solves inside a dielectric
    and not inside a conductor, so this is exactly the line it draws.

    The tool list is read straight from `hfss.modeler.solid_objects`,
    not from `objects_dict`. Several material rules legitimately match
    MORE than one body -- connector_dielectric matches one bead per port
    -- and objects_dict keeps only the first body per rule key, so
    building the tool list from it silently carved just one of them.
    """
    vacuum = objects_dict.get("vacuum")
    if vacuum is None:
        return

    tools, metal = [], []
    for obj in hfss.modeler.solid_objects:
        if obj.name.lower() == "vacuum":
            continue
        ignored_connector = (CONNECTOR_NEUTRAL_MATERIAL not in (None, "removed") and obj.name.startswith("connector_"))
        (tools if getattr(obj, "solve_inside", True) and not ignored_connector else metal).append(obj.name)
    if not tools:
        return

    try:
        # keep_originals: every one of these bodies must survive being
        # cut out of the vacuum -- they are the things being simulated.
        hfss.modeler.subtract(vacuum.name, tools, keep_originals=True)
    except Exception as exc:
        log(
            f"WARNING: could not subtract {len(tools)} bodies from the vacuum ({exc}). "
            "The model has overlapping solids and its results should not be trusted."
        )
        return
    # A boolean Parasolid cannot do does not always raise: it can leave
    # the target an empty body and report success.
    faces = getattr(vacuum, "faces", None)
    if faces is not None and not faces:
        log("WARNING: the vacuum is empty after carving the dielectrics out of it. "
            "The model has no air to solve in and its results cannot be trusted.")
        return
    log(f"Carved {len(tools)} dielectric bodies out of the vacuum: {', '.join(sorted(tools))}. "
        f"{len(metal)} metal bodies overlap it and take their place by material override.")


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


def close_project_safely(hfss, log: Callable[[str], None] = logger.info,
                         save: bool = True) -> bool:
    """Close whatever project `hfss` has open, without ever raising.

    PyAEDT's own `close_project()` asks the session for the project
    object and calls `.GetPath()` on it unchecked, so when the session
    cannot hand one back -- the project is already gone, or its name is
    ambiguous -- it dies with `AttributeError: 'NoneType' object has no
    attribute 'GetPath'` rather than reporting a close that failed. By
    the time we close, the model is built and saved to disk; a close
    going wrong must not take the whole run down with it.

    `save=False` closes it as it is on disk, for a caller that only
    looked (see `validate_saved_model`).

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
        if hfss.close_project(name=name, save=save) is False:
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


@one_aedt_at_a_time
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
        version=aedt_version_for(aedt_version),
        solution_type=solver.hfss_type,
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
    odesktop = hfss.odesktop
    autosave_was_on = _pause_autosave(odesktop, log)

    try:
        # The via posts stand in the undrilled board, so this model relies
        # on metal winning where it overlaps a dielectric (see
        # copper_geometry._via_post). Set here, not left to the default --
        # which is off.
        #
        # It is a setting of the whole design, not of the posts: with it
        # on, HFSS no longer refuses ANY body of metal overlapping a
        # dielectric, so a slip that sinks copper or a connector into a
        # substrate now solves as metal instead of failing validation.
        # That is the price of not drilling the board for every via, which
        # on a dense board is minutes of one AEDT call. Two dielectrics
        # overlapping are still refused.
        try:
            hfss.change_material_override(True)
        except Exception as exc:
            log(f"WARNING: could not turn material override on ({exc}). The via posts "
                "overlap the board and the metal overlaps the vacuum (see carve_vacuum), "
                "and HFSS may refuse the model.")
        _pin_mesher(hfss, log)

        if hfss.modeler.object_names:
            hfss.modeler.delete(hfss.modeler.object_names)
        for bound in hfss.boundaries:
            try:
                bound.delete()
            except Exception:
                pass

        # The worker's field exports of a field-image part, planned from this
        # model's own boxes once it is built (see hfss/driven/field_exports.py).
        field_exports = None
        if solver.build_model is not None:
            # A solver that draws its own model (the line-impedance check):
            # nothing of the package is imported or rebuilt.
            from ..workflow import companion
            try:
                object_names, port_names = solver.build_model(
                    hfss, combo_id, combo_dir, dimensions_txt_content, run,
                    companion.settings_with_modes(run, combo_id, analysis_settings, log=log),
                    log=log,
                )
            except registry.PortPlacementError as exc:
                log(f"{combo_id}: {exc} Not saving or queueing this combination.")
                return
        else:
            log(f"Importing {step_paths['box']}...")
            before_box = set(hfss.modeler.object_names)
            hfss.modeler.import_3d_cad(step_paths["box"])
            # SolidWorks names a part's one solid after the LAST feature that
            # shapes it: "box" while that is the final mirror, "Vent_Screws"
            # once the vent cuts follow it. Everything below looks for "box".
            box_names = [n for n in hfss.modeler.object_names if n not in before_box]
            if len(box_names) > 1:
                hfss.modeler.unite(box_names)
            if box_names and box_names[0] != "box":
                hfss.modeler[box_names[0]].name = "box"

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
            global CONNECTOR_NEUTRAL_MATERIAL
            _mode = analysis_settings.get("launch_connectors") or "kept"
            if analysis_settings.get("reference_plane") == "connector":
                _mode = "kept"
            CONNECTOR_NEUTRAL_MATERIAL = {"vacuum": "vacuum", "dielectric": "Teflon (tm)", "removed": "removed"}.get(_mode)
            layout = build_layout(
                hfss, combo_dir, dimensions_txt_content, lid_inside_names,
                include_wirebonds=analysis_settings["materials"].get("include_wirebonds", True),
                log=log,
            )

            objects_dict = assign_materials_and_boundaries(
                hfss, analysis_settings["materials"], log=log,
                sheets=layout.sheets if layout else None,
            )
            if CONNECTOR_NEUTRAL_MATERIAL == "removed":
                pass
            elif CONNECTOR_NEUTRAL_MATERIAL:
                # After the material rules, which give pin and shell their metal.
                for _name in [n for n in hfss.modeler.object_names if n.startswith("connector_")]:
                    try:
                        hfss.modeler[_name].material_name = CONNECTOR_NEUTRAL_MATERIAL
                        hfss.modeler[_name].solve_inside = True
                    except Exception as exc:
                        log(f"WARNING: {_name} could not be set to {CONNECTOR_NEUTRAL_MATERIAL} ({exc}).")
                log(f"All connector bodies are {CONNECTOR_NEUTRAL_MATERIAL} (connectors ignored).")
            carve_vacuum(hfss, objects_dict, log=log)
            object_names = {key: obj.name for key, obj in objects_dict.items()}

            # After the carve, not before: a boolean renumbers the faces of
            # everything it touches, and a port is attached to a face id.
            try:
                port_names = solver.place_ports(hfss, layout, analysis_settings, log=log)
            except registry.PortPlacementError as exc:
                log(f"{combo_id}: {exc} Not saving or queueing this combination.")
                return

            # A driven run paired with an Eigenmode run gets that run's modes for
            # this combination, to put fine frequency windows around.
            from ..workflow import companion
            solver.create_setup(
                hfss, dimensions_txt_content,
                companion.settings_with_modes(run, combo_id, analysis_settings, log=log), log=log,
            )

            if analysis_settings.get("field_frequency_ghz") is not None:
                # The grids are placed on this model's boxes, so they are read
                # now, while the project is open. A model whose fields cannot be
                # placed is not saved or queued: its solve would have no exports.
                from .driven import field_exports as driven_fields
                try:
                    field_exports = driven_fields.plan_for_model(
                        hfss, objects_dict, port_names, analysis_settings["field_frequency_ghz"],
                        analysis_settings.get("field_quantities") or [], log=log,
                    )
                except ValueError as exc:
                    log(f"{combo_id}: {exc} Not saving or queueing this combination.")
                    return

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

        problem = validate_saved_model(
            project_path, solver.hfss_type, aedt_version, student_version, graphical,
            vacuum=object_names.get("vacuum"), log=log,
        )
        if problem:
            log(f"{combo_id}: not queueing it -- {problem}")
            return

        object_names = _remember_built_model(
            run, combo_id, project_path, object_names, port_names, log,
            field_exports=field_exports,
        )
        return object_names

    finally:
        close_project_safely(hfss, log=log)
        _resume_autosave(odesktop, autosave_was_on)


def validate_saved_model(project_path: str, solution_type: str,
                         aedt_version: Optional[str], student_version: bool, graphical: bool,
                         vacuum: Optional[str] = None,
                         log: Callable[[str], None] = logger.info) -> Optional[str]:
    """Reopen a saved model the way the worker will, and validate it here.

    Returns why it is not fit to queue, or None when it is.

    Reopened rather than checked in the session that built it, because
    AEDT replays the whole modelling history when it opens a project, and
    that replay is not guaranteed to reproduce what was built: a pedestal
    model carved its vacuum without complaint and then lost the vacuum --
    every face of it -- each time the project was opened again. On the
    worker that is a failed solve after the trip over the network; here
    it is a minute on the student licence.

    Three checks: that the vacuum still has a body at all -- the replay
    failure leaves it empty; that no two dielectrics share any volume
    (`dielectric_overlaps`), which HFSS's validation stops reporting once
    material override is on; and HFSS's own validation of the rest --
    ports, boundaries, setup.

    The project is closed without saving: this only looks.
    """
    from ansys.aedt.core import Hfss

    kwargs = dict(
        project=project_path, version=aedt_version_for(aedt_version),
        solution_type=solution_type, new_desktop=False, student_version=student_version,
        non_graphical=not graphical, remove_lock=True,
    )
    design = design_name_in_project(project_path)
    if design:
        kwargs["design"] = design
    hfss = None
    try:
        hfss = Hfss(**kwargs)
        if vacuum and vacuum in hfss.modeler.object_names and not hfss.modeler[vacuum].faces:
            return (f"reopened, {os.path.basename(project_path)} has an empty '{vacuum}' -- "
                    "a boolean in its history failed on replay. There is no air to solve in.")
        overlaps = dielectric_overlaps(hfss)
        # Ignored connectors (launch_connectors vacuum/dielectric): pin and shell are
        # left out of the vacuum carve on purpose (Parasolid cannot cut them), so they
        # share the vacuum's volume; the override resolves it and it is reported in the run notes.
        overlaps = [o for o in overlaps if not (
            vacuum in (o[0], o[1]) and any(n.startswith("connector_") for n in (o[0], o[1])))]
        if overlaps:
            return ("dielectric bodies overlap, which HFSS resolves silently with material "
                    "override on: " + ", ".join(
                        f"{a} / {b} ({volume:.4g} mm^3)" for a, b, volume in overlaps))
        if not hfss.validate_simple():
            errors = [
                str(message).strip() for message in
                hfss.odesktop.GetMessages(hfss.project_name, hfss.design_name, 2)
                if "error" in str(message).lower()
            ]
            return ("HFSS validation failed on the reopened project"
                    + (": " + " | ".join(errors[-3:]) if errors else "."))
        log(f"{os.path.basename(project_path)}: reopened and validated on this machine.")
        return None
    except Exception as exc:
        return f"could not reopen {os.path.basename(project_path)} to validate it ({exc})."
    finally:
        if hfss is not None:
            close_project_safely(hfss, log=log, save=False)
            wait_until_unlocked(project_path)


# Below this, two bodies touching is not two bodies overlapping: a shared
# face intersects to nothing, and floating point leaves crumbs.
OVERLAP_TOLERANCE_MM3 = 1e-6


def _boxes_overlap(a, b) -> bool:
    return all(a[i] < b[i + 3] and b[i] < a[i + 3] for i in range(3))


def dielectric_overlaps(hfss) -> List[tuple]:
    """Every pair of dielectric bodies that share volume, with how much.

    With material override on -- which the via posts and the uncarved
    metal rely on -- HFSS no longer refuses two dielectrics in one place:
    it picks one by priority and solves, so validation passes a model
    where, say, the vacuum still fills part of the board. Metal over a
    dielectric is the override doing its job and is not reported here.

    Each candidate pair -- dielectrics whose bounding boxes meet -- is
    intersected on copies, which are then deleted.
    """
    modeler = hfss.modeler
    bodies = [obj for obj in modeler.solid_objects if getattr(obj, "solve_inside", True)]
    found = []
    for i, first in enumerate(bodies):
        for second in bodies[i + 1:]:
            if not _boxes_overlap(first.bounding_box, second.bounding_box):
                continue
            copies = []
            try:
                for body in (first, second):
                    made = modeler.clone(body.name)
                    copies.extend(made[1] if isinstance(made, tuple) else made)
                result = modeler.intersect(copies, keep_originals=False)
                volume = 0.0
                if result and result in modeler.object_names and modeler[result].faces:
                    volume = float(modeler[result].volume)
                if volume > OVERLAP_TOLERANCE_MM3:
                    found.append((first.name, second.name, volume))
            finally:
                leftovers = [name for name in copies if name in modeler.object_names]
                if leftovers:
                    modeler.delete(leftovers)
    return found


def _pin_mesher(hfss, log: Callable[[str], None]) -> None:
    """Mesh with TAU Flex, and never fall back to the Classic mesher.

    Left on Auto, HFSS chooses the mesher from the geometry -- among other
    things it looks for "stacks of large planar parallel facets with small
    gaps", which is what a 70 x 30 mm board of 35 um copper is. The
    26 x 10 mm boards got TAU and meshed in 82 s; the 70 x 30 mm ones got
    Classic ("Stitch" in the profile), which does not parallelise, and on
    three thousand via posts it had not finished one mesh after a night.
    TAU Flex meshed that same model in six minutes on four cores.

    Flex is the TAU made for this model: it meshes by material priority,
    which is what the material override the via posts rely on asks for.

    No fallback, so a mesh TAU cannot make fails in minutes instead of
    falling back to Classic and running for days. Level 5 and no dynamic
    surface resolution are the settings every model had before.
    """
    try:
        hfss.mesh.assign_initial_mesh_from_slider(
            level=5, method="AnsoftTAU", dynamic_surface=False, flex_mesh=True,
            fallback=False, phi=False,
        )
    except Exception as exc:
        log(f"WARNING: could not set the initial mesh to TAU Flex ({exc}). HFSS will choose "
            "its own mesher, and on a large board that is Classic, which can take days.")


def _pause_autosave(odesktop, log: Callable[[str], None]) -> Optional[bool]:
    """Switch AEDT's autosave off for a build; returns whether it was on.

    AEDT rewrites the whole project every few edits, and a board with
    thousands of vias makes a project of tens of megabytes -- on a
    1960-via board that was a fifth of the build. The project is saved
    explicitly once it is done, so nothing is lost by building without.
    None means the state could not be read, and then it is left alone.
    """
    try:
        was_on = bool(odesktop.GetAutoSaveEnabled())
        if was_on:
            odesktop.EnableAutoSave(False)
        return was_on
    except Exception as exc:
        log(f"Could not pause AEDT's autosave ({exc}); building with it as it is.")
        return None


def _resume_autosave(odesktop, was_on: Optional[bool]) -> None:
    """Put autosave back the way `_pause_autosave` found it. It is an
    AEDT-wide option, not the project's, so it must not stay off."""
    if was_on:
        try:
            odesktop.EnableAutoSave(True)
        except Exception:
            pass


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


def design_name_in_project(project_path: str) -> Optional[str]:
    """The name of the HFSS design in a saved .aedt project, or None.

    Read from the file rather than recorded at build time, so a combo
    built before the name was needed still hands over correctly. The
    design block sits near the top of the file -- ahead of the geometry
    that makes a big project tens of megabytes -- so this stops early.
    """
    try:
        with open(project_path, encoding="utf-8", errors="replace") as handle:
            in_model = False
            for line in handle:
                stripped = line.strip()
                if stripped == "$begin 'HFSSModel'":
                    in_model = True
                elif in_model and stripped.startswith("Name='"):
                    return stripped[len("Name='"):].rstrip("'")
    except OSError:
        pass
    return None


def _remember_built_model(run, combo_id, project_path, object_names,
                          port_names, log, field_exports=None) -> Dict[str, str]:
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

    A field-image part's field exports go in the same way: they are read
    off the open model here, and queue_model builds the task from them later,
    when the share is there.
    """
    extra = {"field_exports": field_exports} if field_exports else {}
    run.update_combo(
        combo_id,
        model_status="built",
        project_path=project_path,
        object_names=object_names,
        ports=port_names,
        **extra,
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

    # A field-image part's exports (see hfss/driven/field_exports.py). An
    # ordinary task has none, and is built without the argument at all.
    combo = run.state["combos"][combo_id]
    field_kwargs = {"field_exports": combo["field_exports"]} if combo.get("field_exports") else {}
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
        solution_type=solver.hfss_type,
        # Named, or PyAEDT does not find it: it matches the saved design
        # against the solution type, and a driven design saves as "HFSS
        # Modal Network", which PyAEDT 1.0.0 does not take for
        # "DrivenModal". It then inserts a new, empty design and the
        # worker finds no setup in it.
        design_name=design_name_in_project(project_path),
        objects=object_names,
        post_processing=solver.post_processing_ops(analysis_settings),
        metadata={
            "combo_id": combo_id,
            # Which solver this task is for, when HFSS cannot tell: the
            # line-impedance check is a "DrivenModal" design to the worker.
            "solver": solver.value,
            "params": run.state["combos"][combo_id]["params"],
            "source_step_files": {k: os.path.abspath(p) for k, p in step_paths.items()},
        },
        **field_kwargs,
    )
    # Under the run's queue lock, so a pause or a resume cannot land in the
    # middle of this. While the run is paused the task is written straight
    # into the run's held folder (see workflow/queue_hold.py): it is never
    # visible to the worker, and it keeps the same name it would have had.
    with run.queue_lock:
        paused = run.queue_paused
        task_path = queue_common.write_task_atomically(
            queue_hold.intake_queue(queue, run.run_id) if paused else queue, task)
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
        run.update_combo(combo_id, task_id=task_id, handoff_status="done",
                         queue_status="held" if paused else "pending")
    log(f"Queued analysis task {task_id} for {combo_id}"
        + (" -- held, because the run is paused" if paused else "") + f": {task_path}")

