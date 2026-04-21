from abc import ABC, abstractmethod
from dataclasses import dataclass, field
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

    Implement this interface to add a new source system (e.g. Jira, Remedy).
    Register the implementation in api/sources/__init__.py.
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

    @abstractmethod
    def get_objects(self) -> list[MigratableObject]:
        """Return all object types that can be migrated from this source."""

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
