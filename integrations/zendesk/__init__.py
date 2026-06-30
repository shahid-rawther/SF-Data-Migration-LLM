"""Zendesk integration package."""


def register() -> None:
    from integrations import register as _register
    from integrations.zendesk.provider import ZendeskProvider

    _register(ZendeskProvider())
