"""Canonical base classes for the plugin architecture.

This module is the single source of truth for the source/destination plugin
contracts. Each integration lives in its own package under ``integrations/``
and implements ``SourcePlugin`` (to read from an ITSM system) and/or
``DestinationPlugin`` (to write into a target system such as Salesforce).

The legacy module ``api/sources/base.py`` now re-exports the dataclasses and
``SourceProvider`` from here for backward compatibility.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator


@dataclass
class MigratableObject:
    name: str
    label: str
    category: str
    salesforce_object: str
    servicenow_tables: list[str]
    dependencies: list[str] = field(default_factory=list)
    priority: int = 9999


@dataclass
class SourceField:
    """Describes a single field exposed by a source ITSM system."""

    name: str
    label: str
    field_type: str  # e.g. "string", "datetime", "integer", "boolean", "reference"
    required: bool = False
    example: str = ""


class SourceProvider(ABC):
    """Abstract base class for all migration source providers.

    Implement this interface (or the richer ``SourcePlugin`` below) to add a
    new source system (e.g. Jira, Remedy). Register the implementation from the
    integration package's ``register()`` function.
    """

    @property
    @abstractmethod
    def source_id(self) -> str:
        """Machine-readable identifier, e.g. 'servicenow'."""

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name shown in the UI, e.g. 'ServiceNow'."""

    @property
    @abstractmethod
    def description(self) -> str:
        """Short description of the source system."""

    @abstractmethod
    def get_credentials_schema(self) -> dict:
        """Return a JSON-Schema object that the UI uses to render a credential form."""

    @abstractmethod
    def validate_credentials(self, credentials: dict) -> bool:
        """Test whether the supplied credentials can connect to the source system."""

    def get_objects(self) -> list[MigratableObject]:
        """Return all object types that can be migrated from this source.

        Default implementation queries the mapping_configs DB table for this
        source_type.  Providers that ship with hardcoded mapping priorities
        can override this for richer logic.
        """
        return self._get_objects_from_db()

    @abstractmethod
    def get_row_count(self, object_name: str, credentials: dict, filters: dict) -> int:
        """Return the number of records for the given object in the source system."""

    def get_source_fields(self, object_name: str, credentials: dict) -> list[SourceField]:
        """Return the list of fields available for the given object.

        Override in providers that support introspection.  The default
        implementation returns an empty list so that existing providers
        are not broken.
        """
        return []

    def get_source_tables(self, credentials: dict) -> list[str]:
        """Return the list of raw table/entity names in the source system.

        Used by the schema-discovery API when building a brand-new mapping.
        Override in providers that support table introspection. Defaults to
        an empty list so existing providers are not broken.
        """
        return []

    def get_table_fields(self, table_name: str, credentials: dict) -> list[SourceField]:
        """Return the fields of a raw source table (not a mapping name).

        Used by the schema-discovery API to populate the source side of the
        mapping editor for an arbitrary table. Override in providers that
        support introspection. Defaults to an empty list.
        """
        return []

    def extract_records(
        self,
        object_name: str,
        credentials: dict,
        options: dict,
    ) -> Iterator[list[dict]]:
        """Yield pages of records for the given object.

        Each yielded value is a list of record dicts.  The default
        implementation raises NotImplementedError so that providers that
        do not yet support direct extraction surface a clear error rather
        than silently returning nothing.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement extract_records()"
        )

    def _get_objects_from_db(self) -> list[MigratableObject]:
        """Build a MigratableObject list entirely from the mapping_configs DB."""
        try:
            from services.internal.mappings_service import (
                list_mapping_configs,
                get_mapping_config,
                cfg_source_tables,
                cfg_destination_object,
            )
        except Exception:
            return []

        objects: list[MigratableObject] = []
        for meta in list_mapping_configs(source_type=self.source_id):
            cfg = get_mapping_config(meta["name"])
            if cfg is None:
                continue
            objects.append(
                MigratableObject(
                    name=meta["name"],
                    label=meta["name"],
                    category="General",
                    salesforce_object=cfg_destination_object(cfg),
                    servicenow_tables=cfg_source_tables(cfg),
                    dependencies=[],
                    priority=meta.get("priority", 9999),
                )
            )
        objects.sort(key=lambda o: o.priority)
        return objects

    def get_routers(self) -> list:
        """Return any FastAPI routers this provider contributes (default none)."""
        return []


class SourcePlugin(SourceProvider):
    """Richer source contract used by the generic ETL pipeline.

    Adds the bulk-extraction hooks the migration pipeline calls. Providers that
    only support schema discovery (e.g. the Jira/Zendesk stubs) can inherit
    from ``SourceProvider`` directly and skip these.
    """

    def configure_client(self, credentials: dict) -> None:
        """Configure the underlying HTTP client with per-run credentials.

        Default is a no-op so providers without a stateful client are unaffected.
        """
        return None

    def get_download_path(self) -> Path:
        """Return the directory where this source's extracted CSVs are written."""
        from config.settings import PATH_TO_DOWNLOADS
        return PATH_TO_DOWNLOADS / self.source_id

    def extract_tables(self, job_id: int, tables: list[str]) -> int:
        """Bulk-extract the given raw tables into CSV. Return rows extracted."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement extract_tables()"
        )

    def extract_attachment_files(self, job_id: int, table_mapping: dict) -> None:
        """Download individual attachment files for an attachment mapping."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement extract_attachment_files()"
        )


class DestinationPlugin(ABC):
    """Abstract contract for a migration destination (target) system.

    A destination receives transformed CSV batches and loads them. Salesforce
    is the reference implementation in ``integrations/salesforce``.
    """

    @property
    @abstractmethod
    def destination_id(self) -> str:
        """Machine-readable identifier, e.g. 'salesforce'."""

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name shown in the UI."""

    def configure_client(self, credentials: dict) -> None:
        """Configure the underlying HTTP client with per-run credentials."""
        return None

    def get_routers(self) -> list:
        """Return any FastAPI routers this destination contributes (default none)."""
        return []

    def validate_credentials(self, credentials: dict) -> bool:
        """Test whether the supplied credentials can connect to the destination."""
        return False

    def list_objects(self, credentials: dict) -> list[dict]:
        """Return available objects in the destination.

        Each dict should have at least ``name`` and ``label`` keys.
        Override in providers that support introspection.
        """
        return []

    def describe_object(self, object_name: str):
        """Return field metadata for a destination object (or None)."""
        return None

    def start_loading(self, job_id: int, mappings_file_names: list[str]) -> dict:
        """Load all selected mappings. Return per-object processed/failed counts."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement start_loading()"
        )

    def start_attachment_loading(self, job_id: int, mappings_file_names: list[str]) -> None:
        """Load attachment records for the selected mappings."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement start_attachment_loading()"
        )
