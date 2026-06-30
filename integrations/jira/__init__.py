"""Jira Service Management integration package."""


def register() -> None:
    from integrations import register as _register
    from integrations.jira.provider import JSMProvider

    _register(JSMProvider())
