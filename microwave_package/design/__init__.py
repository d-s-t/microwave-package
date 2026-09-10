"""
Turning a set of parameter values into a physical design.

Everything here is upstream of any solver: it reads the SolidWorks
equation file, expands a parameter sweep into combinations, checks a
combination is buildable, solves the CPW gap for 50 ohm, lays out the
copper and the bond wires, and drives SolidWorks and Altium to produce
the STEP and layout files for each combination.

None of it knows what will later be solved on that geometry. An
eigenmode run and a driven run build exactly the same design.
"""
