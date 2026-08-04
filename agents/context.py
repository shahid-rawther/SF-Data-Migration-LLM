"""Schema and mapping context for one agent run.

The agent's read-only tools and the validator that checks its conclusions both
read through this object, so a resolution is always judged against exactly the
metadata the agent was shown. Describes are cached for the life of the run: a
relationship pass costs a handful of describes regardless of how many times the
agent asks about an object, and no record data is ever loaded.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

from common.logger import logger
from services.internal.mappings_service import (
    cfg_destination_object,
    cfg_source_tables,
)

from agents import wiring


@dataclass
class MigrationContext:
    source_type: str
    mapping_names: list[str]
    source_credentials: dict = field(default_factory=dict)
    destination_credentials: dict = field(default_factory=dict)
    configs: dict[str, dict] = field(default_factory=dict)

    _destination_cache: dict[str, list[dict]] = field(default_factory=dict, repr=False)
    _source_cache: dict[str, list[dict]] = field(default_factory=dict, repr=False)

    # ── construction ───────────────────────────────────────────────────────

    @classmethod
    def load(
        cls,
        source_type: str,
        mapping_names: list[str],
        source_credentials: dict | None = None,
        destination_credentials: dict | None = None,
    ) -> "MigrationContext":
        configs = wiring.load_configs(mapping_names)
        missing = [name for name in mapping_names if name not in configs]
        if missing:
            logger.warning("[agent] no mapping config found for: %s", ", ".join(missing))
        return cls(
            source_type=source_type,
            mapping_names=[name for name in mapping_names if name in configs],
            source_credentials=source_credentials or {},
            destination_credentials=destination_credentials or {},
            configs=configs,
        )

    # ── mapping accessors ──────────────────────────────────────────────────

    def destination_object(self, mapping_name: str) -> str:
        config = self.configs.get(mapping_name) or {}
        return cfg_destination_object(config)

    def driving_table(self, mapping_name: str) -> str:
        tables = cfg_source_tables(self.configs.get(mapping_name) or {})
        return tables[0] if tables else ""

    def mapping_for_destination_object(self, object_name: str) -> str:
        """Which selected mapping targets a destination object, if any."""
        for name in self.mapping_names:
            if self.destination_object(name) == object_name:
                return name
        return ""

    # ── destination schema ─────────────────────────────────────────────────

    def destination_fields(self, object_name: str) -> list[dict]:
        if object_name in self._destination_cache:
            return self._destination_cache[object_name]
        from api.compat_engine import get_destination_fields

        fields = get_destination_fields(object_name, self.destination_credentials)
        if not fields:
            logger.warning(
                "[agent] destination describe for %s returned nothing; "
                "schema validation will refuse writes against it",
                object_name,
            )
        self._destination_cache[object_name] = fields
        return fields

    def destination_field(self, object_name: str, field_name: str) -> dict | None:
        for entry in self.destination_fields(object_name):
            if entry["name"] == field_name:
                return entry
        return None

    def reference_fields(self, object_name: str) -> list[dict]:
        return [
            entry
            for entry in self.destination_fields(object_name)
            if entry.get("type") == "reference"
        ]

    # ── source schema ──────────────────────────────────────────────────────

    def source_fields(self, table_name: str) -> list[dict]:
        if table_name in self._source_cache:
            return self._source_cache[table_name]
        fields: list[dict] = []
        try:
            from api import sources as source_registry

            provider = source_registry.get(self.source_type)
            if provider:
                fields = [
                    {
                        "name": entry.name,
                        "label": entry.label,
                        "type": entry.field_type,
                        "required": entry.required,
                        "reference_table": getattr(entry, "reference_table", ""),
                    }
                    for entry in provider.get_table_fields(
                        table_name, self.source_credentials
                    )
                ]
        except Exception as exc:  # noqa: BLE001 — introspection is best-effort
            logger.warning("[agent] could not introspect source table %s: %s", table_name, exc)
        self._source_cache[table_name] = fields
        return fields

    def has_source_field(self, table_name: str, field_name: str) -> bool:
        """Whether a source field exists. True when introspection is unavailable.

        A source system that cannot describe its tables must not turn every
        resolution into a rejection; the destination side is where a wrong field
        name actually breaks a load, and that is validated strictly.
        """
        fields = self.source_fields(table_name)
        if not fields:
            return True
        bare = field_name.split(".")[-1]
        return any(entry["name"] in (field_name, bare) for entry in fields)

    # ── analysis ───────────────────────────────────────────────────────────

    def dependency_analysis(self, draft_configs: dict[str, dict] | None = None) -> dict:
        """Parent-first order and relationship warnings for the current configs."""
        from api.compat_engine import resolve_dependency_order

        configs = draft_configs if draft_configs is not None else self.configs
        objects = {
            cfg_destination_object(config)
            for config in configs.values()
            if cfg_destination_object(config)
        }
        return resolve_dependency_order(
            self.mapping_names,
            self.destination_credentials,
            draft_configs=configs,
            destination_fields={name: self.destination_fields(name) for name in objects},
        )

    def draft(self) -> dict[str, dict]:
        """A fresh deep copy of the configs, for patching without side effects."""
        return copy.deepcopy(self.configs)
