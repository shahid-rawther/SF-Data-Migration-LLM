from api.sources.base import SourceProvider
from api.sources.servicenow import ServiceNowProvider
from api.sources.jira_service_management import JSMProvider
from api.sources.zendesk import ZendeskProvider
from api.sources.freshworks import FreshworksProvider
from api.sources.zoho import ZohoProvider

_REGISTRY: dict[str, SourceProvider] = {}


def register(provider: SourceProvider) -> None:
    _REGISTRY[provider.source_id] = provider


def get_all() -> list[SourceProvider]:
    return list(_REGISTRY.values())


def get(source_id: str) -> SourceProvider | None:
    return _REGISTRY.get(source_id)


# Register built-in providers
register(ServiceNowProvider())
register(JSMProvider())
register(ZendeskProvider())
register(FreshworksProvider())
register(ZohoProvider())
