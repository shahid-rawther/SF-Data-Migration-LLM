"""Backward-compatibility shim.

The canonical definitions now live in ``integrations/base.py``. This module is
kept so existing imports like ``from api.sources.base import SourceProvider``
continue to work.
"""
from integrations.base import (  # noqa: F401
    DestinationPlugin,
    MigratableObject,
    SourceField,
    SourcePlugin,
    SourceProvider,
)
