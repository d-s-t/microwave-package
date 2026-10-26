"""
Smith chart of a run's reflection coefficient, drawn with plotly like the
rest of the results.

S11 (or S22) in dB and degrees is turned into the complex reflection
coefficient G = 10^(dB/20) * e^(j*deg) and plotted in the G plane inside
the unit circle, over the standard grid of constant-resistance and
constant-reactance circles. The reference is the port impedance the
S-parameters were computed against, so the normalised impedance
z = (1 + G) / (1 - G) reads as Z / Z0 and Z = z * Z0 in ohms.

Only the pure functions here are unit-tested directly; `smith_html` is
the figure, built from them.
"""
from __future__ import annotations

import html
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from . import plotting

# The ports whose reflection a Smith chart can show, and their columns in
# the results table. S21 and S12 are transmission, not reflection, and sit
# outside the unit circle's meaning, so they are not offered.
PORTS = {"s11": ("s11_db", "s11_deg"), "s22": ("s22_db", "s22_deg")}
PORT_LABELS = {"s11": "S11", "s22": "S22"}
DEFAULT_PORT = "s11"

# The constant-resistance and constant-reactance circles drawn on the grid,
# labelled with the normalised value.
RESISTANCE_VALUES = (0.2, 0.5, 1.0, 2.0, 5.0)
REACTANCE_VALUES = (0.2, 0.5, 1.0, 2.0, 5.0)

_FREQUENCY_COLUMN = "freq_ghz"


def reflection(mag_db, phase_deg) -> np.ndarray:
    """Complex reflection coefficient from a magnitude in dB and a phase in
    degrees, as the results table stores them."""
    mag = np.power(10.0, np.asarray(mag_db, dtype=float) / 20.0)
    return mag * np.exp(1j * np.radians(np.asarray(phase_deg, dtype=float)))


def normalised_impedance(gamma) -> np.ndarray:
    """z = (1 + G) / (1 - G), the impedance divided by the reference. A
    reflection of exactly 1 (an open) is infinite and comes back as NaN."""
    gamma = np.asarray(gamma, dtype=complex)
    with np.errstate(divide="ignore", invalid="ignore"):
        z = (1 + gamma) / (1 - gamma)
    return np.where(np.isfinite(z), z, np.nan)


def resistance_circle(r: float, points: int = 361) -> np.ndarray:
    """Constant normalised resistance r: centre r/(r+1), radius 1/(r+1)."""
    theta = np.linspace(0.0, 2.0 * np.pi, points)
    centre = r / (r + 1.0)
    radius = 1.0 / (r + 1.0)
    return centre + radius * np.exp(1j * theta)


def reactance_circle(x: float, points: int = 721) -> np.ndarray:
    """Constant normalised reactance x: centre (1, 1/x), radius 1/|x|.
    Only the part inside the unit circle is kept: points outside the chart
    are NaN, which breaks the polyline in plotly so no segment is drawn
    across the outside."""
    theta = np.linspace(0.0, 2.0 * np.pi, points)
    centre = 1.0 + 1j / x
    radius = 1.0 / abs(x)
    arc = centre + radius * np.exp(1j * theta)
    return np.where(np.abs(arc) <= 1.0 + 1e-9, arc, np.nan + 0j)


def grid_traces(dark: bool = False) -> List[go.Scatter]:
    """The chart's background: the unit circle, the real axis, the constant
    resistance and reactance circles, each with its label."""
    colour = "#6b7280" if dark else "#9ca3af"
    faint = "rgba(107,114,128,0.45)" if dark else "rgba(156,163,175,0.6)"
    traces: List[go.Scatter] = []

    theta = np.linspace(0.0, 2.0 * np.pi, 361)
    unit = np.exp(1j * theta)
    traces.append(go.Scatter(x=unit.real, y=unit.imag, mode="lines", line=dict(color=colour, width=1.2),
                             hoverinfo="skip", showlegend=False))
    traces.append(go.Scatter(x=[-1.0, 1.0], y=[0.0, 0.0], mode="lines", line=dict(color=faint, width=1),
                             hoverinfo="skip", showlegend=False))

    label_x, label_y, label_text = [], [], []
    for r in RESISTANCE_VALUES:
        circle = resistance_circle(r)
        traces.append(go.Scatter(x=circle.real, y=circle.imag, mode="lines",
                                 line=dict(color=faint, width=1), hoverinfo="skip", showlegend=False))
        # Labelled on the real axis, where the circle meets it on the right.
        label_x.append((r - 1.0) / (r + 1.0))
        label_y.append(0.0)
        label_text.append(f"{r:g}")

    for x in REACTANCE_VALUES:
        for sign in (1.0, -1.0):
            arc = reactance_circle(sign * x)
            traces.append(go.Scatter(x=arc.real, y=arc.imag, mode="lines",
                                     line=dict(color=faint, width=1), hoverinfo="skip", showlegend=False))
            # Labelled where the arc meets the unit circle: z = jx gives
            # G = (jx - 1) / (jx + 1), which has |G| = 1 for every x.
            gamma_edge = (1j * sign * x - 1.0) / (1j * sign * x + 1.0)
            label_x.append(gamma_edge.real * 1.04)
            label_y.append(gamma_edge.imag * 1.04)
            label_text.append(f"{sign * x:+g}j" if sign * x != 0 else "")

    traces.append(go.Scatter(x=label_x, y=label_y, mode="text", text=label_text,
                             textfont=dict(size=10, color=colour), hoverinfo="skip", showlegend=False))
    return traces


def available_ports(df: pd.DataFrame) -> List[str]:
    """The reflection ports this table holds data for. An eigenmode-only
    run has no S-parameter columns at all, so none is offered."""
    ports = []
    for port, (db_column, deg_column) in PORTS.items():
        if db_column in df.columns and deg_column in df.columns and df[[db_column, deg_column]].notna().any().all():
            ports.append(port)
    return ports


def _trace_groups(df: pd.DataFrame) -> List[tuple]:
    """(key, rows) per trace: a curve per combination of each run, split
    where a fine window leaves a gap, so no line runs across the gap."""
    if plotting.LINE_COLUMN in df.columns:
        keys = [plotting.LINE_COLUMN]
    else:
        keys = [c for c in (plotting.RUN_ID_COLUMN, "combo_id") if c in df.columns]
    if not keys:
        return [("", df)]
    return [(key, group) for key, group in df.groupby(keys, sort=False)]


def _label(rows: pd.DataFrame) -> str:
    run = str(rows[plotting.RUN_COLUMN].iloc[0]) if plotting.RUN_COLUMN in rows.columns else ""
    combo = str(rows["combo_id"].iloc[0]) if "combo_id" in rows.columns else ""
    return " · ".join(part for part in (run, combo) if part) or "run"


def smith_html(
    df: pd.DataFrame,
    port: str = DEFAULT_PORT,
    reference_ohm: float = 50.0,
    dark: bool = False,
    plotlyjs_src: Optional[str] = None,
    run_colours: Optional[Dict[str, str]] = None,
    note: Optional[str] = None,
) -> str:
    """The Smith chart of one reflection port for every trace in `df`.

    Each trace (a combination of a run) keeps its run's colour as its line,
    and its points are coloured by frequency on one shared colour bar, so
    where along the band a curve sits on the chart can be read against the
    frequency. Hovering a point shows its frequency, |G|, phase, the
    normalised impedance, and the impedance in ohms at `reference_ohm`.
    """
    port = port if port in PORTS else DEFAULT_PORT
    db_column, deg_column = PORTS[port]
    label = PORT_LABELS[port]
    if db_column not in df.columns or deg_column not in df.columns:
        return f"<p>No {label} data in this run's results.</p>"

    df = df.dropna(subset=[db_column, deg_column, _FREQUENCY_COLUMN])
    if df.empty:
        return f"<p>No {label} points to draw.</p>"

    template = "plotly_dark" if dark else "plotly_white"
    palette = plotting.RUN_COLOURS["dark" if dark else "light"]
    colour_low, colour_high = float(df[_FREQUENCY_COLUMN].min()), float(df[_FREQUENCY_COLUMN].max())

    figure = go.Figure(grid_traces(dark))
    seen_names = set()
    for index, (key, rows) in enumerate(_trace_groups(df)):
        rows = rows.sort_values(_FREQUENCY_COLUMN, kind="stable")
        gamma = reflection(rows[db_column], rows[deg_column])
        z = normalised_impedance(gamma)
        ohm = z * reference_ohm
        magnitude = np.abs(gamma)
        phase = np.degrees(np.angle(gamma))
        name = _label(rows)
        # A compared run keeps its own colour; a lone run's traces take the
        # palette in turn.
        colour = palette[index % len(palette)]
        if run_colours and plotting.RUN_COLUMN in rows.columns:
            colour = run_colours.get(str(rows[plotting.RUN_COLUMN].iloc[0]), colour)

        custom = np.column_stack([
            rows[_FREQUENCY_COLUMN].to_numpy(float), magnitude, phase,
            z.real, z.imag, ohm.real, ohm.imag,
        ])
        first = name not in seen_names
        seen_names.add(name)
        figure.add_trace(go.Scatter(
            x=gamma.real, y=gamma.imag, mode="lines+markers", name=name,
            legendgroup=name, showlegend=first,
            line=dict(color=colour, width=1.6),
            marker=dict(
                size=6, color=rows[_FREQUENCY_COLUMN].to_numpy(float), cmin=colour_low, cmax=colour_high,
                colorscale="Viridis", showscale=first and index == 0,
                colorbar=dict(title=dict(text="GHz"), thickness=12, len=0.6),
            ),
            customdata=custom,
            hovertemplate=(
                "f %{customdata[0]:.4f} GHz<br>"
                "|" + label + "| %{customdata[1]:.3f}<br>"
                "phase %{customdata[2]:.1f}&deg;<br>"
                "z = %{customdata[3]:.3f} %{customdata[4]:+.3f}j<br>"
                "Z = %{customdata[5]:.1f} %{customdata[6]:+.1f}j &Omega;"
                "<extra>" + html.escape(name) + "</extra>"
            ),
        ))

    figure.update_layout(
        template=template,
        xaxis=dict(range=[-1.08, 1.08], scaleanchor="y", scaleratio=1, visible=False),
        yaxis=dict(range=[-1.08, 1.08], visible=False),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=20, r=20, t=30, b=20),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, title_text=""),
        showlegend=True,
    )

    if not plotlyjs_src:
        return figure.to_html(include_plotlyjs=True, full_html=True)
    fragment = figure.to_html(include_plotlyjs=False, full_html=False)
    scheme = "dark" if dark else "light"
    note_html = f"<p class='note'>{html.escape(note)}</p>" if note else ""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<script src='{plotlyjs_src}'></script>"
        "<style>html,body{margin:0;padding:0;background:transparent;"
        f"color-scheme:{scheme}}}"
        ".note{font:13px system-ui,sans-serif;color:#888;margin:.5rem .8rem 0}</style>"
        f"</head><body>{note_html}{fragment}</body></html>"
    )
