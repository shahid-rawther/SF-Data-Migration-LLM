from api.sources.base import SourceProvider
from api.sources.servicenow import ServiceNowProvider

_REGISTRY: dict[str, SourceProvider] = {}


def register(provider: SourceProvider) -> None:
    _REGISTRY[provider.source_id] = provider


def get_all() -> list[SourceProvider]:
    return list(_REGISTRY.values())


def get(source_id: str) -> SourceProvider | None:
    return _REGISTRY.get(source_id)


# Register built-in providers
register(ServiceNowProvider())
