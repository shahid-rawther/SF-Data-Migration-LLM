"""Backward-compatibility shim.

The Salesforce HTTP client moved to ``integrations/salesforce/http_client.py``.
Existing imports such as
``from services.http_clients.salesforce_http_client import salesforce_client``
keep working via the module swap below.
"""
import sys as _sys

from integrations.salesforce import http_client as _impl

_sys.modules[__name__] = _impl
