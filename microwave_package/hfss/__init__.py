"""
Everything that talks to HFSS: building the model from a STEP file, and
the solvers that decide what to ask of it.

`model_builder` builds the geometry, materials and boundaries, which are
the same whatever is being solved. `registry` holds the contract a solver
fills in -- its settings, its setup, its ports, its results -- and the
two implementations live in `eigenmode/` and `driven/`.

Importing this package registers both of them, which is why everything
upstream imports `hfss` (or something inside it) rather than reaching for
`hfss.registry` on its own: the registry is empty until the solvers have
been loaded. Registration order decides the default, so eigenmode is
imported first -- it was the only kind of run this pipeline had, and a
run that does not say which it wants is still one.
"""
from . import registry  # noqa: F401  (re-exported: `from ..hfss import registry`)
from . import eigenmode  # noqa: F401  (registers itself on import -- and first)
from . import driven  # noqa: F401  (registers itself on import)

from . import line  # noqa: F401  (registers itself on import)

from . import launch  # noqa: F401  (registers itself on import)
