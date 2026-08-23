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

# One SVG per sweep parameter, showing which dimension that parameter
# drives. Named after the parameter itself (wall_width.svg, ...) and
# produced by base_parts/parameters_svg.py from parameters_visual.SLDDRW.
PARAMETER_DRAWINGS_DIR = BASE_PARTS_DIR / "parameters_visual"

# Every run's generated STEP/AEDT files, state, and plots live under
# here -- one subfolder per run_id. This is output, not source, so it's
# excluded in .gitignore (these get backed up elsewhere -- see README).
RUNS_ROOT = PACKAGE_DIR / "runs"
