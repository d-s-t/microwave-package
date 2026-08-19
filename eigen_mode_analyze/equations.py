"""
Parsing/editing helpers for SolidWorks equation files (e.g.
`base_parts/default.txt`): lines of the form

    "name"= value    ' comment

where `value` is either a plain numeric literal or a formula referencing
other quoted names (e.g. `"wall_width" + "pedestal_depth" + "pcb_width"`).
Only numeric-literal equations are safe to sweep directly -- formulas are
derived from other equations and get recomputed by SolidWorks on rebuild.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional

# Matches a whole (stripped) line: "name"= value ['comment]. The value
# group excludes `'` and newlines only (not `"`), so formulas containing
# quoted names (e.g. "wall_width" + "pedestal_depth") are captured whole.
_LINE_RE = re.compile(r"^\"(?P<name>[^\"]+)\"\s*=\s*(?P<value>[^'\n]+?)\s*(?:'(?P<comment>.*))?$")


@dataclass
class Equation:
    name: str
    raw_value: str
    comment: Optional[str]

    @property
    def is_numeric(self) -> bool:
        try:
            float(self.raw_value)
            return True
        except ValueError:
            return False

    @property
    def numeric_value(self) -> Optional[float]:
        try:
            return float(self.raw_value)
        except ValueError:
            return None


def read_file(path: str) -> str:
    # utf-8-sig strips a leading BOM if present -- SolidWorks writes one.
    with open(path, "r", encoding="utf-8-sig") as f:
        return f.read()


def write_file(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def parse(content: str) -> Dict[str, Equation]:
    equations: Dict[str, Equation] = {}
    for line in content.splitlines():
        match = _LINE_RE.match(line.strip())
        if not match:
            continue
        name = match.group("name")
        comment = match.group("comment")
        equations[name] = Equation(
            name=name,
            raw_value=match.group("value").strip(),
            comment=comment.strip() if comment else None,
        )
    return equations


def get_numeric_parameters(content: str) -> Dict[str, float]:
    """Only equations with a plain numeric literal value (not a formula)
    -- these are the ones it's safe to sweep directly, and the ones a
    parameter picker (e.g. the web UI) should offer."""
    return {name: eqn.numeric_value for name, eqn in parse(content).items() if eqn.is_numeric}


def set_parameter(content: str, name: str, value: float) -> str:
    """
    Replace a numeric-literal equation's value in place, preserving
    everything else in the file (formatting, comments, other lines).

    Raises KeyError if `name` doesn't exist, or ValueError if it exists
    but isn't a plain numeric literal (i.e. it's a formula -- those are
    derived and shouldn't be set directly).
    """
    equations = parse(content)
    if name not in equations:
        raise KeyError(f"No equation named {name!r} in this file")
    if not equations[name].is_numeric:
        raise ValueError(
            f"Equation {name!r} is a formula ({equations[name].raw_value!r}), "
            "not a plain number -- it can't be swept directly"
        )

    pattern = re.compile(r'("' + re.escape(name) + r'"\s*=\s*)[0-9.eE+\-]+')
    new_content, count = pattern.subn(lambda m: f"{m.group(1)}{value}", content, count=1)
    if count == 0:
        raise KeyError(f"Could not find a settable line for {name!r}")
    return new_content
