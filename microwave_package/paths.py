"""
Central, single-source-of-truth paths for this package -- so every
module resolves the project root, the shared SolidWorks part library,
and the output-runs folder the same way.

PACKAGE_DIR is this file's own folder, so it stays correct wherever the
package is checked out; PROJECT_ROOT is its parent, the repo root.
"""
from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent

BASE_PARTS_DIR = PROJECT_ROOT / "base_parts"
DEFAULT_EQUATIONS_FILE = BASE_PARTS_DIR / "default.txt"

# The two parts the AEDT stage assembles itself (see model_builder.place_lid_inside
# and model_builder.build_vacuum) -- each rebuilt and exported to its own STEP
# file straight from the part, with no assembly and no mates involved. Keyed
# the same way step_generation.Session.build and model_builder.build_model key
# their STEP paths ("box", "lid_inside"), so the two stay in step.
SOLIDWORKS_PART_FILES = {
    "box": BASE_PARTS_DIR / "box.SLDPRT",
    "lid_inside": BASE_PARTS_DIR / "lid_inside.SLDPRT",
}

# Everything the Altium stage reads and writes. The generator and the
# copper exporter are source; the parameter file, the reports and the
# generated boards are per-run output and are not tracked.
ALTIUM_DIR = BASE_PARTS_DIR / "altium"
ALTIUM_GENERATOR = ALTIUM_DIR / "GenerateFromParams.pas"
ALTIUM_COPPER_EXPORTER = ALTIUM_DIR / "ExportCopper.pas"
ALTIUM_PARAMS_FILE = ALTIUM_DIR / "params.txt"
ALTIUM_GENERATE_REPORT = ALTIUM_DIR / "generate_report.txt"
ALTIUM_OUT_DIR = ALTIUM_DIR / "generated"
# A script that does nothing, launched ahead of each real one to find out
# whether Altium will take a script at all, or is sitting on a dialog.
ALTIUM_PING = ALTIUM_DIR / "Ping.pas"
# What Altium actually runs: each script above with "<ALTIUM_DIR>" written
# out as this checkout's ALTIUM_DIR. DelphiScript cannot find its own
# folder, so the sources leave it blank rather than name one machine's.
ALTIUM_RESOLVED_DIR = ALTIUM_DIR / "resolved"

# The vendor's own PCB library for the SMP launch, so the generator can
# place the real footprint -- lands, silkscreen, courtyard and 3D body --
# rather than drawing four pads that merely stand where the lands are.
#
# Extracted from the shipped `.IntLib` with `vendor/extract_intlib.py`,
# because an IntLib is a compiled compound file and Altium cannot open
# one as a library directly. Checked in as an artifact, the same way the
# split connector STEP is: it is a vendor deliverable, not something a
# run regenerates.
ALTIUM_SMP_LIBRARY = ALTIUM_DIR / "vendor" / "Altium_WR-SMP (rev22a).PcbLib"

# What that footprint actually measures, read out of the library by
# MeasureFootprint.pas and committed. SOURCE, not per-run output: a
# sweep never runs the measurement, it only reads the result.
#
# It exists because only Altium can open a PcbLib, and `design.smp`
# needs the numbers in Python. Measuring once and committing beats
# either retyping them off a drawing or opening Altium on every import.
# Re-measure when the library or the footprint name changes --
# `altium_generation.measure_footprint()` does it.
ALTIUM_SMP_FOOTPRINT_FILE = ALTIUM_DIR / "vendor" / "footprint.txt"
ALTIUM_FOOTPRINT_MEASURER = ALTIUM_DIR / "MeasureFootprint.pas"
ALTIUM_MEASURE_REPORT = ALTIUM_DIR / "measure_report.txt"
ALTIUM_COPPER_REPORT = ALTIUM_OUT_DIR / "copper_report.txt"
# The bond layout, computed in Python and read by both Altium (for
# the pads) and HFSS (for the wires), so the two agree.
ALTIUM_BONDS_FILE = ALTIUM_OUT_DIR / "bonds.txt"

# The SMP connector's 3D model (Wuerth WR-SMP 60124102122403). The
# vendor ships one fused solid in every format, so it splits into three
# named solids -- outer conductor / centre conductor / dielectric -- by
# the companion project github.com/d-s-t/wr-smp-connector, and HFSS
# imports it as three bodies so the insulator bead is modelled as a
# dielectric rather than swallowed into a metal lump.
SMP_MODEL_FILE = BASE_PARTS_DIR / "smp_connector_split.step"

# One SVG per sweep parameter, showing which dimension that parameter
# drives. Named after the parameter itself (wall_width.svg, ...) and
# produced by base_parts/parameters_svg.py from parameters_visual.SLDDRW.
PARAMETER_DRAWINGS_DIR = BASE_PARTS_DIR / "parameters_visual"

# Every run's generated STEP/AEDT files, state, and plots live under
# here -- one subfolder per run_id.
#
# At the repo root rather than inside the package: this is output, not
# source, and a run's models folder is the largest thing this project
# produces by a wide margin. Keeping gigabytes of AEDT projects inside
# the importable package meant every tool that walked the source tree
# walked those too.
#
# MICROWAVE_RUNS_PATH moves it somewhere else entirely -- another disk,
# a share, wherever there is room -- without touching the code. Runs
# already written under the old location can simply be moved to the new
# one; nothing inside a run folder records where it lives.
RUNS_ROOT = Path(os.environ.get("MICROWAVE_RUNS_PATH") or (PROJECT_ROOT / "runs"))

# Third-party JS/WASM for the in-browser STEP viewer (three.js and
# occt-import-js). Not committed -- populated by `fetch_vendor.py`, and
# the web UI just hides the "3D view" button when it is absent.
VENDOR_DIR = PACKAGE_DIR / "vendor"
