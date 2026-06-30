"""Backward-compatibility shim.

The ServiceNow HTTP client moved to ``integrations/servicenow/http_client.py``.
Existing imports such as
``from services.http_clients.servicenow_http_client import servicenow_client``
keep working via the module swap below.
"""
import sys as _sys

from integrations.servicenow import http_client as _impl

_sys.modules[__name__] = _impl
