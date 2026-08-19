"""
Plotly-based plotting for a run's combined results: saved as a
self-contained HTML file (plotly.js embedded, so it opens standalone
with no internet needed) plus a CSV, into the run's plots/ folder --
also what the web UI embeds directly for live viewing.
"""
from __future__ import annotations

import logging
import os
from typing import List, Optional

import pandas as pd
import plotly.express as px

logger = logging.getLogger("eigen_mode_analyze")


def plot_results(df: pd.DataFrame, run, sweep_param_names: List[str], log=logger.info) -> Optional[str]:
    if df.empty:
        log("No data to plot.")
        return None

    os.makedirs(run.plots_dir, exist_ok=True)
    combined_csv = os.path.join(run.plots_dir, "all_results.csv")
    df.to_csv(combined_csv, index=False)
    log(f"Combined {len(df)} rows into {combined_csv}")

    x = "freq_ghz"
    y = sweep_param_names[0] if sweep_param_names else "combo_id"
    color = "max_mag_e" if "max_mag_e" in df.columns else (
        sweep_param_names[1] if len(sweep_param_names) > 1 else None
    )
    hover_extra = [c for c in ("combo_id", "mode", "q", *sweep_param_names) if c in df.columns and c not in (x, y)]

    fig = px.scatter(
        df, x=x, y=y, color=color,
        hover_data=hover_extra,
        title="Eigenmode frequencies vs. swept parameter(s)",
        labels={x: "Frequency (GHz)"},
    )
    if y in df.columns and pd.api.types.is_numeric_dtype(df[y]):
        fig.update_yaxes(autorange="reversed")

    plot_path = os.path.join(run.plots_dir, "modes_scatter_plot.html")
    fig.write_html(plot_path, include_plotlyjs=True, full_html=True)
    log(f"Plot saved to {plot_path}")
    return plot_path
