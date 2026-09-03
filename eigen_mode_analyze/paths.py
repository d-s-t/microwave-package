"""
Central, single-source-of-truth paths for this package -- so every
module resolves the project root, the shared SolidWorks part library,
and the output-runs folder the same way.
"""
from __future__ import annotations

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent

BASE_PARTS_DIR = PROJECT_ROOT / "base_parts"
DEFAULT_EQUATIONS_FILE = BASE_PARTS_DIR / "default.txt"
DEFAULT_ASSEMBLY_FILE = BASE_PARTS_DIR / "all_components.SLDASM"

# Everything the Altium stage reads and writes. The generator and the
# copper exporter are source; the parameter file, the reports and the
# generated boards are per-run output and are not tracked.
ALTIUM_DIR = BASE_PARTS_DIR / "altium"
ALTIUM_GENERATOR = ALTIUM_DIR / "GenerateFromParams.pas"
ALTIUM_COPPER_EXPORTER = ALTIUM_DIR / "ExportCopper.pas"
ALTIUM_PARAMS_FILE = ALTIUM_DIR / "params.txt"
ALTIUM_GENERATE_REPORT = ALTIUM_DIR / "generate_report.txt"
ALTIUM_OUT_DIR = ALTIUM_DIR / "generated"
ALTIUM_COPPER_REPORT = ALTIUM_OUT_DIR / "copper_report.txt"

# The SMP connector's 3D model, as shipped by the vendor (Wuerth
# WR-SMP 60124102122403). A single solid -- there is no separated
# pin/dielectric/shell version of this part in any format the vendor
# provides -- so it goes into the model as one body.
SMP_MODEL_FILE = BASE_PARTS_DIR / "smp_connector.step"

# One SVG per sweep parameter, showing which dimension that parameter
# drives. Named after the parameter itself (wall_width.svg, ...) and
# produced by base_parts/parameters_svg.py from parameters_visual.SLDDRW.
PARAMETER_DRAWINGS_DIR = BASE_PARTS_DIR / "parameters_visual"

# Every run's generated STEP/AEDT files, state, and plots live under
# here -- one subfolder per run_id. This is output, not source, so it's
# excluded in .gitignore (these get backed up elsewhere -- see README).
RUNS_ROOT = PACKAGE_DIR / "runs"

# Third-party JS/WASM for the in-browser STEP viewer (three.js and
# occt-import-js). Not committed -- populated by `fetch_vendor.py`, and
# the web UI just hides the "3D view" button when it is absent.
VENDOR_DIR = PACKAGE_DIR / "vendor"
