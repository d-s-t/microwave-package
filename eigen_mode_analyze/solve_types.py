"""
The two things HFSS can be asked to do with these models, and which
analysis settings belong to each.

An **Eigenmode** run is source-free: it finds the cavity's resonances and
reports a frequency and a Q per mode. There are no ports, no excitation
and no frequency sweep -- the question is "what does this box resonate
at", and the answer is a handful of numbers per combination.

A **DrivenModal** run is the opposite: it drives one port, sweeps
frequency, and reports the scattering matrix -- S11 (what comes back) and
S21 (what gets through). The question is "what does this package do to a
signal", and the answer is two curves per combination.

They share a geometry and almost nothing else, which is what this module
exists to keep straight. The settings that only make sense for one of
them (`modes` for eigenmode, `f_stop_ghz` for driven) live in separate
tables here, so the web form can ask for exactly the ones that apply and
nothing else, and so a run's stored config can be read back without
guessing which half of it was ever used.

`SHARED` holds the settings that mean the same thing either way -- the
adaptive-pass counts, which are a mesh-refinement concept, not a
solution-type one.

To add a setting: put it in the right table below with its default and a
label. The form, the CLI and `pipeline.DEFAULT_ANALYSIS_SETTINGS` all
read these, so nothing else needs to be told about it.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

EIGENMODE = "Eigenmode"
DRIVEN_MODAL = "DrivenModal"

# What the form shows for each, in the order it shows them. The value is
# what goes into `analysis_settings["solution_type"]` and, from there,
# straight into `Hfss(solution_type=...)` and the task file -- so these
# strings are PyAEDT's, not ours, and must not be prettified.
SOLUTION_TYPES: List[Dict[str, str]] = [
    {
        "value": EIGENMODE,
        "label": "Eigenmode -- resonant frequencies and Q",
        "help": "Source-free. Finds what the cavity resonates at. No ports.",
    },
    {
        "value": DRIVEN_MODAL,
        "label": "Driven Modal -- S-parameters (S11 / S21)",
        "help": "Drives port 1 and sweeps frequency. Needs two ports, so it "
                "needs port_count = 2.",
    },
]

DEFAULT_SOLUTION_TYPE = EIGENMODE


# --- where the ports go, for a driven run -----------------------------
#
# This is the single most consequential choice in a driven run and it has
# no default that is right for everyone, so it is asked rather than
# assumed. The two options measure genuinely different things, and an S11
# from one is not comparable with an S11 from the other.

REFERENCE_PLANE_CONNECTOR = "connector"
REFERENCE_PLANE_LAUNCH = "launch"

REFERENCE_PLANES: List[Dict[str, str]] = [
    {
        "value": REFERENCE_PLANE_CONNECTOR,
        "label": "Connector -- wave port in the SMP bead",
        "help": "Includes the connector transition and the launch. The port "
                "sits on the coax cross-section through the vendor bead, "
                "which is the connector's own 50 ohm section. Closest to "
                "what a VNA sees at the connector.",
    },
    {
        "value": REFERENCE_PLANE_LAUNCH,
        "label": "Launch -- lumped port at the CPW",
        "help": "Board only: excludes the connector and its land pattern. "
                "The port sits under the CPW trace, through the substrate to "
                "the backing ground plane, just inboard of the launch.",
    },
]

DEFAULT_REFERENCE_PLANE = REFERENCE_PLANE_CONNECTOR


# --- the settings tables ----------------------------------------------
#
# Each entry: name, label, kind ("int"/"float"/"select"), default, and an
# optional help string. `kind` decides the form control and the type the
# submitted value is coerced to; nothing here is free-form text.

SHARED: List[Dict[str, Any]] = [
    {"name": "max_passes", "label": "Max adaptive passes", "kind": "int", "default": 99},
    {"name": "min_passes", "label": "Min adaptive passes", "kind": "int", "default": 5},
    {"name": "min_converged", "label": "Min converged passes", "kind": "int", "default": 3},
]

EIGENMODE_ONLY: List[Dict[str, Any]] = [
    {"name": "modes", "label": "Modes", "kind": "int", "default": 20, "max": 20},
    {"name": "max_delta_f", "label": "Max delta freq per pass (%)", "kind": "float", "default": 1.0},
    {
        "name": "min_freq_safety_factor", "label": "Starting-frequency safety factor",
        "kind": "float", "default": 0.4,
        "help": "The cavity's estimated lowest resonance is multiplied by "
                "this to pick MinimumFrequency, so the sweep starts below "
                "the first real mode rather than on top of it.",
    },
]

DRIVEN_ONLY: List[Dict[str, Any]] = [
    {"name": "f_start_ghz", "label": "Sweep start (GHz)", "kind": "float", "default": 1.0},
    {"name": "f_stop_ghz", "label": "Sweep stop (GHz)", "kind": "float", "default": 15.0},
    {
        "name": "sweep_points", "label": "Sweep points", "kind": "int", "default": 401,
        "help": "Frequencies the interpolating sweep reports. It solves far "
                "fewer than this and interpolates between them, so a large "
                "number here is cheap.",
    },
    {
        "name": "max_delta_s", "label": "Max delta S per pass", "kind": "float", "default": 0.02,
        "help": "Adaptive convergence target, the driven equivalent of max "
                "delta freq.",
    },
    {
        "name": "port_impedance_ohm", "label": "Port impedance (ohm)", "kind": "float", "default": 50.0,
        "help": "S-parameters are renormalised to this. The CPW gap is "
                "already solved for 50 ohm, so changing it here only changes "
                "the reference, not the geometry.",
    },
    {
        "name": "reference_plane", "label": "Reference plane", "kind": "select",
        "default": DEFAULT_REFERENCE_PLANE, "options": REFERENCE_PLANES,
    },
]

# Settings that are neither solution-type-specific nor shown in the
# solution-type section of the form -- machine and workflow settings.
# Listed here only so `all_setting_names()` can be complete.
NON_SOLVE_SETTINGS = (
    "aedt_version", "student_version", "graphical", "poll_seconds",
    "use_altium", "materials", "solution_type",
)


def specific_fields(solution_type: str) -> List[Dict[str, Any]]:
    """Only the settings this solution type does not share with the other.

    What the form puts in the panel it shows and hides. The shared
    settings must NOT go in there: the form renders a panel per solution
    type, and a field appearing in both would submit its name twice, so
    the value read back would be whichever panel came first in the
    document rather than the one being looked at.
    """
    return list(DRIVEN_ONLY if is_driven(solution_type) else EIGENMODE_ONLY)


def fields_for(solution_type: str) -> List[Dict[str, Any]]:
    """Every setting a run of this solution type actually uses, in form order."""
    return specific_fields(solution_type) + list(SHARED)


def defaults_for(solution_type: str) -> Dict[str, Any]:
    """`{name: default}` for one solution type's own settings."""
    return {field["name"]: field["default"] for field in fields_for(solution_type)}


def all_defaults() -> Dict[str, Any]:
    """Both solution types' defaults in one dict.

    A run's stored `analysis_settings` carries the whole set regardless of
    which type it is, so that switching an existing run's solution type --
    or copying settings forward from a previous run, which is what the web
    form does -- never lands on a missing key. The unused half is inert.
    """
    merged: Dict[str, Any] = {}
    for field in list(EIGENMODE_ONLY) + list(DRIVEN_ONLY) + list(SHARED):
        merged[field["name"]] = field["default"]
    return merged


def is_driven(solution_type: Optional[str]) -> bool:
    """True for a solution type that has ports and an S-matrix.

    Takes the string rather than the settings dict so it can be called
    from places that only have one or the other. `None` -- an
    `analysis_settings` from before driven runs existed -- is Eigenmode,
    which is what those runs were.
    """
    return (solution_type or EIGENMODE) != EIGENMODE


def solution_type_of(analysis_settings: Dict[str, Any]) -> str:
    """The solution type of a run, defaulting old runs to Eigenmode."""
    return analysis_settings.get("solution_type") or EIGENMODE


def coerce(field: Dict[str, Any], raw: Any) -> Any:
    """One submitted form value, as the type its field says it is."""
    kind = field["kind"]
    if kind == "int":
        return int(raw)
    if kind == "float":
        return float(raw)
    if kind == "select":
        allowed = [option["value"] for option in field["options"]]
        if raw not in allowed:
            raise ValueError(f"{field['name']}={raw!r} is not one of {allowed}")
        return raw
    raise ValueError(f"Unknown field kind {kind!r} for {field['name']!r}")


def validate(solution_type: str, port_counts: Iterable[float]) -> Optional[str]:
    """Why this solution type cannot run against this geometry, or None.

    Geometry, not settings: the driven solve needs two real ports to have
    an S21 at all, and `port_count` is an equation-file value rather than
    an analysis setting. Returning a string rather than raising lets the
    web form show it instead of failing the request.

    Takes every port count the run would build, not one, because
    `port_count` can itself be swept -- and a sweep where only some
    combinations have two ports is still a run that cannot finish.
    """
    if not is_driven(solution_type):
        return None
    wrong = sorted({int(count) for count in port_counts if int(count) != 2})
    if wrong:
        return (
            f"A driven run needs exactly 2 ports to report S21, but this run would "
            f"build combinations with port_count = {', '.join(str(c) for c in wrong)}. "
            f"Set port_count to 2, or use an Eigenmode run."
        )
    return None
