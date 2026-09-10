"""
Plotly-based plotting for a run's combined results: saved as a
self-contained HTML file (plotly.js embedded, so it opens standalone
with no internet needed) plus a CSV, into the run's plots/ folder --
also what the web UI embeds directly for live viewing.

Either axis can be any swept parameter or any result quantity; the run
page picks them and re-renders through `figure_html`.
"""
from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional

import pandas as pd
import plotly.express as px

from ..design import parameter_meta
from ..hfss import registry

logger = logging.getLogger("microwave_package")

# Result quantities a run can plot, and how they are labelled. Keys are
# the column names post-processing writes into the results table; a
# quantity only shows up in the axis pickers if it is actually present.
def result_fields() -> Dict[str, str]:
    """Result column -> label, gathered from every registered solver.

    A column only reaches an axis picker if some solver claims it, so a
    new solution type's quantities become plottable by being declared on
    its solver rather than by being listed here.
    """
    return registry.result_fields()


# Where a run's plot is saved inside its plots/ folder. The name is
# historical -- a driven run's plot is neither modes nor a scatter -- and
# is kept because runs plotted before driven runs existed look for a file
# with exactly this name.
PLOT_FILENAME = "modes_scatter_plot.html"


def has_sparameters(df: pd.DataFrame) -> bool:
    """Whether this results table holds a scattering matrix.

    Asked of the data rather than of the run's settings because the plot
    can only draw what the table actually holds, and because it decides a
    presentation question -- curves or points -- that belongs to the
    figure rather than to the solver.
    """
    return any(column in df.columns for column in ("s11_db", "s21_db"))


def default_axes(df: pd.DataFrame, sweep_param_names: List[str],
                 solution_type: Optional[str] = None) -> Dict[str, Optional[str]]:
    """Which columns a run's plot should open on.

    The solver decides -- an eigenmode sweep is read across combinations
    and a driven run along the band -- so all this does is hand it the
    swept columns in the form the table names them.

    A caller that has the run should pass its `solution_type`. Without
    one this falls back to what the table says about itself, which is how
    a bare CSV still plots sensibly.
    """
    swept = [parameter_meta.column_name(name) for name in sweep_param_names]
    solver = registry.get(solution_type or solution_type_of(df))
    return solver.default_axes(df, swept)


def solution_type_of(df: pd.DataFrame) -> Optional[str]:
    """Which solver produced this table, from the table itself.

    Post-processing records it per row, so a table assembled from several
    combinations of one run agrees with itself. A table from before that
    was recorded has no column at all and reads as None, which callers
    resolve to the default solver -- which is what those runs used.
    """
    if "solution_type" in df.columns:
        values = df["solution_type"].dropna().unique().tolist()
        if len(values) == 1:
            return values[0]
    return None


def axis_options(df: pd.DataFrame, sweep_param_names: List[str]) -> List[Dict[str, str]]:
    """Everything either axis can be set to, for this run's data:
    its swept parameters first, then whichever results exist."""
    options = []
    for name in sweep_param_names:
        column = parameter_meta.column_name(name)
        if column in df.columns:
            options.append({"value": column, "label": parameter_meta.axis_label(name), "kind": "parameter"})
    for column, label in result_fields().items():
        if column in df.columns and df[column].notna().any():
            options.append({"value": column, "label": label, "kind": "result"})
    if "combo_id" in df.columns:
        options.append({"value": "combo_id", "label": "Combination", "kind": "other"})
    return options


def label_for_column(column: str, sweep_param_names: List[str]) -> str:
    for name in sweep_param_names:
        if column == parameter_meta.column_name(name):
            return parameter_meta.axis_label(name)
    fields = result_fields()
    if column in fields:
        return fields[column]
    return "Combination" if column == "combo_id" else column


def _as_key(value) -> str:
    """One value rendered the same way on both sides of a filter match.

    The filter travels through a URL as text while the table holds
    floats, so 0.5 has to meet "0.5" and 1.0 has to meet "1" -- `%g` is
    what the rest of the UI already prints these with.
    """
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


def filter_options(df: pd.DataFrame, sweep_param_names: List[str]) -> List[Dict]:
    """The distinct value of each swept parameter, for the filter panel.

    Only swept parameters get one: they are what a run varies, so they
    are what there is any point picking apart. Values come back in
    numeric order with the key the filter matches on alongside the label
    the form shows.
    """
    options = []
    for name in sweep_param_names:
        column = parameter_meta.column_name(name)
        if column not in df.columns:
            continue
        values = sorted(df[column].dropna().unique().tolist())
        if len(values) < 2:
            continue  # nothing to pick apart
        options.append({
            "column": column,
            "label": parameter_meta.axis_label(name),
            "values": [{"key": _as_key(v), "label": _as_key(v)} for v in values],
        })
    return options


def apply_filters(df: pd.DataFrame, filters: Dict[str, List[str]]) -> pd.DataFrame:
    """Keep only the rows whose swept values are still selected.

    `filters` maps a column to the values left ticked. A column that is
    not mentioned is not filtered at all; a column with nothing ticked
    genuinely means "none of them", and the empty result is what the
    plot then says.
    """
    for column, allowed in filters.items():
        if column not in df.columns:
            continue
        wanted = set(allowed)
        df = df[df[column].map(_as_key).isin(wanted)]
    return df


def figure_html(
    df: pd.DataFrame,
    sweep_param_names: List[str],
    x: Optional[str] = None,
    y: Optional[str] = None,
    color: Optional[str] = None,
    solution_type: Optional[str] = None,
    dark: bool = False,
    plotlyjs_src: Optional[str] = None,
) -> str:
    """Render one scatter of this run's results as HTML.

    `x`/`y` are column names; anything missing falls back to something
    that exists, so a stale bookmark or a run whose post-processing did
    not produce a quantity still renders instead of erroring.

    With `plotlyjs_src` the library is linked instead of inlined, so the
    browser caches it once rather than re-fetching several megabytes
    every time an axis changes. Without it the page is self-contained,
    which is what the saved plot file needs.
    """
    options = [o["value"] for o in axis_options(df, sweep_param_names)]
    if not options:
        return "<p>No plottable columns in this run's results.</p>"

    def usable(column, fallback):
        return column if column in df.columns else fallback

    # A stale bookmark, or a run whose post-processing did not produce
    # the column it asks for, falls back to what this solver would have
    # opened on -- and to any column at all if even that is missing.
    preferred = default_axes(df, sweep_param_names, solution_type)
    x = usable(x, usable(preferred["x"], options[0]))
    fallback_y = preferred["y"] if preferred["y"] in df.columns and preferred["y"] != x else next(
        (o for o in options if o != x), x)
    y = usable(y, fallback_y)
    if color and color not in df.columns:
        color = None

    labels = {column: label_for_column(column, sweep_param_names) for column in df.columns}

    # A driven run against frequency is a curve, not a cloud: several
    # hundred points per combination drawn as markers is unreadable, and
    # the shape between the points is the whole result. Anything else --
    # including a driven run plotted against a swept parameter rather
    # than frequency -- stays a scatter, where each point is its own
    # measurement and joining them would invent a trend.
    as_curve = has_sparameters(df) and x == "freq_ghz"
    draw = px.line if as_curve else px.scatter
    extra = {"line_group": "combo_id"} if as_curve and "combo_id" in df.columns else {}

    figure = draw(
        df.sort_values(x) if as_curve else df,
        x=x, y=y, color=color,
        hover_data=[c for c in ("combo_id", "mode", *options) if c in df.columns and c not in (x, y)],
        # always first in each point's customdata, whatever hover_data
        # holds -- the run page reads it back to tell which combination
        # was clicked (see the click bridge below).
        custom_data=["combo_id"] if "combo_id" in df.columns else None,
        labels=labels,
        template="plotly_dark" if dark else "plotly_white",
        **extra,
    )
    # let the page behind the plot show through, so the embedded figure
    # matches whichever theme the run page is being viewed in
    figure.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=60, r=30, t=30, b=60),
    )

    if not plotlyjs_src:
        return figure.to_html(include_plotlyjs=True, full_html=True)

    fragment = figure.to_html(include_plotlyjs=False, full_html=False)
    # `color-scheme` matters as much as the transparent backgrounds here:
    # without it the browser paints an embedded document's canvas white
    # whatever its CSS says, and a dark-themed figure ends up sitting on
    # a white rectangle.
    scheme = "dark" if dark else "light"
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<script src='{plotlyjs_src}'></script>"
        "<style>html,body{margin:0;padding:0;background:transparent;"
        f"color-scheme:{scheme}}}"
        ".js-plotly-plot .plotly .cursor-crosshair{cursor:pointer}</style>"
        f"</head><body>{fragment}{_CLICK_BRIDGE}</body></html>"
    )


# The figure is embedded in an iframe, so a click on a point has to be
# handed out to the run page that owns the panel. Same origin either way;
# postMessage keeps it to one explicit channel rather than reaching
# across documents. combo_id is customdata[0] -- see `custom_data` above.
_CLICK_BRIDGE = """
<script>
(function () {
  var plot = document.querySelector('.js-plotly-plot');
  if (!plot || !plot.on) { return; }
  plot.on('plotly_click', function (event) {
    var point = event && event.points && event.points[0];
    var custom = point && point.customdata;
    var comboId = Array.isArray(custom) ? custom[0] : custom;
    if (!comboId) { return; }
    parent.postMessage({ source: 'eigenmode-plot', comboId: String(comboId) }, '*');
  });
})();
</script>"""


def plot_results(df: pd.DataFrame, run, sweep_param_names: List[str], log=logger.info) -> Optional[str]:
    if df.empty:
        log("No data to plot.")
        return None

    os.makedirs(run.plots_dir, exist_ok=True)
    combined_csv = os.path.join(run.plots_dir, "all_results.csv")
    df.to_csv(combined_csv, index=False)
    log(f"Combined {len(df)} rows into {combined_csv}")

    solution_type = registry.solution_type_of(run.config.get("analysis_settings", {}))
    html = figure_html(
        df, sweep_param_names, solution_type=solution_type,
        **default_axes(df, sweep_param_names, solution_type),
    )

    plot_path = os.path.join(run.plots_dir, PLOT_FILENAME)
    with open(plot_path, "w", encoding="utf-8") as handle:
        handle.write(html)
    log(f"Plot saved to {plot_path}")
    return plot_path
