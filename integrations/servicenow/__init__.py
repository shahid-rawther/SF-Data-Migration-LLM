"""ServiceNow integration package.

Exposes ``register()`` which the integration registry calls during discovery to
register the ServiceNow source provider.
"""


def register() -> None:
    from integrations import register as _register
    from integrations.servicenow.provider import ServiceNowProvider

    _register(ServiceNowProvider())
