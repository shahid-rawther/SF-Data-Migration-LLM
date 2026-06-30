"""Integration plugin registry with lazy auto-discovery.

Each integration is a subpackage of ``integrations`` exposing a top-level
``register()`` function that calls :func:`register` (for source plugins) and/or
:func:`register_destination` (for destination plugins).

Discovery is lazy: the first call to any ``get*`` function scans the
``integrations/`` directory, imports each subpackage, and runs its
``register()``. This avoids import-order/circular-import problems at module
load time.
"""
from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

from integrations.base import (  # re-exported for convenience
    DestinationPlugin,
    MigratableObject,
    SourceField,
    SourcePlugin,
    SourceProvider,
)

_SOURCE_REGISTRY: dict[str, SourceProvider] = {}
_DEST_REGISTRY: dict[str, DestinationPlugin] = {}
_discovered = False


def register(provider: SourceProvider) -> None:
    """Register a source provider/plugin instance."""
    _SOURCE_REGISTRY[provider.source_id] = provider


def register_destination(plugin: DestinationPlugin) -> None:
    """Register a destination plugin instance."""
    _DEST_REGISTRY[plugin.destination_id] = plugin


def _ensure_discovered() -> None:
    global _discovered
    if _discovered:
        return
    # Mark first so a partially-failing import doesn't loop forever.
    _discovered = True
    package_dir = Path(__file__).resolve().parent
    for mod in pkgutil.iter_modules([str(package_dir)]):
        if not mod.ispkg or mod.name.startswith("_"):
            continue
        try:
            pkg = importlib.import_module(f"integrations.{mod.name}")
            register_fn = getattr(pkg, "register", None)
            if callable(register_fn):
                register_fn()
        except Exception:  # pragma: no cover - a broken plugin must not kill the app
            import logging
            logging.getLogger(__name__).exception(
                "Failed to load integration '%s'", mod.name
            )


def get(source_id: str) -> SourceProvider | None:
    _ensure_discovered()
    return _SOURCE_REGISTRY.get(source_id)


def get_all() -> list[SourceProvider]:
    _ensure_discovered()
    return list(_SOURCE_REGISTRY.values())


def get_destination(destination_id: str) -> DestinationPlugin | None:
    _ensure_discovered()
    return _DEST_REGISTRY.get(destination_id)


def get_all_destinations() -> list[DestinationPlugin]:
    _ensure_discovered()
    return list(_DEST_REGISTRY.values())


__all__ = [
    "DestinationPlugin",
    "MigratableObject",
    "SourceField",
    "SourcePlugin",
    "SourceProvider",
    "register",
    "register_destination",
    "get",
    "get_all",
    "get_destination",
    "get_all_destinations",
]
