"""
What a solver is, and how the rest of the pipeline finds one.

The pipeline asks HFSS one of two questions. An **Eigenmode** run is
source-free: it finds the cavity's resonances and reports a frequency and
a Q per mode. A **DrivenModal** run drives a port, sweeps frequency, and
reports the scattering matrix -- S11 and S21. They build the same
geometry and differ in everything after it: which setup to create, where
the ports go (if any), what to read back out of the solved project, and
how the answer is plotted.

Rather than branch on the solution type in each of those places, each
one is a method on a `Solver`, and the two implementations live in
`eigenmode/` and `driven/` beside this file. Nothing here imports them --
they import this and register themselves, and `hfss/__init__.py` imports
both so that anything holding the package has the full set. So adding a
third solution type is a new folder and a `register(...)` call, with no
edits here or anywhere upstream.

`SHARED` holds the settings that mean the same thing to every solver --
the adaptive-pass counts, which are a mesh-refinement concept rather than
a solution-type one -- so a solver only lists what is genuinely its own.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional

# Settings every solver takes, with the same meaning. Each entry is
# {name, label, kind ("int"/"float"/"select"), default, and optionally
# help / max / options} -- `kind` decides both the form control and the
# type a submitted value is coerced to.
SHARED: List[Dict[str, Any]] = [
    {"name": "max_passes", "label": "Max adaptive passes", "kind": "int", "default": 99},
    {"name": "min_passes", "label": "Min adaptive passes", "kind": "int", "default": 5},
    {"name": "min_converged", "label": "Min converged passes", "kind": "int", "default": 3},
]


class PortPlacementError(RuntimeError):
    """A solver could not build the ports its run needs.

    Part of the contract rather than of any one solver, because the
    caller that has to react to it -- the model builder, which must not
    save or queue a portless model -- should not have to import a
    particular solver to catch it.

    Its own type because a model that needs ports and has none is
    worthless: there is nothing to excite and no S-matrix to read. That
    is different in kind from the warnings the geometry code raises when
    something merely looks wrong.
    """


def _no_ports(hfss, layout, settings, log) -> List[str]:
    return []


def _no_ops(settings) -> List[Dict[str, Any]]:
    return []


def _always_valid(port_counts: Iterable[float]) -> Optional[str]:
    return None


@dataclass(frozen=True)
class Solver:
    """One thing HFSS can be asked to do with these models.

    `value` is PyAEDT's own solution-type string -- it goes straight into
    `Hfss(solution_type=...)` and into the queued task -- so it is not
    ours to prettify. `label` and `help` are what the UI shows.

    The callables are the whole contract between a solution type and the
    pipeline:

    `create_setup(hfss, dimensions_txt_content, settings, log)` makes the
    project's single analysis setup (and, for a driven run, its frequency
    sweep). It runs on the student licence, which covers creating a setup
    but not solving one -- the worker only ever runs setups this side
    already made.

    `place_ports(hfss, layout, settings, log)` returns the port names it
    created, port 1 first, or `[]` for a solver that has no ports.

    `post_processing_ops(settings)` is the generic per-result op list the
    task carries for the client's own post-processing step.

    `read_results(hfss, setup_names, task, out_dir, log)` pulls the
    answer out of the solved project as a list of row dicts -- modes for
    one solver, frequency points for another -- which is what the results
    table is built from.

    `default_axes(df, swept_columns)` says which columns this solver's
    results should be plotted against by default, as
    `{"x": ..., "y": ..., "color": ...}`.

    `validate(port_counts)` gets every port count the run would build and
    returns why this solver cannot run against them, or None.
    """

    value: str
    label: str
    help: str
    fields: List[Dict[str, Any]]
    create_setup: Callable[..., Any]
    read_results: Callable[..., List[Dict[str, Any]]]
    default_axes: Callable[..., Dict[str, Optional[str]]]
    # What the results table calls this solver's own columns, for the
    # axis pickers. Column name -> label.
    result_fields: Dict[str, str] = field(default_factory=dict)
    place_ports: Callable[..., List[str]] = _no_ports
    post_processing_ops: Callable[..., List[Dict[str, Any]]] = _no_ops
    validate: Callable[..., Optional[str]] = _always_valid
    # Prefixes the task id, and so the queue folder a combination's
    # project is copied into -- every task is a "run_setups" task now, so
    # this is what says at a glance which kind of run one came from.
    task_id_prefix: str = "analyze"

    @property
    def all_fields(self) -> List[Dict[str, Any]]:
        """Every setting a run of this type uses, in form order."""
        return list(self.fields) + list(SHARED)


_SOLVERS: Dict[str, Solver] = {}
# First registered wins the default, which makes it whichever solver
# hfss/__init__ imports first rather than a name repeated here.
_DEFAULT: List[str] = []


def register(solver: Solver) -> Solver:
    if solver.value in _SOLVERS:
        raise ValueError(f"A solver is already registered for {solver.value!r}.")
    _SOLVERS[solver.value] = solver
    _DEFAULT.append(solver.value)
    return solver


def default_solution_type() -> str:
    if not _DEFAULT:
        raise RuntimeError(
            "No solvers are registered. Import microwave_package.hfss, which "
            "loads them, rather than hfss.registry on its own."
        )
    return _DEFAULT[0]


def solution_types() -> List[Dict[str, str]]:
    """Every registered solver as `{value, label, help}`, in registration order."""
    return [
        {"value": s.value, "label": s.label, "help": s.help}
        for s in (_SOLVERS[v] for v in _DEFAULT)
    ]


def get(solution_type: Optional[str]) -> Solver:
    """The solver for a solution type.

    `None` -- an `analysis_settings` written before runs had a solution
    type -- is the default, which is the eigenmode solver those runs
    were.
    """
    return _SOLVERS[solution_type or default_solution_type()]


def is_known(solution_type: Optional[str]) -> bool:
    return (solution_type or default_solution_type()) in _SOLVERS


def solution_type_of(analysis_settings: Dict[str, Any]) -> str:
    """The solution type of a run, defaulting old runs to the default one."""
    return analysis_settings.get("solution_type") or default_solution_type()


def for_settings(analysis_settings: Dict[str, Any]) -> Solver:
    return get(solution_type_of(analysis_settings))


def all_defaults() -> Dict[str, Any]:
    """Every solver's defaults in one dict.

    A run's stored `analysis_settings` carries the whole set regardless of
    which solver it uses, so that switching an existing run's solution
    type -- or copying settings forward from a previous run, which is what
    the web form does -- never lands on a missing key. The unused ones are
    inert.
    """
    merged: Dict[str, Any] = {}
    for solver in _SOLVERS.values():
        for f in solver.all_fields:
            merged[f["name"]] = f["default"]
    return merged


def result_fields() -> Dict[str, str]:
    """Every solver's result columns and their labels, for the axis pickers."""
    merged: Dict[str, str] = {}
    for value in _DEFAULT:
        merged.update(_SOLVERS[value].result_fields)
    return merged


def coerce(field_spec: Dict[str, Any], raw: Any) -> Any:
    """One submitted form value, as the type its field says it is."""
    kind = field_spec["kind"]
    if kind == "int":
        return int(raw)
    if kind == "float":
        return float(raw)
    if kind == "select":
        allowed = [option["value"] for option in field_spec["options"]]
        if raw not in allowed:
            raise ValueError(f"{field_spec['name']}={raw!r} is not one of {allowed}")
        return raw
    raise ValueError(f"Unknown field kind {kind!r} for {field_spec['name']!r}")
