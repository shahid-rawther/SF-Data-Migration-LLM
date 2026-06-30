"""Zoho (Zoho Desk) integration package."""


def register() -> None:
    from integrations import register as _register
    from integrations.zoho.provider import ZohoProvider

    _register(ZohoProvider())
