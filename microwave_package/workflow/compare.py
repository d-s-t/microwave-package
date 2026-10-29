"""
Comparing runs: several runs' results read as one table, the settings
they were made with set against each other, and their numbers lined up
point by point.

A run's own page answers "what did this sweep do". A comparison answers
the question after it -- "what changed when I changed that?" -- asked of
runs made with something different: the wire bonds left out, another
convergence target, the same sweep on another grid or another equations
file. So besides the combined results it says what that difference was,
read from each run's own record -- its settings and the geometry it was
built with -- rather than from whatever the runs happen to be called.

Like `plotting`, nothing here knows solution types. The one choice that
depends on the solver -- which axes to open on -- is asked of it, and
whether results line up point by point is asked of the data.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import cached_property
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

from ..design import equations as eq
from ..design import parameter_meta
from ..hfss import model_builder, registry
from . import plotting
from .run_manager import Run

# The palette's size (see plotting.RUN_COLOURS): no two runs in one
# comparison ever share a colour.
MAX_RUNS = len(plotting.RUN_COLOURS["light"])

# The side-by-side table lists at most this many points at once. Past it
# the list is longer than anyone reads -- the plot is the comparison
# then, and the filters narrow the list back down.
MAX_TABLE_ROWS = 60

# Past this many rows of results the table is not built at all.
MAX_TABLE_POINTS = 5000

# Written by step_generation.write_common_dimensions.
COMMON_DIMENSIONS = "common_dimensions.txt"

# A value a run's record cannot supply, and one that does not apply to it
# at all. Neither disagrees with anything -- see SettingRow.differs.
NOT_RECORDED = "not recorded"
NOT_APPLICABLE = "n/a"


@dataclass
class Compared:
    """One run in a comparison.

    `slot` is its place in the comparison, which is where its colour,
    marker and letter come from. `name` is the letter and the run's own
    title together: unique even when two runs are called the same thing,
    and what the legend and the tooltips say.
    """
    run: Run
    slot: int
    title: str
    results: Optional[pd.DataFrame]

    @property
    def letter(self) -> str:
        return chr(ord("A") + self.slot)

    @property
    def name(self) -> str:
        return plotting.run_name(self.letter, self.title)

    @property
    def run_id(self) -> str:
        return self.run.run_id

    @property
    def has_results(self) -> bool:
        return self.results is not None and not self.results.empty

    @cached_property
    def held(self) -> Dict[str, float]:
        """This run's `held_values`, read once for everything that asks."""
        return held_values(self.run)


def compared(runs: Sequence[Run], titles: Sequence[str],
             results: Sequence[Optional[pd.DataFrame]]) -> List[Compared]:
    """The runs to compare, in the order given -- the first is the one the
    others are measured against until the page is told otherwise."""
    return [
        Compared(run, slot, title, frame)
        for slot, (run, title, frame) in enumerate(zip(runs, titles, results))
    ]


def swept_names(runs: Iterable[Run]) -> List[str]:
    """Every parameter any of the runs swept, in the order they first appear."""
    names: List[str] = []
    for run in runs:
        for name in run.sweep_param_names:
            if name not in names and not parameter_meta.is_retired(name):
                names.append(name)
    return names


def held_values(run: Run) -> Dict[str, float]:
    """Every parameter this run held at a single value, in millimetres.

    `common_dimensions.txt` is the geometry all of the run's combinations
    actually shared -- the equations file as it stood then, with the
    run's pinned values applied -- so it is a record even of the values
    nobody typed in. A run from before it was written, or one stopped
    before any geometry was built, has only what it pinned -- which the
    oldest runs wrote into every combination rather than beside them, so
    a value all of a run's combinations share counts as pinned too.

    Parameters that no longer exist (parameter_meta.RETIRED_PARAMETERS) are left out.
    """
    values = dict(run.parameter_defaults)
    combos = [combo.get("params") or {} for combo in run.state.get("combos", {}).values()]
    if combos:
        values.update({
            name: value for name, value in combos[0].items()
            if all(combo.get(name) == value for combo in combos[1:])
        })
    try:
        content = eq.read_file(os.path.join(run.run_dir, COMMON_DIMENSIONS))
    except OSError:
        content = ""
    values.update(eq.get_numeric_parameters(content))
    return {name: value for name, value in values.items() if not parameter_meta.is_retired(name)}


def combined_results(entries: Sequence[Compared]) -> pd.DataFrame:
    """Every compared run's results as one table, each row saying which
    run it came from (plotting.RUN_ID_COLUMN and RUN_COLUMN).

    A parameter one run swept, another may have held at one value -- which
    is still a value, and the one that run's points belong at. It is
    filled in from that run's record (see `held_values`), which is what
    lets a sweep and a single run share an axis, and lets the side-by-side
    table find the same point in each.
    """
    swept = swept_names(entry.run for entry in entries)
    frames = []
    for entry in entries:
        if not entry.has_results:
            continue
        df = entry.results.copy()
        # Results from before the distortion option was removed still carry its columns.
        df = df.drop(columns=[c for c in df.columns if parameter_meta.is_retired(c)])
        held = entry.held
        for name in swept:
            column = parameter_meta.column_name(name)
            if column not in df.columns and column != name and name in df.columns:
                # Results from before a micrometre parameter's column was
                # named for its unit hold it in millimetres, under its
                # bare name.
                df[column] = pd.to_numeric(df[name], errors="coerce") * parameter_meta.MICRONS_PER_MM
            if column in df.columns or name not in held:
                continue
            df[column] = parameter_meta.to_display(name, held[name])
        df[plotting.RUN_ID_COLUMN] = entry.run_id
        df[plotting.RUN_COLUMN] = entry.name
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True, sort=False)


def default_axes(df: pd.DataFrame, swept: List[str],
                 solution_type: Optional[str] = None) -> Dict[str, Optional[str]]:
    """Which columns a comparison's plot opens on.

    The solver's own choice, as long as every run has something on that
    axis -- a run without it would otherwise just be missing from the
    plot, with nothing to say it was ever there. Failing that, the mode
    number when every run solved several, and failing that the runs
    themselves, side by side along the bottom. Colour is the run, since
    telling the runs apart is the point.
    """
    axes = dict(plotting.default_axes(df, swept, solution_type))
    if df.empty or plotting.RUN_ID_COLUMN not in df.columns:
        return axes
    by_run = df.groupby(plotting.RUN_ID_COLUMN)

    def every_run_has(column: Optional[str]) -> bool:
        return bool(column in df.columns and by_run[column].count().gt(0).all())

    def every_run_varies(column: str) -> bool:
        return bool(column in df.columns and by_run[column].nunique().gt(1).all())

    if axes["x"] == "combo_id" or not every_run_has(axes["x"]):
        if every_run_varies(plotting.MODE_COLUMN):
            axes["x"] = plotting.MODE_COLUMN
        elif df[plotting.RUN_ID_COLUMN].nunique() > 1:
            axes["x"] = plotting.RUN_COLUMN
    axes["color"] = plotting.RUN_COLUMN
    return axes


def runs_off_axes(df: pd.DataFrame, x: Optional[str], y: Optional[str]) -> List[str]:
    """The runs in `df` that have no point on these axes, by name.

    The plot cannot draw a point with no value on either axis, so a run
    whose rows have none -- a single run on the axis of a parameter only
    another run swept, say -- would otherwise just be absent from it.
    """
    columns = [column for column in (x, y) if column and column in df.columns]
    if df.empty or not columns or plotting.RUN_ID_COLUMN not in df.columns:
        return []
    drawn = set(df.loc[df[columns].notna().all(axis=1), plotting.RUN_ID_COLUMN])
    runs = df.drop_duplicates(plotting.RUN_ID_COLUMN)
    return [name for run_id, name in zip(runs[plotting.RUN_ID_COLUMN], runs[plotting.RUN_COLUMN])
            if run_id not in drawn]


# -- what the runs were made with -------------------------------------------
@dataclass
class SettingRow:
    group: str
    label: str
    values: List[str]

    @property
    def differs(self) -> bool:
        """Whether the runs' records disagree on this. A run that recorded
        nothing, or that the setting does not apply to, disagrees with
        nobody -- so a setting added after a run was made does not list
        that run as different for want of a record."""
        recorded = {value for value in self.values if value not in (NOT_RECORDED, NOT_APPLICABLE)}
        return len(recorded) > 1


def setting_rows(runs: Sequence[Run], held: Optional[Sequence[Dict[str, float]]] = None,
                 ) -> Tuple[List[SettingRow], List[SettingRow]]:
    """What the runs were made with, one row per setting, split into the
    rows where their records disagree and the rest.

    Only what shapes the result is listed: how the solve was set up, what
    it was built from and of, and every dimension of the geometry. Where
    the solve ran, and how often the page polled it, are left out.

    `held` is each run's `held_values`, when the caller has them already.
    """
    if held is None:
        held = [held_values(run) for run in runs]
    rows = (_solve_rows(runs) + _source_rows(runs)
            + _material_rows(runs) + _parameter_rows(runs, held))
    return [row for row in rows if row.differs], [row for row in rows if not row.differs]


def single_run_rows(run: Run) -> List[SettingRow]:
    """One run's own settings, as its page lists them: how it was solved,
    where its geometry came from, and what it was made of.

    The rows the comparison sets against each other, for one run. A material
    the run does not record says so, and gives the default the build uses
    for it -- the same one the run list's PCB name uses.
    """
    rows = _solve_rows([run]) + _source_rows([run]) + _material_rows([run])
    for key, default in model_builder.MATERIAL_FALLBACKS.items():
        label = model_builder.MATERIAL_LABELS.get(key)
        for row in rows:
            if row.label == label and row.values == [NOT_RECORDED]:
                row.values = [f"{NOT_RECORDED} (default {default})"]
    return rows


def _text(value) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return f"{value:g}"
    return str(value)


def _field_text(field: dict, value) -> str:
    for option in field.get("options", []):
        if option["value"] == value:
            return option["label"]
    return _text(value)


def _solve_rows(runs: Sequence[Run]) -> List[SettingRow]:
    types = [registry.solution_type_of(run.analysis_settings) for run in runs]
    solvers = [registry.get(t) if registry.is_known(t) else None for t in types]
    rows = [SettingRow("Solve", "Solution type", types)]

    # Every setting any of the runs' solvers takes, in form order. One
    # that a run's own solver does not take does not apply to it.
    fields: Dict[str, dict] = {}
    for solver in solvers:
        for field in (solver.all_fields if solver else []):
            fields.setdefault(field["name"], field)
    for name, field in fields.items():
        values = []
        for run, solver in zip(runs, solvers):
            if solver is None or name not in {f["name"] for f in solver.all_fields}:
                values.append(NOT_APPLICABLE)
            elif name not in run.analysis_settings:
                values.append(NOT_RECORDED)
            else:
                values.append(_field_text(field, run.analysis_settings[name]))
        rows.append(SettingRow("Solve", field["label"], values))

    rows.append(SettingRow("Solve", "AEDT version", [
        (run.analysis_settings["aedt_version"] or "auto-detect")
        if "aedt_version" in run.analysis_settings else NOT_RECORDED
        for run in runs
    ]))
    return rows


def _source_rows(runs: Sequence[Run]) -> List[SettingRow]:
    return [SettingRow("Geometry source", "PCB and chip layouts from Altium", [
        _text(run.analysis_settings["use_altium"])
        if "use_altium" in run.analysis_settings else NOT_RECORDED
        for run in runs
    ])]


def _material_rows(runs: Sequence[Run]) -> List[SettingRow]:
    rows = []
    for key, label in model_builder.MATERIAL_LABELS.items():
        values = []
        for run in runs:
            materials = run.analysis_settings.get("materials") or {}
            values.append(_text(materials[key]) if key in materials else NOT_RECORDED)
        rows.append(SettingRow("Materials", label, values))
    return rows


def _swept_text(name: str, values: List[float]) -> str:
    distinct = sorted(set(values))
    if len(distinct) == 1:
        # The oldest runs stored a pinned value as a one-value sweep. It
        # is still one value, and reads like any other run holding it.
        return _value_text(name, distinct[0])
    shown = [parameter_meta.format_value(name, value) for value in distinct]
    unit = parameter_meta.unit_for(name)
    suffix = f" {unit}" if unit else ""
    if len(shown) <= 6:
        return f"swept: {', '.join(shown)}{suffix}"
    return f"swept: {shown[0]} … {shown[-1]}{suffix} ({len(shown)} values)"


def _value_text(name: str, value: float) -> str:
    unit = parameter_meta.unit_for(name)
    return parameter_meta.format_value(name, value) + (f" {unit}" if unit else "")


def _parameter_rows(runs: Sequence[Run], held: Sequence[Dict[str, float]]) -> List[SettingRow]:
    swept = [{p["name"]: p.get("values", []) for p in run.config.get("sweep_params", [])}
             for run in runs]

    names: List[str] = []
    for values in swept + held:
        names.extend(name for name in values if name not in names and not parameter_meta.is_retired(name))

    rows = []
    for name in names:
        values = []
        for run, run_held, run_swept in zip(runs, held, swept):
            if name in run_swept:
                values.append(_swept_text(name, run_swept[name]))
            elif name in run_held:
                values.append(_value_text(name, run_held[name]))
            else:
                values.append(NOT_RECORDED)
        rows.append(SettingRow("Geometry", parameter_meta.label_for(name), values))
    return rows


# -- the numbers, point by point --------------------------------------------
def _sort_part(text: str) -> Tuple[int, float, str]:
    """Numbers in numeric order, then text, then missing -- one shape for
    all three, so any two keys can be compared."""
    if text == "":
        return (2, 0.0, "")
    try:
        return (0, float(text), "")
    except ValueError:
        return (1, 0.0, text)


def side_by_side(df: pd.DataFrame, entries: Sequence[Compared],
                 swept: List[str]) -> Tuple[Optional[dict], Optional[str]]:
    """The runs' results lined up point by point, for the page's table.

    A point is one place in the swept parameters and one mode, whatever
    each run called its combination -- so a run from before pinned values
    stopped being written into combination ids still lines up with a run
    from after. Each point keeps one value per run per result quantity,
    and the keys it is filed under are the filter panel's own, so the
    page can apply the same filter to both the plot and this.

    Returns (table, None), or (None, why there is none) -- and (None,
    None) when there is simply nothing to line up.
    """
    if df.empty or plotting.RUN_ID_COLUMN not in df.columns:
        return None, None
    quantities = [
        (column, label) for column, label in plotting.result_fields().items()
        if column in df.columns and pd.to_numeric(df[column], errors="coerce").notna().any()
    ]
    if not quantities:
        return None, None
    if len(df) > MAX_TABLE_POINTS:
        return None, (f"{len(df):,} rows of results is more than a table can show. "
                      "The plot is the comparison here.")

    key_columns = [c for c in (parameter_meta.column_name(n) for n in swept) if c in df.columns]
    if plotting.MODE_COLUMN in df.columns:
        key_columns.append(plotting.MODE_COLUMN)
    if not key_columns:
        if "combo_id" not in df.columns:
            return None, None  # nothing says which rows are the same point
        key_columns = ["combo_id"]

    numbers = {column: pd.to_numeric(df[column], errors="coerce").tolist() for column, _ in quantities}
    rows: Dict[Tuple[str, ...], dict] = {}
    seen = set()
    records = df[key_columns + [plotting.RUN_ID_COLUMN]].itertuples(index=False, name=None)
    for position, (*key_values, run_id) in enumerate(records):
        key = tuple("" if pd.isna(v) else plotting.filter_key(v) for v in key_values)
        if (key, run_id) in seen:
            return None, ("These results have more than one row for the same point -- curves "
                          "rather than points, such as a frequency sweep -- so there is no one "
                          "value per run to line up. The plot is the comparison here.")
        seen.add((key, run_id))
        row = rows.setdefault(key, {"key": list(key), "values": {c: {} for c, _ in quantities}})
        for column, _ in quantities:
            value = numbers[column][position]
            if pd.notna(value):
                row["values"][column][run_id] = float(value)

    return {
        "key_columns": [
            {"column": c, "label": plotting.label_for_column(c, swept)} for c in key_columns
        ],
        "runs": [
            {"id": e.run_id, "letter": e.letter, "name": e.name, "slot": e.slot}
            for e in entries if e.has_results
        ],
        "quantities": [{"column": c, "label": label} for c, label in quantities],
        "rows": sorted(rows.values(), key=lambda row: tuple(_sort_part(v) for v in row["key"])),
        "max_rows": MAX_TABLE_ROWS,
    }, None
