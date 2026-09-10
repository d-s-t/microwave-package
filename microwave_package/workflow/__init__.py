"""
Running a sweep end to end: run state on disk, the queue handoff to the
worker, waiting for results, reading them back, and plotting them.

Solution-type agnostic. Where a step needs to know what was solved --
which setup to make, where the ports go, how to read the results back --
it asks the run's solver (see `hfss.registry`) rather than branching.
"""
