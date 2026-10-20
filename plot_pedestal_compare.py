#!/usr/bin/env python3
"""Overlay S21 and S11 (2-20 GHz) for several HFSS runs, with a per-band table.

Standard library only (no matplotlib): writes one SVG file and prints the
band table to stdout.

Usage:
  python plot_pedestal_compare.py OUT.svg LABEL=RUN_DIR [LABEL=RUN_DIR ...]

RUN_DIR is a run folder that contains plots/all_results.csv.

Bands: 2-10 GHz is f <= 10; 10-17 GHz is 10 < f <= 17; 17-20 GHz is f > 17.
Mean S11 is over each band in dB. "Clean" is the share of points with
S21 above -3 dB. Points are the freq rows in all_results.csv (0.25 GHz step).
"""
import csv
import html
import os
import sys

FMIN, FMAX = 2.0, 20.0
BANDS = [("2-10 GHz", lambda f: f <= 10.0),
         ("10-17 GHz", lambda f: 10.0 < f <= 17.0),
         ("17-20 GHz", lambda f: f > 17.0)]
PALETTE = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#8c564b"]
DASHES = ["", "6 3", "2 2", "8 3 2 3", "", "6 3"]


def load_run(run_dir):
    path = os.path.join(run_dir, "plots", "all_results.csv")
    rows = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            f = float(r["freq_ghz"])
            if FMIN - 1e-9 <= f <= FMAX + 1e-9:
                rows.append((f, float(r["s11_db"]), float(r["s21_db"])))
    rows.sort()
    if len(rows) < 2:
        raise SystemExit(f"{path}: fewer than 2 points in {FMIN}-{FMAX} GHz")
    return rows


def band_stats(rows):
    stats = []
    for _, test in BANDS:
        sel = [(s11, s21) for f, s11, s21 in rows if test(f)]
        n = len(sel)
        mean_s11 = sum(s11 for s11, _ in sel) / n
        clean = 100.0 * sum(1 for _, s21 in sel if s21 > -3.0) / n
        stats.append((n, mean_s11, clean))
    return stats


def nice_range(values, lo_default, hi_default, step):
    lo = min(values + [lo_default])
    hi = max(values + [hi_default])
    lo = step * int(lo // step) - step
    hi = step * int(-(-hi // step)) + step
    return max(lo, lo_default), min(hi, hi_default)


def ticks(lo, hi, step):
    out, v = [], lo
    while v <= hi + 1e-9:
        out.append(round(v, 6))
        v += step
    return out


class Panel:
    """Maps data coordinates into SVG coordinates for one plot box."""

    def __init__(self, left, top, width, height, ymin, ymax):
        self.left, self.top, self.width, self.height = left, top, width, height
        self.ymin, self.ymax = ymin, ymax

    def x(self, f):
        return self.left + (f - FMIN) / (FMAX - FMIN) * self.width

    def y(self, v):
        return self.top + (self.ymax - v) / (self.ymax - self.ymin) * self.height


def draw_panel(out, panel, title, ylabel, series, yticks, ystep_label):
    p = panel
    out.append(f'<text x="{p.left}" y="{p.top - 12}" class="title">{html.escape(title)}</text>')
    for v in yticks:
        yy = p.y(v)
        out.append(f'<line x1="{p.left}" y1="{yy:.1f}" x2="{p.left + p.width}" y2="{yy:.1f}" class="grid"/>')
        out.append(f'<text x="{p.left - 8}" y="{yy + 4:.1f}" class="tick" text-anchor="end">{v:g}</text>')
    for f in ticks(FMIN, FMAX, 2.0):
        xx = p.x(f)
        out.append(f'<line x1="{xx:.1f}" y1="{p.top}" x2="{xx:.1f}" y2="{p.top + p.height}" class="grid"/>')
        out.append(f'<text x="{xx:.1f}" y="{p.top + p.height + 16}" class="tick" text-anchor="middle">{f:g}</text>')
    out.append(f'<rect x="{p.left}" y="{p.top}" width="{p.width}" height="{p.height}" class="frame"/>')
    out.append(f'<text x="{p.left - 44}" y="{p.top + p.height / 2:.1f}" class="axis" '
               f'transform="rotate(-90 {p.left - 44} {p.top + p.height / 2:.1f})" text-anchor="middle">'
               f'{html.escape(ylabel)}</text>')
    out.append(f'<text x="{p.left + p.width / 2:.1f}" y="{p.top + p.height + 34}" class="axis" '
               f'text-anchor="middle">Frequency (GHz)</text>')
    for i, (label, values) in enumerate(series):
        color = PALETTE[i % len(PALETTE)]
        dash = DASHES[i % len(DASHES)]
        pts = " ".join(f"{p.x(f):.1f},{p.y(v):.1f}" for f, v in values)
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        out.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.8"{dash_attr}/>')
    # Legend in the top-right of the box.
    lx, ly = p.left + p.width - 8, p.top + 10
    for i, (label, _) in enumerate(series):
        color = PALETTE[i % len(PALETTE)]
        dash = DASHES[i % len(DASHES)]
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        yy = ly + i * 16
        out.append(f'<line x1="{lx - 170}" y1="{yy:.1f}" x2="{lx - 146}" y2="{yy:.1f}" stroke="{color}" '
                   f'stroke-width="2"{dash_attr}/>')
        out.append(f'<text x="{lx - 140}" y="{yy + 4:.1f}" class="legend">{html.escape(label)}</text>')


def write_svg(path, runs, stats_by_label):
    W, H = 900, 1040
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
           '<style>'
           'text{font-family:Arial,Helvetica,sans-serif;fill:#222}'
           '.title{font-size:16px;font-weight:bold}'
           '.axis{font-size:13px}'
           '.tick{font-size:11px;fill:#555}'
           '.legend{font-size:12px}'
           '.grid{stroke:#e3e3e3;stroke-width:1}'
           '.frame{fill:none;stroke:#888;stroke-width:1}'
           '.th{font-size:12px;font-weight:bold}'
           '.td{font-size:12px}'
           '</style>',
           f'<rect width="{W}" height="{H}" fill="#fff"/>',
           f'<text x="{W / 2}" y="22" text-anchor="middle" class="title">TMM 10i chip, 0.25 GHz, driven, 2-20 GHz</text>']

    s21_all = [v for _, rows in runs for _, _, v in rows]
    s11_all = [v for _, rows in runs for _, v, _ in rows]
    s21_lo, s21_hi = nice_range(s21_all, -10.0, 0.0, 2.0)
    s11_lo, s11_hi = nice_range(s11_all, -40.0, 0.0, 5.0)

    left, width = 90, 760
    p21 = Panel(left, 56, width, 220, s21_lo, s21_hi)
    p11 = Panel(left, 344, width, 220, s11_lo, s11_hi)
    s21_series = [(label, [(f, s21) for f, _, s21 in rows]) for label, rows in runs]
    s11_series = [(label, [(f, s11) for f, s11, _ in rows]) for label, rows in runs]
    draw_panel(out, p21, "S21 (dB)", "S21 (dB)", s21_series, ticks(s21_lo, s21_hi, 2.0), "2")
    draw_panel(out, p11, "S11 (dB)", "S11 (dB)", s11_series, ticks(s11_lo, s11_hi, 5.0), "5")

    # Band table.
    ty = 640
    out.append(f'<text x="{left}" y="{ty - 16}" class="title">Per-band summary</text>')
    cols = ["Run", "S11 2-10", "S11 10-17", "S11 17-20", "Clean 2-10", "Clean 10-17", "Clean 17-20"]
    xs = [left, 330, 430, 530, 630, 720, 810]
    out.append(f'<line x1="{left}" y1="{ty + 6}" x2="{left + width}" y2="{ty + 6}" stroke="#888"/>')
    for c, x in zip(cols, xs):
        out.append(f'<text x="{x}" y="{ty + 22}" class="th">{html.escape(c)}</text>')
    out.append(f'<line x1="{left}" y1="{ty + 30}" x2="{left + width}" y2="{ty + 30}" stroke="#888"/>')
    row_y = ty + 50
    for label, _ in runs:
        cells = stats_by_label[label]
        vals = [f"{s[1]:.1f} dB" for s in cells] + [f"{s[2]:.0f} %" for s in cells]
        out.append(f'<text x="{xs[0]}" y="{row_y}" class="td">{html.escape(label)}</text>')
        for v, x in zip(vals, xs[1:]):
            out.append(f'<text x="{x}" y="{row_y}" class="td">{v}</text>')
        row_y += 22
    out.append(f'<text x="{left}" y="{row_y + 12}" class="tick">Bands: 2-10 GHz f&lt;=10; 10-17 GHz 10&lt;f&lt;=17; '
               f'17-20 GHz f&gt;17. Clean = share of points with S21 &gt; -3 dB.</text>')
    out.append('</svg>')
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))


def main(argv):
    if len(argv) < 3:
        raise SystemExit(__doc__)
    out_path = argv[1]
    runs = []
    for item in argv[2:]:
        if "=" not in item:
            raise SystemExit(f"expected LABEL=RUN_DIR, got {item!r}")
        label, run_dir = item.split("=", 1)
        runs.append((label, load_run(run_dir)))

    stats = {label: band_stats(rows) for label, rows in runs}
    write_svg(out_path, runs, stats)

    print(f"{'Run':<36}{'n 2-10':>7}{'n 10-17':>8}{'n 17-20':>8}"
          f"{'S11 2-10':>10}{'S11 10-17':>11}{'S11 17-20':>11}"
          f"{'Clean 2-10':>12}{'Clean 10-17':>13}{'Clean 17-20':>13}")
    for label, _ in runs:
        s = stats[label]
        print(f"{label:<36}{s[0][0]:>7}{s[1][0]:>8}{s[2][0]:>8}"
              f"{s[0][1]:>9.1f}{s[1][1]:>11.1f}{s[2][1]:>11.1f}"
              f"{s[0][2]:>11.0f}%{s[1][2]:>12.0f}%{s[2][2]:>12.0f}%")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main(sys.argv)
