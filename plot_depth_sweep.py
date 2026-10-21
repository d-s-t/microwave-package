#!/usr/bin/env python3
"""Pedestal depth figures, standard library only.

Zo: --line DEPTH=line_check.json (valid depths; mean/min/max over 1-20 GHz).
S21/S11: --drv LABEL=CSV where CSV has freq_ghz,s11_db,s21_db (2-20 GHz rows).
LABEL is a depth in mm, or 'ref' for the converged reference (drawn dashed / hollow).
"""
import argparse, csv, json, os

BANDS = [("2-10 GHz", lambda f: f <= 10.0), ("10-17 GHz", lambda f: 10.0 < f <= 17.0), ("17-20 GHz", lambda f: f > 17.0)]
BCOL = ["#1f77b4", "#d62728", "#2ca02c"]
DCOL = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#8c564b"]


def load(path):
    rows = []
    for r in csv.DictReader(open(path, newline="")):
        f = float(r["freq_ghz"])
        if 2.0 - 1e-9 <= f <= 20.0 + 1e-9:
            rows.append((f, float(r["s11_db"]), float(r["s21_db"])))
    rows.sort()
    return rows


def band_stats(rows):
    out = []
    for _, t in BANDS:
        sel = [r for r in rows if t(r[0])]
        n = len(sel)
        out.append({"n": n, "s11": sum(r[1] for r in sel) / n, "s21": sum(r[2] for r in sel) / n,
                    "clean": 100.0 * sum(1 for r in sel if r[2] > -3.0) / n})
    return out


def wrap(w, h, body):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
            '<style>text{font-family:Arial,Helvetica,sans-serif;fill:#222}.t{font-size:15px;font-weight:bold}'
            '.a{font-size:12px}.k{font-size:11px;fill:#555}.g{stroke:#e3e3e3}.f{fill:none;stroke:#888}</style>'
            f'<rect width="{w}" height="{h}" fill="#fff"/>{body}</svg>')


def zo_svg(line, invalid):
    W, H = 760, 430
    L, T, PW, PH = 80, 50, 620, 300
    lo, hi = 44.0, 60.0
    xmax = 1.1
    X = lambda d: L + d / xmax * PW
    Y = lambda v: T + (hi - v) / (hi - lo) * PH
    b = [f'<text x="{W/2}" y="26" text-anchor="middle" class="t">Die-line Zo (chip CPW, 1-20 GHz) vs pedestal depth, line model</text>']
    for v in range(44, 61, 2):
        b.append(f'<line x1="{L}" y1="{Y(v):.1f}" x2="{L+PW}" y2="{Y(v):.1f}" class="g"/>')
        b.append(f'<text x="{L-8}" y="{Y(v)+4:.1f}" text-anchor="end" class="k">{v}</text>')
    b.append(f'<line x1="{L}" y1="{Y(50):.1f}" x2="{L+PW}" y2="{Y(50):.1f}" stroke="#333" stroke-dasharray="4 3"/>')
    b.append(f'<text x="{L+PW-4}" y="{Y(50)-5:.1f}" text-anchor="end" class="k">50 ohm, tolerance +-2.5 ohm</text>')
    b.append(f'<rect x="{L}" y="{T}" width="{PW}" height="{PH}" class="f"/>')
    for d in sorted(line):
        j = line[d]; x = X(d)
        b.append(f'<line x1="{x:.1f}" y1="{Y(j["max_ohm"]):.1f}" x2="{x:.1f}" y2="{Y(j["min_ohm"]):.1f}" stroke="#1f77b4" stroke-width="2"/>')
        b.append(f'<line x1="{x-5:.1f}" y1="{Y(j["max_ohm"]):.1f}" x2="{x+5:.1f}" y2="{Y(j["max_ohm"]):.1f}" stroke="#1f77b4" stroke-width="2"/>')
        b.append(f'<line x1="{x-5:.1f}" y1="{Y(j["min_ohm"]):.1f}" x2="{x+5:.1f}" y2="{Y(j["min_ohm"]):.1f}" stroke="#1f77b4" stroke-width="2"/>')
        b.append(f'<circle cx="{x:.1f}" cy="{Y(j["mean_ohm"]):.1f}" r="5" fill="#1f77b4"/>')
        b.append(f'<text x="{x:.1f}" y="{T+PH+16}" text-anchor="middle" class="k">{d:g}</text>')
    for d in invalid:
        x = X(d)
        b.append(f'<line x1="{x-7:.1f}" y1="{Y(50)-7:.1f}" x2="{x+7:.1f}" y2="{Y(50)+7:.1f}" stroke="#d62728" stroke-width="2.5"/>')
        b.append(f'<line x1="{x+7:.1f}" y1="{Y(50)-7:.1f}" x2="{x-7:.1f}" y2="{Y(50)+7:.1f}" stroke="#d62728" stroke-width="2.5"/>')
        b.append(f'<text x="{x:.1f}" y="{T+PH+16}" text-anchor="middle" class="k">{d:g}</text>')
        b.append(f'<text x="{x:.1f}" y="{Y(50)+24:.1f}" text-anchor="middle" class="k" fill="#d62728">invalid: solver error</text>')
    b.append(f'<text x="{L+PW/2}" y="{H-10}" text-anchor="middle" class="a">pedestal_depth (mm). Blue: bar = min to max over 1-20 GHz, dot = mean. Red x = no valid result.</text>')
    b.append(f'<text x="18" y="{T+PH/2}" transform="rotate(-90 18 {T+PH/2})" text-anchor="middle" class="a">Zo (ohm)</text>')
    return wrap(W, H, "".join(b))


def s21_svg(curves):
    W, H = 760, 620
    L, PW, PH = 80, 620, 210
    panels = [("S21, full range (dB)", -60.0, 0.0, 10.0, 50), ("S21, zoom (dB)", -12.0, 0.0, 2.0, 330)]
    b = [f'<text x="{W/2}" y="26" text-anchor="middle" class="t">Driven S21, 2-20 GHz, TMM 10i chip, quick settings, by pedestal depth</text>']
    for title, lo, hi, step, T in panels:
        X = lambda f: L + (f - 2.0) / 18.0 * PW
        Y = lambda v: T + (hi - v) / (hi - lo) * PH
        b.append(f'<text x="{L}" y="{T-8}" class="a">{title}</text>')
        v = lo
        while v <= hi + 1e-9:
            b.append(f'<line x1="{L}" y1="{Y(v):.1f}" x2="{L+PW}" y2="{Y(v):.1f}" class="g"/>')
            b.append(f'<text x="{L-8}" y="{Y(v)+4:.1f}" text-anchor="end" class="k">{v:g}</text>')
            v += step
        b.append(f'<line x1="{L}" y1="{Y(-3):.1f}" x2="{L+PW}" y2="{Y(-3):.1f}" stroke="#333" stroke-dasharray="4 3"/>')
        b.append(f'<rect x="{L}" y="{T}" width="{PW}" height="{PH}" class="f"/>')
        for f in range(2, 21, 2):
            b.append(f'<text x="{X(f):.1f}" y="{T+PH+15}" text-anchor="middle" class="k">{f}</text>')
        for i, (lab, rows, dash) in enumerate(curves):
            col = DCOL[i % len(DCOL)]
            pts = " ".join(f"{X(f):.1f},{Y(max(s, lo)):.1f}" for f, _, s in rows)
            da = ' stroke-dasharray="6 3"' if dash else ""
            b.append(f'<polyline points="{pts}" fill="none" stroke="{col}" stroke-width="1.6"{da}/>')
        if T == 50:
            for i, (lab, rows, dash) in enumerate(curves):
                b.append(f'<text x="{L+PW-6}" y="{T+14+i*14}" text-anchor="end" class="a" fill="{DCOL[i % len(DCOL)]}">{lab}</text>')
    b.append(f'<text x="{L+PW/2}" y="{H-8}" text-anchor="middle" class="a">Frequency (GHz). Dashed = converged reference at 0.5 mm (run_20261020_150512), not in the sweep.</text>')
    return wrap(W, H, "".join(b))


def bands_svg(stats, ref_stats):
    W, H = 760, 800
    depths = sorted(stats)
    panels = [("Mean S11 (dB) per band", "s11", -10.0, 0.0, 2.0),
              ("Mean S21 (dB) per band", "s21", -25.0, 0.0, 5.0),
              ("Clean share (S21 > -3 dB), %", "clean", 0.0, 100.0, 20.0)]
    b = [f'<text x="{W/2}" y="26" text-anchor="middle" class="t">Band means and clean share vs pedestal depth (quick settings)</text>']
    for p, (title, key, lo, hi, step) in enumerate(panels):
        L, T, PW, PH = 80, 50 + p * 250, 620, 190
        b.append(f'<text x="{L}" y="{T-8}" class="a">{title}</text>')
        v = lo
        while v <= hi + 1e-9:
            y = T + (hi - v) / (hi - lo) * PH
            b.append(f'<line x1="{L}" y1="{y:.1f}" x2="{L+PW}" y2="{y:.1f}" class="g"/>')
            b.append(f'<text x="{L-8}" y="{y+4:.1f}" text-anchor="end" class="k">{v:g}</text>')
            v += step
        b.append(f'<rect x="{L}" y="{T}" width="{PW}" height="{PH}" class="f"/>')
        xs = {d: L + (i + 0.5) / len(depths) * PW for i, d in enumerate(depths)}
        for bi, (bname, _) in enumerate(BANDS):
            col = BCOL[bi]
            pts = []
            for d in depths:
                x = xs[d]; y = T + (hi - stats[d][bi][key]) / (hi - lo) * PH
                pts.append(f"{x:.1f},{y:.1f}")
                b.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="{col}"/>')
            b.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="{col}" stroke-width="1.5"/>')
            if ref_stats is not None and 0.5 in xs:
                x = xs[0.5]; y = T + (hi - ref_stats[bi][key]) / (hi - lo) * PH
                b.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" fill="#fff" stroke="{col}" stroke-width="1.8"/>')
            b.append(f'<text x="{L+PW-6}" y="{T+14+bi*15}" text-anchor="end" class="a" fill="{col}">{bname}</text>')
        for d in depths:
            b.append(f'<text x="{xs[d]:.1f}" y="{T+PH+15}" text-anchor="middle" class="k">{d:g}</text>')
    b.append(f'<text x="{W/2}" y="{H-24}" text-anchor="middle" class="a">pedestal_depth (mm). Open circle at 0.5 = converged reference (run_20261020_150512), for comparison only.</text>')
    b.append(f'<text x="{W/2}" y="{H-8}" text-anchor="middle" class="k">Bands: 2-10 GHz (33 points), 10-17 GHz (28), 17-20 GHz (12). One run per depth.</text>')
    return wrap(W, H, "".join(b))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--line", action="append", default=[], metavar="DEPTH=JSON")
    ap.add_argument("--invalid", default="", help="comma list of depths with no valid Zo")
    ap.add_argument("--drv", action="append", default=[], metavar="LABEL=CSV")
    a = ap.parse_args()
    os.makedirs(a.outdir, exist_ok=True)
    tag = "2026-10-21"
    line = {float(k): json.load(open(p)) for k, p in (x.split("=", 1) for x in a.line)}
    invalid = [float(x) for x in a.invalid.split(",") if x.strip()]
    open(os.path.join(a.outdir, f"zo_vs_depth_{tag}.svg"), "w").write(zo_svg(line, invalid))
    drv, ref = {}, None
    for item in a.drv:
        lab, p = item.split("=", 1)
        if lab == "ref":
            ref = load(p)
        else:
            drv[float(lab)] = load(p)
    curves = [(f"depth {d:g} mm", drv[d], False) for d in sorted(drv)]
    if ref is not None:
        curves.append(("0.5 mm converged ref", ref, True))
    open(os.path.join(a.outdir, f"s21_overlay_{tag}.svg"), "w").write(s21_svg(curves))
    stats = {d: band_stats(r) for d, r in drv.items()}
    ref_stats = band_stats(ref) if ref is not None else None
    open(os.path.join(a.outdir, f"bands_vs_depth_{tag}.svg"), "w").write(bands_svg(stats, ref_stats))
    print("| depth | band | n | mean S11 dB | mean S21 dB | clean % |")
    for d in sorted(stats):
        for (bn, _), s in zip(BANDS, stats[d]):
            print(f"| {d:g} | {bn} | {s['n']} | {s['s11']:.2f} | {s['s21']:.2f} | {s['clean']:.0f} |")
    if ref_stats:
        for (bn, _), s in zip(BANDS, ref_stats):
            print(f"| 0.5 converged ref | {bn} | {s['n']} | {s['s11']:.2f} | {s['s21']:.2f} | {s['clean']:.0f} |")
    print("| Zo (ohm), depth | mean | min | max |")
    for d in sorted(line):
        j = line[d]
        print(f"| {d:g} | {j['mean_ohm']:.2f} | {j['min_ohm']:.2f} | {j['max_ohm']:.2f} |")


if __name__ == "__main__":
    main()
