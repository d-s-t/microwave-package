"""
Plotly-based plotting for a run's combined results: saved as a
self-contained HTML file (plotly.js embedded, so it opens standalone
with no internet needed) plus a CSV, into the run's plots/ folder --
also what the web UI embeds directly for live viewing.

Either axis can be any swept parameter or any result quantity; the run
page picks them and re-renders through `figure_html`. The comparison
page draws several runs' results through the same function, with each
row tagged by the run it came from (see `RUN_COLUMN` and
`workflow/compare.py`).
"""
from __future__ import annotations

import html
import logging
import os
from typing import Dict, List, Optional

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

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

# The combined results table beside it, one row per mode (or frequency
# point) per combination.
RESULTS_FILENAME = "all_results.csv"


# A driven run's rows say which sweep each came from: the broad one
# ("coarse"), or one of the fine windows ("Fine_1", ...) -- either its own,
# placed around an eigenmode, or a refinement run's, which also carries the
# refinement's run id. Rows from before this was recorded have no `sweep`
# and are all broad data.
SWEEP_COLUMN = "sweep"
BROAD_SWEEP = "coarse"
REFINEMENT_COLUMN = "refinement"
WINDOW_COLUMN = "window"
LINE_COLUMN = "_line"

VIEW_BROAD, VIEW_FINE, VIEW_BOTH = "broad", "fine", "both"
VIEWS = [
    {"value": VIEW_BOTH, "label": "Broad + fine, one trace"},
    {"value": VIEW_BROAD, "label": "Broad only"},
    {"value": VIEW_FINE, "label": "Fine ranges only"},
]


def fine_mask(df: pd.DataFrame) -> pd.Series:
    """Which rows come from a fine window rather than the broad sweep."""
    if SWEEP_COLUMN not in df.columns:
        return pd.Series(False, index=df.index)
    sweep = df[SWEEP_COLUMN]
    return sweep.notna() & (sweep != BROAD_SWEEP)


def has_fine_data(df: pd.DataFrame) -> bool:
    return bool(fine_mask(df).any())


def _window_labels(df: pd.DataFrame) -> pd.Series:
    """One label per fine window of a combination: its sweep name, and the
    refinement run it came from when it came from one."""
    refined = df[REFINEMENT_COLUMN].fillna("") if REFINEMENT_COLUMN in df.columns else ""
    return (refined.astype(str) + ":" if REFINEMENT_COLUMN in df.columns else "") + df[SWEEP_COLUMN].astype(str)


def apply_view(df: pd.DataFrame, view: Optional[str]) -> pd.DataFrame:
    """The broad data, the fine data, or both as one trace per combination.

    Marks each row's line in `_line` so a curve is drawn per combination --
    or, for the fine data alone, per combination AND window, so that
    several ranges are separate pieces instead of one line running across
    the gaps between them.

    "Both" puts a fine window's points where the broad sweep's own points
    inside that window were, which are interpolated samples of the same
    curve -- keeping both would draw a zig-zag where they disagree.
    """
    if not has_fine_data(df):
        out = df.copy()
        if view == VIEW_FINE:
            return out.iloc[0:0]
        return out
    fine = fine_mask(df)
    group = df["combo_id"].astype(str) if "combo_id" in df.columns else pd.Series("", index=df.index)
    if "run_id" in df.columns:
        group = df["run_id"].astype(str) + "/" + group
    if view == VIEW_BROAD:
        out = df[~fine].copy()
        out[LINE_COLUMN] = group[~fine]
        return out
    windows = df[fine].assign(**{WINDOW_COLUMN: _window_labels(df[fine])})
    if view == VIEW_FINE:
        out = windows.copy()
        out[LINE_COLUMN] = group[fine] + "|" + out[WINDOW_COLUMN]
        return out
    # Both: drop the broad rows that fall inside a fine window of the same
    # combination, then draw each combination as one line.
    keep = ~fine
    spans = windows.groupby([group[fine], windows[WINDOW_COLUMN]])["freq_ghz"].agg(["min", "max"]).reset_index()
    for _, span in spans.iterrows():
        inside = (group == span.iloc[0]) & ~fine & df["freq_ghz"].between(span["min"], span["max"])
        keep &= ~inside
    out = df[keep | fine].copy()
    out[LINE_COLUMN] = group[keep | fine]
    return out.sort_values("freq_ghz", kind="stable")


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


# An eigenmode run solves several modes per combination, one row each.
# The mode number then varies within the run just like a swept parameter
# does, so it is offered wherever they are -- but only when there is more
# than one, since a single mode has nothing to plot against or pick apart.
MODE_COLUMN = "mode"
MODE_LABEL = "Mode"

# A table holding several runs' results (the comparison page's) says on
# every row which run it came from: the id, which is what the filter
# matches on because a run's name can hold any character at all, and the
# name the legend and the tooltips show.
RUN_ID_COLUMN = "run_id"
RUN_COLUMN = "run"
RUN_LABEL = "Run"

# Each compared run's colour, by its position in the comparison -- the
# validated categorical palette, both modes stepped for their own page
# background (see _theme.html's --bg). A colour belongs to the run, not
# to where it happens to fall in the filtered data, so hiding one run
# never repaints the others. Eight is the cap: a ninth colour would have
# to be generated, and one of these reused is worse than none.
RUN_COLOURS = {
    "light": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
              "#e87ba4", "#008300", "#4a3aa7", "#e34948"],
    "dark": ["#3987e5", "#d95926", "#199e70", "#c98500",
             "#d55181", "#008300", "#9085e9", "#e66767"],
}
# A marker shape per run as well, so a run is never told apart by colour
# alone -- past three colours at once a scatter cannot promise every pair
# reads apart for every reader, and two runs that agree put their points
# on top of each other.
RUN_SYMBOLS = ["circle", "diamond", "square", "triangle-up",
               "x", "triangle-down", "pentagon", "star"]
# The page background under the transparent figure, for the ring that
# keeps overlapping markers apart.
_SURFACE = {"light": "#ffffff", "dark": "#16181c"}


def has_several_modes(df: pd.DataFrame) -> bool:
    return MODE_COLUMN in df.columns and df[MODE_COLUMN].dropna().nunique() > 1


def _has_several_runs(df: pd.DataFrame) -> bool:
    return RUN_ID_COLUMN in df.columns and df[RUN_ID_COLUMN].dropna().nunique() > 1


def run_colours(names: List[str], dark: bool = False) -> Dict[str, str]:
    """Run name -> its colour, in comparison order. See RUN_COLOURS."""
    palette = RUN_COLOURS["dark" if dark else "light"]
    return {name: palette[index] for index, name in enumerate(names[:len(palette)])}


_RUN_NAME_SEPARATOR = " · "


def run_name(letter: str, title: str) -> str:
    """What a compared run is called in RUN_COLUMN: its letter in the
    comparison, then its own name -- unique even when two runs share a
    name, and short to refer to by the letter alone."""
    return f"{letter}{_RUN_NAME_SEPARATOR}{title}"


def _run_letter(name: str) -> str:
    return name.split(_RUN_NAME_SEPARATOR, 1)[0]


def axis_options(df: pd.DataFrame, sweep_param_names: List[str]) -> List[Dict[str, str]]:
    """Everything either axis can be set to, for this run's data:
    its swept parameters first, then the mode number when a run solved
    several, the run itself in a comparison's table, then whichever
    results exist.

    The run is offered even when only one compared run has results: it
    is still the colour that run is known by on the rest of the page."""
    options = []
    for name in sweep_param_names:
        column = parameter_meta.column_name(name)
        if column in df.columns:
            options.append({"value": column, "label": parameter_meta.axis_label(name), "kind": "parameter"})
    if has_several_modes(df):
        options.append({"value": MODE_COLUMN, "label": MODE_LABEL, "kind": "parameter"})
    if RUN_COLUMN in df.columns:
        options.append({"value": RUN_COLUMN, "label": RUN_LABEL, "kind": "parameter"})
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
    if column == MODE_COLUMN:
        return MODE_LABEL
    if column == RUN_COLUMN:
        return RUN_LABEL
    return "Combination" if column == "combo_id" else column


def filter_key(value) -> str:
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

    Only swept parameters -- and the mode number, when a run solved
    several, and the runs themselves in a table of more than one -- get
    one: they are what varies, so they are what there is any point
    picking apart. Values come back in numeric order with the key the
    filter matches on alongside the label the form shows.
    """
    options = []
    if _has_several_runs(df):
        # In the order the table holds them -- the comparison's own order
        # -- and matched on the id, while the chip shows the name.
        runs = df.drop_duplicates(RUN_ID_COLUMN)
        options.append({
            "column": RUN_ID_COLUMN,
            "label": RUN_LABEL,
            "values": [
                {"key": str(run_id), "label": str(name)}
                for run_id, name in zip(runs[RUN_ID_COLUMN], runs[RUN_COLUMN])
            ],
        })
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
            "values": [{"key": filter_key(v), "label": filter_key(v)} for v in values],
        })
    if has_several_modes(df):
        values = sorted(df[MODE_COLUMN].dropna().unique().tolist())
        options.append({
            "column": MODE_COLUMN,
            "label": MODE_LABEL,
            "values": [{"key": filter_key(v), "label": filter_key(v)} for v in values],
        })
    return options


def apply_filters(df: pd.DataFrame, filters: Dict[str, List[str]]) -> pd.DataFrame:
    """Keep only the rows whose swept values are still selected.

    `filters` maps a column to the values left ticked. A column that is
    not mentioned is not filtered at all; a column with nothing ticked
    genuinely means "none of them", and the empty result is what the
    plot then says.

    A row with no value in the column at all is not at any of its
    values, so no choice among them hides it: unticking one value of a
    swept parameter hides the points at that value, not a compared run
    that never swept it.
    """
    for column, allowed in filters.items():
        if column not in df.columns:
            continue
        wanted = set(allowed)
        df = df[df[column].map(filter_key).isin(wanted) | df[column].isna()]
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
    run_colours: Optional[Dict[str, str]] = None,
    note: Optional[str] = None,
    mode_markers: Optional[List[Dict]] = None,
) -> str:
    """Render one scatter of this run's results as HTML.

    `x`/`y` are column names; anything missing falls back to something
    that exists, so a stale bookmark or a run whose post-processing did
    not produce a quantity still renders instead of erroring.

    With `plotlyjs_src` the library is linked instead of inlined, so the
    browser caches it once rather than re-fetching several megabytes
    every time an axis changes. Without it the page is self-contained,
    which is what the saved plot file needs.

    A table from several runs (one with a RUN_COLUMN) gives each run its
    own marker, and `run_colours` -- every compared run's name to its
    colour, in comparison order, including runs the filter has hidden --
    keeps each run's colour and marker its own however many are shown.
    `note` is a line to show above the figure.

    `mode_markers` -- `{combo_id, freq_ghz, q}` per eigenmode, from the
    Eigenmode run a driven run is paired with -- are drawn as triangles
    in a strip of their own under an S-parameter curve, one per mode of
    the combinations shown, so a dip can be read against the resonance it
    belongs to.
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
    extra = {"line_group": LINE_COLUMN if LINE_COLUMN in df.columns else "combo_id"} \
        if as_curve and ("combo_id" in df.columns or LINE_COLUMN in df.columns) else {}

    compared = RUN_COLUMN in df.columns
    if compared:
        order = list(run_colours) if run_colours else list(dict.fromkeys(df[RUN_COLUMN]))
        extra["category_orders"] = {RUN_COLUMN: order}
        if run_colours and color == RUN_COLUMN:
            extra["color_discrete_map"] = run_colours
        if not as_curve:
            extra["symbol"] = RUN_COLUMN
            extra["symbol_map"] = {name: RUN_SYMBOLS[i % len(RUN_SYMBOLS)] for i, name in enumerate(order)}
        elif color != RUN_COLUMN:
            # Curves coloured by something else still need their run: two
            # runs share combination ids, and one line_group across both
            # would draw a single line zig-zagging between them.
            extra["line_dash"] = RUN_COLUMN

    figure = draw(
        df.sort_values(x) if as_curve else df,
        x=x, y=y, color=color,
        hover_data={**{c: True for c in dict.fromkeys(
            c for c in (RUN_COLUMN, "combo_id", MODE_COLUMN, *options)
            if c in df.columns and c not in (x, y))},
            # the line a row is drawn on is bookkeeping, not data
            **({LINE_COLUMN: False} if LINE_COLUMN in df.columns else {})},
        # always first in each point's customdata, whatever hover_data
        # holds -- the run page reads it back to tell which combination
        # was clicked (see the click bridge below). A comparison's rows
        # also say which run, second.
        custom_data=[c for c in ("combo_id", RUN_ID_COLUMN) if c in df.columns] or None,
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
    if compared:
        # Run names are whatever their runs were called, often a sentence:
        # a legend beside the plot would take half its width, so it goes
        # above, one row per run if need be.
        figure.update_layout(legend=dict(orientation="h", yanchor="bottom", y=1.02,
                                         xanchor="left", x=0, title_text=""))
        # On an axis of their own, the runs go by their letters -- the
        # legend spells them out, and a sentence per tick would push the
        # plot itself off the page.
        shown = set(df[RUN_COLUMN])
        present = [name for name in order if name in shown]
        ticks = dict(tickmode="array", tickvals=present, ticktext=[_run_letter(n) for n in present])
        if x == RUN_COLUMN:
            figure.update_xaxes(**ticks)
        if y == RUN_COLUMN:
            figure.update_yaxes(**ticks)
        if not as_curve:
            # Big enough to hit, and ringed in the page colour so two runs
            # that agree still show both their markers.
            figure.update_traces(marker=dict(
                size=9, line=dict(width=1.5, color=_SURFACE["dark" if dark else "light"])))
        if x == MODE_COLUMN and not as_curve:
            # Runs that agree to a percent or two stack their markers into
            # one at every mode, and the run underneath looks absent. The
            # mode number is a count, not a measurement, so the runs can
            # stand side by side at each one without misstating anything
            # -- which is never true of a swept dimension.
            figure.update_layout(scattermode="group", scattergap=0.55)

    # Last: the run-letter ticks above go on every y axis, and the strip has
    # a y axis of its own that they must not reach.
    if as_curve and mode_markers:
        _add_mode_markers(figure, df, mode_markers, dark)

    if not plotlyjs_src:
        return figure.to_html(include_plotlyjs=True, full_html=True)

    fragment = figure.to_html(include_plotlyjs=False, full_html=False)
    # `color-scheme` matters as much as the transparent backgrounds here:
    # without it the browser paints an embedded document's canvas white
    # whatever its CSS says, and a dark-themed figure ends up sitting on
    # a white rectangle.
    scheme = "dark" if dark else "light"
    note_html = f"<p class='note'>{html.escape(note)}</p>" if note else ""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<script src='{plotlyjs_src}'></script>"
        "<style>html,body{margin:0;padding:0;background:transparent;"
        f"color-scheme:{scheme}}}"
        ".note{font:13px system-ui,sans-serif;color:#888;margin:.5rem .8rem 0}"
        ".js-plotly-plot .plotly .cursor-crosshair{cursor:pointer}</style>"
        f"</head><body>{note_html}{fragment}{_CLICK_BRIDGE}</body></html>"
    )


# The eigenmode strip under an S-parameter plot: its share of the figure's
# height, and the gap between it and the plot above.
MODE_STRIP_HEIGHT = 0.10
MODE_STRIP_GAP = 0.03


def _add_mode_markers(figure, df: pd.DataFrame, markers: List[Dict], dark: bool) -> None:
    """Eigenmodes as triangles in a strip under the plotted curves.

    Only the combinations in `df` (so a filter hides their modes with
    them), and only inside the frequency range being drawn. Where
    combinations share a mode frequency to the kHz it is drawn once.

    The strip is a second row of the same figure, sharing the curves'
    frequency axis, with its own y axis fixed at 0..1 and `fixedrange`:
    zooming, panning or autoscaling the S-parameters in y cannot move a
    triangle out of view. The frequency labels move under the strip.
    Nothing changes when no mode is drawn, so there is no empty strip.
    """
    shown = set(df["combo_id"]) if "combo_id" in df.columns else None
    low, high = df["freq_ghz"].min(), df["freq_ghz"].max()
    # The paired run's modes are those of the model as built (triangles); an eigenmode
    # part's are those of the model terminated like the driven run's ports (diamonds).
    groups = {False: ([], []), True: ([], [])}
    seen = set()
    for marker in markers:
        f = marker["freq_ghz"]
        terminated = bool(marker.get("terminated"))
        if (shown is not None and marker["combo_id"] not in shown) or not low <= f <= high:
            continue
        key = (terminated, round(f, 6))
        if key in seen:
            continue
        seen.add(key)
        q = marker.get("q")
        xs, texts = groups[terminated]
        xs.append(f)
        texts.append(("terminated " if terminated else "") + f"eigenmode {f:.4f} GHz"
                     + (f", Q {q:.0f}" if isinstance(q, (int, float)) and q == q else "")
                     + f" ({marker['combo_id']})")
    if not groups[False][0] and not groups[True][0]:
        return
    label = figure.layout.xaxis.title.text
    figure.update_layout(
        xaxis=dict(showticklabels=False, ticks="", title_text=None),
        yaxis=dict(domain=[MODE_STRIP_HEIGHT + MODE_STRIP_GAP, 1]),
        xaxis2=dict(matches="x", anchor="y2", title_text=label),
        yaxis2=dict(domain=[0, MODE_STRIP_HEIGHT], anchor="x2", range=[0, 1], fixedrange=True,
                    showgrid=False, zeroline=False, ticks="", tickvals=[0.5],
                    ticktext=["eigenmodes"], tickfont=dict(size=10)),
    )
    edge = _SURFACE["dark" if dark else "light"]
    for terminated, name, symbol, colour, height in (
        (False, "Eigenmodes", "triangle-up", "#e9a100" if dark else "#b47600", 0.5),
        (True, "Eigenmodes (terminated)", "diamond", "#4fb3d9" if dark else "#0b6e99", 0.5),
    ):
        xs, texts = groups[terminated]
        if not xs:
            continue
        figure.add_trace(go.Scatter(
            x=xs, y=[height] * len(xs), xaxis="x2", yaxis="y2", mode="markers", name=name,
            text=texts, hoverinfo="text",
            marker=dict(symbol=symbol, size=9, color=colour, line=dict(width=1, color=edge)),
        ))


# The figure is embedded in an iframe, so a click on a point has to be
# handed out to the run page that owns the panel. Same origin either way;
# postMessage keeps it to one explicit channel rather than reaching
# across documents. combo_id is customdata[0] -- see `custom_data` above
# -- and on the comparison page the run id is customdata[1].
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
    var runId = Array.isArray(custom) && custom.length > 1 ? String(custom[1]) : null;
    parent.postMessage({ source: 'eigenmode-plot', comboId: String(comboId), runId: runId }, '*');
  });
})();
</script>"""


def plot_results(df: pd.DataFrame, run, sweep_param_names: List[str], log=logger.info) -> Optional[str]:
    if df.empty:
        log("No data to plot.")
        return None

    os.makedirs(run.plots_dir, exist_ok=True)
    combined_csv = os.path.join(run.plots_dir, RESULTS_FILENAME)
    df.to_csv(combined_csv, index=False)
    log(f"Combined {len(df)} rows into {combined_csv}")

    solution_type = registry.solution_type_of(run.config.get("analysis_settings", {}))
    from ..workflow import companion
    html = figure_html(
        df, sweep_param_names, solution_type=solution_type,
        mode_markers=companion.mode_markers(run) if has_sparameters(df) else None,
        **default_axes(df, sweep_param_names, solution_type),
    )

    plot_path = os.path.join(run.plots_dir, PLOT_FILENAME)
    with open(plot_path, "w", encoding="utf-8") as handle:
        handle.write(html)
    log(f"Plot saved to {plot_path}")
    return plot_path
