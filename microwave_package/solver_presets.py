"""
Solver presets: the five analysis settings that decide how hard the worker
works on each solve, given one name.

    QUICK      a first look -- about 20 minutes a solve, not converged
    CONVERGED  what a result that will be quoted needs -- 4.5 to 10 hours a solve

The two sets were mixed up once already (a quick result quoted as converged),
so a run records which preset it was made with (`solver_preset`) and which
of the five it then overrode (`solver_overrides`). The numbers are the ones
the two reference runs were made with; tests/test_solver_presets.py pins them
to those runs' saved configs.

Kept free of heavy imports: the sweep runner reads these too.
"""
from __future__ import annotations

from typing import Dict, List, Mapping, Optional, Tuple

# In the order the command line and the run config list them.
SOLVER_KEYS: Tuple[str, ...] = ("max_passes", "min_passes", "min_converged", "max_delta_s", "max_delta_f")

PRESETS: Dict[str, Dict[str, float]] = {
    "quick": {
        "max_passes": 4,
        "min_passes": 1,
        "min_converged": 1,
        "max_delta_s": 0.05,
        "max_delta_f": 5.0,
    },
    "converged": {
        "max_passes": 99,
        "min_passes": 3,
        "min_converged": 2,
        "max_delta_s": 0.02,
        "max_delta_f": 3.0,
    },
}
PRESET_NAMES: Tuple[str, ...] = tuple(PRESETS)


def resolve(explicit: Mapping[str, Optional[float]], preset: Optional[str],
            defaults: Mapping[str, float]) -> Tuple[Dict[str, float], List[str]]:
    """The five solver settings for a run, and which of them were overridden.

    Each setting takes the first of: the value given explicitly (a flag left
    as None counts as not given), the preset's value, the solver's default.
    `overrides` lists the settings given explicitly while a preset was in
    effect -- with no preset there is nothing to override, so it is empty.
    """
    if preset is not None and preset not in PRESETS:
        raise ValueError(f"Unknown solver preset {preset!r}; known: {', '.join(PRESET_NAMES)}")
    values: Dict[str, float] = {}
    overrides: List[str] = []
    for key in SOLVER_KEYS:
        given = explicit.get(key)
        if given is not None:
            values[key] = given
            if preset is not None:
                overrides.append(key)
        elif preset is not None:
            values[key] = PRESETS[preset][key]
        else:
            values[key] = defaults[key]
    return values, overrides
