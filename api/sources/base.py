from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class MigratableObject:
    name: str
    label: str
    category: str
    salesforce_object: str
    servicenow_tables: list[str]
    dependencies: list[str] = field(default_factory=list)
    priority: int = 9999


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
