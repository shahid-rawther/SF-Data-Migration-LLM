"""Backward-compatibility shim for the source registry.

The registry and provider implementations now live in the ``integrations``
package. Existing callers do ``from api import sources as source_registry`` and
use ``source_registry.get(...)`` / ``.get_all()`` / ``.register(...)`` — those
names are re-exported here and delegate to :mod:`integrations`.
"""
from integrations import (  # noqa: F401
    get,
    get_all,
    get_destination,
    get_all_destinations,
    register,
    register_destination,
)
