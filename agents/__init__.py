"""Selectively agentic layer over the deterministic migration pipeline.

Two agents live here and neither one moves data:

* the **relationship agent** reads schema metadata and draft mapping configs,
  resolves relationship edge cases (id remaps, parent order, junctions,
  self-references, missing parents) and writes the resolution into the mapping
  configs the existing ETL already obeys;
* the **repair agent** reads the destination's load errors and patches the
  offending field transform so the object can be re-transformed and retried.

Everything the agents may change goes through :mod:`agents.wiring`, is limited
to an allow-list of transforms, and is journalled in :mod:`agents.audit` so any
individual change can be reverted.
"""
