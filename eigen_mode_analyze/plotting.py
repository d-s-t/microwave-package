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
    "chip_mag_E_max": "Chip max |E| (V/m)",
    "chip_mag_E_mean": "Chip mean |E| (V/m)",
    "vacuum_mag_E_max": "Vacuum max |E| (V/m)",
    "vacuum_mag_E_mean": "Vacuum mean |E| (V/m)",
}

# The sweep reads most naturally with the parameter you varied along the
# bottom and the frequency it produced up the side.
DEFAULT_Y = "freq_ghz"


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

    x = usable(x, options[0])
    default_y = DEFAULT_Y if DEFAULT_Y in df.columns and DEFAULT_Y != x else next(
        (o for o in options if o != x), x)
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


def _interpolate(points, axis: int, resolution: int = 120):
    """Turn scattered mesh-node samples into a grid that can be drawn as
    a heatmap, over the two axes that are not the body's thin one."""
    import numpy as np
    from scipy.interpolate import griddata

    across = [a for a in range(3) if a != axis]
    # coordinates come out of AEDT in metres; millimetres read better
    horizontal = np.array([c[across[0]] for c, _ in points]) * 1000.0
    vertical = np.array([c[across[1]] for c, _ in points]) * 1000.0
    values = np.array([v for _, v in points])

    x = np.linspace(horizontal.min(), horizontal.max(), resolution)
    y = np.linspace(vertical.min(), vertical.max(), resolution)
    grid_x, grid_y = np.meshgrid(x, y)
    grid_z = griddata((horizontal, vertical), values, (grid_x, grid_y), method="linear")
    return x, y, grid_z, across


def chip_heatmap_html(
    entries: List[dict],
    dark: bool = False,
    plotlyjs_src: Optional[str] = None,
    columns: int = 3,
) -> str:
    """Chip top-face |E| as a heatmap, for one variant or all of them.

    Every panel is drawn against one shared colour axis, so the grid can
    be read across variants -- a per-panel scale would make the weakest
    and strongest chips look identical.
    """
    from plotly.subplots import make_subplots
    import plotly.graph_objects as go

    usable = [e for e in entries if e.get("points")]
    if not usable:
        return "<p>No chip field data in this run yet.</p>"

    lowest = min(min(v for _, v in e["points"]) for e in usable)
    highest = max(max(v for _, v in e["points"]) for e in usable)

    count = len(usable)
    cols = 1 if count == 1 else min(columns, count)
    rows = (count + cols - 1) // cols

    figure = make_subplots(
        rows=rows, cols=cols,
        subplot_titles=[e["label"] for e in usable],
        horizontal_spacing=0.08, vertical_spacing=0.12 if rows > 1 else 0.0,
    )

    axis_names = None
    for index, entry in enumerate(usable):
        row, col = index // cols + 1, index % cols + 1
        x, y, z, across = _interpolate(entry["points"], entry["axis"])
        axis_names = ("xyz"[across[0]], "xyz"[across[1]])
        figure.add_trace(
            go.Heatmap(x=x, y=y, z=z, coloraxis="coloraxis",
                       hovertemplate="%{x:.2f}, %{y:.2f} mm<br>|E| = %{z:.4g}<extra></extra>"),
            row=row, col=col,
        )
        figure.update_xaxes(title_text=f"{axis_names[0]} (mm)", row=row, col=col,
                            constrain="domain")
        figure.update_yaxes(title_text=f"{axis_names[1]} (mm)", row=row, col=col,
                            scaleanchor=f"x{'' if index == 0 else index + 1}", scaleratio=1)

    figure.update_layout(
        coloraxis=dict(
            cmin=lowest, cmax=highest, colorscale="Turbo",
            colorbar=dict(title="|E| (V/m)"),
        ),
        template="plotly_dark" if dark else "plotly_white",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        height=max(320, 300 * rows),
        margin=dict(l=60, r=30, t=50, b=50),
        showlegend=False,
    )

    if not plotlyjs_src:
        return figure.to_html(include_plotlyjs=True, full_html=True)
    fragment = figure.to_html(include_plotlyjs=False, full_html=False)
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
        x=swept[0] if swept else "combo_id",
        y=DEFAULT_Y,
        color="chip_mag_E_max" if "chip_mag_E_max" in df.columns else (swept[1] if len(swept) > 1 else None),
    )

    plot_path = os.path.join(run.plots_dir, "modes_scatter_plot.html")
    with open(plot_path, "w", encoding="utf-8") as handle:
        handle.write(html)
    log(f"Plot saved to {plot_path}")
    return plot_path
