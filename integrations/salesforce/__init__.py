"""Salesforce integration package.

Exposes ``register()`` which the integration registry calls during discovery to
register the Salesforce destination plugin.
"""


def register() -> None:
    from integrations import register_destination
    from integrations.salesforce.provider import SalesforcePlugin

    register_destination(SalesforcePlugin())
