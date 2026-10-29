"""
What a run was built with, in the few words the run list filters on and
the run page shows: the PCB substrate, whether the board carries a chip,
the pedestal under it, the solution type, and how far the run has got.

A run's own record says what it pinned and what it swept (see
`run_manager.facet_inputs`). A setting it did not pin is taken from the
equation file its geometry came from: `common_dimensions.txt` in the run
folder once a model was built, then the committed `base_parts/default.txt`.
Those two files are read through a cache keyed by path, modification time
and size, because the run list asks for every run every few seconds.

A solver that draws its own model (the launch study, the line check) builds
no board from the equation file, so for it only a pinned or swept value
counts. Anything else is "n/a" rather than the board's defaults.

A run that sweeps a setting over values of both kinds (a pedestal depth of 0
and of 0.5, say) carries both words, so it matches either filter.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from .. import paths
from ..design import equations as eq
from ..design import parameter_meta
from ..hfss import eigenmode, model_builder, registry
from .compare import COMMON_DIMENSIONS, NOT_RECORDED

NO_CHIP = parameter_meta.NO_CHIP_PARAM
PEDESTAL_DEPTH = "pedestal_depth"
PEDESTAL_SHAPE = "pedestal_shape"
VENTS = parameter_meta.VENTS_PARAM
SCREWS = parameter_meta.SCREWS_PARAM

# Where a setting's value came from. Shown on the run page beside the value.
SWEPT = "swept"
PINNED = "pinned"
COMMON = "common dimensions"
DEFAULT = "equation file"
NOT_APPLICABLE = "n/a"

# A run's stage, as the run list files it. Anything not named here is still
# running -- the pipeline's middle stages all are.
_STAGE_WORDS = {"complete": "complete", "error": "failed", "stopped": "stopped", "created": "created",
                "stalled": "stalled"}


@dataclass(frozen=True)
class Setting:
    """One setting as a run had it: the values it takes across the run's
    combinations (empty when nothing says) and where they came from."""
    values: Tuple[float, ...] = ()
    source: str = ""


# -- reading the equation files ----------------------------------------------
# path -> ((mtime_ns, size), numbers). A file is read again only when its
# version changes, so a poll of a hundred runs costs a stat each, not a read.
_numbers_cache: Dict[str, Tuple[Tuple[int, int], Dict[str, float]]] = {}


def _numbers_in(path: str) -> Dict[str, float]:
    """The numeric-literal equations in a file, read once per version of it."""
    try:
        stat = os.stat(path)
    except OSError:
        return {}
    version = (stat.st_mtime_ns, stat.st_size)
    cached = _numbers_cache.get(path)
    if cached is not None and cached[0] == version:
        return cached[1]
    try:
        numbers = eq.get_numeric_parameters(eq.read_file(path))
    except (OSError, ValueError):
        return {}
    _numbers_cache[path] = (version, numbers)
    return numbers


def _floats(values: Iterable) -> Tuple[float, ...]:
    result = []
    for value in values:
        try:
            result.append(float(value))
        except (TypeError, ValueError):
            continue
    return tuple(result)


def _setting(name: str, inputs: dict, run_dir: Optional[str], own_model: bool) -> Setting:
    """A parameter's value for the run: swept, else pinned, else the equation
    file the run was built from, else the committed defaults."""
    for spec in inputs.get("sweep_params") or []:
        if spec.get("name") == name:
            values = _floats(spec.get("values") or ())
            if values:
                return Setting(values, SWEPT)
            break
    pinned = inputs.get("parameter_defaults") or {}
    if name in pinned:
        values = _floats((pinned[name],))
        if values:
            return Setting(values, PINNED)
    if own_model:
        return Setting((), NOT_APPLICABLE)
    if run_dir:
        common = _numbers_in(os.path.join(run_dir, COMMON_DIMENSIONS))
        if name in common:
            return Setting((common[name],), COMMON)
    defaults = _numbers_in(str(paths.DEFAULT_EQUATIONS_FILE))
    if name in defaults:
        return Setting((defaults[name],), DEFAULT)
    return Setting()


def settings_of(inputs: dict, run_dir: Optional[str] = None) -> Dict[str, Setting]:
    """The chip, pedestal, pedestal shape, vent and screw settings of a run,
    each as the run had it. `inputs` is `run_manager.facet_inputs(config)`."""
    solution = inputs.get("solution_type") or registry.default_solution_type()
    own_model = registry.is_known(solution) and not registry.get(solution).needs_solid
    return {
        name: _setting(name, inputs, run_dir, own_model)
        for name in (NO_CHIP, PEDESTAL_DEPTH, PEDESTAL_SHAPE, VENTS, SCREWS)
    }


# -- the words the run list filters on ---------------------------------------
def short_substrate(material: Optional[str]) -> str:
    """A PCB material as the run list names it: the Rogers library name
    without its "Rogers " and " (tm)" -- "TMM 10i", "RO3003"."""
    name = (material or "").strip()
    if name.startswith("Rogers "):
        name = name[len("Rogers "):]
    if name.endswith(" (tm)"):
        name = name[:-len(" (tm)")]
    return name.strip()


def solution_label(value: str) -> str:
    """A solution type's name as the form gives it, up to the dash that
    starts its explanation: "Driven Modal", "Eigenmode", "Launch study"."""
    for solver in registry.solution_types():
        if solver["value"] == value:
            return solver["label"].split(" -- ")[0]
    return value


def _words(words: Iterable[str]) -> str:
    return " ".join(sorted(set(words)))


def _filter_words(setting: Setting, classify) -> str:
    return _words(classify(value) for value in setting.values)


def _solution_words(inputs: dict) -> str:
    words = [inputs.get("solution_type") or registry.default_solution_type()]
    if inputs.get("paired"):
        # A driven run that solved its eigenmodes too: it matches both.
        words.append(eigenmode.EIGENMODE)
    return _words(words)


def _stage_words(stage: Optional[str], failed: int, unconverged: bool = False) -> str:
    if not stage:
        return ""
    words = {_STAGE_WORDS.get(stage, "running")}
    if failed or stage == "error":
        words.add("failed")
    if unconverged:
        # A run whose frequency sweep did not converge (see solver_status.py).
        words.add("unconverged")
    return _words(words)


def run_facets(inputs: dict, run_dir: Optional[str] = None, *,
               stage: Optional[str] = None, failed: int = 0, unconverged: bool = False) -> Dict[str, str]:
    """The run list's filter words for one run, as plain strings.

    Words are separated by spaces, except the substrate, which is one name
    and is compared whole. A word that no value of the run gives is absent,
    and a setting nothing says is an empty string -- matched by "All" only.
    """
    settings = settings_of(inputs, run_dir)
    pedestal = _filter_words(settings[PEDESTAL_DEPTH], lambda depth: "recess" if depth > 0 else "flat")
    shape = ""
    if "recess" in pedestal.split():
        shape = _filter_words(settings[PEDESTAL_SHAPE], lambda s: "circle" if s >= 0.5 else "square")
    return {
        "substrate": short_substrate(inputs.get("mat_pcb") or model_builder.MATERIAL_FALLBACKS["mat_pcb"]),
        "chip": _filter_words(settings[NO_CHIP], lambda flag: "bare" if flag > 0.5 else "chip"),
        "pedestal": pedestal,
        "shape": shape,
        "solution": _solution_words(inputs),
        "stage": _stage_words(stage, failed, unconverged),
    }


# -- the words the run page shows --------------------------------------------
def _values_text(name: str, values: Iterable[float]) -> str:
    shown = [parameter_meta.format_value(name, value) for value in sorted(set(values))]
    unit = parameter_meta.unit_for(name)
    suffix = f" {unit}" if unit else ""
    if len(shown) <= 6:
        return ", ".join(shown) + suffix
    return f"{shown[0]} to {shown[-1]}{suffix} ({len(shown)} values)"


_SOURCE_TEXT = {
    SWEPT: "swept",
    PINNED: "pinned for this run",
    COMMON: "from common_dimensions.txt",
    DEFAULT: "from the equation file",
}
NOT_APPLICABLE_TEXT = "not applicable: this solver draws its own model"
# The on/off switches, under the names the new-run form gives them.
_FLAG_LABELS = {NO_CHIP: "Bare board (no chip)", VENTS: "Vent grooves", SCREWS: "Corner screws"}


def _switch_text(setting: Setting, on: str, off: str) -> str:
    if not setting.values:
        return NOT_RECORDED
    words = {on if value > 0.5 else off for value in setting.values}
    if len(words) == 1:
        return words.pop()
    return "swept: " + " and ".join(sorted(words))


def _chip_text(setting: Setting) -> str:
    if not setting.values:
        return NOT_RECORDED
    words = {"no chip (bare board)" if value > 0.5 else "with a chip" for value in setting.values}
    if len(words) == 1:
        return words.pop()
    return "swept: " + " and ".join(sorted(words))


def _pedestal_text(depth: Setting, shape: Setting) -> str:
    if not depth.values:
        return NOT_RECORDED
    if depth.source == SWEPT and len(set(depth.values)) > 1:
        return "swept: " + _values_text(PEDESTAL_DEPTH, depth.values)
    value = depth.values[0]
    if value <= 0:
        return "no pedestal (flat floor)"
    text = f"recess {parameter_meta.format_value(PEDESTAL_DEPTH, value)} mm"
    if shape.values:
        kinds = {"circle" if s >= 0.5 else "square" for s in shape.values}
        text += ", " + (kinds.pop() if len(kinds) == 1 else "square or circle")
    return text


def _row(label: str, setting: Setting, text: str) -> Dict[str, str]:
    if setting.source == NOT_APPLICABLE:
        return {"label": label, "text": NOT_APPLICABLE_TEXT, "source": ""}
    return {"label": label, "text": text, "source": _SOURCE_TEXT.get(setting.source, "")}


def geometry_rows(inputs: dict, run_dir: Optional[str] = None) -> List[Dict[str, str]]:
    """The run page's geometry table: what was swept, and what the chip, the
    pedestal, the vents and the screws were, each with where it came from.

    Rows are dicts of label, text and source ("" when there is no source to
    give). A setting the solver does not build says so, row by row.
    """
    settings = settings_of(inputs, run_dir)
    rows = []
    for sweep in inputs.get("sweep_params") or []:
        name = sweep.get("name")
        values = _floats(sweep.get("values") or ())
        if not (name and values):
            continue
        if name in _FLAG_LABELS:
            label = _FLAG_LABELS[name]
            text = ", ".join(_switch_text(Setting((value,)), "on", "off") for value in sorted(set(values)))
        else:
            label, text = parameter_meta.label_for(name), _values_text(name, values)
        rows.append({"label": label, "text": text, "source": _SOURCE_TEXT[SWEPT]})
    chip, depth = settings[NO_CHIP], settings[PEDESTAL_DEPTH]
    vents, screws = settings[VENTS], settings[SCREWS]
    rows += [
        _row("Chip", chip, _chip_text(chip)),
        _row("Pedestal", depth, _pedestal_text(depth, settings[PEDESTAL_SHAPE])),
        _row("Vent grooves", vents, _switch_text(vents, "on", "off")),
        _row("Corner screws", screws, _switch_text(screws, "on", "off")),
    ]
    return rows


def title_chips(inputs: dict, run_dir: Optional[str] = None) -> List[str]:
    """The short facts shown as chips beside a run's title: substrate, chip
    and pedestal. A fact nothing says is left out rather than guessed at."""
    facets = run_facets(inputs, run_dir)
    chips = [facets["substrate"]]
    chip_words = facets["chip"].split()
    if chip_words == ["bare"]:
        chips.append("No chip")
    elif chip_words == ["chip"]:
        chips.append("With chip")
    elif chip_words:
        chips.append("Chip swept")
    depth = settings_of(inputs, run_dir)[PEDESTAL_DEPTH]
    pedestal_words = facets["pedestal"].split()
    if pedestal_words == ["flat"]:
        chips.append("No pedestal")
    elif pedestal_words == ["recess"]:
        chips.append(f"Recess {parameter_meta.format_value(PEDESTAL_DEPTH, depth.values[0])} mm")
    elif pedestal_words:
        chips.append("Pedestal swept")
    return [chip for chip in chips if chip]
