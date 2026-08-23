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

from . import parameter_meta

logger = logging.getLogger("eigen_mode_analyze")

# Result quantities a run can plot, and how they are labelled. Keys are
# the column names post-processing writes into the results table; a
# quantity only shows up in the axis pickers if it is actually present.
RESULT_FIELDS: Dict[str, str] = {
    "freq_ghz": "Frequency (GHz)",
    "q": "Q factor",
    "max_mag_e": "Max |E| (V/m)",
    "mean_mag_e": "Mean |E| (V/m)",
}

DEFAULT_X = "freq_ghz"


def axis_options(df: pd.DataFrame, sweep_param_names: List[str]) -> List[Dict[str, str]]:
    """Everything either axis can be set to, for this run's data:
    its swept parameters first, then whichever results exist."""
    options = []
    for name in sweep_param_names:
        column = parameter_meta.column_name(name)
        if column in df.columns:
            options.append({"value": column, "label": parameter_meta.axis_label(name), "kind": "parameter"})
    for column, label in RESULT_FIELDS.items():
        if column in df.columns and df[column].notna().any():
            options.append({"value": column, "label": label, "kind": "result"})
    if "combo_id" in df.columns:
        options.append({"value": "combo_id", "label": "Combination", "kind": "other"})
    return options


def label_for_column(column: str, sweep_param_names: List[str]) -> str:
    for name in sweep_param_names:
        if column == parameter_meta.column_name(name):
            return parameter_meta.axis_label(name)
    if column in RESULT_FIELDS:
        return RESULT_FIELDS[column]
    return "Combination" if column == "combo_id" else column


def figure_html(
    df: pd.DataFrame,
    sweep_param_names: List[str],
    x: Optional[str] = None,
    y: Optional[str] = None,
    color: Optional[str] = None,
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

    x = usable(x, DEFAULT_X if DEFAULT_X in df.columns else options[0])
    default_y = next((o for o in options if o != x), x)
    y = usable(y, default_y)
    if color and color not in df.columns:
        color = None

    labels = {column: label_for_column(column, sweep_param_names) for column in df.columns}

    figure = px.scatter(
        df, x=x, y=y, color=color,
        hover_data=[c for c in ("combo_id", "mode", *options) if c in df.columns and c not in (x, y)],
        labels=labels,
        template="plotly_dark" if dark else "plotly_white",
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
        f"color-scheme:{scheme}}}</style>"
        f"</head><body>{fragment}</body></html>"
    )


def plot_results(df: pd.DataFrame, run, sweep_param_names: List[str], log=logger.info) -> Optional[str]:
    if df.empty:
        log("No data to plot.")
        return None

    os.makedirs(run.plots_dir, exist_ok=True)
    combined_csv = os.path.join(run.plots_dir, "all_results.csv")
    df.to_csv(combined_csv, index=False)
    log(f"Combined {len(df)} rows into {combined_csv}")

    swept = [parameter_meta.column_name(n) for n in sweep_param_names]
    html = figure_html(
        df, sweep_param_names,
        x=DEFAULT_X,
        y=swept[0] if swept else "combo_id",
        color="max_mag_e" if "max_mag_e" in df.columns else (swept[1] if len(swept) > 1 else None),
    )

    plot_path = os.path.join(run.plots_dir, "modes_scatter_plot.html")
    with open(plot_path, "w", encoding="utf-8") as handle:
        handle.write(html)
    log(f"Plot saved to {plot_path}")
    return plot_path
