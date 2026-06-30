"""Freshworks (Freshservice) integration package."""


def register() -> None:
    from integrations import register as _register
    from integrations.freshworks.provider import FreshworksProvider

    _register(FreshworksProvider())
